"""준비 단계 입력: 지도·후보·거리뷰 메타데이터를 읽어 미터 좌표의 MapData로 만듭니다."""

import csv
import json
import math
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

import numpy as np
import shapely
from shapely.geometry import shape

from .config import CONTINUATION_TURN_DEG, require_files
from .geo import to_meters
from .zones import build_child_zone, zone_evidence


@dataclass
class MapData:
    """한 번 로딩해 여러 후보와 판정 모드에서 공유하는 읽기용 데이터."""

    intersections: list = field(default_factory=list)   # 교차 노드 (참고용)
    intersection_area: object = None                     # 교차로 면 (shapely, 미터 좌표). H1 판정에 사용
    child_zone: object = None                            # 어린이보호구역 추정 구간 (shapely 선, T-GIS 표지·노면표시 근거). 없으면 시설 반경 규칙
    child_zone_facility: object = None                   # 지정 시설 위치만으로 추정한 주변 도로 (표지 근거 없음, 확인 필요용)
    child_zone_evidence: list = field(default_factory=list)   # [(좌표, 설명)] 구간 근거 표지·노면표시
    crosswalks: list = field(default_factory=list)
    hydrants: list = field(default_factory=list)
    buses: list = field(default_factory=list)
    schools: list = field(default_factory=list)          # [(좌표, 이름)] 보호구역 대상시설. 지정 목록이 있으면 실제 지정 시설
    planned_roads: list = field(default_factory=list)    # [(면, 등급명, 최소 폭 m)] 도시계획시설 도로 (규모별 등급)
    lines: dict = field(default_factory=dict)
    hd_reg: list = field(default_factory=list)
    hd_park: list = field(default_factory=list)
    hd_bus: list = field(default_factory=list)
    hd_safe: list = field(default_factory=list)
    hd_link: list = field(default_factory=list)
    images: dict = field(default_factory=dict)
    taxi_stands: list = field(default_factory=list)      # [(좌표, 설명)] 서울시 택시승차대. None이면 자료 없음
    lines_2023: dict = None                              # {선 코드: [2023 목록에도 있는지]} lines와 같은 순서. None이면 2023 목록 없음
    police_zones: list = None                            # [(면, 시설 이름)] 경찰청 어린이보호구역 지정 경계. None이면 자료 없음
    map1000: object = None                               # Map1000 (1:1000 수치지형도 도로중심선·차도·보도). None이면 자료 없음


SIDEWALK_SEARCH_MARGIN_M = 2   # 차도 폭 계산 때 '이 도로의 보도'로 보는 범위 = 중심선에서 도로폭/2 + 이 값.
                               # 도로선이 한가운데서 조금 치우치거나 보도 면 경계가 도로폭과 어긋나도 이 도로의 보도를 놓치지 않으면서,
                               # 건물 너머 다른 도로의 보도는 빼기 위한 측정 허용 오차 (판정 기준값 아님)


@dataclass
class Map1000:
    """1:1000 수치지형도에서 판정에 쓰는 세 레이어 (미터 좌표)."""

    lines: list          # [(선, 차로수 양방향 합, 일방통행 여부, 도로명, 도로폭 m)]  A0020000 도로중심선. 도로폭은 보도 포함
    carriageway: object  # 도로경계 면(A0010000, 보도 포함) - 보도 면(A0033320) = 차도
    sidewalks: list      # [(면, 폭 m 또는 None)]  A0033320 인도(보도)
    safety: list = field(default_factory=list)   # [(면, 구조)]  A0053326 안전지대 중 교통섬·평면(도색). 어린이보호구역 구조는 뺌
    line_tree: object = None
    sidewalk_tree: object = None

    def __post_init__(self):
        from shapely.strtree import STRtree
        self.line_tree = STRtree([geometry for geometry, *_ in self.lines])
        self.sidewalk_tree = STRtree([geometry for geometry, _ in self.sidewalks])

    def carriageway_width(self, center, heading, road_width):
        """차도 폭 = 도로폭(보도 포함) - 양쪽 보도 폭. center를 heading 방향으로 가로지르는 선에 걸리는 보도 면 중
        도로 안쪽(도로폭/2 + SIDEWALK_SEARCH_MARGIN_M) 것만, 쪽마다 가장 넓은 폭을 뺍니다. 도로폭이 없거나 결과가 0 이하이면 None."""
        if not road_width:
            return None
        from shapely.geometry import LineString
        dx, dy = math.sin(math.radians(heading)), math.cos(math.radians(heading))
        reach = road_width / 2 + SIDEWALK_SEARCH_MARGIN_M
        ray = LineString([(center.x - dx * reach, center.y - dy * reach), (center.x + dx * reach, center.y + dy * reach)])
        sides = {1: 0.0, -1: 0.0}
        for index in self.sidewalk_tree.query(ray):
            area, walk_width = self.sidewalks[index]
            if walk_width and area.intersects(ray) and area.distance(center) <= reach:
                side = 1 if (area.centroid.x - center.x) * dx + (area.centroid.y - center.y) * dy > 0 else -1
                sides[side] = max(sides[side], walk_width)
        width = road_width - sides[1] - sides[-1]
        return width if width > 0 else None


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


