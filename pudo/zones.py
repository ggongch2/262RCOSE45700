"""어린이보호구역 지정 구간 복원 (서울시 T-GIS 안전표지·노면 문자표시).

공개된 보호구역 경계 자료가 없어, 현장에 설치된 시설물로 구간을 추정합니다.
  - 구간 안 근거(씨앗): 노면 문자 "어린이"·"보호구역"·"학교앞", 어린이보호 표지(324, 133),
    "어린이보호구역" 문구가 있는 표지, 초등학교 등 지정 대상시설과 가장 가까운 도로
  - 구간 끝(경계): "어린이보호구역 해제", "여기부터" 보조표지
씨앗에서 같은 도로를 따라 퍼져 나가되 경계 표지에서 멈추고, 지정 범위 상한(시설 주출입문 300m,
「어린이·노인 및 장애인 보호구역의 지정 및 관리에 관한 규칙」 제3조)을 넘지 않습니다.
표지가 빠진 도로는 상한까지 이어질 수 있으므로 결과는 추정 구간입니다.
"""

import heapq
import re
from collections import defaultdict

from shapely.geometry import LineString, Point
from shapely.ops import substring, unary_union
from shapely.strtree import STRtree

from .geo import to_meters

ZONE_REACH_M = 300      # 같은 도로의 두 근거 사이를 구간으로 잇는 최대 거리 (지정 범위 상한)
SEED_MARGIN_M = 50      # 표지·노면표시 하나가 앞뒤로 덮는 거리
FACILITY_REACH_M = 150  # 대상시설과 가장 가까운 도로에서 모든 방향으로 퍼지는 거리 (주출입문 위치 모름)
SNAP_M = 20          # 표지·노면표시를 도로에 붙이는 최대 거리
SEED_MARKS = ("어린이", "보호구역", "보호구역30", "학교앞")
BOUNDARY = re.compile(r"해제|여기부터|여기서부터")
NOT_BOUNDARY = re.compile(r"50|주정차|\d+m")


def _point(feature):
    coordinates = feature["geometry"]["coordinates"]
    while isinstance(coordinates[0], list):
        coordinates = coordinates[0]
    return to_meters(*coordinates[:2])


def zone_evidence(signs, marks, facilities):
    """(씨앗 [(좌표, 설명)], 경계 [(좌표, 설명)])."""
    seeds, boundaries = [], []
    elderly = [_point(f) for f in marks if f["properties"].get("CTT") == "노인"]
    for feature in marks:
        text = feature["properties"].get("CTT", "")
        if text not in SEED_MARKS:
            continue
        position = _point(feature)
        if any(abs(position[0] - x) + abs(position[1] - y) < 15 for x, y in elderly):
            continue   # "노인 보호구역" 글씨
        seeds.append((position, f"노면 '{text}'"))
    for feature in signs:
        properties = feature["properties"]
        number, remark = properties.get("MRK_NUM", ""), properties.get("RMK", "")
        if BOUNDARY.search(remark) and not NOT_BOUNDARY.search(remark.replace("30", "")):
            boundaries.append((_point(feature), f"표지 '{remark}'"))
        elif number in ("324", "133") or "어린이" in remark or "학교앞" in remark:
            label = {"324": "어린이보호 표지", "133": "어린이보호 주의표지"}.get(number, f"표지 '{remark}'")
            seeds.append((_point(feature), label))
    seeds += [(position, f"대상시설 {name}") for position, name in facilities]
    return seeds, boundaries


