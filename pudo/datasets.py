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


@dataclass
class MapData:
    """한 번 로딩해 여러 후보와 판정 모드에서 공유하는 읽기용 데이터."""

    intersections: list = field(default_factory=list)
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

    return MapData(
        intersections=_intersections(read_json(data_dir / "osm_roads_gangnam.json")["elements"]),
        crosswalks=points("C 횡단보도"),
        hydrants=points("C 소화전"),
        buses=points("C 버스정류소"),
        schools=[
            (to_meters(*feature["geometry"]["coordinates"]), feature["properties"]["이름"])
            for feature in layers["C 학교(보호구역 300m 버퍼)"]["features"]
        ],
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