POLICE_ZONE_FILE = "child_zones_police.json"        # 경찰청_전국 보호구역 현황 API (pudo download)
POLICE_CHILD_TYPE = "1"                             # fcltTypeCd 1: 어린이 (2 노인, 3 장애인은 제32조 8호 대상 아님)


def _police_zones(path):
    """[(미터 좌표 면, 시설 이름)] 경찰청 어린이보호구역 경계. 파일이 없으면 None.

    WKT 좌표계는 EPSG:5181 (중부원점, 가산 500000). 도로를 따라 그린 면이며, 같이 들어 있는 점(시설 위치)은 쓰지 않습니다.
    """
    if not path.is_file():
        return None
    from pyproj import Transformer
    from shapely import wkt
    to_wgs84 = Transformer.from_crs(5181, 4326, always_xy=True)

    def to_local(array):
        lon, lat = to_wgs84.transform(array[:, 0], array[:, 1])
        return np.array([to_meters(a, b) for a, b in zip(lon, lat)])

    zones = []
    for item in read_json(path)["items"]:
        if item.get("fcltTypeCd") != POLICE_CHILD_TYPE or item.get("useYn") != "Y" or not item.get("fturGeomVl"):
            continue
        geometry = wkt.loads(item["fturGeomVl"])
        polygons = [shapely.make_valid(part) for part in getattr(geometry, "geoms", [geometry]) if part.geom_type in ("Polygon", "MultiPolygon")]
        if polygons:
            zones.append((shapely.transform(shapely.union_all(polygons), to_local), item.get("trgtFcltNm") or "이름 없음"))
    return zones


MAP1000_DIR = "map1000"                             # 국토지리정보원 1:1000 수치지형도 v2.0 SHP, 도엽 폴더마다 세 레이어


def _map1000(folder):
    """data/map1000/<도엽>/의 도로중심선·도로경계·보도 → Map1000. 폴더가 없으면 None.

    강남구·서초구 파일에 같은 도엽이 겹쳐 들어 있어 UFID로 중복을 뺍니다. 좌표계는 EPSG:5186.
    """
    if not folder.is_dir():
        return None
    import shapefile
    from pyproj import Transformer
    to_wgs84 = Transformer.from_crs(5186, 4326, always_xy=True)

    def to_local(array):
        lon, lat = to_wgs84.transform(array[:, 0], array[:, 1])
        return np.array([to_meters(a, b) for a, b in zip(lon, lat)])

    def read(name):
        seen = {}
        for sheet in sorted(path for path in folder.iterdir() if path.is_dir()):
            if not (sheet / f"{name}.shp").is_file():
                continue
            reader = shapefile.Reader(str(sheet / name), encoding="cp949")
            for item, record in zip(reader.iterShapes(), reader.iterRecords()):
                if item.points and record["UFID"] not in seen:
                    seen[record["UFID"]] = (shapely.transform(shapely.make_valid(shape(item.__geo_interface__)), to_local), record.as_dict())
        return list(seen.values())

    lines = [(geometry, record["차로수"], record["일방통행"] == "일방통행", record.get("도로명") or record.get("명칭") or "", record.get("도로폭") or None)
             for geometry, record in read("N1L_A0020000")]
    sidewalks = [(geometry, record["폭"] or None) for geometry, record in read("N1A_A0033320")]
    safety = [(geometry, record["구조"]) for geometry, record in read("N1A_A0053326") if record.get("구조") in ("교통섬", "평면(도색)")]
    road = shapely.union_all([geometry for geometry, _ in read("N1A_A0010000")])
    if not lines or road.is_empty:
        return None
    carriageway = road.difference(shapely.union_all([geometry for geometry, _ in sidewalks])) if sidewalks else road
    return Map1000(lines=lines, carriageway=carriageway, sidewalks=sidewalks, safety=safety)


