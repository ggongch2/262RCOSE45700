"""판정 결과를 GeoJSON·웹 파일로 저장하고 실행 요약을 출력합니다."""

import json
from collections import Counter, defaultdict
from pathlib import Path

from .config import CENTER_LAT, CENTER_LON, RADIUS_M, TH
from .geo import to_lonlat


def _lonlat(geometry):
    """미터 좌표 shapely 도형 → GeoJSON 도형 (WGS84)."""
    import shapely
    from shapely.geometry import mapping

    def convert(coordinates):
        return [to_lonlat(x, y) for x, y in coordinates]

    return mapping(shapely.transform(geometry, lambda array: convert(array)))


def overlay_layers(map_data):
    """화면에서 판정 근거를 겹쳐 볼 레이어. 미터 좌표를 WGS84로 되돌립니다."""
    from shapely.geometry import LineString, Point

    def points(items, **extra):
        return [{"type": "Feature", "geometry": _lonlat(Point(xy)), "properties": dict(extra)} for xy in items]

    def parts(geometry):
        if geometry is None or geometry.is_empty:
            return []
        return list(getattr(geometry, "geoms", [geometry]))

    no_stop = [segment for code in ("010", "014", "015", "005", "017") for segment in map_data.lines.get(code, ())]
    layers = {
        "intersection": [{"type": "Feature", "geometry": _lonlat(part), "properties": {}} for part in parts(map_data.intersection_area)],
        "child_zone": [{"type": "Feature", "geometry": _lonlat(part), "properties": {}} for part in parts(map_data.child_zone)],
        "child_zone_evidence": [{"type": "Feature", "geometry": _lonlat(Point(xy)), "properties": {"label": label}}
                                for xy, label in map_data.child_zone_evidence],
        "crosswalk": points(map_data.crosswalks),
        "bus": points(map_data.buses),
        "hydrant": points(map_data.hydrants),
        "no_stop_line": [{"type": "Feature", "geometry": _lonlat(LineString(segment)), "properties": {}}
                         for segment in no_stop if len(segment) >= 2],
    }
    return {name: {"type": "FeatureCollection", "features": features} for name, features in layers.items()}


DATA_SCRIPT_TAG = '<script src="pudo_v0_data.js"></script>'


def data_script(result, *, thresholds=None, layers=None, rules_off=()):
    """화면이 읽는 데이터 스크립트: 판정 결과(PUDO), 메타(PUDO_META), 지도 근거 레이어(PUDO_LAYERS)."""
    meta = {
        "center": [CENTER_LAT, CENTER_LON], "radius_m": RADIUS_M,
        "thresholds": TH if thresholds is None else thresholds, "rules_off": sorted(rules_off), "generated": "v0",
    }
    javascript = "const PUDO=" + json.dumps(result, ensure_ascii=False) + ";\n"
    javascript += "const PUDO_META=" + json.dumps(meta, ensure_ascii=False) + ";\n"
    javascript += "const PUDO_LAYERS=" + json.dumps(layers or {}, ensure_ascii=False) + ";\n"
    return javascript


def standalone_html(template_html, javascript):
    """화면 HTML에 데이터를 넣어 서버 없이 열리는 단일 파일로 만듭니다."""
    return template_html.replace(DATA_SCRIPT_TAG, "<script>\n" + javascript + "\n</script>", 1)


def write_results(result, data_dir, web_dir, *, thresholds=None, layers=None):
    data_dir, web_dir = Path(data_dir), Path(web_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    web_dir.mkdir(parents=True, exist_ok=True)
    geojson_path = data_dir / "pudo_v0.geojson"
    geojson_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    javascript = data_script(result, thresholds=thresholds, layers=layers)
    js_path = web_dir / "pudo_v0_data.js"
    js_path.write_text(javascript, encoding="utf-8")
    outputs = [geojson_path, js_path]
    template = web_dir / "index.html"
    if template.is_file():
        standalone = web_dir / "pudo_v0_standalone.html"
        standalone.write_text(standalone_html(template.read_text(encoding="utf-8"), javascript), encoding="utf-8")
        outputs.append(standalone)
    return outputs


def print_summary(result):
    features = result["features"]
    n = len(features)
    verdicts = Counter(f["properties"]["verdict"] for f in features)
    print(
        f"후보 {n}개 → 가능 {verdicts['가능']} ({100 * verdicts['가능'] / max(1, n):.0f}%) / "
        f"확인 필요 {verdicts['확인 필요']} ({100 * verdicts['확인 필요'] / max(1, n):.0f}%) / "
        f"불가 {verdicts['불가']} ({100 * verdicts['불가'] / max(1, n):.0f}%)"
    )
    failures = Counter(
        " ".join(reason.split(" ")[:2]) for f in features for reason in f["properties"]["fail_reasons"]
    )
    print("불가 사유 (중복 포함):", failures.most_common())
    needs = Counter(
        " ".join(reason.split(" ")[:2]) for f in features for reason in f["properties"]["check_reasons"]
    )
    print("확인 필요 사유:", needs.most_common())
    by_road = defaultdict(Counter)
    for feature in features:
        properties = feature["properties"]
        by_road[properties["road_type"]][properties["verdict"]] += 1
    for road, counts in by_road.items():
        print(f"  {road:14s} 가능 {counts['가능']:4d}  확인 {counts['확인 필요']:4d}  불가 {counts['불가']:4d}")
