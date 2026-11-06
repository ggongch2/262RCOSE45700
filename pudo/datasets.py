"""지도·후보·거리뷰 메타데이터를 읽어 판정용 미터 좌표로 준비합니다."""

import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONTINUATION_TURN_DEG, require_files
from .geo import to_meters
from .vision import load_assessments, PhotoContractError
from .zones import build_child_zone, zone_evidence


@dataclass
class MapData:
    """한 번 로딩해 여러 후보와 판정 모드에서 공유하는 읽기용 데이터."""

    intersections: list = field(default_factory=list)   # 교차 노드 (참고용)
    intersection_area: object = None                     # 교차로 면 (shapely, 미터 좌표). H1 판정에 사용
    child_zone: object = None                            # 어린이보호구역 추정 구간 (shapely 선). 없으면 시설 반경 규칙
    child_zone_evidence: list = field(default_factory=list)   # [(좌표, 설명)] 구간 근거 표지·노면표시
    crosswalks: list = field(default_factory=list)
    hydrants: list = field(default_factory=list)
    buses: list = field(default_factory=list)
    schools: list = field(default_factory=list)
    lines: dict = field(default_factory=dict)
    hd_reg: list = field(default_factory=list)
    hd_park: list = field(default_factory=list)
    hd_bus: list = field(default_factory=list)
    hd_safe: list = field(default_factory=list)
    hd_link: list = field(default_factory=list)
    images: dict = field(default_factory=dict)
    photo_assessments: dict = field(default_factory=dict)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_candidates(path):
    return read_json(path)["features"]


def _hd_lines(features, keep):
    lines = []
    for feature in features:
        if not feature.get("geometry") or not keep(feature["properties"]):
            continue
        geometry = feature["geometry"]
        coordinates = geometry["coordinates"]
        if geometry["type"] != "LineString":
            coordinates = [point for part in coordinates for point in part]
        lines.append([to_meters(*point[:2]) for point in coordinates])
    return lines


def _direction(points, index):
    """노드에서 길을 따라 나가는 방향. 같은 좌표가 반복되면 다음 점을 사용합니다."""
    step = 1 if index == 0 else -1
    x, y = points[index]
    for other in points[index + step::step]:
        if other != (x, y):
            return other[0] - x, other[1] - y
    return None


def _is_straight_continuation(entries):
    """서로 다른 두 길의 끝이 거의 직선으로 이어지는 분할 지점인지."""
    if len(entries) != 2 or any(index not in (0, len(points) - 1) for _, index, points in entries):
        return False
    (_, first_index, first_points), (_, second_index, second_points) = entries
    first, second = _direction(first_points, first_index), _direction(second_points, second_index)
    if first is None or second is None:
        return False
    cosine = (first[0] * second[0] + first[1] * second[1]) / (math.hypot(*first) * math.hypot(*second))
    turn = 180 - math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
    return turn < CONTINUATION_TURN_DEG


def _intersections(elements):
    """차량 교차로·도로 모퉁이 노드. 닫힌 길의 시작점과 같은 길의 직선 분할 지점은 제외합니다."""
    entries, positions = defaultdict(list), {}
    for way in (element for element in elements if element["type"] == "way"):
        points = [to_meters(point["lon"], point["lat"]) for point in way["geometry"]]
        for index, (node, position) in enumerate(zip(way["nodes"], points)):
            entries[node].append((way["id"], index, points))
            positions[node] = position
    return [
        positions[node] for node, node_entries in entries.items()
        if len({way_id for way_id, _, _ in node_entries}) >= 2 and not _is_straight_continuation(node_entries)
    ]


INTERSECTION_REACH_M = 40   # 교차 노드에서 이 반경 안의 도로 겹침만 교차로 면으로 봄
MEDIAN_FILL_M = 10          # 같은 이름의 상하행 차도 사이(중앙분리대) 20m까지 한 도로로 메움
HALF_WIDTH_DEFAULT = {"primary": 6.5, "primary_link": 3.25, "secondary": 4.9, "tertiary": 3.25}