MOCT_NODE_FILE = "vworld_moct_nodes_gangnam.geojson"   # 표준노드링크 노드 (VWorld LT_P_MOCTNODE)
JUNCTION_SNAP_M = 2          # 1:1000 도로선 끝점이 이 거리 안에 모이면 같은 교차점
MOCT_SNAP_M = 15             # 표준노드 교차로 점에서 이 거리 안에 1:1000 교차점이 있으면 같은 교차로
MOCT_ARM_M = 12              # 1:1000 교차점이 없는 표준노드는 이 거리 안의 도로선을 팔로 봄
DEFAULT_HALF_M = 3.0         # 차도 폭을 모르는 도로선의 띠 반폭
WIDTH_AWAY_M = (15, 25)      # 교차점에서 도로를 따라 이만큼 떨어진 곳에서 차도 폭을 잼 (교차로 밖의 곧은 구간)
CORNER_LOOK_M = 5            # 모퉁이 판단: 점 앞뒤 이 거리의 방향을 비교
CORNER_TURN_DEG = 30         # 앞뒤 방향이 이보다 크게 꺾이면 도로의 모퉁이 (강남역 자료: 직선 이음 0~13°, 모퉁이 34° 이상)
DIRECTION_LOOK_M = 2         # 도로선 위 한 점의 진행 방향을 잴 때 앞뒤로 보는 거리
JUNCTION_LOOK_M = 3          # 교차점에서 각 도로의 방향을 잴 때 앞뒤로 보는 거리 (교차점의 짧은 꺾임에 덜 흔들리게 조금 길게)
SEGMENT_MATCH_M = 1          # 이은 도로선 위 한 점에서 이 거리 안의 원래 토막을 찾아 그 도로폭을 씀
INTERSECTION_MARGIN_M = 2    # 교차로·모퉁이 범위(두 반폭 네모의 대각선, 반폭 x 2)에 더하는 여유
CLIP_TOLERANCE_M = 0.5       # 교차로 면을 차도 면으로 자를 때 차도 경계에 주는 여유
CLIP_KEEP_RATIO = 0.5        # 차도 면으로 잘랐을 때 이 비율보다 많이 남으면 자른 면, 아니면(차도 면 없이 선만 있는 좁은 길) 자르지 않음


def _moct_junctions(path):
    """표준노드링크 노드 중 '교차로' 유형의 미터 좌표. 파일이 없으면 []."""
    if not path.is_file():
        return []
    points = []
    for feature in read_json(path)["features"]:
        if not str(feature["properties"].get("nd_type_h", "")).startswith("교차로"):
            continue
        coordinates = feature["geometry"]["coordinates"]
        while isinstance(coordinates[0], list):
            coordinates = coordinates[0]
        points.append(to_meters(*coordinates[:2]))
    return points


