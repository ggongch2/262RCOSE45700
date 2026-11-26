"""사진 관측값 → 최종 가능/불가/판단 불가 (Python 결정 규칙).

VLM은 관측값과 근거만 반환하고, 최종 판정은 여기서만 합니다.
즉시 '불가' (하나라도 명확히 확인되면):
  1. 후보 바로 앞에 사람이 내릴 보도가 없음
  2. 표시된 연석과 보도 사이에 영구 고정 차단물이 있음
  3. 고정 차단물이 이어지고 표시된 위치에 열린 개구부가 없음
  4. 후보 위치가 주차장·차고 차량 출입구와 직접 겹침
  5. 황색 복선
  6. 황색 실선이며 적용 가능한 조건부 허용이 확인되지 않음
'판단 불가': 연석 위치 미발견, 보도 유무 불명, 구조물이 연석 차단물인지 대지 경계인지 구분 불가,
            JSON·좌표 검증 실패, 필요한 사진 근거 누락.
"""
from .vlm_policy import PERMANENT_TYPES

DECISION_VERSION = "pudo3-photo-decision.v1"
POSSIBLE, IMPOSSIBLE, UNDECIDABLE = "가능", "불가", "판단 불가"

TYPE_KO = {
    "metal_railing": "금속 난간", "permanent_fence": "고정 펜스", "wall": "벽", "planter": "화단",
    "bollard": "볼라드", "other_fixed": "고정 구조물", "temporary_facility": "임시 시설",
    "none": "구조물 없음", "unknown": "종류를 알 수 없는 구조물",
}
RELATION_KO = {
    "curb_edge": "차도와 보도 사이(연석 위)", "inside_sidewalk": "보도 내부",
    "property_boundary": "보도 뒤 건물·대지 경계", "background": "대상 지점과 무관한 배경",
    "none": "없음", "unknown": "관계 불명",
}
LINE_KO = {"white": "흰색 실선", "yellow_dashed": "황색 점선", "yellow_solid": "황색 실선",
           "yellow_double": "황색 복선", "none": "규제선 없음", None: "확인 못함"}
LABELS = {
    "curb_localization": "보도 판독 위치",
    "sidewalk": "후보 바로 앞 보도",
    "fixed_obstruction": "연석–보도 사이 고정 차단물",
    "vehicle_entrance": "차량 출입구 겹침",
    "curb_line": "도로 가장자리 규제선",
    "stop_exception": "조건부 정차 허용 표지",
    "assessment": "2차 조건 판독",
}


def _josa(word, with_final, without_final):
    code = ord(word[-1]) - 0xAC00 if word else -1
    return with_final if 0 <= code <= 11171 and code % 28 else without_final


def _ev(obs, name):
    return ((obs or {}).get(name) or {}).get("evidence", "") or ""


def undecidable(field, reason, evidence=""):
    """연석 탐색 실패·JSON 검증 실패 등 판독 전체를 판단 불가로 끝낼 때."""
    return _result([], [{"field": field, "label": LABELS.get(field, field), "reason": reason, "evidence": evidence}],
                   [], [], [])