def _half_widths(candidates_path):
    """길별 차도 반폭. 후보 생성 때 쓴 값(가장자리 이동 거리 + 1.5m)의 중앙값."""
    if not Path(candidates_path).is_file():
        return {}
    offsets = defaultdict(list)
    for feature in read_json(candidates_path)["features"]:
        properties = feature["properties"]
        if properties.get("osm_way") is not None and properties.get("curb_offset_m") is not None:
            offsets[properties["osm_way"]].append(properties["curb_offset_m"])
    return {way: sorted(values)[len(values) // 2] + 1.5 for way, values in offsets.items()}


def _intersection_area(elements, half_widths):
    """차도 면이 겹치는 교차로 면 (도로교통법 제32조 1·2호의 '교차로'와 '가장자리' 판정용).

    길마다 차도 반폭만큼 면을 만들고, 이름이 같은 길(상하행 분리 차도 포함)을 한 도로로 합친 뒤
    교차 노드 주변에서 서로 다른 도로가 겹치는 부분을 교차로 면으로 봅니다.
    이름이 같은 길끼리 꺾이는 모퉁이는 두 길의 면이 겹치는 부분을 씁니다.
    """
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union

    ways = [element for element in elements if element["type"] == "way" and len(element["geometry"]) >= 2]
    if not ways:
        return None
    road = {way["id"]: way.get("tags", {}).get("name") or f"way{way['id']}" for way in ways}
    shapes, by_road, entries, positions = {}, defaultdict(list), defaultdict(list), {}
    for way in ways:
        points = [to_meters(point["lon"], point["lat"]) for point in way["geometry"]]
        half = half_widths.get(way["id"], HALF_WIDTH_DEFAULT.get(way.get("tags", {}).get("highway"), 3.25))
        shapes[way["id"]] = (LineString(points), half)
        by_road[road[way["id"]]].append(LineString(points).buffer(half, cap_style=2))
        for index, (node, position) in enumerate(zip(way["nodes"], points)):
            entries[node].append((way["id"], index, points))
            positions[node] = position
    corridors = {
        name: unary_union(parts).buffer(MEDIAN_FILL_M, join_style=2).buffer(-MEDIAN_FILL_M, join_style=2)
        for name, parts in by_road.items()
    }
    pieces = []
    for node, node_entries in entries.items():
        way_ids = sorted({way_id for way_id, _, _ in node_entries})
        if len(way_ids) < 2 or _is_straight_continuation(node_entries):
            continue
        center = Point(positions[node])
        reach = center.buffer(INTERSECTION_REACH_M)
        names = sorted({road[way_id] for way_id in way_ids})
        if len(names) >= 2:
            for i, first in enumerate(names):
                for second in names[i + 1:]:
                    overlap = corridors[first].intersection(corridors[second]).intersection(reach)
                    pieces += [part for part in getattr(overlap, "geoms", [overlap])
                               if part.geom_type == "Polygon" and part.distance(center) < 1]
        else:
            local = [shapes[way_id][0].intersection(reach).buffer(shapes[way_id][1], cap_style=2) for way_id in way_ids]
            pieces += [a.intersection(b) for i, a in enumerate(local) for b in local[i + 1:] if a.intersects(b)]
    pieces = [piece for piece in pieces if not piece.is_empty and piece.area > 0]
    return unary_union(pieces) if pieces else None


ZONE_STANDARD_FILE = "child_protection_zones.csv"   # 전국어린이보호구역표준데이터 (공공데이터포털 15012891)
NOT_ZONE_TARGET = ("중학교", "고등학교")             # 도로교통법 제12조 지정 대상이 아님


def _protection_facilities(data_dir, layers):
    """어린이보호구역 대상시설 [(미터 좌표, 이름)].

    data/에 표준데이터 CSV가 있으면 실제 지정 시설(유치원·어린이집·학원 포함)을 쓰고,
    없으면 스냅샷의 학교 좌표에서 지정 대상이 아닌 중·고등학교를 뺍니다.
    """
    path = Path(data_dir) / ZONE_STANDARD_FILE
    if path.is_file():
        for encoding in ("utf-8-sig", "cp949"):
            try:
                with path.open(encoding=encoding, newline="") as stream:
                    rows = list(csv.DictReader(stream))
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError(f"{path.name}: UTF-8 또는 CP949가 아닙니다.")
        facilities = []
        for row in rows:
            try:
                position = to_meters(float(row["경도"]), float(row["위도"]))
            except (KeyError, TypeError, ValueError):
                continue
            if math.hypot(*position) <= 1500:   # 판정 영역 500m + 지정 범위 상한 여유
                facilities.append((position, f"{row.get('대상시설명', '').strip()} ({row.get('시설종류', '').strip()})"))
        return facilities
    return [
        (to_meters(*feature["geometry"]["coordinates"]), feature["properties"]["이름"])
        for feature in layers["C 학교(보호구역 300m 버퍼)"]["features"]
        if not feature["properties"]["이름"].endswith(NOT_ZONE_TARGET)
    ]


def _images(path):
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as stream:
        return {
            row["id"]: row for row in csv.DictReader(stream)
            if row["status"] == "OK" and "Google" in row.get("copyright", "Google")
        }


def load_map_data(data_dir, sv_dir):
    data_dir, sv_dir = Path(data_dir), Path(sv_dir)
    layers = read_json(data_dir / "layers_gangnam.json")

    def points(name):
        return [to_meters(*feature["geometry"]["coordinates"]) for feature in layers[name]["features"]]

    lines = defaultdict(list)
    for feature in layers["D 규제차선 (T-GIS)"]["features"]:
        lines[feature["properties"]["code"]].append(
            [to_meters(*point) for point in feature["geometry"]["coordinates"]]
        )

    # 기존 v0 동작: 두 정밀도로지도 파일 중 하나라도 없으면 해당 자료를 모두 생략합니다.
    try:
        surface = read_json(data_dir / "hdmap" / "lt_l_b2surfacelinemark.geojson")["features"]
        links = read_json(data_dir / "hdmap" / "lt_l_a2link.geojson")["features"]
    except FileNotFoundError:
        surface, links = [], []

    roads = read_json(data_dir / "osm_roads_gangnam.json")["elements"]
    facilities = _protection_facilities(data_dir, layers)
    child_zone, evidence = None, []
    signs_path, marks_path = data_dir / "tgis" / "A064_P_signs.geojson", data_dir / "tgis" / "A054_P_text_marks.geojson"
    if signs_path.is_file() and marks_path.is_file():
        # 학교 좌표(블록 중심)는 도로와 멀어 구간 근거로 쓰지 않습니다. 학교 주변은 표지·노면표시가 덮습니다.
        evidence, boundaries = zone_evidence(read_json(signs_path)["features"], read_json(marks_path)["features"], [])
        child_zone = build_child_zone(roads, evidence, boundaries)
    return MapData(
        intersections=_intersections(roads),
        intersection_area=_intersection_area(roads, _half_widths(data_dir / "candidates_v1.geojson")),
        crosswalks=points("C 횡단보도"),
        hydrants=points("C 소화전"),
        buses=points("C 버스정류소"),
        schools=facilities,
        child_zone=child_zone,
        child_zone_evidence=evidence,
        lines=dict(lines),
        hd_reg=_hd_lines(surface, lambda p: (p.get("kind") == "515" and p.get("type") in ("111", "121")) or p.get("kind") == "516"),
        hd_park=_hd_lines(surface, lambda p: p.get("kind") == "515" and p.get("type") == "112"),
        hd_bus=_hd_lines(surface, lambda p: p.get("kind") == "504"),
        hd_safe=_hd_lines(surface, lambda p: p.get("kind") == "531"),
        hd_link=_hd_lines(links, lambda p: True),
        images=_images(sv_dir / "metadata.csv"),
    )


def load_inputs(data_dir, sv_dir, *, photo_results=None):
    data_dir = Path(data_dir)
    require_files(
        data_dir / "layers_gangnam.json",
        data_dir / "osm_roads_gangnam.json",
        data_dir / "candidates_v1.geojson",
    )
    candidates = load_candidates(data_dir / "candidates_v1.geojson")
    map_data = load_map_data(data_dir, sv_dir)
    if photo_results is not None:
        map_data.photo_assessments = load_assessments(photo_results)
        if map_data.photo_assessments.keys() - {feature["properties"]["id"] for feature in candidates}:
            raise PhotoContractError("사진 판독 결과에 현재 후보 집합에 없는 candidate_id가 있습니다.")
    return candidates, map_data