def _intersection_area_map1000(source, moct_points):
    """1:1000 도로선·차도 폭으로 교차로 면을 만듭니다 (도로교통법 제32조 1·2호의 '교차로'와 '가장자리' 판정용).

    1) 교차로 위치: 1:1000 도로선 끝점이 모인 곳 중 서로 다른 도로가 만나거나 같은 이름 길이 세 갈래 이상으로 갈라지는 곳
       + 표준노드링크 '교차로' 점
    2) 도로마다 띠: 같은 이름의 토막을 이은 도로선을 따라, 교차점에서 15·25m 떨어진 곳에서 잰 차도 폭만큼
       (도로폭 - 양쪽 보도 폭, 모르면 6m)
    3) 교차로 면 = 교차로 위치 주변에서 방향이 30° 넘게 다른 두 띠가 겹치는 부분(도로 끝선 4개가 만나는 네모), 차도 면 안으로 자름
    둥근 모퉁이는 네모 모서리 바로 바깥이라 '가장자리 5m' 판정으로 덮습니다.
    4) 같은 도로가 30° 넘게 꺾이는 '도로의 모퉁이'도 꺾이기 전·후 띠가 겹치는 부분을 면으로 넣습니다.
    띠를 겹친 근사값이라 비스듬하거나 폭이 바뀌는 교차로에서는 몇 m 어긋날 수 있습니다.
    반환: (교차로 면, 교차로 위치 목록)
    """
    from shapely.geometry import Point

    def key(index):
        name = source.lines[index][3]
        return name or f"#{index}"

    def end_direction(line, at_start):
        a, b = (line.coords[0], line.coords[min(1, len(line.coords) - 1)]) if at_start else (line.coords[-1], line.coords[-2])
        return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360

    # 1:1000은 도로선을 교차점·속성 변화·도엽 경계마다 토막으로 나눠 그립니다. 같은 이름의 토막을 이어 한 줄로 만들어
    # 띠를 그립니다 (교차로 근처의 2~7m짜리 토막으로 띠가 짧게 잘리지 않게). 이름 없는 선은 그대로 씁니다.
    joined = []                                  # [(이어진 선, 도로 이름 또는 '#번호', 도로폭)]
    by_name = defaultdict(list)
    for index, (line, _, _, name, road_width) in enumerate(source.lines):
        if name:
            by_name[name].append(index)
        else:
            joined.append((line, f"#{index}", road_width))
    for name, indices in by_name.items():
        merged = shapely.line_merge(shapely.union_all([source.lines[i][0] for i in indices]))
        widths = sorted(source.lines[i][4] for i in indices if source.lines[i][4])
        road_width = widths[len(widths) // 2] if widths else None
        joined += [(part, name, road_width) for part in getattr(merged, "geoms", [merged]) if part.geom_type == "LineString"]
    def width_at(line, along, name):
        """이어진 선 위 along 지점에서 가로로 잰 차도 폭 (모르면 None). 도로폭은 그 지점에 있는 원래 토막의 값을 씁니다
        (같은 도로도 구간마다 도로폭이 다름. 서초대로는 53m 구간과 30m 구간이 있음)."""
        along = min(max(along, 0.0), line.length)
        at = line.interpolate(along)
        a, b = line.interpolate(max(0.0, along - DIRECTION_LOOK_M)), line.interpolate(min(line.length, along + DIRECTION_LOOK_M))
        heading = (math.degrees(math.atan2(b.x - a.x, b.y - a.y)) + 90) % 360
        nearby = [i for i in source.line_tree.query(at.buffer(SEGMENT_MATCH_M)) if (source.lines[i][3] or f"#{i}") == name]
        road_width = min(((source.lines[i][0].distance(at), source.lines[i][4]) for i in nearby), default=(None, None))[1]
        return source.carriageway_width(at, heading, road_width)

    def half_near(index, point):
        """교차점에서 도로를 따라 WIDTH_AWAY_M 떨어진 곳(양쪽)에서 잰 차도 폭의 가운데 값 / 2.
        교차로 한가운데서 재면 옆 길 쪽으로 빠져 넓게 나오므로 교차로 밖의 곧은 구간에서 잽니다."""
        line, name, _ = joined[index]
        at = line.project(point)
        widths = sorted(w for offset in WIDTH_AWAY_M for sign in (1, -1)
                        if 0 <= at + sign * offset <= line.length
                        for w in [width_at(line, at + sign * offset, name)] if w)
        if not widths:
            widths = [w for w in [width_at(line, line.length / 2, name)] if w]
        return widths[len(widths) // 2] / 2 if widths else DEFAULT_HALF_M

    from shapely.strtree import STRtree
    road_tree = STRtree([line for line, _, _ in joined])

    # 1) 1:1000 교차점: 끝점 묶음
    ends = []
    for index, (line, *_) in enumerate(source.lines):
        if line.length == 0:
            continue
        ends.append((Point(line.coords[0]), index, end_direction(line, True)))
        ends.append((Point(line.coords[-1]), index, end_direction(line, False)))
    clusters = []
    for point, index, direction in ends:
        for cluster in clusters:
            if cluster["point"].distance(point) <= JUNCTION_SNAP_M:
                cluster["arms"].append((index, direction)); break
        else:
            clusters.append({"point": point, "arms": [(index, direction)]})
    nodes = []
    for cluster in clusters:
        arms = cluster["arms"]
        # T자: 다른 선의 중간에 끝점이 닿는 경우 그 선도 팔
        touching = {i for i in source.line_tree.query(cluster["point"].buffer(JUNCTION_SNAP_M))
                    if source.lines[i][0].distance(cluster["point"]) <= JUNCTION_SNAP_M}
        indices = {i for i, _ in arms} | touching
        roads = {key(i) for i in indices}
        if len(indices) == 2 and len(arms) == 2:
            turn = abs((arms[0][1] - arms[1][1] + 180) % 360 - 180)
            if turn > 180 - CONTINUATION_TURN_DEG:      # 거의 일직선으로 이어지면 같은 길의 분할 지점 (도엽 경계 등)
                continue
        if len(roads) >= 2 or len(indices) >= 3:       # 다른 도로가 만나거나, 같은 이름 길이 세 갈래 이상으로 갈라짐 (골목 가지)
            nodes.append((cluster["point"], indices))
    # 2) 표준노드 교차로 점 중 1:1000 교차점과 떨어진 것
    for x, y in moct_points:
        point = Point(x, y)
        if any(node.distance(point) <= MOCT_SNAP_M for node, _ in nodes):
            continue
        indices = {i for i in source.line_tree.query(point.buffer(MOCT_ARM_M)) if source.lines[i][0].distance(point) <= MOCT_ARM_M}
        if len({key(i) for i in indices}) >= 2:
            nodes.append((point, indices))

    pieces = []
    for point, _ in nodes:
        indices = [i for i in road_tree.query(point.buffer(MOCT_ARM_M)) if joined[i][0].distance(point) <= JUNCTION_SNAP_M]
        if len(indices) < 2:
            indices = [i for i in road_tree.query(point.buffer(MOCT_ARM_M)) if joined[i][0].distance(point) <= MOCT_ARM_M]
        if len(indices) < 2:
            continue
        halves = {i: half_near(i, point) for i in indices}
        widest = sorted(halves.values(), reverse=True)
        # 십자로의 겹친 네모는 교차점에서 두 도로 반폭의 대각선 거리 안에 있습니다. 그 밖의 겹침은 나란히 달리는 길입니다.
        local = point.buffer(math.hypot(widest[0], widest[1] if len(widest) > 1 else widest[0]) + INTERSECTION_MARGIN_M)
        # 띠는 이은 도로선마다 따로 그리고, 방향이 30° 넘게 다른 두 띠의 겹침만 교차로로 봅니다
        # (같은 이름 길이 T자로 갈라지는 골목도 잡히고, 일직선으로 이어지는 두 토막은 겹침으로 보지 않음).
        def direction(i):
            line = joined[i][0]; at = line.project(point)
            a, b = line.interpolate(max(0.0, at - JUNCTION_LOOK_M)), line.interpolate(min(line.length, at + JUNCTION_LOOK_M))
            return math.degrees(math.atan2(b.x - a.x, b.y - a.y)) % 180
        bands = {i: joined[i][0].buffer(halves[i], cap_style=2).intersection(local) for i in indices}
        pairs = [(a, b) for n, a in enumerate(indices) for b in indices[n + 1:]
                 if min(abs(direction(a) - direction(b)), 180 - abs(direction(a) - direction(b))) > CORNER_TURN_DEG]
        core = shapely.union_all([bands[a].intersection(bands[b]) for a, b in pairs]) if pairs else shapely.Polygon()
        if core.is_empty:
            continue
        # 차도 면 안으로 자릅니다 (보도로 넘어가지 않게). 차도 면 없이 선만 그려진 좁은 길은 자르지 않습니다.
        clipped = core.intersection(source.carriageway.buffer(CLIP_TOLERANCE_M))
        pieces.append(clipped if clipped.area >= CLIP_KEEP_RATIO * core.area else core)
    # 같은 도로가 꺾이는 모퉁이 (제32조 2호 '도로의 모퉁이'): 이은 도로선에서 앞뒤 CORNER_LOOK_M 방향이
    # CORNER_TURN_DEG 넘게 꺾이는 점. 꺾이기 전·후 구간의 차도 띠가 겹치는 부분을 모퉁이 면으로 씁니다.
    from shapely.ops import substring
    corners = []
    for index, (line, _, _) in enumerate(joined):
        for x, y in line.coords[1:-1]:
            at = line.project(Point(x, y))
            if at < CORNER_LOOK_M or at > line.length - CORNER_LOOK_M:
                continue
            a, b, c = line.interpolate(at - CORNER_LOOK_M), line.interpolate(at), line.interpolate(at + CORNER_LOOK_M)
            turn = abs((math.degrees(math.atan2(c.x - b.x, c.y - b.y)) - math.degrees(math.atan2(b.x - a.x, b.y - a.y)) + 180) % 360 - 180)
            if turn < CORNER_TURN_DEG:
                continue
            if any(b.distance(node) <= MOCT_SNAP_M for node, _ in nodes) or any(b.distance(q) <= CORNER_LOOK_M for q, _, _ in corners):
                continue                                  # 교차로이거나 이미 찾은 모퉁이
            corners.append((b, index, at))
    for point, index, at in corners:
        line = joined[index][0]
        half = half_near(index, point)
        reach = 2 * half + INTERSECTION_MARGIN_M
        before = substring(line, max(0.0, at - reach), at).buffer(half, cap_style=3)    # 꺾인 점 너머로 반폭만큼 연장
        after = substring(line, at, min(line.length, at + reach)).buffer(half, cap_style=3)
        core = before.intersection(after).intersection(point.buffer(reach))
        if core.is_empty:
            continue
        clipped = core.intersection(source.carriageway.buffer(CLIP_TOLERANCE_M))
        pieces.append(clipped if clipped.area >= CLIP_KEEP_RATIO * core.area else core)
    area = shapely.union_all(pieces) if pieces else None
    return area, [(point.x, point.y) for point, _ in nodes] + [(point.x, point.y) for point, _, _ in corners]


ZONE_API_FILE = "child_protection_zones.json"       # 전국어린이보호구역표준데이터 API (pudo download)
ZONE_STANDARD_FILE = "child_protection_zones.csv"   # 같은 표준데이터의 파일 다운로드 (공공데이터포털 15012891)
NOT_ZONE_TARGET = ("중학교", "고등학교")             # 도로교통법 제12조 지정 대상이 아님
FACILITY_RADIUS_M = 1500                            # 판정 영역 500m + 지정 범위 상한 여유


def _read_zone_rows(data_dir):
    """지정 보호구역 시설 행 [(경도, 위도, 이름, 종류)]. 지정 목록이 없으면 None."""
    api_path, csv_path = Path(data_dir) / ZONE_API_FILE, Path(data_dir) / ZONE_STANDARD_FILE
    if api_path.is_file():
        items = read_json(api_path)["items"]
        return [(item.get("longitude"), item.get("latitude"), item.get("trgetFcltyNm", ""), item.get("fcltyKnd", ""))
                for item in items]
    if csv_path.is_file():
        for encoding in ("utf-8-sig", "cp949"):
            try:
                with csv_path.open(encoding=encoding, newline="") as stream:
                    rows = list(csv.DictReader(stream))
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError(f"{csv_path.name}: UTF-8 또는 CP949가 아닙니다.")
        return [(row.get("경도"), row.get("위도"), row.get("대상시설명", ""), row.get("시설종류", "")) for row in rows]
    return None


def _protection_facilities(data_dir, layers):
    """(어린이보호구역 대상시설 [(미터 좌표, 이름)], 지정 목록 여부).

    지정 목록(표준데이터 API 또는 CSV)이 있으면 실제 지정된 시설(유치원·어린이집·학원 포함)을 쓰고,
    없으면 스냅샷의 학교 좌표에서 지정 대상이 아닌 중·고등학교를 뺍니다.
    """
    rows = _read_zone_rows(data_dir)
    if rows is not None:
        facilities = []
        for lon, lat, name, kind in rows:
            try:
                position = to_meters(float(lon), float(lat))
            except (TypeError, ValueError):
                continue
            if math.hypot(*position) <= FACILITY_RADIUS_M:
                facilities.append((position, f"{str(name).strip()} ({str(kind).strip()})"))
        return facilities, True
    return [
        (to_meters(*feature["geometry"]["coordinates"]), feature["properties"]["이름"])
        for feature in layers["C 학교(보호구역 300m 버퍼)"]["features"]
        if not feature["properties"]["이름"].endswith(NOT_ZONE_TARGET)
    ], False


TAXI_STAND_FILE = "taxi_stands.xlsx"   # 서울시 택시승차대 운영 목록 (2026년, 서울시 택시정책과 엑셀 원본)
_XLSX = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_xlsx_rows(path):
    """엑셀 첫 시트의 행 목록 [{열 문자: 값}]. 별도 라이브러리 없이 표준 라이브러리로 읽습니다."""
    with zipfile.ZipFile(path) as book:
        names = set(book.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared = ["".join(t.text or "" for t in item.iter(_XLSX + "t"))
                      for item in ElementTree.fromstring(book.read("xl/sharedStrings.xml"))]
        sheet = ElementTree.fromstring(book.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in sheet.iter(_XLSX + "row"):
        values = {}
        for cell in row.findall(_XLSX + "c"):
            column = re.match(r"[A-Z]+", cell.get("r", "")).group()
            value = cell.find(_XLSX + "v")
            if value is None:
                inline = cell.find(_XLSX + "is")
                values[column] = "".join(t.text or "" for t in inline.iter(_XLSX + "t")) if inline is not None else ""
            else:
                values[column] = shared[int(value.text)] if cell.get("t") == "s" else (value.text or "")
        rows.append(values)
    return rows


def read_xlsx_table(path):
    """엑셀 첫 시트를 [{머리글: 값}]으로. 제목 행 아래 '순번'이 있는 행을 머리글로 쓰고 머리글의 공백·줄바꿈은 지웁니다."""
    rows = read_xlsx_rows(path)
    header_index = next((i for i, row in enumerate(rows) if any(str(v).strip() == "순번" for v in row.values())), None)
    if header_index is None:
        raise ValueError(f"{Path(path).name}: '순번' 머리글 행이 없습니다.")
    header = {column: re.sub(r"\s+", "", name) for column, name in rows[header_index].items() if str(name).strip()}
    return [{header[column]: str(value).strip() for column, value in row.items() if column in header}
            for row in rows[header_index + 1:]]


def _taxi_stands(path):
    """[(미터 좌표, 설명)] 판정 영역 주변 택시승차대. 파일이 없으면 None (자료 없음)."""
    if not path.is_file():
        return None
    stands = []
    for item in read_xlsx_table(path):
        try:
            position = to_meters(float(item["좌표(Poi_X)"]), float(item["좌표(Poi_Y)"]))
        except (KeyError, TypeError, ValueError):
            continue
        if math.hypot(*position) <= FACILITY_RADIUS_M:
            stands.append((position, f"{item.get('위치명', '')} ({item.get('타입', '')}, {item.get('ID주소(신관리번호)', '')})"))
    return stands


LANE_2023_FILE = "tgis/lane_facilities_2023.xlsx"   # 서울시 차선시설물현황 (OA-15537 차선시설물현황_0724.xlsx, 2023-07)
LANE_2023_KINDS = {"주정차금지": "010", "버스정차구획선": "015", "정차금지지대": "014"}   # 2021 T-GIS 선 코드와 대응
LANE_MATCH_M = 3   # 2023 목록의 점이 2021 선에서 이 거리 안이면 같은 선 (좌표 대조 허용 오차, 판정 기준값 아님)


def _lane_points_2023(path):
    """{2021 선 코드: [미터 좌표]} 2023년 목록의 금지선 대표점 (선마다 점 하나). 파일이 없으면 None."""
    if not path.is_file():
        return None
    from pyproj import Transformer
    to_wgs84 = Transformer.from_crs(5186, 4326, always_xy=True)   # T-GIS: KGD2002 중부원점
    points = defaultdict(list)
    for item in read_xlsx_table(path):
        code = LANE_2023_KINDS.get(item.get("차선종류"))
        if code is None:
            continue
        try:
            position = to_meters(*to_wgs84.transform(float(item["X좌표"]), float(item["Y좌표"])))
        except (KeyError, TypeError, ValueError):
            continue
        if math.hypot(*position) <= FACILITY_RADIUS_M:
            points[code].append(position)
    return dict(points)


def _confirm_2023(lines, points):
    """{코드: [2023 목록에도 있는지]} 2021 선마다 같은 종류의 2023 점이 선 위(3m 안)에 있는지. 목록이 없으면 None."""
    if points is None:
        return None
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree
    confirmed = {}
    for code, segments in lines.items():
        candidates = [Point(xy) for xy in points.get(code, [])]
        tree = STRtree(candidates) if candidates else None
        flags = []
        for segment in segments:
            if tree is None or len(segment) < 2:
                flags.append(False)
                continue
            line = LineString(segment)
            flags.append(any(line.distance(candidates[i]) <= LANE_MATCH_M for i in tree.query(line.buffer(LANE_MATCH_M))))
        confirmed[code] = flags
    return confirmed


PLANNED_ROAD_FILE = "vworld_upis_roads_gangnam.geojson"
# 도시·군계획시설의 결정·구조 및 설치기준에 관한 규칙 제9조 규모별 구분: 등급별 최소 폭(m).
# 소로 3류는 '폭 8m 미만'이라 하한이 없습니다.
PLANNED_MIN_WIDTH = {
    ("광로", "1"): 70, ("광로", "2"): 50, ("광로", "3"): 40,
    ("대로", "1"): 35, ("대로", "2"): 30, ("대로", "3"): 25,
    ("중로", "1"): 20, ("중로", "2"): 15, ("중로", "3"): 12,
    ("소로", "1"): 10, ("소로", "2"): 8, ("소로", "3"): 0,
}


def _planned_roads(path):
    """[(미터 좌표 면, 등급명, 최소 폭)]. 등급이 없는 '기타도로시설' 면은 폭 근거가 없어 뺍니다."""
    if not path.is_file():
        return []
    roads = []
    for feature in read_json(path)["features"]:
        properties, geometry = feature.get("properties", {}), feature.get("geometry")
        grade = (str(properties.get("grad_se") or "").strip(), str(properties.get("road_ty") or "").strip())
        if grade not in PLANNED_MIN_WIDTH or not geometry:
            continue
        area = shape(geometry)
        area = shapely.transform(area, lambda xy: np.column_stack(to_meters(xy[:, 0], xy[:, 1])))
        roads.append((area.buffer(0), f"{grade[0]}{grade[1]}류", PLANNED_MIN_WIDTH[grade]))
    return roads


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
    facilities, designated = _protection_facilities(data_dir, layers)
    child_zone, child_zone_facility, evidence = None, None, []
    signs_path, marks_path = data_dir / "tgis" / "A064_P_signs.geojson", data_dir / "tgis" / "A054_P_text_marks.geojson"
    if signs_path.is_file() and marks_path.is_file():
        # 구간은 현장 표지·노면표시로 복원합니다. 지정 목록의 시설은 보호구역이 있다는 근거지만 정문 위치와
        # 지정 범위를 몰라, 시설 주변 도로는 따로 만들어 '확인 필요'로만 씁니다 (간선도로로 번지는 과대 추정 방지).
        # 지정 목록이 없을 때의 스냅샷 학교 좌표(블록 중심, 지정 여부 모름)는 쓰지 않습니다.
        evidence, boundaries = zone_evidence(read_json(signs_path)["features"], read_json(marks_path)["features"], [])
        child_zone = build_child_zone(roads, evidence, boundaries)
        if designated:
            facility_seeds, _ = zone_evidence([], [], facilities)
            child_zone_facility = build_child_zone(roads, facility_seeds, boundaries)
            evidence = evidence + facility_seeds
    # 교차로 면: 1:1000 도로선·차도 폭 + 표준노드 교차로 점. 1:1000이 없으면 교차로 면 없음(H1 자료 없음 → 확인 필요)
    map1000 = _map1000(data_dir / MAP1000_DIR)
    intersection_area, junctions = (_intersection_area_map1000(map1000, _moct_junctions(data_dir / MOCT_NODE_FILE))
                                    if map1000 is not None else (None, []))
    return MapData(
        intersections=junctions,
        intersection_area=intersection_area,
        crosswalks=points("C 횡단보도"),
        hydrants=points("C 소화전"),
        buses=points("C 버스정류소"),
        schools=facilities,
        planned_roads=_planned_roads(data_dir / PLANNED_ROAD_FILE),
        child_zone=child_zone,
        child_zone_facility=child_zone_facility,
        child_zone_evidence=evidence,
        lines=dict(lines),
        hd_reg=_hd_lines(surface, lambda p: p.get("kind") == "515" and p.get("type") in ("111", "121")),
        hd_park=_hd_lines(surface, lambda p: p.get("kind") == "515" and p.get("type") == "112"),
        hd_bus=_hd_lines(surface, lambda p: p.get("kind") == "504"),
        hd_safe=_hd_lines(surface, lambda p: p.get("kind") == "531"),
        hd_link=_hd_lines(links, lambda p: True),
        images=_images(sv_dir / "metadata.csv"),
        taxi_stands=_taxi_stands(data_dir / TAXI_STAND_FILE),
        police_zones=_police_zones(data_dir / POLICE_ZONE_FILE),
        map1000=map1000,
        lines_2023=_confirm_2023(dict(lines), _lane_points_2023(data_dir / LANE_2023_FILE)),
    )


def load_inputs(data_dir, sv_dir):
    """준비 단계 입력: (후보 Feature 목록, 지도 데이터 MapData). 사진 판독 결과는 판정 단계 입력이라 여기서 읽지 않습니다."""
    data_dir = Path(data_dir)
    require_files(
        data_dir / "layers_gangnam.json",
        data_dir / "osm_roads_gangnam.json",
        data_dir / "candidates_v1.geojson",
    )
    candidates = load_candidates(data_dir / "candidates_v1.geojson")
    return candidates, load_map_data(data_dir, sv_dir)
