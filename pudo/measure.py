"""준비 단계: 후보마다 지도에서 사실(측정)과 추정값을 구합니다. 판정은 하지 않습니다.

원칙
  - 기준값(TH)을 쓰지 않습니다. 기준값이 바뀌어도 측정값은 그대로입니다.
  - 데이터에 없으면 None(모름)으로 둡니다. 가정으로 채우지 않습니다.
  - 계산에 꼭 필요한 추정(후보 위치, 교차로 면, 보호구역 구간)은 추정이라는 것과 근거를 남깁니다.

결과는 data/prepared/measurements.json (`python -m pudo prepare`)이며 판정(rules.py)의 유일한 입력입니다.
거리 단위는 m입니다.
"""

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR, SV_DIR
from .geo import distance_to_area, distance_to_lines, distance_to_points, distance_to_segment, distance_to_segments, to_meters

SCHEMA_VERSION = "pudo-measure.v3"
PREPARED_DIR = "prepared"
MEASUREMENT_FILE = "measurements.json"
LAYER_FILE = "layers.json"
HD_COVER_M = 15                       # 후보에서 이 거리 안에 정밀도로지도 차로링크가 있으면 그 도로를 덮는 것으로 봄
NO_STOP = ("010", "014", "015", "005", "017")   # T-GIS 규제차선 코드: 주정차금지
NO_PARK = ("009",)                              # T-GIS 규제차선 코드: 주차금지 (정차 가능)
LANE_SOURCE = {"hdmap": "정밀도로지도", "osm": "OSM"}
TAXI_SEARCH_M = 60                    # 이 직선거리 안의 택시승차대만 봄 (같은 도로 앞뒤)
TAXI_SIDEWALK_M = 15                  # 승차대 좌표가 연석에서 바깥(보도·건물 쪽)으로 이만큼까지는 같은 도로로 봄


def _round(value):
    """측정값은 정밀도를 줄이지 않고 저장합니다 (반올림하면 기준값 경계에서 판정이 바뀝니다)."""
    return None if value is None else float(value)


def _child_zone(pr, p, data):
    """어린이보호구역 측정. T-GIS 구간이 있으면 구간 안/지정 시설 주변/밖, 없으면 대상시설까지 거리."""
    if data.child_zone is not None or data.child_zone_facility is not None:
        road = to_meters(*pr["centerline"]) if pr.get("centerline") else p

        def nearest(facility):
            found = [(math.hypot(p[0] - x, p[1] - y), label) for (x, y), label in data.child_zone_evidence
                     if label.startswith("지정 시설") == facility]
            return min(found, default=(None, None))

        if data.child_zone is not None and distance_to_area(road, data.child_zone) <= 1.0:
            near = nearest(False)
            label = f"근거 {near[1]} {near[0]:.0f}m" if near[0] is not None else "근거 없음"
            return {"mode": "tgis", "state": "in", "label": label}
        if data.child_zone_facility is not None and distance_to_area(road, data.child_zone_facility) <= 1.0:
            near = nearest(True)
            label = f"{near[1]} {near[0]:.0f}m" if near[0] is not None else "지정 시설"
            return {"mode": "tgis", "state": "near", "label": label}
        return {"mode": "tgis", "state": "out", "label": None}
    nearest_school = min(((math.hypot(p[0] - s[0], p[1] - s[1]), name) for s, name in data.schools), default=(None, None))
    distance, name = nearest_school
    return {"mode": "radius", "distance": _round(distance), "label": f"{name} {distance:.0f}m" if distance is not None else None}


def _no_stop_2023(p, data):
    """후보에서 가장 가까운 2021 주정차금지 선이 2023 목록에도 있는지. 2023 목록이 없거나 2021 선이 없으면 None."""
    if data.lines_2023 is None:
        return None
    nearest = min(((distance_to_segment(p, segment), code, index) for code in NO_STOP
                   for index, segment in enumerate(data.lines.get(code, ()))), default=None)
    if nearest is None:
        return None
    _, code, index = nearest
    flags = data.lines_2023.get(code, [])
    return bool(flags[index]) if index < len(flags) else False


