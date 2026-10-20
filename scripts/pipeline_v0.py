"""PUDO 뼈대 v0 — 지도 데이터만으로 후보점 판정 (카카오 안내 p.18 1→4a→GeoJSON).

입력 (모두 data/ 아래, 강남역 반경 약 600m로 잘라둔 것)
  candidates_v0_centerline.geojson  후보점 (OSM 도로 중심선 15m 간격, heading = 우측 가장자리 방향)
  candidates_v0_legal.geojson       같은 후보점 + parcel_width_m(지적도 폭), hd_lanes_max(정밀도로지도 차로수)
  layers_gangnam.json               횡단보도·소화전·정류소·학교·규제차선·표지
  osm_roads_gangnam.json            OSM 도로 (교차 노드 계산용)
  ../sv_out/metadata.csv            Google 메타데이터 (사진 유무·촬영일·pano_id), 없으면 건너뜀
출력
  data/pudo_v0.geojson              후보마다 verdict / score / fail_reasons / checks
  web/pudo_v0_data.js               웹 화면이 읽는 같은 내용

실행: python scripts/pipeline_v0.py
"""
import csv
import json
import math
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = lambda *p: os.path.join(ROOT, *p)

CL, CA = 127.0276, 37.4979
CX, CY = math.cos(math.radians(CA)) * 111320, 110540
def m(lon, lat): return ((lon - CL) * CX, (lat - CA) * CY)

# ---------- 임계값 (부록 A). 바꾸면 여기만.
TH = {
    "H1 교차로": 15,     # 임시: 교차로 중심 노드 기준. 면 폴리곤 확보 후 "연석 모서리 5m"로 교체
    "H2 횡단보도": 10,
    "H3 정류장": 10,
    "H4 소화전": 5,
    "H5 규제선": 5,      # 주정차금지선·정차금지지대·버스구획·버스전용에서 5m 이내면 그 구간으로 본다
    "H6 학교": 300,      # 임시: 학교 좌표 반경. 경찰청 실제 경계로 교체
    "P1 통과폭": 5.5,    # 폭 - 차량 2.5m ≥ 차로 3.0m
    "COORD_ERR": 7,      # 출처 간 좌표 오차 (실측 중앙값 6.8m). 임계값 + 이 값 안이면 확인 필요
}
LANES_DEFAULT = {"primary": 4, "primary_link": 2, "secondary": 3, "tertiary": 2, "residential": 1, "service": 1, "living_street": 1, "unclassified": 1}
SIDEWALK_DEFAULT = {"primary": True, "primary_link": True, "secondary": True, "tertiary": True}

# ---------- 데이터
layers = json.load(open(D("data", "layers_gangnam.json"), encoding="utf-8"))
def pts(name): return [m(*f["geometry"]["coordinates"]) for f in layers[name]["features"]]
crosswalks, hydrants, buses = pts("C 횡단보도"), pts("C 소화전"), pts("C 버스정류소")
schools = [(m(*f["geometry"]["coordinates"]), f["properties"]["이름"]) for f in layers["C 학교(보호구역 300m 버퍼)"]["features"]]
lines = defaultdict(list)
for f in layers["D 규제차선 (T-GIS)"]["features"]:
    lines[f["properties"]["code"]].append([m(*c) for c in f["geometry"]["coordinates"]])
NO_STOP = ["010", "014", "015", "005", "017"]   # 주정차금지선, 정차금지지대, 버스정차구획, 버스전용(가로·중앙)
NO_PARK = ["009"]                                # 주차금지(황색점선) = 정차 가능

osm = json.load(open(D("data", "osm_roads_gangnam.json"), encoding="utf-8"))["elements"]
node_cnt = Counter(n for w in osm if w["type"] == "way" for n in w["nodes"])
node_pos = {}
for w in osm:
    if w["type"] == "way":
        for n, g in zip(w["nodes"], w["geometry"]): node_pos[n] = m(g["lon"], g["lat"])
intersections = [node_pos[n] for n, c in node_cnt.items() if c >= 2]

extra = {f["properties"]["id"]: f["properties"] for f in json.load(open(D("data", "candidates_v0_legal.geojson"), encoding="utf-8"))["features"]}
images = {}
mp = D("sv_out", "metadata.csv")
if os.path.exists(mp):
    for r in csv.DictReader(open(mp, encoding="utf-8")):
        if r["status"] == "OK": images[r["id"]] = r

