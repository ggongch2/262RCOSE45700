"""판정 단계: 측정값(measure.py)에 기준값(TH)과 규칙을 적용합니다.

지리 계산과 파일 읽기는 하지 않습니다. 같은 측정값에 기준값만 바꿔 여러 번 판정할 수 있습니다.
  - 불가 사유가 하나라도 있으면 불가, 확인 사유만 있으면 확인 필요, 둘 다 없으면 가능
  - 측정값이 모름(None)이면 확인 필요로 둡니다
  - rules_off에 넣은 규칙(H1, H2 … 사유 앞 코드)은 사유에서 빼고 판정합니다 (조건 실험용)
"""

from .config import TH
from .vision import apply_observations, validate_confidence_threshold

RULE_NAMES = {
    "H1": "교차로", "H2": "횡단보도", "H3": "정류장", "H4": "소화전", "H5": "규제선·전용차로",
    "H6": "어린이보호구역", "H7": "안전지대", "H8": "택시승강장", "P1": "통과 폭·차로", "A1": "보도", "A2": "연석 막힘(사진)",
    "V0": "사진 대응", "V1": "사진 규제선", "V2": "공사(사진)", "V3": "일시적 점유(사진)",
}


def _within(distance, limit):
    """거리가 있고 기준 이내인지."""
    return distance is not None and distance <= limit


def rule_code(text):
    """사유·항목 문자열 앞의 규칙 코드 (예: 'H2 횡단보도 8.1m' → 'H2')."""
    return str(text).split(" ")[0]


