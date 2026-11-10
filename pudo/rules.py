"""지도 기반 v0 규칙. 데이터 로딩·파일 출력 없이 후보를 판정합니다."""

import math
from typing import Literal

from shapely.geometry import Point

from .config import TH
from .datasets import MapData
from .geo import distance_to_area, distance_to_lines, distance_to_points, distance_to_segments, to_meters
from .vision import apply_observations, validate_confidence_threshold

Mode = Literal["strict", "lenient"]
SIDEWALK_DEFAULT = {"primary": True, "primary_link": True, "secondary": True, "tertiary": True}
NO_STOP = ("010", "014", "015", "005", "017")
NO_PARK = ("009",)


def _within(distance, limit):
    """거리가 있고 기준 이내인지. 거리 0도 기준 이내로 판단합니다."""
    return distance is not None and distance <= limit


def _child_zone(pr, p, data, th):
    """(상태, 표시 정보). 상태: "in" 보호구역 안, "near" 지정 시설 주변(구간 미확인), "out" 밖.

    T-GIS 추정 구간이 있으면 그것을, 없으면 대상시설 반경을 씁니다. 지정 시설 주변 도로는 표지 근거가 없어
    구간 안으로 단정하지 않습니다.
    """
    if data.child_zone is not None or data.child_zone_facility is not None:
        road = to_meters(*pr["centerline"]) if pr.get("centerline") else p

        def nearest(prefix):
            found = [(math.hypot(p[0] - x, p[1] - y), label) for (x, y), label in data.child_zone_evidence
                     if label.startswith("지정 시설") == prefix]
            return min(found, default=(None, None))

        if data.child_zone is not None and distance_to_area(road, data.child_zone) <= 1.0:
            near = nearest(False)
            evidence = f"근거 {near[1]} {near[0]:.0f}m" if near[0] is not None else "근거 없음"
            return "in", {"mode": "tgis", "value": f"구간 안 ({evidence})", "reason": f"추정 구간 안, {evidence}"}
        if data.child_zone_facility is not None and distance_to_area(road, data.child_zone_facility) <= 1.0:
            near = nearest(True)
            label = f"{near[1]} {near[0]:.0f}m" if near[0] is not None else "지정 시설"
            return "near", {"mode": "tgis", "value": f"{label}, 표지 근거 없음",
                            "reason": f"{label} 주변, 보호구역 범위 미확인 → 표지·사진 확인"}
        return "out", {"mode": "tgis", "value": "구간 밖", "reason": ""}
    sch = min(((math.hypot(p[0] - s[0], p[1] - s[1]), nm) for s, nm in data.schools), default=(None, None))
    inside = sch[0] is not None and sch[0] <= th["H6 학교"]
    value = f"{sch[1]} {sch[0]:.0f}m" if sch[0] is not None else None
    return ("in" if inside else "out"), {"mode": "radius", "value": value, "reason": f"{sch[1]} {sch[0]:.0f}m, 임시 버퍼" if inside else ""}


def _planned_road(pr, p, data):
    """후보 도로가 속한 도시계획시설 도로 (등급명, 최소 폭). 없으면 (None, None).

    교차로에서는 가로지르는 도로의 면에 걸리기 쉬워, 도로 중심점과 진행 방향 앞뒤 10m가
    모두 같은 면 안에 있을 때만 그 도로의 등급으로 봅니다.
    """
    if not data.planned_roads:
        return None, None
    center = to_meters(*pr["centerline"]) if pr.get("centerline") else p
    along = math.radians((pr.get("heading", 0) - 90) % 360)   # heading은 진행 방향 우측(연석 쪽)
    dx, dy = math.sin(along) * 10, math.cos(along) * 10
    probes = [Point(center), Point(center[0] + dx, center[1] + dy), Point(center[0] - dx, center[1] - dy)]
    for area, grade, min_width in data.planned_roads:
        if all(area.contains(probe) for probe in probes):
            return grade, min_width
    return None, None


