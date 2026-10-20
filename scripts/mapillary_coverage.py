"""강남역 반경 500m Mapillary 커버리지 실측 스크립트.

사용법:
  1. https://www.mapillary.com/dashboard/developers 에서 앱 등록 → Client Token 발급 (무료)
  2. 환경변수 MLY_TOKEN 에 토큰 설정 후 실행
       PowerShell:  $env:MLY_TOKEN="MLY|...";  python scripts/mapillary_coverage.py
  3. 결과: 콘솔 요약 + mapillary_gangnam.geojson (이미지 위치·촬영일·heading)

bbox 제한: Mapillary 검색 API는 한 번에 0.01° × 0.01° 보다 작은 bbox만 허용하므로
중심 ±0.0045° (약 ±500m) 범위를 4개 타일로 나눠 조회한다.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

TOKEN = os.environ.get("MLY_TOKEN")
if not TOKEN:
    sys.exit("MLY_TOKEN 환경변수에 Mapillary client token을 넣어 주세요.")

CENTER_LON, CENTER_LAT = 127.0276, 37.4979  # 강남역
HALF = 0.0045  # 약 500m (위도 기준). 경도는 cos(37.5°)≈0.79 이므로 실제 폭은 약 400m
FIELDS = "id,captured_at,compass_angle,computed_geometry,is_pano,sequence,thumb_1024_url"

# 0.01° 제한 때문에 2×2 타일로 분할
steps = [-HALF, 0.0]
tiles = [
    (CENTER_LON + dx, CENTER_LAT + dy, CENTER_LON + dx + HALF, CENTER_LAT + dy + HALF)
    for dx in steps
    for dy in steps
]

features = []
seen = set()
for (w, s, e, n) in tiles:
    url = "https://graph.mapillary.com/images?" + urllib.parse.urlencode(
        {"access_token": TOKEN, "fields": FIELDS, "bbox": f"{w},{s},{e},{n}", "limit": 2000}
    )
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    for img in data.get("data", []):
        if img["id"] in seen:
            continue
        seen.add(img["id"])
        ts = datetime.fromtimestamp(img["captured_at"] / 1000, tz=timezone.utc)
        features.append(
            {
                "type": "Feature",
                "geometry": img["computed_geometry"],
                "properties": {
                    "id": img["id"],
                    "captured_at": ts.strftime("%Y-%m"),
                    "compass_angle": img.get("compass_angle"),
                    "is_pano": img.get("is_pano"),
                    "sequence": img.get("sequence"),
                    "thumb_1024_url": img.get("thumb_1024_url"),
                },
            }
        )
    print(f"tile {w:.4f},{s:.4f},{e:.4f},{n:.4f}: {len(data.get('data', []))} images (limit 2000)")

print(f"\n총 이미지: {len(features)}")
print("촬영 연도별:", Counter(f["properties"]["captured_at"][:4] for f in features).most_common())
print("파노라마 여부:", Counter(f["properties"]["is_pano"] for f in features))
print("시퀀스 수:", len({f["properties"]["sequence"] for f in features}))

out = "mapillary_gangnam.geojson"
with open(out, "w", encoding="utf-8") as fp:
    json.dump({"type": "FeatureCollection", "features": features}, fp, ensure_ascii=False)
print(f"저장: {out}  (QGIS/geojson.io 에서 열어 OSM 후보점과 겹쳐 보세요)")