def _taxi_stand(pr, p, data):
    """같은 도로·같은 쪽에 있는 가장 가까운 택시승차대까지 도로를 따라 잰 거리.

    승차대 목록은 시설물 점 하나만 있고 정차 구간(시작·끝)은 없습니다. 길 건너편 승차대를 빼려고
    도로 중심선 기준으로 후보와 같은 쪽(연석 쪽)에 있는 승차대만 보고, 진행 방향 성분만 거리로 씁니다.
    승차대 좌표가 차도 안에 찍힌 경우가 있어 중심선과 연석 사이도 같은 쪽으로 봅니다.
    일방통행 차도(중앙분리대로 나뉜 대로의 한쪽 포함)는 연석이 오른쪽 하나뿐이라 차도 안 전체를 같은 쪽으로 봅니다.
    """
    if data.taxi_stands is None:
        return None
    heading = math.radians(pr.get("heading", 0))          # 중심선 → 연석 방향
    nx, ny = math.sin(heading), math.cos(heading)
    tx, ty = -ny, nx                                       # 진행 방향
    offset = pr.get("curb_offset_m") or 0
    oneway = str(pr.get("oneway", "no")).lower() in ("yes", "true", "1", "-1")
    center = to_meters(*pr["centerline"]) if pr.get("centerline") else (p[0] - nx * offset, p[1] - ny * offset)
    best = (None, None)
    for (x, y), label in data.taxi_stands:
        if math.hypot(x - p[0], y - p[1]) > TAXI_SEARCH_M:
            continue
        across = (x - center[0]) * nx + (y - center[1]) * ny
        lowest = -offset if oneway else 0
        if not lowest < across <= offset + TAXI_SIDEWALK_M:
            continue
        along = abs((x - p[0]) * tx + (y - p[1]) * ty)
        if best[0] is None or along < best[0]:
            best = (along, label)
    return {"distance": _round(best[0]), "label": best[1], "estimated": True}


def _planned_road(pr, p, data):
    """후보 도로가 속한 도시계획시설 도로 {grade, min_width}. 없으면 None.

    교차로에서는 가로지르는 도로의 면에 걸리기 쉬워, 도로 중심점과 진행 방향 앞뒤 10m가
    모두 같은 면 안에 있을 때만 그 도로의 등급으로 봅니다.
    """
    if not data.planned_roads:
        return None
    from shapely.geometry import Point
    center = to_meters(*pr["centerline"]) if pr.get("centerline") else p
    along = math.radians((pr.get("heading", 0) - 90) % 360)   # heading은 진행 방향 우측(연석 쪽)
    dx, dy = math.sin(along) * 10, math.cos(along) * 10
    probes = [Point(center), Point(center[0] + dx, center[1] + dy), Point(center[0] - dx, center[1] - dy)]
    for area, grade, min_width in data.planned_roads:
        if all(area.contains(probe) for probe in probes):
            return {"grade": grade, "min_width": min_width}
    return None


def _position_basis(pr):
    """후보 위치(연석)를 중심선에서 옮긴 근거. candidates.py의 이동 거리 계산과 같은 순서입니다."""
    lanes, width = pr.get("lanes"), pr.get("road_width_m")
    oneway = str(pr.get("oneway", "no")).lower() in ("yes", "true", "1", "-1")
    if lanes is not None:
        half = lanes * 3.25 / 2 if oneway else max(lanes, 2) * 3.25 / 2
        if width and width / 2 < half:
            return "지적도 도로 폭"
    return {"hdmap": "정밀도로지도 차로 수", "osm": "OSM 차로 수"}.get(pr.get("lanes_source"), "근거 없음 (도로 유형별 기본 차로 수)")