def judge(f, data: MapData, *, mode: Mode = "strict", thresholds=None, photo_min_confidence=0.8):
    """후보 하나를 판정합니다. 파일 접근이나 입력 변경 없이 모드를 인자로 받습니다."""
    if mode not in ("strict", "lenient"):
        raise ValueError(f"지원하지 않는 판정 모드: {mode}")
    lenient = mode == "lenient"
    validate_confidence_threshold(photo_min_confidence)
    th = TH if thresholds is None else thresholds
    pr = f["properties"]; p = to_meters(*f["geometry"]["coordinates"])
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
    d_int = distance_to_area(p, data.intersection_area)
    if d_int == 0:
        checks.append(dict(item="H1 교차로", value="교차로 안", threshold=f"가장자리에서 {th['H1 교차로']}m 초과", **{"pass": False}, estimated=True))
        fails.append("H1 교차로 안 (도로교통법 제32조 1호)")
    else:
        dist_rule("H1 교차로", d_int, th["H1 교차로"]); checks[-1]["estimated"] = True
        checks[-1]["threshold"] = f"가장자리에서 {th['H1 교차로']}m 초과"
    dist_rule("H2 횡단보도", distance_to_points(p, data.crosswalks), th["H2 횡단보도"])
    dist_rule("H3 정류장", distance_to_points(p, data.buses), th["H3 정류장"])
    dist_rule("H4 소화전", distance_to_points(p, data.hydrants), th["H4 소화전"])
    # ① 합법 — 어린이보호구역 (도로교통법 제32조 8호, 2021.10.21부터 주정차 금지)
    zone_state, zone_note = _child_zone(pr, p, data, th)
    in_zone = zone_state == "in"
    zone_item = dict(item="H6 어린이보호구역", estimated=True)
    if zone_note["mode"] == "tgis":
        zone_item.update(value=zone_note["value"], threshold="보호구역 구간 밖 (T-GIS 표지·노면표시로 추정)",
                         **{"pass": {"in": False, "near": None, "out": True}[zone_state]})
    else:
        zone_item.update(value=zone_note["value"], threshold=f"대상시설 {th['H6 학교']}m 밖 (지정 구간 미확인)", **{"pass": not in_zone})
    checks.append(zone_item)
    if in_zone: (needs if lenient else fails).append(f"H6 어린이보호구역 ({zone_note['reason']}{' → 확인 필요' if lenient else ''})")
    elif zone_state == "near": needs.append(f"H6 어린이보호구역 지정 시설 ({zone_note['reason']})")
    # ① 합법 — 안전지대 사방 10m (정밀도로지도 531). 지도가 없는 도로는 판정 불가(null)
    if data.hd_safe and _within(distance_to_segments(p, data.hd_link), 15):
        d_sz = distance_to_segments(p, data.hd_safe)
        if d_sz is not None and d_sz <= th["H7 안전지대"]:
            checks.append(dict(item="H7 안전지대", value=f"{d_sz:.1f}m", threshold=f"{th['H7 안전지대']}m 초과", **{"pass": False}, estimated=False)); fails.append(f"H7 안전지대 {d_sz:.1f}m (기준 {th['H7 안전지대']}m)")
        else:
            checks.append(dict(item="H7 안전지대", value=f"{d_sz:.1f}m" if d_sz is not None and d_sz < 9e8 else "없음", threshold=f"{th['H7 안전지대']}m 초과", **{"pass": True}, estimated=False))
    # ① 합법 — 규제선. 서울시 2021 자료와 정밀도로지도 2023~25를 교차 확인
    d_ns = distance_to_lines(p, data.lines, NO_STOP); d_np = distance_to_lines(p, data.lines, NO_PARK)
    tgis_ban = d_ns is not None and d_ns <= th["H5 규제선"]
    hd_cov = _within(distance_to_segments(p, data.hd_link), 15)          # 정밀도로지도가 이 도로를 덮는가
    hd_th = th["H5 정밀도로지도 규제선"]
    hd_ban = hd_cov and _within(distance_to_segments(p, data.hd_reg), hd_th)
    if tgis_ban and hd_ban:
        curb = "yellow_solid"; checks.append(dict(item="H5 규제선", value="주정차금지 구간 (2021 서울시 + 2023 정밀도로지도 일치)", threshold="금지 구간 아님", **{"pass": False}, estimated=False)); fails.append("H5 주정차금지 구간 (두 자료 일치)")
    elif tgis_ban and hd_cov:
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="2021 서울시: 금지 / 2023 정밀도로지도: 없음", threshold="금지 구간 아님", **{"pass": None}, estimated=True)); needs.append("H5 규제선 자료 불일치 (2021 금지, 2023 없음) → 사진 확인")
    elif tgis_ban:
        curb = "yellow_solid"; checks.append(dict(item="H5 규제선", value="주정차금지 구간 (2021 서울시 자료, 교차 확인 불가)", threshold="금지 구간 아님", **{"pass": False}, estimated=True)); fails.append("H5 주정차금지 구간 (2021 서울시 자료, 단일 출처)")
    elif hd_ban:
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="2021 서울시: 없음 / 2023 정밀도로지도: 규제선 있음", threshold="금지 구간 아님", **{"pass": None}, estimated=True)); needs.append("H5 규제선 자료 불일치 (2021 없음, 2023 있음) → 사진 확인")
    elif hd_cov and _within(distance_to_segments(p, data.hd_bus), hd_th):
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="버스전용차로 (2023 정밀도로지도). 시간제면 심야 가능", threshold="금지 구간 아님", **{"pass": None}, estimated=True)); needs.append("H5 버스전용차로, 운영 시간 확인 (표지)")
    elif _within(d_np, th["H5 규제선"]) or (hd_cov and _within(distance_to_segments(p, data.hd_park), hd_th)):
        curb = "yellow_dashed"; checks.append(dict(item="H5 규제선", value="황색점선 (주차금지, 정차 가능)", threshold="금지 구간 아님", **{"pass": True}, estimated=False))
    else:
        curb = "unknown"; checks.append(dict(item="H5 규제선", value="두 자료 모두 규제선 없음" if hd_cov else "2021 자료에 규제선 없음 (정밀도로지도 미커버)", threshold="금지 구간 아님", **{"pass": True}, estimated=not hd_cov))
    # ② 물리 — 잔여 차로
    lanes = pr["lanes"]; lanes_est = pr.get("lanes_source") == "default"
    width = pr.get("road_width_m")
    planned_grade, planned_min = _planned_road(pr, p, data)
    oneway = str(pr.get("oneway", "no")).lower() in ("yes", "true", "1", "-1")
    if oneway or lanes >= 2:
        checks.append(dict(item="P1 잔여 차로", value=f"{'일방통행' if oneway else f'편도 {lanes}차로'}", threshold="편도 2차로 이상 또는 일방통행", **{"pass": True}, estimated=lanes_est and not oneway))
        if lanes_est and not oneway: needs.append("P1 차로 수가 추정값")
    else:
        # 폭 근거: 지적도 도로 필지 폭(실측 추정)과 도시계획시설 도로 등급(법정 최소 폭). 둘 다 보도 포함 폭입니다.
        need = th["P1 통과폭"]; threshold = f"{need}m 이상"
        planned_ok = planned_min is not None and planned_min >= need
        planned_text = f"도시계획 {planned_grade} ({planned_min}m 이상)" if planned_grade else None
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
            needs.append(f"P1 폭이 도시계획 등급 하한 ({planned_grade}, 지적도 폭 없음)")
        else:
            checks.append(dict(item="P1 통과 폭", value=planned_text, threshold=threshold, **{"pass": None}, estimated=True)); needs.append("P1 도로 폭 없음 (단지 내 통로일 수 있음)")
    # ③ 승객 접근 — 보도
    sw = SIDEWALK_DEFAULT.get(pr["road_type"])
    checks.append(dict(item="A1 보도", value={True: "있음 (도로 유형 추정)", None: "모름"}[sw], threshold="보도 있음", **{"pass": sw}, estimated=True))
    if sw is None: needs.append("A1 보도 유무 모름 (사진 필요)")
    # 사진
    img = data.images.get(pr["id"])
    # 다시 수집한 도로망은 후보 ID 순서가 바뀔 수 있습니다. 기존 사진을 ID만으로 연결하지 않습니다.
    if img:
        try:
            image_query = to_meters(float(img["lon"]), float(img["lat"]))
            if math.hypot(p[0] - image_query[0], p[1] - image_query[1]) > 1:
                img = None
        except (KeyError, TypeError, ValueError):
            img = None
    photo = data.photo_assessments.get(pr["id"])
    photo_report = None
    if photo is not None:
        photo_report = apply_observations(
            photo, f, img, checks, fails, needs, min_confidence=photo_min_confidence,
        )
    # 판정
    if fails: verdict, score = "불가", None
    elif needs: verdict, score = "확인 필요", None
    else:
        verdict = "가능"
        interf = {"primary": 0.0, "primary_link": 0.0, "secondary": 0.3, "tertiary": 0.5}.get(pr["road_type"], 1.0)
        score = round((1.0 + 1.0 + interf) / 3, 2)   # 잔여차로 1, 보도 1, 간섭(이면도로 1 / 대로 0)
    result = {
        "type": "Feature", "geometry": f["geometry"],
        "properties": {
            "id": pr["id"], "name": pr.get("name", ""), "road_type": pr["road_type"], "oneway": oneway, "heading": pr["heading"], "side": pr.get("side"), "centerline": pr.get("centerline"),
            "verdict": verdict, "score": score, "needs_check": bool(needs), "fail_reasons": fails, "check_reasons": needs, "checks": checks,
            "map": {"dist_intersection_m": round(d_int, 1) if d_int is not None else None, "lanes": lanes, "lanes_estimated": lanes_est, "road_width_m": width, "planned_road": planned_grade, "curb_line": curb, "school_zone": in_zone},
            "image_id": img["pano_id"] if img else None, "image_source": "google" if img else None, "captured_at": None,   # 촬영일은 저장하지 않음 (구글 정책)
            "review": None, "note": None, "reviewed_at": None, "curb_offset_m": pr.get("curb_offset_m"),
        },
    }
    if photo_report is not None:
        result["properties"]["vision"] = photo_report
    return result


def evaluate_candidates(candidates, data: MapData, *, thresholds=None, photo_min_confidence=0.8):
    """기본 판정에 완화 모드의 판정·사유를 더해 기존 GeoJSON 계약을 유지합니다."""
    features = []
    for candidate in candidates:
        strict = judge(candidate, data, mode="strict", thresholds=thresholds, photo_min_confidence=photo_min_confidence)
        lenient = judge(candidate, data, mode="lenient", thresholds=thresholds, photo_min_confidence=photo_min_confidence)
        properties = strict["properties"]
        properties["verdict_lenient"] = lenient["properties"]["verdict"]
        properties["fail_reasons_lenient"] = lenient["properties"]["fail_reasons"]
        properties["check_reasons_lenient"] = lenient["properties"]["check_reasons"]
        features.append(strict)
    return {"type": "FeatureCollection", "features": features}
