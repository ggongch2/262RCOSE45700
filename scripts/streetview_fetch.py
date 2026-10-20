"""Google Street View 커버리지 실측 + 이미지 확보 (카카오 p.18 2단계).

사용법:
  $env:GOOGLE_MAPS_KEY = "AIza..."          # PowerShell
  python scripts/streetview_fetch.py candidates.geojson --out sv_out [--fetch-images]

입력: 후보점 GeoJSON (Point, properties에 id, heading 있으면 사용)
      없으면 --demo 로 강남역 주변 격자 점 49개를 생성해 커버리지만 봅니다.

동작:
  1) Metadata API (무료)로 각 후보점에 사진이 있는지·촬영일·pano_id 확인
  2) --fetch-images 를 주면 있는 것만 Static API로 640x640 요청 (1,000장당 약 $7)
출력:
  sv_out/metadata.csv      id, lon, lat, status, pano_id, date, pano_lon, pano_lat, offset_m
  sv_out/img/<id>.jpg      (--fetch-images 일 때)

약관 메모: 사진 파일은 분석 후 장기 보관하지 않는다. pano_id와 결과 라벨만 저장한다.
"""
import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

KEY = os.environ.get("GOOGLE_MAPS_KEY")
META = "https://maps.googleapis.com/maps/api/streetview/metadata"
IMG = "https://maps.googleapis.com/maps/api/streetview"


def dist_m(lon1, lat1, lon2, lat2):
    dx = (lon2 - lon1) * math.cos(math.radians(lat1)) * 111320
    dy = (lat2 - lat1) * 110540
    return math.hypot(dx, dy)


def metadata(lon, lat, radius=30):
    q = urllib.parse.urlencode(
        {"location": f"{lat},{lon}", "radius": radius, "source": "outdoor", "key": KEY}
    )
    with urllib.request.urlopen(f"{META}?{q}", timeout=30) as r:
        return json.load(r)


def fetch_image(pano_id, heading, path, fov=90, pitch=0):
    q = urllib.parse.urlencode(
        {"size": "640x640", "pano": pano_id, "heading": heading, "fov": fov, "pitch": pitch, "key": KEY}
    )
    with urllib.request.urlopen(f"{IMG}?{q}", timeout=60) as r, open(path, "wb") as f:
        f.write(r.read())


def demo_points():
    cl, ca = 127.0276, 37.4979
    pts = []
    for i in range(-3, 4):
        for j in range(-3, 4):
            pts.append({"id": f"D{i+3}{j+3}", "lon": cl + i * 0.0015, "lat": ca + j * 0.0012, "heading": 0})
    return pts


def load_geojson(path):
    gj = json.load(open(path, encoding="utf-8"))
    out = []
    for k, f in enumerate(gj["features"]):
        lon, lat = f["geometry"]["coordinates"][:2]
        p = f.get("properties", {})
        out.append({"id": p.get("id", f"C{k:04d}"), "lon": lon, "lat": lat, "heading": p.get("heading", 0)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("geojson", nargs="?")
    ap.add_argument("--out", default="sv_out")
    ap.add_argument("--fetch-images", action="store_true")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    if not KEY:
        sys.exit("GOOGLE_MAPS_KEY 환경변수가 없습니다.")
    pts = demo_points() if a.demo or not a.geojson else load_geojson(a.geojson)
    os.makedirs(os.path.join(a.out, "img"), exist_ok=True)

    rows = []
    for p in pts:
        try:
            m = metadata(p["lon"], p["lat"])
        except Exception as e:
            m = {"status": f"ERROR {e}"}
        row = {"id": p["id"], "lon": p["lon"], "lat": p["lat"], "status": m.get("status"),
               "pano_id": m.get("pano_id", ""), "date": m.get("date", ""),
               "pano_lon": "", "pano_lat": "", "offset_m": ""}
        if m.get("status") == "OK":
            loc = m["location"]
            row["pano_lon"], row["pano_lat"] = loc["lng"], loc["lat"]
            row["offset_m"] = round(dist_m(p["lon"], p["lat"], loc["lng"], loc["lat"]), 1)
            if a.fetch_images:
                fetch_image(m["pano_id"], p["heading"], os.path.join(a.out, "img", f"{p['id']}.jpg"))
                time.sleep(0.1)
        rows.append(row)
        print(f"{row['id']}: {row['status']} {row['date']} offset={row['offset_m']}")

    with open(os.path.join(a.out, "metadata.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if r["status"] == "OK"]
    years = {}
    for r in ok:
        years[r["date"][:4]] = years.get(r["date"][:4], 0) + 1
    print(f"\n후보 {len(rows)}개 중 사진 있음 {len(ok)}개 ({100*len(ok)/max(1,len(rows)):.0f}%)")
    print("촬영 연도별:", dict(sorted(years.items())))
    if ok:
        offs = sorted(float(r["offset_m"]) for r in ok)
        print(f"후보점과 파노라마 위치 차이: 중앙값 {offs[len(offs)//2]}m, 최대 {offs[-1]}m")


if __name__ == "__main__":
    main()