def _image(pr, p, data):
    """후보에 연결된 Google 사진. 다시 수집한 도로망은 후보 ID 순서가 바뀔 수 있어 좌표까지 확인합니다."""
    image = data.images.get(pr["id"])
    if not image:
        return None
    try:
        query = to_meters(float(image["lon"]), float(image["lat"]))
    except (KeyError, TypeError, ValueError):
        return None
    return {"pano_id": image["pano_id"]} if math.hypot(p[0] - query[0], p[1] - query[1]) <= 1 else None


def measure(feature, data):
    """후보 하나의 측정값."""
    pr = feature["properties"]
    p = to_meters(*feature["geometry"]["coordinates"])
    d_intersection = distance_to_area(p, data.intersection_area)
    hd_covered = (d := distance_to_segments(p, data.hd_link)) is not None and d <= HD_COVER_M
    lanes_known = pr.get("lanes_source") in LANE_SOURCE
    return {
        "id": pr["id"], "coordinates": feature["geometry"]["coordinates"], "heading": pr["heading"],
        "side": pr.get("side"), "centerline": pr.get("centerline"),
        "road": {"name": pr.get("name", ""), "type": pr["road_type"], "osm_way": pr.get("osm_way"),
                 "oneway": str(pr.get("oneway", "no")).lower() in ("yes", "true", "1", "-1")},
        # 연석 위치는 데이터에 없어 중심선에서 차도 반폭만큼 옮긴 추정 위치입니다.
        "position": {"estimated": True, "offset_m": pr.get("curb_offset_m"), "basis": _position_basis(pr)},
        "lanes": {"value": pr["lanes"] if lanes_known else None, "source": LANE_SOURCE.get(pr.get("lanes_source"))},
        "width": {"value": _round(pr.get("road_width_m")), "source": "지적도 도로 필지 (보도 포함)" if pr.get("road_width_m") is not None else None},
        "planned_road": _planned_road(pr, p, data),
        "sidewalk": {"value": None, "source": None},      # 보도 자료 없음. 사진 판독 결과가 들어오면 판정에서 반영
        "intersection": {"inside": None if d_intersection is None else d_intersection == 0,
                         "distance": _round(d_intersection), "estimated": True},
        "crosswalk": _round(distance_to_points(p, data.crosswalks)),
        "bus_stop": _round(distance_to_points(p, data.buses)),
        "hydrant": _round(distance_to_points(p, data.hydrants)),
        "child_zone": _child_zone(pr, p, data),
        "safety_zone": {"covered": bool(data.hd_safe) and hd_covered,
                        "distance": _round(distance_to_segments(p, data.hd_safe)) if data.hd_safe and hd_covered else None},
        "reg_line": {
            "tgis_no_stop": _round(distance_to_lines(p, data.lines, NO_STOP)),
            "tgis_no_park": _round(distance_to_lines(p, data.lines, NO_PARK)),
            "tgis_no_stop_2023": _no_stop_2023(p, data),   # 가장 가까운 2021 금지선이 2023 목록에도 있나 (None: 목록 없음)
            "hd_covered": hd_covered,
            "hd_no_stop": _round(distance_to_segments(p, data.hd_reg)) if hd_covered else None,
            "hd_bus": _round(distance_to_segments(p, data.hd_bus)) if hd_covered else None,
            "hd_no_park": _round(distance_to_segments(p, data.hd_park)) if hd_covered else None,
        },
        # 서울시 택시승차대 (시설물 점). None = 자료 없음, distance None = 같은 쪽 60m 안에 없음
        "taxi_stand": _taxi_stand(pr, p, data),
        "image": _image(pr, p, data),
    }


def measure_all(candidates, data):
    return [measure(feature, data) for feature in candidates]


def prepared_paths(data_dir):
    folder = Path(data_dir) / PREPARED_DIR
    return folder / MEASUREMENT_FILE, folder / LAYER_FILE


