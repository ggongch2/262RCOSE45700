"""정차 판독 (순서형): 보도 → 고정 장애물 → 주차장 입구 → 연석 앞 차선.

보도에 점을 찍지 않습니다. 모델에는 원본 캡처 사진을 그대로 넣고, 사진 가운데(후보 정면)를 보게 합니다.
모델은 한 번 호출(생각 켬)로 네 항목을 순서대로 답하고, 판정은 Python이 이 순서대로 합니다.

  1. 보도가 없으면 → 불가
  2. 차도와 보도 사이에 사람이 건널 수 없는 고정 장애물(난간·펜스·벽·화단 띠·가드레일)이 있고 개구부가 없으면 → 불가
     띄엄띄엄 선 가로수·기둥·볼라드, 보도 뒤 건물 쪽 펜스·벽, 사람·차량·콘은 장애물이 아님
  3. 후보 앞이 주차장·건물 차량 출입구면 → 불가
  4. 연석 앞 황색 실선·복선이면 → 불가 (점선·선 없음은 통과). 노란 칠은 Python 색 측정을 우선
  가려져 확인 못 한 항목이 있으면 → 판단 불가. 불가 사유가 하나라도 있으면 불가가 우선
"""
from .photo_decision import IMPOSSIBLE, POSSIBLE, UNDECIDABLE
from .vlm_policy import (CURB_LINES, TEMPORARY_FACILITY_ALIASES, TRANSIENT_OCCUPANCY_ALIASES, AssessmentError,
                         VLMError, _bool_or_none, _enum, _evidence, extract_json_object)

CHECK_VERSION = "pudo3-stop-check.v1"
DECISION_VERSION = "pudo3-stop-decision.v1"
SIDEWALK_VALUES = {"present", "absent", "not_visible"}
BARRIER_TYPES = {"metal_railing", "fence", "wall", "planter", "guardrail", "other_fixed", "none"}
BARRIER_ALIASES = {"permanent_fence": "fence", "railing": "metal_railing", "hedge": "planter", "shrubs": "planter",
                   "flower_bed": "planter", "planter_strip": "planter", "guard_rail": "guardrail"}
NOT_BARRIER = {"tree", "trees", "street_tree", "pole", "poles", "bollard", "bollards", "sign", "lamp", "streetlight"}
BARRIER_KO = {"metal_railing": "금속 난간", "fence": "펜스", "wall": "벽", "planter": "화단(관목 띠)",
              "guardrail": "가드레일", "other_fixed": "고정 시설", "none": "없음"}
LINE_KO = {"white": "흰색 선", "yellow_dashed": "황색 점선", "yellow_solid": "황색 실선",
           "yellow_double": "황색 복선", "none": "규제선 없음", None: "확인 못함"}
LABELS = {"sidewalk": "1. 보도", "barrier": "2. 차도–보도 사이 고정 장애물", "parking_entrance": "3. 주차장 입구",
          "curb_line": "4. 연석 앞 차선"}
SCOPE = "후보 정면 로드뷰 사진 가운데의 관측만 판정. GIS 거리·구역 판정이나 최종 운영 승인을 대체하지 않음"


def stop_question():
    return """이 사진은 카카오 로드뷰를 후보 도로변 정면(도로에 수직인 방향)으로 캡처한 것입니다.
사진 가운데 부분에 보이는 '카메라 바로 앞 가까운 차도 → 연석 → 보도'만 보고 답하세요. 후보는 사진 가운데 정면에 있습니다.
사진 왼쪽·오른쪽 끝, 반대편 도로, 멀리 작게 보이는 것은 판단에 쓰지 마세요.
빨간 원·카카오 핀은 프로그램 표시이며 판독 기준이 아닙니다.

아래 순서대로 확인하세요.

1. sidewalk (보도): 사진 가운데에서 차도 다음에 사람이 서서 걸을 수 있는 보도 바닥(타일·포장면)이 있는가?
   present = 있음
   absent = 차도 옆이 바로 벽·건물·화단·비탈 등이라 사람이 설 보도가 없음
   not_visible = 주차 차량 등에 가려 확인할 수 없음 (가려진 것은 absent가 아닙니다)

2. barrier (차도와 보도 사이 고정 장애물): 사진 가운데 부분에서 차도와 보도 사이(연석 위 또는 연석 바로 뒤)에
   사람이 차에서 내려 보도로 건너갈 수 없게 길게 이어져 막는 고정 시설이 있는가?
   장애물에 해당: 금속 난간, 펜스, 벽, 연석을 따라 이어진 화단·관목 띠, 가드레일
   장애물이 아님: 띄엄띄엄 선 가로수·가로등·표지 기둥·볼라드(사이로 지나갈 수 있음),
                 보도 안쪽 끝(건물 쪽)의 펜스·벽·화단, 사람·차량·이동식 콘(일시적인 것)
   value = true(있음) / false(없음) / null(가려져 확인 못함)
   type = metal_railing / fence / wall / planter / guardrail / other_fixed / none
   opening = 장애물이 있을 때, 사진 가운데 근처에 사람이 지나갈 수 있는 끊긴 틈·출입구가 있으면 true, 없으면 false, 안 보이면 null

3. parking_entrance (주차장 입구): 사진 가운데 근처 보도가 주차장·건물·차고로 들어가는 차량 출입구(진출입로)인가?
   단서: 차단기, 주차 정산 부스, '입구'·'출구'·'IN'·'OUT'·'P' 표시, 건물로 들어가거나 내려가는 경사로,
         보도 턱을 낮춘 넓은 차량 통로, 진출입로 양옆의 줄무늬 경계석
   value = true / false / null

4. curb_line (연석 앞 차선): 연석 바로 앞 도로 가장자리 선을 사진 왼쪽 끝부터 오른쪽 끝까지 따라가며 판단하세요.
   칠해진 구간과 빈 구간이 번갈아 나오면 yellow_dashed, 끊김 없이 이어지면 yellow_solid, 두 줄이면 yellow_double,
   흰 선이면 white, 선이 없으면 none, 도로 가장자리가 가려져 안 보이면 null

5. temporary_occupancy (참고): 사진 가운데 연석 앞에 주차 차량·사람·적재물이 있으면 true, 없으면 false

각 항목의 evidence에는 사진에서 실제로 본 것을 한국어 한두 문장으로 적고, 그 관찰에 맞게 값을 정하세요.
다른 설명 없이 아래 키 순서의 JSON 하나만 출력하세요.
{"sidewalk": {"evidence": "...", "value": "present | absent | not_visible"},
 "barrier": {"evidence": "...", "type": "...", "opening": "true | false | null", "value": "true | false | null"},
 "parking_entrance": {"evidence": "...", "value": "true | false | null"},
 "curb_line": {"evidence": "...", "value": "..."},
 "temporary_occupancy": {"evidence": "...", "value": "true | false"}}"""