# ---------- 거리 함수
def d_pts(p, arr): return min((math.hypot(p[0]-a, p[1]-b) for a, b in arr), default=None)
def d_seg(p, seg):
    best = 9e9
    for (x1, y1), (x2, y2) in zip(seg, seg[1:]):
        dx, dy = x2-x1, y2-y1; L2 = dx*dx+dy*dy
        t = 0 if L2 == 0 else max(0, min(1, ((p[0]-x1)*dx+(p[1]-y1)*dy)/L2))
        best = min(best, math.hypot(p[0]-(x1+t*dx), p[1]-(y1+t*dy)))
    return best
def d_lines(p, codes): return min((d_seg(p, s) for c in codes for s in lines[c]), default=None)

# ---------- 판정
def judge(f):
    pr = f["properties"]; p = m(*f["geometry"]["coordinates"]); ex = extra.get(pr["id"], {})
    checks, fails, needs = [], [], []
    def dist_rule(item, d, thr):
        if d is None:
            checks.append(dict(item=item, value=None, threshold=f"{thr}m 초과", **{"pass": None}, estimated=False)); needs.append(f"{item} 데이터 없음"); return
        ok = d > thr; near = (not ok) or (d <= thr + TH["COORD_ERR"])
        checks.append(dict(item=item, value=f"{d:.1f}m", threshold=f"{thr}m 초과", **{"pass": ok}, estimated=False))
        if not ok: fails.append(f"{item} {d:.1f}m (기준 {thr}m)")
        elif near: needs.append(f"{item} {d:.1f}m, 기준 {thr}m에 근접 (좌표 오차 ±{TH['COORD_ERR']}m)")
    # ① 합법 — 거리
    d_int = d_pts(p, intersections); dist_rule("H1 교차로", d_int, TH["H1 교차로"]); checks[-1]["estimated"] = True
    dist_rule("H2 횡단보도", d_pts(p, crosswalks), TH["H2 횡단보도"])
    dist_rule("H3 정류장", d_pts(p, buses), TH["H3 정류장"])
    dist_rule("H4 소화전", d_pts(p, hydrants), TH["H4 소화전"])
    # ① 합법 — 어린이보호구역 (임시 버퍼)
    sch = min(((math.hypot(p[0]-s[0], p[1]-s[1]), nm) for s, nm in schools), default=(None, None))
    in_zone = sch[0] is not None and sch[0] <= TH["H6 학교"]
    checks.append(dict(item="H6 어린이보호구역", value=f"{sch[1]} {sch[0]:.0f}m" if sch[0] is not None else None, threshold=f"학교 {TH['H6 학교']}m 밖 (임시)", **{"pass": not in_zone}, estimated=True))
    if in_zone: fails.append(f"H6 어린이보호구역 ({sch[1]} {sch[0]:.0f}m, 임시 버퍼)")
    # ① 합법 — 규제선 (2021 T-GIS)
    d_ns = d_lines(p, NO_STOP); d_np = d_lines(p, NO_PARK)
    if d_ns is not None and d_ns <= TH["H5 규제선"]:
        curb = "yellow_solid"; checks.append(dict(item="H5 연석선", value="황색실선·정차금지 구간 (2021)", threshold="실선·복선·금지지대 아님", **{"pass": False}, estimated=False)); fails.append("H5 주정차금지선 구간 (2021 지도)")
    elif d_np is not None and d_np <= TH["H5 규제선"]:
        curb = "yellow_dashed"; checks.append(dict(item="H5 연석선", value="황색점선 (주차금지, 정차 가능)", threshold="실선·복선·금지지대 아님", **{"pass": True}, estimated=False))
    else:
        curb = "unknown"; checks.append(dict(item="H5 연석선", value="지도에 규제선 없음", threshold="실선·복선·금지지대 아님", **{"pass": True}, estimated=True))
    # ② 물리 — 잔여 차로
    lanes = ex.get("hd_lanes_max") or (int(pr["lanes"]) if str(pr.get("lanes") or "").isdigit() else None)
    lanes_est = ex.get("hd_lanes_max") is None
    if lanes is None: lanes = LANES_DEFAULT.get(pr["road_type"], 1)
    width = ex.get("parcel_width_m")
    oneway = str(pr.get("oneway", "no")).lower() in ("yes", "true", "1", "-1")
    if oneway or lanes >= 2:
        checks.append(dict(item="P1 잔여 차로", value=f"{'일방통행' if oneway else f'편도 {lanes}차로'}", threshold="편도 2차로 이상 또는 일방통행", **{"pass": True}, estimated=lanes_est and not oneway))
        if lanes_est and not oneway: needs.append("P1 차로 수가 추정값")
    elif width is not None:
        ok = width >= TH["P1 통과폭"]
        checks.append(dict(item="P1 통과 폭", value=f"도로 땅 폭 {width:.1f}m (보도 포함)", threshold=f"{TH['P1 통과폭']}m 이상", **{"pass": ok}, estimated=True))
        if not ok: fails.append(f"P1 통과 폭 부족 ({width:.1f}m, 편도 1차로 양방향)")
        else: needs.append("P1 폭이 보도 포함 지적도 값 (추정)")
    else:
        checks.append(dict(item="P1 통과 폭", value=None, threshold=f"{TH['P1 통과폭']}m 이상", **{"pass": None}, estimated=True)); needs.append("P1 도로 폭 없음 (단지 내 통로일 수 있음)")
    # ③ 승객 접근 — 보도
    sw = SIDEWALK_DEFAULT.get(pr["road_type"])
    checks.append(dict(item="A1 보도", value={True: "있음 (도로 유형 추정)", None: "모름"}[sw], threshold="보도 있음", **{"pass": sw}, estimated=True))
    if sw is None: needs.append("A1 보도 유무 모름 (사진 필요)")
    # 사진
    img = images.get(pr["id"])
    # 판정
    if fails: verdict, score = "불가", None
    elif needs: verdict, score = "확인 필요", None
    else:
        verdict = "가능"
        interf = {"primary": 0.0, "primary_link": 0.0, "secondary": 0.3, "tertiary": 0.5}.get(pr["road_type"], 1.0)
        score = round((1.0 + 1.0 + interf) / 3, 2)   # 잔여차로 1, 보도 1, 간섭(이면도로 1 / 대로 0)
    return {
        "type": "Feature", "geometry": f["geometry"],
        "properties": {
            "id": pr["id"], "name": pr.get("name", ""), "road_type": pr["road_type"], "oneway": oneway, "heading": pr["heading"],
            "verdict": verdict, "score": score, "needs_check": bool(needs), "fail_reasons": fails, "check_reasons": needs, "checks": checks,
            "map": {"dist_intersection_m": round(d_int, 1) if d_int is not None else None, "lanes": lanes, "lanes_estimated": lanes_est, "road_width_m": width, "curb_line": curb, "school_zone": in_zone},
            "image_id": img["pano_id"] if img else None, "image_source": "google" if img else None, "captured_at": img["date"] if img else None,
            "review": None, "note": None, "reviewed_at": None,
        },
    }