def _input_files(data_dir, sv_dir):
    data_dir = Path(data_dir)
    names = ["candidates_v1.geojson", "layers_gangnam.json", "osm_roads_gangnam.json", "child_protection_zones.json",
             "child_protection_zones.csv", "vworld_upis_roads_gangnam.geojson", "hdmap/lt_l_a2link.geojson",
             "hdmap/lt_l_b2surfacelinemark.geojson", "tgis/A064_P_signs.geojson", "tgis/A054_P_text_marks.geojson",
             "taxi_stands.xlsx", "tgis/lane_facilities_2023.xlsx"]
    files = [data_dir / name for name in names] + [Path(sv_dir) / "metadata.csv"]
    return [path for path in files if path.is_file()]


def is_stale(data_dir, sv_dir):
    """측정값 파일이 없거나 입력 데이터보다 오래되었는지."""
    measurement_path, layer_path = prepared_paths(data_dir)
    if not measurement_path.is_file() or not layer_path.is_file():
        return True
    built = min(measurement_path.stat().st_mtime, layer_path.stat().st_mtime)
    return any(path.stat().st_mtime > built for path in _input_files(data_dir, sv_dir))


def prepare(data_dir, sv_dir):
    """입력 데이터 → 측정값과 지도 근거 레이어를 data/prepared/에 저장합니다."""
    from .datasets import load_inputs
    from .export import overlay_layers

    candidates, data = load_inputs(data_dir, sv_dir)
    measurements = measure_all(candidates, data)
    measurement_path, layer_path = prepared_paths(data_dir)
    measurement_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
               "count": len(measurements), "candidates": measurements}
    for path, value in ((measurement_path, payload), (layer_path, overlay_layers(data))):
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    return measurements


def load_prepared(data_dir, sv_dir, *, refresh=False):
    """(측정값 목록, 지도 근거 레이어). 없거나 입력보다 오래되면 다시 준비합니다."""
    measurement_path, layer_path = prepared_paths(data_dir)
    if refresh or is_stale(data_dir, sv_dir):
        prepare(data_dir, sv_dir)
    payload = json.loads(measurement_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        prepare(data_dir, sv_dir)
        payload = json.loads(measurement_path.read_text(encoding="utf-8"))
    return payload["candidates"], json.loads(layer_path.read_text(encoding="utf-8"))


def summarize(measurements):
    """측정값의 모름·추정 현황."""
    total = len(measurements)
    unknown = {
        "차로 수 모름": sum(m["lanes"]["value"] is None for m in measurements),
        "도로 폭 모름": sum(m["width"]["value"] is None for m in measurements),
        "보도 모름": sum(m["sidewalk"]["value"] is None for m in measurements),
        "도시계획 등급 없음": sum(m["planned_road"] is None for m in measurements),
        "정밀도로지도 미커버": sum(not m["reg_line"]["hd_covered"] for m in measurements),
        "택시승차대 자료 없음": sum(m["taxi_stand"] is None for m in measurements),
    }
    basis = {}
    for m in measurements:
        basis[m["position"]["basis"]] = basis.get(m["position"]["basis"], 0) + 1
    print(f"후보 {total}개 측정")
    print("  모름·없음:", ", ".join(f"{name} {count}" for name, count in unknown.items()))
    print("  후보 위치(연석) 추정 근거:", ", ".join(f"{name} {count}" for name, count in sorted(basis.items(), key=lambda x: -x[1])))


def main(argv=None):
    argparse.ArgumentParser(description="준비: 후보별 측정값 생성 (판정 없음)").parse_args(argv)
    measurements = prepare(DATA_DIR, SV_DIR)
    summarize(measurements)
    print(f"저장: {prepared_paths(DATA_DIR)[0]}, {prepared_paths(DATA_DIR)[1]}")


if __name__ == "__main__":
    main()
