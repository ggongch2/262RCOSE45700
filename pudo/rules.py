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
    "H6": "어린이보호구역", "H7": "안전지대", "H8": "택시승강장", "P1": "통과 폭·차로", "A1": "보도", "A2": "차도 끝 접근 막힘(사진)",
    "V0": "사진 대응", "V1": "사진 규제선", "V2": "공사(사진)", "V3": "일시적 점유(사진)",
}


def _within(distance, limit):
    """거리가 있고 기준 이내인지."""
    return distance is not None and distance <= limit


def rule_code(text):
    """사유·항목 문자열 앞의 규칙 코드 (예: 'H2 횡단보도 8.1m' → 'H2')."""
    return str(text).split(" ")[0]


def _checks(m, th):
    """측정값 → (checks, 불가 사유, 확인 사유, 차도 끝 선 상태)."""
    checks, fails, needs = [], [], []

    def dist_rule(item, d, thr):
        if d is None:
            checks.append(dict(item=item, value=None, threshold=f"{thr}m 초과", **{"pass": None}, estimated=False)); needs.append(f"{item} 데이터 없음"); return
        ok = d > thr; near = (not ok) or (d <= thr + th["H1~H4 오차 범위"])
        checks.append(dict(item=item, value=f"{d:.1f}m", threshold=f"{thr}m 초과", **{"pass": ok}, estimated=False))
        if not ok: fails.append(f"{item} {d:.1f}m (기준 {thr}m)")
        elif near: needs.append(f"{item} {d:.1f}m, 기준 {thr}m에 근접 (오차 범위 {th['H1~H4 오차 범위']}m)")

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
    police = zone.get("police")
    if police is not None and police["distance"] is not None:
        # 경찰청 지정 경계가 있으면 그것으로 판정합니다. 후보 위치는 추정이라 경계 바로 밖은 확인 필요.
        distance, label, near_th = police["distance"], police["label"], th["H6 경계 확인"]
        tgis_in = zone["mode"] == "tgis" and zone["state"] == "in"
        if distance == 0:
            value, ok = f"지정 구역 안 ({label})", False
            fails.append(f"H6 어린이보호구역 (경찰청 지정 구역 안, {label})")
        elif distance <= near_th:
            value, ok = f"지정 경계 {distance:.1f}m ({label})", None
            needs.append(f"H6 어린이보호구역 경계 {distance:.0f}m ({label}) → 표지·사진 확인")
        elif tgis_in:
            value, ok = f"지정 구역 밖 {distance:.0f}m, 표지로는 구간 안 ({zone['label']})", None
            needs.append("H6 표지로는 보호구역 안, 경찰청 경계 밖 → 표지·사진 확인")
        else:
            value, ok = f"지정 구역 밖 {distance:.0f}m", True
        checks.append(dict(item="H6 어린이보호구역", value=value, threshold=f"경찰청 지정 구역 밖 (경계 {near_th}m 안은 확인)",
                           **{"pass": ok}, estimated=ok is None))
    elif zone["mode"] == "tgis":
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

    # ① 합법 — 안전지대 사방 10m (제32조 3호). 정밀도로지도 노면선 531 + 1:1000 안전지대 면(교통섬·도색).
    # 두 자료 모두 덮지 않는 곳은 판정하지 않음
    safety = m["safety_zone"]
    if safety["covered"]:
        d_sz, near = safety["distance"], th["H7 안전지대"]
        if _within(d_sz, near):
            checks.append(dict(item="H7 안전지대", value=f"{d_sz:.1f}m ({safety.get('source')})", threshold=f"안전지대(빗금·교통섬)에서 {near}m 초과", **{"pass": False}, estimated=False))
            fails.append(f"H7 안전지대 {d_sz:.1f}m (기준 {near}m, {safety.get('source')})")
        else:
            checks.append(dict(item="H7 안전지대", value=f"{d_sz:.1f}m" if d_sz is not None else "없음", threshold=f"안전지대(빗금·교통섬)에서 {near}m 초과", **{"pass": True}, estimated=False))

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

    # ① 합법 — 규제선. 그 지점을 덮는 가장 최신 자료 하나로 판단합니다.
    #   1) 정밀도로지도(2023 무렵)가 덮으면 정밀도로지도만
    #   2) 아니면 2021 서울시 금지선이 2023 서울시 차선 목록에도 있는지
    #   3) 2023 목록도 없으면 2021 서울시 금지선
    # 2021 자료와 결과가 다르면 항목 값에만 적어 둡니다 (로드뷰 확인 때 참고).
    line = m["reg_line"]
    near, hd_th = th["H5 규제선"], th["H5 정밀도로지도 규제선"]
    tgis_ban = _within(line["tgis_no_stop"], near)
    listed_2023 = line.get("tgis_no_stop_2023")

    def h5(value, ok, estimated, state):
        checks.append(dict(item="H5 규제선", value=value, threshold="금지 구간 아님", **{"pass": ok}, estimated=estimated))
        return state

    if line["hd_covered"]:
        differs_2021 = lambda banned: "" if banned == tgis_ban else (" / 2021 서울시 자료엔 금지선 있음" if tgis_ban else " / 2021 서울시 자료엔 없음")
        if _within(line["hd_no_stop"], hd_th):
            curb = h5("주정차금지선 (정밀도로지도)" + differs_2021(True), False, False, "yellow_solid")
            fails.append("H5 주정차금지 구간 (정밀도로지도)")
        elif _within(line["hd_bus"], hd_th):
            curb = h5("버스전용차로 (정밀도로지도). 시간제면 심야 가능" + differs_2021(False), None, True, "unknown")
            needs.append("H5 버스전용차로, 운영 시간 확인 (표지)")
        elif _within(line["hd_no_park"], hd_th):
            curb = h5("황색점선 (주차금지, 정차 가능, 정밀도로지도)" + differs_2021(False), True, False, "yellow_dashed")
        else:
            curb = h5("규제선 없음 (정밀도로지도)" + differs_2021(False), True, False, "unknown")
    elif tgis_ban and listed_2023 is True:
        curb = h5("주정차금지선 (2021 서울시 선, 2023 서울시 목록에도 있음)", False, False, "yellow_solid")
        fails.append("H5 주정차금지 구간 (2021 선 + 2023 목록)")
    elif tgis_ban and listed_2023 is False:
        curb = h5("규제선 없음 (2023 서울시 목록) / 2021 서울시 자료엔 금지선 있음", True, True, "unknown")
    elif tgis_ban:
        curb = h5("주정차금지선 (2021 서울시 자료, 2023 목록 없음)", False, True, "yellow_solid")
        fails.append("H5 주정차금지 구간 (2021 서울시 자료)")
    elif _within(line["tgis_no_park"], near):
        curb = h5("황색점선 (주차금지, 정차 가능, 2021 서울시)", True, False, "yellow_dashed")
    else:
        curb = h5("규제선 없음 (2021 서울시 자료)", True, True, "unknown")

    # ② 물리 — 잔여 차로 / 통과 폭
    lanes, oneway, lane_source = m["lanes"]["value"], m["road"]["oneway"], m["lanes"]["source"]
    width, planned = m["width"]["value"], m["planned_road"]
    map1000 = m.get("map1000") or {}
    carriageway = map1000.get("carriageway_width")
    if map1000.get("lanes_total") is not None:
        # 1:1000 수치지형도 차로수는 양방향 합. 차선이 없는 도로는 1로 적혀 있어 양방향이면 편도 0(차선 없음)이 됩니다.
        oneway = oneway or bool(map1000.get("oneway"))
        per_direction = map1000["lanes_total"] if oneway else map1000["lanes_total"] // 2
        lanes, lane_source = per_direction, "1:1000 수치지형도"
    # 1단계 통과 폭: 차도 폭을 알면 폭으로 판정합니다. 서 있는 차 옆으로 지나갈 폭이 남아야 합니다.
    #   일방통행 = 차 2.5 + 한 방향 3.0, 양방통행 = 차 2.5 + 양방향 각 3.0
    # 2단계 잔여 차로: 차도 폭을 모를 때만, 편도 2차로 이상·일방통행이면 통과로 봅니다 (큰길).
    need = th["P1 통과폭 일방"] if oneway else th["P1 통과폭 양방"]
    lane_text = "일방통행" if oneway else ("차선 없음" if lanes == 0 else f"편도 {lanes}차로" if lanes is not None else "차로 수 모름")
    if carriageway is not None:
        ok = carriageway >= need
        value = f"차도 폭 {carriageway:.1f}m (1:1000 도로폭−보도폭), {lane_text}"
        checks.append(dict(item="P1 통과 폭", value=value, threshold=f"{need}m 이상 ({'일방' if oneway else '양방'})", **{"pass": ok}, estimated=False))
        if not ok:
            fails.append(f"P1 통과 폭 부족 (차도 {carriageway:.1f}m, {'일방' if oneway else '양방'} 기준 {need}m)")
    elif oneway or (lanes is not None and lanes >= 2):
        checks.append(dict(item="P1 잔여 차로", value=f"{lane_text} ({lane_source})" if not oneway else "일방통행", threshold="차도 폭 모름 → 편도 2차로 이상 또는 일방통행", **{"pass": True}, estimated=True))
    else:
        if lanes is None:
            needs.append("P1 차로 수 모름 (1:1000·정밀도로지도·OSM에 없음)")
        # 차도 폭을 모를 때: 1:1000 도로폭(보도 포함)과 도시계획시설 도로 등급(법정 최소 폭).
        threshold = f"{need}m 이상"
        planned_ok = planned is not None and planned["min_width"] >= need
        planned_text = f"도시계획 {planned['grade']} ({planned['min_width']}m 이상)" if planned else None
        if width is not None and width >= need:
            value = f"도로폭 {width:.1f}m (1:1000, 보도 포함)" + (f", {planned_text}" if planned_text else "")
            checks.append(dict(item="P1 통과 폭", value=value, threshold=threshold, **{"pass": True}, estimated=True))
            needs.append("P1 폭이 보도 포함 도로폭 (차도 폭 모름)")
        elif width is not None and planned_ok:
            checks.append(dict(item="P1 통과 폭", value=f"도로폭 {width:.1f}m / {planned_text}", threshold=threshold, **{"pass": None}, estimated=True))
            needs.append(f"P1 폭 자료 불일치 (도로폭 {width:.1f}m, {planned_text}) → 사진 확인")
        elif width is not None:
            checks.append(dict(item="P1 통과 폭", value=f"도로폭 {width:.1f}m (1:1000, 보도 포함)" + (f", {planned_text}" if planned_text else ""), threshold=threshold, **{"pass": False}, estimated=True))
            fails.append(f"P1 통과 폭 부족 ({width:.1f}m, 양방 기준 {need}m)")
        elif planned_ok:
            checks.append(dict(item="P1 통과 폭", value=f"{planned_text}, 보도 포함", threshold=threshold, **{"pass": True}, estimated=True))
            needs.append(f"P1 폭이 도시계획 등급 하한 ({planned['grade']}, 도로폭 없음)")
        else:
            checks.append(dict(item="P1 통과 폭", value=planned_text, threshold=threshold, **{"pass": None}, estimated=True)); needs.append("P1 도로 폭 없음 (단지 내 통로일 수 있음)")

    # ③ 승객 접근 — 보도. 자료가 없으면 모름 (사진 판독 결과가 있으면 아래에서 교체)
    # 1:1000 보도 면은 지도 근거로만 씁니다. 확정은 사진(로드뷰)으로 합니다.
    sidewalk = m["sidewalk"]["value"]
    on_map, map_width = map1000.get("sidewalk"), map1000.get("sidewalk_width")
    map_text = None if on_map is None else ("지도상 있음" + (f" (폭 {map_width:.1f}m)" if map_width else "") if on_map else "지도상 없음")
    value = {True: "있음", False: "없음", None: "모름"}[sidewalk] + (f" ({map_text}, 1:1000)" if sidewalk is None and map_text else "")
    checks.append(dict(item="A1 보도", value=value, threshold="보도 있음", **{"pass": sidewalk}, estimated=False))
    if sidewalk is False: fails.append("A1 보도 없음")
    elif sidewalk is None and map_text: needs.append(f"A1 보도 {map_text} (1:1000) → 로드뷰 확인")
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
        photo_report = apply_observations(photo, feature, m["image"], checks, fails, needs, min_confidence=photo_min_confidence,
                                          map_sidewalk=(m.get("map1000") or {}).get("sidewalk"))
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