def _item(data, name):
    item = data.get(name)
    if not isinstance(item, dict) or "value" not in item:
        raise AssessmentError(f"{name}: value와 evidence가 있는 객체가 필요합니다.")
    return item


def _norm(value):
    return value.strip().lower().replace(" ", "_").replace("-", "_") if isinstance(value, str) else value


def parse_stop_check(text):
    """모델 응답 검증 → 정규화된 관측값. 실패하면 AssessmentError."""
    try:
        data = extract_json_object(text)
    except VLMError as error:
        raise AssessmentError(f"판독 응답 형식 오류: {error}") from None
    result = {"version": CHECK_VERSION}

    item = _item(data, "sidewalk")
    value = _norm(item["value"])
    value = {"true": "present", "false": "absent", "null": "not_visible", None: "not_visible",
             "occluded": "not_visible", "visible": "present"}.get(value, value)
    if value is True:
        value = "present"
    elif value is False:
        value = "absent"
    if value not in SIDEWALK_VALUES:
        raise AssessmentError("sidewalk.value: present/absent/not_visible 중 하나가 필요합니다.")
    result["sidewalk"] = {"value": value, "evidence": _evidence(item.get("evidence"), "sidewalk.evidence", AssessmentError)}

    item = _item(data, "barrier")
    present = _bool_or_none(item["value"], "barrier.value", AssessmentError)
    kind = _norm(item.get("type")) or "none"
    kind = BARRIER_ALIASES.get(kind, kind)
    excluded = None
    if kind in NOT_BARRIER or kind in TRANSIENT_OCCUPANCY_ALIASES or kind in TEMPORARY_FACILITY_ALIASES:
        excluded, kind = kind, "none"
    elif kind not in BARRIER_TYPES:
        kind = "other_fixed" if present else "none"
    barrier = {"value": False if excluded else present, "type": kind,
               "opening": _bool_or_none(item.get("opening"), "barrier.opening", AssessmentError),
               "evidence": _evidence(item.get("evidence"), "barrier.evidence", AssessmentError)}
    if barrier["value"] is True and kind == "none":
        barrier["type"] = "other_fixed"
    if excluded:
        barrier["excluded"] = excluded
        barrier["reported_value"] = present
    result["barrier"] = barrier

    item = _item(data, "parking_entrance")
    result["parking_entrance"] = {"value": _bool_or_none(item["value"], "parking_entrance.value", AssessmentError),
                                  "evidence": _evidence(item.get("evidence"), "parking_entrance.evidence", AssessmentError)}

    item = _item(data, "curb_line")
    line = _norm(item["value"])
    if line in {"unknown", "not_visible", "null"}:
        line = None
    result["curb_line"] = {"value": _enum(line, CURB_LINES, "curb_line.value"),
                           "evidence": _evidence(item.get("evidence"), "curb_line.evidence", AssessmentError)}

    item = data.get("temporary_occupancy")
    if isinstance(item, dict):
        result["temporary_occupancy"] = {
            "value": _bool_or_none(item.get("value"), "temporary_occupancy.value", AssessmentError),
            "evidence": _evidence(item.get("evidence"), "temporary_occupancy.evidence", AssessmentError, required=False)}
    return result