def _checks(m, th):
    """측정값 → (checks, 불가 사유, 확인 사유, 연석 선 상태)."""
    checks, fails, needs = [], [], []

    def dist_rule(item, d, thr):
        if d is None:
            checks.append(dict(item=item, value=None, threshold=f"{thr}m 초과", **{"pass": None}, estimated=False)); needs.append(f"{item} 데이터 없음"); return
        ok = d > thr; near = (not ok) or (d <= thr + th["COORD_ERR"])
        checks.append(dict(item=item, value=f"{d:.1f}m", threshold=f"{thr}m 초과", **{"pass": ok}, estimated=False))
        if not ok: fails.append(f"{item} {d:.1f}m (기준 {thr}m)")
        elif near: needs.append(f"{item} {d:.1f}m, 기준 {thr}m에 근접 (좌표 오차 ±{th['COORD_ERR']}m)")

    # ① 합법 — 거리
    # 도로교통법 제32조: 교차로 안(1호), 교차로 가장자리·도로 모퉁이 5m 이내(2호) 정차 금지.
    # 교차로 면은 차도 폭 추정으로 만들므로 추정값이며, 기준 직후 구간은 확인 필요로 둡니다.
    intersection = m["intersection"]
    if intersection["inside"]:
        checks.append(dict(item="H1 교차로", value="교차로 안", threshold=f"가장자리에서 {th['H1 교차로']}m 초과", **{"pass": False}, estimated=True))
        fails.append("H1 교차로 안 (도로교통법 제32조 1호)")
    else:
        dist_rule("H1 교차로", intersection["distance"], th["H1 교차로"]); checks[-1]["estimated"] = True
        checks[-1]["threshold"] = f"가장자리에서 {th['H1 교차로']}m 초과"
    dist_rule("H2 횡단보도", m["crosswalk"], th["H2 횡단보도"])
    dist_rule("H3 정류장", m["bus_stop"], th["H3 정류장"])
    dist_rule("H4 소화전", m["hydrant"], th["H4 소화전"])

    # ① 합법 — 어린이보호구역 (도로교통법 제32조 8호, 2021.10.21부터 주정차 금지). 구간 안은 항상 불가.
    zone = m["child_zone"]
    if zone["mode"] == "tgis":
        state, label = zone["state"], zone["label"]
        value = {"in": f"구간 안 ({label})", "near": f"{label}, 표지 근거 없음", "out": "구간 밖"}[state]
        checks.append(dict(item="H6 어린이보호구역", value=value, threshold="보호구역 구간 밖 (T-GIS 표지·노면표시로 추정)",
                           **{"pass": {"in": False, "near": None, "out": True}[state]}, estimated=True))
        if state == "in": fails.append(f"H6 어린이보호구역 (추정 구간 안, {label})")
        elif state == "near": needs.append(f"H6 어린이보호구역 지정 시설 ({label} 주변, 보호구역 범위 미확인 → 표지·사진 확인)")
    else:
        inside = _within(zone["distance"], th["H6 학교"])
        checks.append(dict(item="H6 어린이보호구역", value=zone["label"], threshold=f"대상시설 {th['H6 학교']}m 밖 (지정 구간 미확인)", **{"pass": not inside}, estimated=True))
        if inside: fails.append(f"H6 어린이보호구역 ({zone['label']}, 임시 버퍼)")

    # ① 합법 — 안전지대 사방 10m (정밀도로지도 531). 지도가 없는 도로는 판정하지 않음
    safety = m["safety_zone"]
    if safety["covered"]:
        d_sz = safety["distance"]
        if d_sz is not None and d_sz <= th["H7 안전지대"]:
            checks.append(dict(item="H7 안전지대", value=f"{d_sz:.1f}m", threshold=f"{th['H7 안전지대']}m 초과", **{"pass": False}, estimated=False)); fails.append(f"H7 안전지대 {d_sz:.1f}m (기준 {th['H7 안전지대']}m)")
        else:
            checks.append(dict(item="H7 안전지대", value=f"{d_sz:.1f}m" if d_sz is not None and d_sz < 9e8 else "없음", threshold=f"{th['H7 안전지대']}m 초과", **{"pass": True}, estimated=False))

    # ① 운영 — 택시승강장. 승차대 목록에는 시설물 점만 있어 정차 구간 길이를 모릅니다.
    # 택시 1대 길이 안은 불가, 2대 길이 안은 구간에 걸칠 수 있어 확인 필요로 둡니다.
    taxi, ban, review = m["taxi_stand"], th["H8 택시승강장"], th["H8 택시승강장 확인"]
    if taxi is None:
        checks.append(dict(item="H8 택시승강장", value=None, threshold=f"승차대에서 {ban}m 초과", **{"pass": None}, estimated=True))
        needs.append("H8 택시승차대 자료 없음 (data/taxi_stands.xlsx)")
    else:
        d_taxi = taxi["distance"]
        value = f"{d_taxi:.1f}m, {taxi['label']}" if d_taxi is not None else "같은 쪽 60m 안에 없음"
        status = False if _within(d_taxi, ban) else None if _within(d_taxi, review) else True
        checks.append(dict(item="H8 택시승강장", value=value, threshold=f"승차대에서 도로를 따라 {ban}m 초과", **{"pass": status}, estimated=True))
        if status is False: fails.append(f"H8 택시승강장 {d_taxi:.1f}m (기준 택시 1대 {ban}m, {taxi['label']})")
        elif status is None: needs.append(f"H8 택시승차대 {d_taxi:.1f}m, 정차 구간 길이 모름 (택시 2대 {review}m 안) → 사진 확인")

    # ① 합법 — 규제선. 서울시 2021 자료와 정밀도로지도 2023~25를 교차 확인
    line = m["reg_line"]
    tgis_ban = _within(line["tgis_no_stop"], th["H5 규제선"])
    hd_cov = line["hd_covered"]
    hd_th = th["H5 정밀도로지도 규제선"]
    hd_ban = hd_cov and _within(line["hd_no_stop"], hd_th)
    if tgis_ban and hd_ban:
        curb = "yellow_solid"; checks.append(dict(item="H5 규제선", value="주정차금지 구간 (2021 서울시 + 2023 정밀도로지도 일치)", threshold="금지 구간 아님", **{"pass": False}, estimated=False)); fails.append("H5 주정차금지 구간 (두 자료 일치)")
    elif tgis_ban and hd_cov:
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="2021 서울시: 금지 / 2023 정밀도로지도: 없음", threshold="금지 구간 아님", **{"pass": None}, estimated=True)); needs.append("H5 규제선 자료 불일치 (서울시 2021 금지, 정밀도로지도 없음) → 사진 확인")
    elif tgis_ban and line.get("tgis_no_stop_2023") is True:
        curb = "yellow_solid"; checks.append(dict(item="H5 규제선", value="주정차금지 구간 (2021 서울시 선, 2023 서울시 목록에도 있음)", threshold="금지 구간 아님", **{"pass": False}, estimated=False)); fails.append("H5 주정차금지 구간 (2021 선 + 2023 목록)")
    elif tgis_ban and line.get("tgis_no_stop_2023") is False:
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="2021 서울시: 금지 / 2023 서울시 목록: 없음", threshold="금지 구간 아님", **{"pass": None}, estimated=True)); needs.append("H5 2021 주정차금지선이 2023 목록에 없음 → 사진 확인")
    elif tgis_ban:
        curb = "yellow_solid"; checks.append(dict(item="H5 규제선", value="주정차금지 구간 (2021 서울시 자료, 교차 확인 불가)", threshold="금지 구간 아님", **{"pass": False}, estimated=True)); fails.append("H5 주정차금지 구간 (2021 서울시 자료, 단일 출처)")
    elif hd_ban:
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="2021 서울시: 없음 / 2023 정밀도로지도: 규제선 있음", threshold="금지 구간 아님", **{"pass": None}, estimated=True)); needs.append("H5 규제선 자료 불일치 (서울시 2021 없음, 정밀도로지도 있음) → 사진 확인")
    elif hd_cov and _within(line["hd_bus"], hd_th):
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="버스전용차로 (2023 정밀도로지도). 시간제면 심야 가능", threshold="금지 구간 아님", **{"pass": None}, estimated=True)); needs.append("H5 버스전용차로, 운영 시간 확인 (표지)")
    elif _within(line["tgis_no_park"], th["H5 규제선"]) or (hd_cov and _within(line["hd_no_park"], hd_th)):
        curb = "yellow_dashed"; checks.append(dict(item="H5 규제선", value="황색점선 (주차금지, 정차 가능)", threshold="금지 구간 아님", **{"pass": True}, estimated=False))
    else:
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="두 자료 모두 규제선 없음" if hd_cov else "2021 자료에 규제선 없음 (정밀도로지도 미커버)", threshold="금지 구간 아님", **{"pass": True}, estimated=not hd_cov))

    # ② 물리 — 잔여 차로 / 통과 폭
    lanes, oneway = m["lanes"]["value"], m["road"]["oneway"]
    width, planned = m["width"]["value"], m["planned_road"]
    if oneway or (lanes is not None and lanes >= 2):
        checks.append(dict(item="P1 잔여 차로", value="일방통행" if oneway else f"편도 {lanes}차로 ({m['lanes']['source']})", threshold="편도 2차로 이상 또는 일방통행", **{"pass": True}, estimated=False))
    else:
        if lanes is None:
            needs.append("P1 차로 수 모름 (정밀도로지도·OSM에 없음)")
        # 폭 근거: 지적도 도로 필지 폭(보도 포함 땅 폭)과 도시계획시설 도로 등급(법정 최소 폭).
        need = th["P1 통과폭"]; threshold = f"{need}m 이상"
        planned_ok = planned is not None and planned["min_width"] >= need
        planned_text = f"도시계획 {planned['grade']} ({planned['min_width']}m 이상)" if planned else None
        if width is not None and width >= need:
            value = f"도로 땅 폭 {width:.1f}m (보도 포함)" + (f", {planned_text}" if planned_text else "")
            checks.append(dict(item="P1 통과 폭", value=value, threshold=threshold, **{"pass": True}, estimated=True))
            needs.append("P1 폭이 보도 포함 지적도 값 (추정)")
        elif width is not None and planned_ok:
            checks.append(dict(item="P1 통과 폭", value=f"지적도 {width:.1f}m / {planned_text}", threshold=threshold, **{"pass": None}, estimated=True))
            needs.append(f"P1 폭 자료 불일치 (지적도 {width:.1f}m, {planned_text}) → 사진 확인")
        elif width is not None:
            checks.append(dict(item="P1 통과 폭", value=f"도로 땅 폭 {width:.1f}m (보도 포함)" + (f", {planned_text}" if planned_text else ""), threshold=threshold, **{"pass": False}, estimated=True))
            fails.append(f"P1 통과 폭 부족 ({width:.1f}m, 편도 1차로 양방향)")
        elif planned_ok:
            checks.append(dict(item="P1 통과 폭", value=f"{planned_text}, 보도 포함", threshold=threshold, **{"pass": True}, estimated=True))
            needs.append(f"P1 폭이 도시계획 등급 하한 ({planned['grade']}, 지적도 폭 없음)")
        else:
            checks.append(dict(item="P1 통과 폭", value=planned_text, threshold=threshold, **{"pass": None}, estimated=True)); needs.append("P1 도로 폭 없음 (단지 내 통로일 수 있음)")

    # ③ 승객 접근 — 보도. 자료가 없으면 모름 (사진 판독 결과가 있으면 아래에서 교체)
    sidewalk = m["sidewalk"]["value"]
    checks.append(dict(item="A1 보도", value={True: "있음", False: "없음", None: "모름"}[sidewalk], threshold="보도 있음", **{"pass": sidewalk}, estimated=False))
    if sidewalk is False: fails.append("A1 보도 없음")
    elif sidewalk is None: needs.append("A1 보도 유무 모름 (사진 필요)")
    return checks, fails, needs, curb


