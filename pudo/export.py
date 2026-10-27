"""판정 결과를 GeoJSON·웹 파일로 저장하고 실행 요약을 출력합니다."""

import json
from collections import Counter, defaultdict
from pathlib import Path

from .config import CENTER_LAT, CENTER_LON, RADIUS_M, TH


def write_results(result, data_dir, web_dir, *, thresholds=None):
    data_dir, web_dir = Path(data_dir), Path(web_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    web_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False)
    geojson_path = data_dir / "pudo_v0.geojson"
    geojson_path.write_text(payload, encoding="utf-8")

    meta = {
        "center": [CENTER_LAT, CENTER_LON], "radius_m": RADIUS_M,
        "thresholds": TH if thresholds is None else thresholds, "generated": "v0",
    }
    javascript = "const PUDO=" + payload + ";\n"
    javascript += "const PUDO_META=" + json.dumps(meta, ensure_ascii=False) + ";\n"
    js_path = web_dir / "pudo_v0_data.js"
    js_path.write_text(javascript, encoding="utf-8")
    outputs = [geojson_path, js_path]

    template = web_dir / "index.html"
    if template.is_file():
        html = template.read_text(encoding="utf-8")
        standalone = web_dir / "pudo_v0_standalone.html"
        standalone.write_text(
            html.replace('<script src="pudo_v0_data.js"></script>', "<script>\n" + javascript + "\n</script>", 1),
            encoding="utf-8",
        )
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
    lenient = Counter(f["properties"]["verdict_lenient"] for f in features)
    print(f"  완화(임시 규칙→확인 필요): 가능 {lenient['가능']} / 확인 필요 {lenient['확인 필요']} / 불가 {lenient['불가']}")
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
