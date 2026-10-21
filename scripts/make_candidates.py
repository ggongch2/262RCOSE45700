"""후보점 생성 (카카오 안내 p.18 1단계).

OSM 도로를 15m 간격으로 샘플링하고, 방향별로 우측 가장자리 차로에 점을 놓는다.
  - 일방통행(OSM이 방향별로 나눠 그린 대로 포함): 진행 방향 우측 1개
  - 양방향: 두 방향 각각 우측 1개 → 2개
가장자리로 옮기는 거리 = 차도 반폭 - 1.5m. 차도 반폭은 정밀도로지도 차로 수(대로) 또는 지적도 도로 폭(이면도로)으로.

입력: data/osm_roads_gangnam.json, data/hdmap/lt_l_a2link.geojson, data/vworld_road_parcels_gangnam.geojson
출력: data/candidates_v1.geojson  (id, heading, side, road_type, oneway, lanes, road_width_m, curb_offset_m, centerline)
실행: python scripts/make_candidates.py
"""
import json
import math
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = lambda *p: os.path.join(ROOT, *p)
sys.path.insert(0, r"C:/Users/ggong/AppData/Local/Temp/claude/c--SSJ-code-262RCOSE45700/1bb4ce32-af11-47e5-b627-ddfcf494a8d9/scratchpad/pylib")
from shapely.geometry import shape, Point, LineString
from shapely.ops import unary_union
from shapely.strtree import STRtree

CL, CA = 127.0276, 37.4979
CX, CY = math.cos(math.radians(CA)) * 111320, 110540
m = lambda lon, lat: ((lon - CL) * CX, (lat - CA) * CY)
inv = lambda x, y: (round(CL + x / CX, 7), round(CA + y / CY, 7))
STEP, RADIUS, LANE_W = 15, 500, 3.25
DRIVABLE = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "residential", "service", "living_street", "unclassified"}
LANES_DEFAULT = {"primary": 4, "primary_link": 2, "secondary": 3, "tertiary": 2}

osm = [e for e in json.load(open(D("data", "osm_roads_gangnam.json"), encoding="utf-8"))["elements"] if e["type"] == "way" and e["tags"].get("highway") in DRIVABLE]
a2 = [(LineString([m(*c[:2]) for c in f["geometry"]["coordinates"][0]]), f["properties"]) for f in json.load(open(D("data", "hdmap", "lt_l_a2link.geojson"), encoding="utf-8"))["features"] if f.get("geometry")]
a2g = [g for g, _ in a2]; a2tree = STRtree(a2g)
parcels = unary_union([shape({"type": f["geometry"]["type"], "coordinates": [[[list(m(*c[:2])) for c in ring] for ring in poly] for poly in f["geometry"]["coordinates"]]}) for f in json.load(open(D("data", "vworld_road_parcels_gangnam.geojson"), encoding="utf-8"))["features"]])

def hd_lanes(pt):
    ln = [a2[i][1]["laneno"] for i in a2tree.query(pt.buffer(15)) if a2g[i].distance(pt) <= 15 and isinstance(a2[i][1]["laneno"], int) and a2[i][1]["laneno"] < 90]
    return max(ln) if ln else None

def parcel_width(pt, heading):
    if not parcels.contains(pt): return None
    dx, dy = math.sin(math.radians(heading)), math.cos(math.radians(heading))
    ray = LineString([(pt.x - dx * 60, pt.y - dy * 60), (pt.x + dx * 60, pt.y + dy * 60)]).intersection(parcels)
    for s in ([ray] if ray.geom_type == "LineString" else list(getattr(ray, "geoms", []))):
        if s.distance(pt) < 0.5: return round(s.length, 1)
    return None

feats = []; k = 0
for w in osm:
    tags = w["tags"]; hw = tags["highway"]
    oneway = str(tags.get("oneway", "no")).lower() in ("yes", "true", "1", "-1")
    osm_lanes = int(tags["lanes"]) if str(tags.get("lanes", "")).isdigit() else None
    pts = [m(p["lon"], p["lat"]) for p in w["geometry"]]
    acc = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        L = math.hypot(x2 - x1, y2 - y1)
        if L == 0: continue
        fwd = (math.degrees(math.atan2(x2 - x1, y2 - y1))) % 360
        while acc <= L:
            t = acc / L; x, y = x1 + t * (x2 - x1), y1 + t * (y2 - y1)
            if math.hypot(x, y) <= RADIUS:
                pt = Point(x, y)
                lanes = hd_lanes(pt) or osm_lanes or LANES_DEFAULT.get(hw, 1)
                lanes_src = "hdmap" if hd_lanes(pt) else ("osm" if osm_lanes else "default")
                width = parcel_width(pt, (fwd + 90) % 360)
                for side_hd in ([fwd] if oneway else [fwd, (fwd + 180) % 360]):
                    heading = (side_hd + 90) % 360                      # 진행 방향의 우측
                    half = lanes * LANE_W / 2 if oneway else max(lanes, 2) * LANE_W / 2
                    if width: half = min(half, width / 2)
                    off = max(0.0, half - 1.5)
                    hx, hy = x + math.sin(math.radians(heading)) * off, y + math.cos(math.radians(heading)) * off
                    feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": list(inv(hx, hy))},
                        "properties": {"id": f"P{k:04d}", "heading": round(heading), "side": "R" if side_hd == fwd else "L",
                                       "road_type": hw, "name": tags.get("name", ""), "osm_way": w["id"], "oneway": oneway,
                                       "lanes": lanes, "lanes_source": lanes_src, "road_width_m": width, "curb_offset_m": round(off, 1),
                                       "centerline": list(inv(x, y))}})
                    k += 1
            acc += STEP
        acc -= L

json.dump({"type": "FeatureCollection", "features": feats}, open(D("data", "candidates_v1.geojson"), "w", encoding="utf-8"), ensure_ascii=False)
by = defaultdict(int)
for f in feats: by[f["properties"]["road_type"]] += 1
print(f"후보 {len(feats)}개 (양방향 도로는 방향별 2개). 도로 유형별: {dict(by)}")
print("저장: data/candidates_v1.geojson")