def build_child_zone(road_elements, seeds, boundaries, *, reach=ZONE_REACH_M, facility_reach=FACILITY_REACH_M, snap=SNAP_M):
    """씨앗에서 도로를 따라 퍼진 구간(선)을 돌려줍니다. 씨앗이 없으면 None.

    표지·노면표시는 도로마다 설치되므로 같은 이름의 도로를 따라서만 퍼집니다. 근거 하나는 앞뒤 50m를,
    같은 도로의 두 근거가 reach 안에 있으면 그 사이 전체를 덮습니다.
    대상시설 씨앗만 주변 도로 어느 방향으로든 facility_reach까지 퍼집니다.
    """
    edges = []   # (선, 시작 노드, 끝 노드, 도로 이름)
    for way in road_elements:
        if way.get("type") != "way":
            continue
        name = way.get("tags", {}).get("name") or f"way{way['id']}"
        points = [to_meters(p["lon"], p["lat"]) for p in way["geometry"]]
        for (a, b), pa, pb in zip(zip(way["nodes"], way["nodes"][1:]), points, points[1:]):
            if pa != pb:
                edges.append((LineString([pa, pb]), a, b, name))
    if not edges or not seeds:
        return None
    tree = STRtree([edge[0] for edge in edges])
    events = defaultdict(list)   # 선 번호 → [(위치, 종류, 키)]

    def attach(position, kind, key):
        point = Point(position)
        index = int(tree.nearest(point))
        line = edges[index][0]
        if line.distance(point) <= snap:
            events[index].append((line.project(point), kind, key))
            return index
        return None

    for number, (position, _) in enumerate(boundaries):
        attach(position, "boundary", number)
    groups = defaultdict(list)   # 퍼지는 규칙 → 시작 노드
    for number, (position, label) in enumerate(seeds):
        index = attach(position, "seed", number)
        if index is not None:
            groups["*" if label.startswith("대상시설") else edges[index][3]].append(("seed", number))
    if not groups:
        return None

    segments = []   # (start 쪽 노드, end 쪽 노드, 선 번호, start 위치, end 위치)
    for index, (line, a, b, _) in enumerate(edges):
        stops = [(0.0, ("node", a))]
        for offset, kind, key in sorted(events[index], key=lambda event: event[0]):
            if kind == "seed":
                stops.append((offset, ("seed", key)))
            else:   # 경계: 양쪽을 서로 다른 노드로 끊어 통과하지 못하게 함
                stops.append((offset, ("cut-in", index, key)))
                stops.append((offset, ("cut-out", index, key)))
        stops.append((line.length, ("node", b)))
        for (start, first), (end, second) in zip(stops, stops[1:]):
            if not (first[0] == "cut-in" and second[0] == "cut-out"):
                segments.append((first, second, index, start, end))

    pieces = []
    for road, starts in groups.items():
        if road == "*":
            pieces += _spread(segments, starts, facility_reach, edges)
        else:
            usable = [segment for segment in segments if edges[segment[2]][3] == road]
            pieces += _spread(usable, starts, SEED_MARGIN_M, edges, between=reach)
    pieces = [piece for piece in pieces if piece.length > 0]
    return unary_union(pieces) if pieces else None


def _spread(segments, starts, reach, edges, *, between=None):
    """starts에서 reach까지 덮고, between이 있으면 서로 다른 두 씨앗 사이 경로(합 ≤ between)도 덮습니다."""
    graph = defaultdict(list)
    for first, second, _, start, end in segments:
        graph[first].append((second, end - start))
        graph[second].append((first, end - start))
    nearest = defaultdict(list)   # 노드 → [(거리, 씨앗)] 서로 다른 씨앗 최대 2개
    limit = max(reach, between or 0)
    queue = [(0.0, start, start) for start in starts]
    while queue:
        current, node, seed = heapq.heappop(queue)
        found = nearest[node]
        if len(found) >= 2 or any(source == seed for _, source in found) or current > limit:
            continue
        found.append((current, seed))
        for neighbor, length in graph[node]:
            heapq.heappush(queue, (current + length, neighbor, seed))

    def first(node):
        return nearest[node][0][0] if nearest[node] else float("inf")

    def linked(node):
        found = nearest[node]
        return between is not None and len(found) == 2 and found[0][0] + found[1][0] <= between

    pieces = []
    for first_node, second_node, index, start, end in segments:
        line, length = edges[index][0], end - start
        if linked(first_node) and linked(second_node):
            pieces.append(substring(line, start, end))
            continue
        from_start = max(0.0, reach - first(first_node))
        from_end = max(0.0, reach - first(second_node))
        if from_start + from_end >= length:
            pieces.append(substring(line, start, end))
            continue
        if from_start > 0:
            pieces.append(substring(line, start, start + from_start))
        if from_end > 0:
            pieces.append(substring(line, end - from_end, end))
    return pieces