cands = json.load(open(D("data", "candidates_v0_centerline.geojson"), encoding="utf-8"))["features"]
out = [judge(f) for f in cands]
json.dump({"type": "FeatureCollection", "features": out}, open(D("data", "pudo_v0.geojson"), "w", encoding="utf-8"), ensure_ascii=False)
os.makedirs(D("web"), exist_ok=True)
with open(D("web", "pudo_v0_data.js"), "w", encoding="utf-8") as fp:
    fp.write("const PUDO=" + json.dumps({"type": "FeatureCollection", "features": out}, ensure_ascii=False) + ";\n")
    fp.write("const PUDO_META=" + json.dumps({"center": [CA, CL], "radius_m": 500, "thresholds": TH, "generated": "v0"}, ensure_ascii=False) + ";\n")

# ---------- 요약
n = len(out); v = Counter(f["properties"]["verdict"] for f in out)
print(f"후보 {n}개 → 가능 {v['가능']} ({100*v['가능']/n:.0f}%) / 확인 필요 {v['확인 필요']} ({100*v['확인 필요']/n:.0f}%) / 불가 {v['불가']} ({100*v['불가']/n:.0f}%)")
fr = Counter(r.split(" ")[0] + " " + r.split(" ")[1] for f in out for r in f["properties"]["fail_reasons"])
print("불가 사유 (중복 포함):", fr.most_common())
nr = Counter(" ".join(r.split(" ")[:2]) for f in out for r in f["properties"]["check_reasons"])
print("확인 필요 사유:", nr.most_common())
by = defaultdict(Counter)
for f in out: by[f["properties"]["road_type"]][f["properties"]["verdict"]] += 1
for hw, c in by.items(): print(f"  {hw:14s} 가능 {c['가능']:4d}  확인 {c['확인 필요']:4d}  불가 {c['불가']:4d}")
# 한 파일짜리 공유용 HTML
try:
    h = open(D("web", "index.html"), encoding="utf-8").read()
    js = open(D("web", "pudo_v0_data.js"), encoding="utf-8").read()
    open(D("web", "pudo_v0_standalone.html"), "w", encoding="utf-8").write(h.replace('<script src="pudo_v0_data.js"></script>', "<script>" + chr(10) + js + chr(10) + "</script>", 1))
    print("저장: data/pudo_v0.geojson, web/pudo_v0_data.js, web/pudo_v0_standalone.html (공유용 한 파일)")
except FileNotFoundError:
    print("저장: data/pudo_v0.geojson, web/pudo_v0_data.js")
