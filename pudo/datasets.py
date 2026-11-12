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
    return MapData(
        intersections=_intersections(roads),
        intersection_area=_intersection_area(roads, _half_widths(data_dir / "candidates_v1.geojson")),
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