def decide(localization, assessment, *, curb_line_required=True):
    """localization: parse_localization 결과, assessment: parse_assessment 결과."""
    status = (localization or {}).get("sidewalk_status")
    evidence = (localization or {}).get("evidence", "")
    if status == "absent":
        # 보도 자체가 없음 → 2차 없이 즉시 불가
        item = {"field": "sidewalk", "label": LABELS["sidewalk"],
                "reason": "후보 바로 앞에 사람이 설 수 있는 보도가 없음", "evidence": evidence}
        result = _result([item], [], [], [], [])
        result["conditions"] = [{"field": "sidewalk", "label": LABELS["sidewalk"], "value": "없음",
                                 "status": "fail", "evidence": evidence}]
        return result
    if status == "occluded":
        return undecidable("sidewalk", "보도가 차량 등에 가려져 주변에서도 보도 바닥을 찾지 못함 (보도 없음과 다름)", evidence)
    if not localization or not localization.get("target_visible"):
        return undecidable("curb_localization", "보도 판독 위치를 찾지 못함", evidence)
    if not assessment:
        return undecidable("assessment", "2차 조건 판독 결과가 없음")

    fails, unverified, notes, scene, conditions = [], [], [], [], []

    def fail(field, reason):
        fails.append({"field": field, "label": LABELS[field], "reason": reason, "evidence": _ev(assessment, field)})

    def unknown(field, reason):
        unverified.append({"field": field, "label": LABELS[field], "reason": reason, "evidence": _ev(assessment, field)})

    def note(field, text):
        notes.append({"field": field, "label": LABELS[field], "note": text, "evidence": _ev(assessment, field)})

    def row(field, value_text, status):
        conditions.append({"field": field, "label": LABELS[field], "value": value_text, "status": status,
                           "evidence": _ev(assessment, field)})

    # 1. 보도
    sidewalk = assessment["sidewalk"]["value"]
    if sidewalk is False:
        fail("sidewalk", "후보 바로 앞에 사람이 설 수 있는 보도가 없음")
        row("sidewalk", "없음", "fail")
    elif sidewalk is None:
        unknown("sidewalk", "후보 바로 앞 보도 유무가 가림 등으로 불명확함")
        row("sidewalk", "확인 못함", "unknown")
    else:
        row("sidewalk", "있음", "pass")

    # 2·3. 고정 차단물 (관계 기반)
    fo = assessment["fixed_obstruction"]
    value, kind, relation, opening = fo["value"], fo["type"], fo["relation"], fo["opening_at_target"]
    name = TYPE_KO.get(kind, kind)
    where = RELATION_KO.get(relation, relation)
    if fo.get("excluded_transient"):
        note("fixed_obstruction", f"'{fo['excluded_transient']}'는 사람·차량·적재물 등 일시 점유물이라 영구 차단물로 보지 않음")
    if kind == "temporary_facility":
        scene.append({"field": "fixed_obstruction", "label": "촬영 당시 임시 시설",
                      "note": f"공사 펜스·이동식 콘·바리케이드 등 임시 시설({where})은 영구 차단 사유와 분리. 현재 상태 확인 필요",
                      "evidence": fo["evidence"]})
        row("fixed_obstruction", f"임시 시설 · {where}", "note")
    elif value is None:
        unknown("fixed_obstruction", "표시된 연석과 보도 사이의 고정 차단물 여부를 확인하지 못함 (가려짐)")
        row("fixed_obstruction", "확인 못함", "unknown")
    elif value is False:
        if relation == "curb_edge" and kind in PERMANENT_TYPES and opening is False:
            unknown("fixed_obstruction",
                    f"{name}{_josa(name, '이', '가')} 연석에 있고 개구부가 없다고 하면서 차단 없음으로 판독해 값이 서로 모순됨")
            row("fixed_obstruction", "판독값 모순", "unknown")
        else:
            if kind in PERMANENT_TYPES and relation in {"property_boundary", "background", "inside_sidewalk"}:
                note("fixed_obstruction", f"{name}{_josa(name, '은', '는')} {where}에 있어 차도→보도 진입을 막지 않으므로 불가 사유가 아님")
            row("fixed_obstruction", "차단 없음" + (f" ({name} · {where})" if kind in PERMANENT_TYPES else ""), "pass")
    else:   # value is True
        if kind in {"none", "unknown"} and relation == "curb_edge":
            unknown("fixed_obstruction", "연석에 구조물이 있으나 영구 고정 시설인지 구분하지 못함")
            row("fixed_obstruction", f"{name} · {where}", "unknown")
        elif kind in {"none"}:
            unknown("fixed_obstruction", "고정 차단물이 있다고 했지만 종류가 '없음'으로 기록되어 판독값이 모순됨")
            row("fixed_obstruction", "판독값 모순", "unknown")
        elif relation in {"property_boundary", "background"}:
            note("fixed_obstruction", f"{name}{_josa(name, '은', '는')} {where}에 있어 승객 접근 차단물로 보지 않음")
            row("fixed_obstruction", f"{name} · {where} (차단 아님)", "pass")
        elif relation == "inside_sidewalk":
            note("fixed_obstruction", f"{name}{_josa(name, '은', '는')} 보도 내부에 있어 차도→보도 진입 차단 사유가 아님 (보행 여유 참고)")
            row("fixed_obstruction", f"{name} · 보도 내부 (차단 아님)", "pass")
        elif relation in {"unknown", "none"}:
            unknown("fixed_obstruction", f"{name}{_josa(name, '이', '가')} 연석 차단물인지 보도 뒤 대지 경계인지 구분하지 못함")
            row("fixed_obstruction", f"{name} · 관계 불명", "unknown")
        elif kind == "unknown":
            unknown("fixed_obstruction", "연석에 구조물이 있으나 영구 고정 시설인지 구분하지 못함")
            row("fixed_obstruction", f"{name} · {where}", "unknown")
        elif opening is False:
            fail("fixed_obstruction", f"표시된 연석과 보도 사이에 {name}{_josa(name, '이', '가')} 있고 정면 개구부가 없음")
            row("fixed_obstruction", f"{name} · 연석 · 개구부 없음", "fail")
        elif opening is True:
            note("fixed_obstruction", f"연석의 {name}{_josa(name, '이', '가')} 이어지지만 표시된 위치에 열린 개구부가 있어 차단으로 보지 않음")
            row("fixed_obstruction", f"{name} · 연석 · 정면 개구부 있음", "pass")
        else:
            unknown("fixed_obstruction", f"연석의 {name} 정면에 열린 개구부가 있는지 확인하지 못함")
            row("fixed_obstruction", f"{name} · 연석 · 개구부 확인 못함", "unknown")

    # 4. 차량 출입구
    entrance = assessment["vehicle_entrance"]["value"]
    if entrance is True:
        fail("vehicle_entrance", "표시된 위치가 주차장·차고 차량 출입구와 직접 겹침")
        row("vehicle_entrance", "겹침", "fail")
    elif entrance is None:
        unknown("vehicle_entrance", "표시된 위치가 주차장·차고 차량 출입구와 겹치는지 확인하지 못함")
        row("vehicle_entrance", "확인 못함", "unknown")
    else:
        row("vehicle_entrance", "겹치지 않음", "pass")

    # 5·6. 규제선
    line = assessment["curb_line"]["value"]
    exception = assessment["stop_exception"]["value"]
    if line == "yellow_double":
        fail("curb_line", "표시된 연석의 도로 가장자리에 황색 복선(정차·주차 금지)이 있음")
        row("curb_line", LINE_KO[line], "fail")
    elif line == "yellow_solid":
        if exception is True:
            unknown("stop_exception", "황색 실선 구간의 조건부 허용 표지가 현재 시간·요일·차종에 적용되는지 확인하지 못함")
            row("curb_line", "황색 실선 + 조건부 허용 표지", "unknown")
        else:
            fail("curb_line", "표시된 연석에 황색 실선이 있고 적용 가능한 조건부 정차 허용이 확인되지 않음")
            row("curb_line", LINE_KO[line], "fail")
    elif line is None:
        if curb_line_required:
            unknown("curb_line", "표시된 연석의 도로 가장자리 규제선을 확인하지 못함")
            row("curb_line", "확인 못함", "unknown")
        else:
            note("curb_line", "사진에서 규제선을 확인하지 못했으나 GIS 규제선 자료로 판정함")
            row("curb_line", "확인 못함 (GIS 자료 사용)", "note")
    else:
        row("curb_line", LINE_KO[line], "pass")

    occ = assessment.get("temporary_occupancy") or {}
    if occ.get("value") is True:
        scene.append({"field": "temporary_occupancy", "label": "촬영 당시 일시 점유",
                      "note": "사람·차량·적재물은 영구 차단 사유와 분리. 현재 상태 확인 필요",
                      "evidence": occ.get("evidence", "")})
    return _result(fails, unverified, notes, scene, conditions)


def _result(fails, unverified, notes, scene, conditions):
    if fails:
        verdict = IMPOSSIBLE
        reason = "불가: " + fails[0]["reason"]
    elif unverified:
        verdict = UNDECIDABLE
        reason = ("판단 불가: " + unverified[0]["reason"]) if len(unverified) == 1 else (
            f"판단 불가: 필수 조건 {len(unverified)}개를 확인하지 못함 — " + "; ".join(u["reason"] for u in unverified))
    else:
        verdict = POSSIBLE
        reason = "가능: 표시된 연석에서 보도가 연결되고 고정 차단물·차량 출입구·정차 금지선이 확인되지 않음"
    return {"version": DECISION_VERSION, "verdict": verdict, "reason": reason,
            "fail_reasons": fails, "unverified": unverified, "notes": notes,
            "scene_conditions": scene, "conditions": conditions,
            "scope": "표시된 연석 지점의 사진 관측 조건만 판정. GIS 거리·구역 판정이나 최종 운영 승인을 대체하지 않음"}