def _josa(word, with_final, without_final):
    code = ord(word[-1]) - 0xAC00 if word else -1
    return with_final if 0 <= code <= 11171 and code % 28 else without_final


def decide_stop(check, *, curb_line_required=True):
    """순서대로 판정: 1 보도 → 2 고정 장애물 → 3 주차장 입구 → 4 차선. 불가 사유는 앞 순서가 먼저 나옴."""
    fails, unverified, notes, conditions = [], [], [], []

    def add(field, value_text, status, reason=None, note=None):
        evidence = check[field]["evidence"]
        conditions.append({"field": field, "label": LABELS[field], "value": value_text, "status": status,
                           "evidence": evidence})
        entry = {"field": field, "label": LABELS[field], "reason": reason, "evidence": evidence}
        if status == "fail":
            fails.append(entry)
        elif status == "unknown":
            unverified.append(entry)
        if note:
            notes.append({"field": field, "label": LABELS[field], "note": note, "evidence": evidence})

    sidewalk = check["sidewalk"]["value"]
    if sidewalk == "absent":
        add("sidewalk", "없음", "fail", "후보 앞에 사람이 설 수 있는 보도가 없음")
    elif sidewalk == "not_visible":
        add("sidewalk", "확인 못함", "unknown", "보도가 차량 등에 가려 있는지 확인하지 못함 (보도 없음과 다름)")
    else:
        add("sidewalk", "있음", "pass")

    barrier = check["barrier"]
    name = BARRIER_KO.get(barrier["type"], barrier["type"])
    if barrier.get("excluded"):
        notes.append({"field": "barrier", "label": LABELS["barrier"], "evidence": barrier["evidence"],
                      "note": f"'{barrier['excluded']}'는 사이로 지나갈 수 있거나 일시적인 것이라 고정 장애물로 보지 않음"})
    if barrier["value"] is None:
        add("barrier", "확인 못함", "unknown", "차도와 보도 사이 고정 장애물이 있는지 가려져 확인하지 못함")
    elif barrier["value"] is False:
        add("barrier", "없음", "pass")
    elif barrier["opening"] is False:
        add("barrier", f"{name} · 개구부 없음", "fail",
            f"차도와 보도 사이에 {name}{_josa(name, '이', '가')} 있고 사람이 지나갈 개구부가 없음")
    elif barrier["opening"] is True:
        add("barrier", f"{name} · 개구부 있음", "pass",
            note=f"{name}{_josa(name, '이', '가')} 있지만 후보 앞에 사람이 지나갈 개구부가 있어 막히지 않음")
    else:
        add("barrier", f"{name} · 개구부 확인 못함", "unknown",
            f"차도와 보도 사이 {name}에 사람이 지나갈 개구부가 있는지 확인하지 못함")

    entrance = check["parking_entrance"]["value"]
    if entrance is True:
        add("parking_entrance", "출입구", "fail", "후보 앞이 주차장·건물 차량 출입구임")
    elif entrance is None:
        add("parking_entrance", "확인 못함", "unknown", "후보 앞이 주차장·건물 차량 출입구인지 확인하지 못함")
    else:
        add("parking_entrance", "아님", "pass")

    line = check["curb_line"]["value"]
    if line == "yellow_solid":
        add("curb_line", LINE_KO[line], "fail", "연석 앞에 황색 실선이 있음 (주정차 금지)")
    elif line == "yellow_double":
        add("curb_line", LINE_KO[line], "fail", "연석 앞에 황색 복선이 있음 (정차·주차 금지)")
    elif line is None and curb_line_required:
        add("curb_line", "확인 못함", "unknown", "연석 앞 차선이 가려져 확인하지 못함")
    elif line is None:
        add("curb_line", "확인 못함 (GIS 자료 사용)", "pass", note="사진에서 차선을 확인하지 못했으나 GIS 규제선 자료로 판정함")
    else:
        add("curb_line", LINE_KO[line], "pass")

    scene = []
    occ = check.get("temporary_occupancy") or {}
    if occ.get("value") is True:
        scene.append({"field": "temporary_occupancy", "label": "촬영 당시 일시 점유",
                      "note": "주차 차량·사람·적재물은 정차 불가 사유가 아님. 현재 상태 확인 필요",
                      "evidence": occ.get("evidence", "")})

    if fails:
        verdict, reason = IMPOSSIBLE, "불가: " + fails[0]["reason"]
    elif unverified:
        verdict = UNDECIDABLE
        reason = ("판단 불가: " + unverified[0]["reason"]) if len(unverified) == 1 else (
            f"판단 불가: 조건 {len(unverified)}개를 확인하지 못함 — " + "; ".join(u["reason"] for u in unverified))
    else:
        verdict = POSSIBLE
        reason = "가능: 후보 앞 보도가 있고 차도–보도 사이 고정 장애물·주차장 입구·황색 실선/복선이 없음"
    return {"version": DECISION_VERSION, "verdict": verdict, "reason": reason, "fail_reasons": fails,
            "unverified": unverified, "notes": notes, "scene_conditions": scene, "conditions": conditions,
            "scope": SCOPE}