def score(road_type):
    """가능 지점의 적합도. 잔여차로 1, 보도 1, 간섭(이면도로 1 / 대로 0)의 평균."""
    interf = {"primary": 0.0, "primary_link": 0.0, "secondary": 0.3, "tertiary": 0.5}.get(road_type, 1.0)
    return round((1.0 + 1.0 + interf) / 3, 2)


def evaluate(m, *, thresholds=None, rules_off=(), photo=None, photo_min_confidence=0.8):
    """측정값 하나 → 판정 결과 Feature."""
    th = TH if thresholds is None else thresholds
    checks, fails, needs, curb = _checks(m, th)
    feature = {"type": "Feature", "geometry": {"type": "Point", "coordinates": m["coordinates"]},
               "properties": {"id": m["id"], "heading": m["heading"]}}
    photo_report = None
    if photo is not None:
        photo_report = apply_observations(photo, feature, m["image"], checks, fails, needs, min_confidence=photo_min_confidence)
    off = set(rules_off)
    fails = [reason for reason in fails if rule_code(reason) not in off]
    needs = [reason for reason in needs if rule_code(reason) not in off]
    verdict = "불가" if fails else "확인 필요" if needs else "가능"
    road = m["road"]
    properties = {
        "id": m["id"], "name": road["name"], "road_type": road["type"], "oneway": road["oneway"], "heading": m["heading"],
        "side": m["side"], "centerline": m["centerline"],
        "verdict": verdict, "score": score(road["type"]) if verdict == "가능" else None, "needs_check": bool(needs),
        "fail_reasons": fails, "check_reasons": needs, "checks": checks, "rules_off": sorted(off),
        "map": {"dist_intersection_m": m["intersection"]["distance"], "lanes": m["lanes"]["value"], "road_width_m": m["width"]["value"],
                "planned_road": m["planned_road"]["grade"] if m["planned_road"] else None, "curb_line": curb,
                "position_basis": m["position"]["basis"]},
        "image_id": m["image"]["pano_id"] if m["image"] else None, "image_source": "google" if m["image"] else None,
        "captured_at": None,   # 촬영일은 저장하지 않음 (구글 정책)
        "review": None, "note": None, "reviewed_at": None, "curb_offset_m": m["position"]["offset_m"],
    }
    if photo_report is not None:
        properties["vision"] = photo_report
    return {**feature, "properties": properties}


def evaluate_all(measurements, *, thresholds=None, rules_off=(), photo_assessments=None, photo_min_confidence=0.8):
    """측정값 목록 → 판정 FeatureCollection."""
    validate_confidence_threshold(photo_min_confidence)
    photos = photo_assessments or {}
    return {"type": "FeatureCollection", "features": [
        evaluate(m, thresholds=thresholds, rules_off=rules_off, photo=photos.get(m["id"]), photo_min_confidence=photo_min_confidence)
        for m in measurements
    ]}
