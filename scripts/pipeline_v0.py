"""PUDO 뼈대 v0 — 지도 데이터만으로 후보점 판정 (카카오 안내 p.18 1→4a→GeoJSON).

입력 (모두 data/ 아래, 강남역 반경 약 600m로 잘라둔 것)
  candidates_v1.geojson             후보점 (make_candidates.py: 방향별 우측 가장자리 차로, 차로수·도로폭 포함)
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
    "H7 안전지대": 10,   # 32조 3호. 정밀도로지도 안전지대 선(531)에서 10m
    "P1 통과폭": 5.5,    # 폭 - 차량 2.5m ≥ 차로 3.0m
    "COORD_ERR": 7,      # 출처 간 좌표 오차 (실측 중앙값 6.8m). 임계값 + 이 값 안이면 확인 필요
}
LENIENT = bool(os.environ.get("LENIENT_TEMP"))   # 1이면 임시 규칙(교차로 노드·학교 버퍼)을 불가 대신 확인 필요로
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

# 정밀도로지도(2023~25) 규제 성격 노면선: 515/516(주정차 규제), 504(버스전용). 차로링크는 커버리지 판별용
def _hd(path, keep):
    out = []
    for f in json.load(open(D("data", "hdmap", path), encoding="utf-8"))["features"]:
        if not f.get("geometry") or not keep(f["properties"]): continue
        g = f["geometry"]; coords = g["coordinates"] if g["type"] == "LineString" else [c for part in g["coordinates"] for c in part]
        out.append([m(*c[:2]) for c in coords])
    return out
try:
    # 사진 대조 결과: kind 515 + type 111 = 황색 실선(정차·주차 금지), 515 + 112 = 황색 점선(주차만 금지, 정차 가능), 121 = 황색 복선
    HD_REG = _hd("lt_l_b2surfacelinemark.geojson", lambda p: (p.get("kind") == "515" and p.get("type") in ("111", "121")) or p.get("kind") == "516")
    HD_PARK = _hd("lt_l_b2surfacelinemark.geojson", lambda p: p.get("kind") == "515" and p.get("type") == "112")
    HD_BUS = _hd("lt_l_b2surfacelinemark.geojson", lambda p: p.get("kind") == "504")
    HD_SAFE = _hd("lt_l_b2surfacelinemark.geojson", lambda p: p.get("kind") == "531")   # 안전지대 (32조 3호: 사방 10m 금지)
    HD_LINK = _hd("lt_l_a2link.geojson", lambda p: True)
except FileNotFoundError:
    HD_REG, HD_PARK, HD_BUS, HD_SAFE, HD_LINK = [], [], [], [], []
def d_segs(p, segs): return min((d_seg(p, s) for s in segs), default=None)
osm = json.load(open(D("data", "osm_roads_gangnam.json"), encoding="utf-8"))["elements"]
node_cnt = Counter(n for w in osm if w["type"] == "way" for n in w["nodes"])
node_pos = {}
for w in osm:
    if w["type"] == "way":
        for n, g in zip(w["nodes"], w["geometry"]): node_pos[n] = m(g["lon"], g["lat"])
intersections = [node_pos[n] for n, c in node_cnt.items() if c >= 2]

images = {}
mp = D("sv_out", "metadata.csv")
if os.path.exists(mp):
    for r in csv.DictReader(open(mp, encoding="utf-8")):
        if r["status"] == "OK" and "Google" in r.get("copyright", "Google"): images[r["id"]] = r   # 사용자 업로드 사진 제외

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
    pr = f["properties"]; p = m(*f["geometry"]["coordinates"])
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
    if LENIENT and fails and fails[-1].startswith("H1 교차로"): needs.append(fails.pop() + " [임시 규칙 → 확인 필요]")
    dist_rule("H2 횡단보도", d_pts(p, crosswalks), TH["H2 횡단보도"])
    dist_rule("H3 정류장", d_pts(p, buses), TH["H3 정류장"])
    dist_rule("H4 소화전", d_pts(p, hydrants), TH["H4 소화전"])
    # ① 합법 — 어린이보호구역 (임시 버퍼)
    sch = min(((math.hypot(p[0]-s[0], p[1]-s[1]), nm) for s, nm in schools), default=(None, None))
    in_zone = sch[0] is not None and sch[0] <= TH["H6 학교"]
    checks.append(dict(item="H6 어린이보호구역", value=f"{sch[1]} {sch[0]:.0f}m" if sch[0] is not None else None, threshold=f"학교 {TH['H6 학교']}m 밖 (임시)", **{"pass": not in_zone}, estimated=True))
    if in_zone: (needs if LENIENT else fails).append(f"H6 어린이보호구역 ({sch[1]} {sch[0]:.0f}m, 임시 버퍼{' → 확인 필요' if LENIENT else ''})")
    # ① 합법 — 안전지대 사방 10m (정밀도로지도 531). 지도가 없는 도로는 판정 불가(null)
    if HD_SAFE and (d_segs(p, HD_LINK) or 9e9) <= 15:
        d_sz = d_segs(p, HD_SAFE)
        if d_sz is not None and d_sz <= TH["H7 안전지대"]:
            checks.append(dict(item="H7 안전지대", value=f"{d_sz:.1f}m", threshold=f"{TH['H7 안전지대']}m 초과", **{"pass": False}, estimated=False)); fails.append(f"H7 안전지대 {d_sz:.1f}m (기준 {TH['H7 안전지대']}m)")
        else:
            checks.append(dict(item="H7 안전지대", value=f"{d_sz:.1f}m" if d_sz is not None and d_sz < 9e8 else "없음", threshold=f"{TH['H7 안전지대']}m 초과", **{"pass": True}, estimated=False))
    # ① 합법 — 규제선. 서울시 2021 자료와 정밀도로지도 2023~25를 교차 확인
    d_ns = d_lines(p, NO_STOP); d_np = d_lines(p, NO_PARK)
    tgis_ban = d_ns is not None and d_ns <= TH["H5 규제선"]
    hd_cov = (d_segs(p, HD_LINK) or 9e9) <= 15          # 정밀도로지도가 이 도로를 덮는가
    hd_ban = hd_cov and (d_segs(p, HD_REG) or 9e9) <= 6
    if tgis_ban and hd_ban:
        curb = "yellow_solid"; checks.append(dict(item="H5 규제선", value="주정차금지 구간 (2021 서울시 + 2023 정밀도로지도 일치)", threshold="금지 구간 아님", **{"pass": False}, estimated=False)); fails.append("H5 주정차금지 구간 (두 자료 일치)")
    elif tgis_ban and hd_cov:
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="2021 서울시: 금지 / 2023 정밀도로지도: 없음", threshold="금지 구간 아님", **{"pass": None}, estimated=True)); needs.append("H5 규제선 자료 불일치 (2021 금지, 2023 없음) → 사진 확인")
    elif tgis_ban:
        curb = "yellow_solid"; checks.append(dict(item="H5 규제선", value="주정차금지 구간 (2021 서울시 자료, 교차 확인 불가)", threshold="금지 구간 아님", **{"pass": False}, estimated=True)); fails.append("H5 주정차금지 구간 (2021 서울시 자료, 단일 출처)")
    elif hd_ban:
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="2021 서울시: 없음 / 2023 정밀도로지도: 규제선 있음", threshold="금지 구간 아님", **{"pass": None}, estimated=True)); needs.append("H5 규제선 자료 불일치 (2021 없음, 2023 있음) → 사진 확인")
    elif hd_cov and (d_segs(p, HD_BUS) or 9e9) <= 6:
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="버스전용차로 (2023 정밀도로지도). 시간제면 심야 가능", threshold="금지 구간 아님", **{"pass": None}, estimated=True)); needs.append("H5 버스전용차로, 운영 시간 확인 (표지)")
    elif (d_np is not None and d_np <= TH["H5 규제선"]) or (hd_cov and (d_segs(p, HD_PARK) or 9e9) <= 6):
        curb = "yellow_dashed"; checks.append(dict(item="H5 규제선", value="황색점선 (주차금지, 정차 가능)", threshold="금지 구간 아님", **{"pass": True}, estimated=False))
    else:
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="두 자료 모두 규제선 없음" if hd_cov else "2021 자료에 규제선 없음 (정밀도로지도 미커버)", threshold="금지 구간 아님", **{"pass": True}, estimated=not hd_cov))
    # ② 물리 — 잔여 차로
    lanes = pr["lanes"]; lanes_est = pr.get("lanes_source") == "default"
    width = pr.get("road_width_m")
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
            "id": pr["id"], "name": pr.get("name", ""), "road_type": pr["road_type"], "oneway": oneway, "heading": pr["heading"], "side": pr.get("side"), "centerline": pr.get("centerline"),
            "verdict": verdict, "score": score, "needs_check": bool(needs), "fail_reasons": fails, "check_reasons": needs, "checks": checks,
            "map": {"dist_intersection_m": round(d_int, 1) if d_int is not None else None, "lanes": lanes, "lanes_estimated": lanes_est, "road_width_m": width, "curb_line": curb, "school_zone": in_zone},
            "image_id": img["pano_id"] if img else None, "image_source": "google" if img else None, "captured_at": None,   # 촬영일은 저장하지 않음 (구글 정책)
            "review": None, "note": None, "reviewed_at": None, "curb_offset_m": pr.get("curb_offset_m"),
        },
    }

cands = json.load(open(D("data", "candidates_v1.geojson"), encoding="utf-8"))["features"]
out = []
for f in cands:
    LENIENT = False; s = judge(f)
    LENIENT = True;  l = judge(f)
    s["properties"]["verdict_lenient"] = l["properties"]["verdict"]
    s["properties"]["fail_reasons_lenient"] = l["properties"]["fail_reasons"]
    s["properties"]["check_reasons_lenient"] = l["properties"]["check_reasons"]
    out.append(s)
LENIENT = bool(os.environ.get("LENIENT_TEMP"))
json.dump({"type": "FeatureCollection", "features": out}, open(D("data", "pudo_v0.geojson"), "w", encoding="utf-8"), ensure_ascii=False)
os.makedirs(D("web"), exist_ok=True)
with open(D("web", "pudo_v0_data.js"), "w", encoding="utf-8") as fp:
    fp.write("const PUDO=" + json.dumps({"type": "FeatureCollection", "features": out}, ensure_ascii=False) + ";\n")
    fp.write("const PUDO_META=" + json.dumps({"center": [CA, CL], "radius_m": 500, "thresholds": TH, "generated": "v0"}, ensure_ascii=False) + ";\n")

# ---------- 요약
n = len(out); v = Counter(f["properties"]["verdict"] for f in out)
print(f"후보 {n}개 → 가능 {v['가능']} ({100*v['가능']/n:.0f}%) / 확인 필요 {v['확인 필요']} ({100*v['확인 필요']/n:.0f}%) / 불가 {v['불가']} ({100*v['불가']/n:.0f}%)")
vl = Counter(f["properties"]["verdict_lenient"] for f in out)
print(f"  완화(임시 규칙→확인 필요): 가능 {vl['가능']} / 확인 필요 {vl['확인 필요']} / 불가 {vl['불가']}")
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
