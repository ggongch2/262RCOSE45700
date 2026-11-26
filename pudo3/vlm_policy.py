"""Qwen3.5 2단계 판독의 질문(프롬프트)과 응답 검증.

1차: 실제 연석 판독 위치 탐색 (localization)
2차: 표시된 연석 위치의 승하차 조건 관측 (assessment)

모델은 관측값과 근거만 반환합니다. 최종 가능/불가는 photo_decision.py의 Python 규칙이 정합니다.
검증에 실패하면 추측해서 고치지 않고 예외를 냅니다 (호출 측에서 '판단 불가' 처리).
"""
import json
import math
import re

LOCALIZATION_VERSION = "pudo3-sidewalk-localization.v3"
ASSESSMENT_VERSION = "pudo3-curb-assessment.v1"

NORM_MAX = 1000
# 판독 기준: 화면 가운데 세로선. 가운데 보도가 가려졌을 때 좌우로 옮겨 찍을 수 있는 최대 거리 (0~1000 기준)
CENTER_X = 500
MAX_CENTER_SHIFT = 350
# 연석 점은 보도 지점과 같은 좌우 위치여야 함 (0~1000 기준)
SIDEWALK_X_TOLERANCE = 150
SIDEWALK_STATUSES = {"visible", "occluded", "absent"}
# 탐색 상자 밖 허용 여유 (0~1000). 가로는 가려진 보도를 옆에서 찾는 경우를 위해 더 넓게.
BOX_MARGIN_X = 120
# tilt=0 정면 화면의 지평선 높이 (0~1000). 땅 위의 보도는 항상 이보다 아래
HORIZON_Y = 500
# 보도 영역 아래쪽(연석 쪽) 30% 안에 찍힌 점은 아래에서 40% 높이로 올림
BOX_MARGIN_Y = 50
BOX_MARGIN_TOP = 20   # 위쪽(먼 쪽) 여유는 작게: 너무 멀리 보이는 보도는 찍지 않음

LAYER_VALUES = {"road", "curb", "sidewalk", "property_fence", "building",
                "curb_railing", "planter", "wall", "driveway", "other"}

OBSTRUCTION_TYPES = {"metal_railing", "permanent_fence", "wall", "planter", "bollard",
                     "other_fixed", "temporary_facility", "none", "unknown"}
PERMANENT_TYPES = {"metal_railing", "permanent_fence", "wall", "planter", "bollard", "other_fixed"}
RELATIONS = {"curb_edge", "inside_sidewalk", "property_boundary", "background", "none", "unknown"}
CURB_LINES = {"white", "yellow_dashed", "yellow_solid", "yellow_double", "none"}

# 모델이 허용 목록 밖의 이름으로 일시 점유물·이동식 시설을 적었을 때의 정규화.
# 사람·차량·적재물은 영구 고정 차단물이 아니며, 콘·이동식 바리케이드·공사 펜스는 임시 시설입니다.
TRANSIENT_OCCUPANCY_ALIASES = {
    "vehicle", "car", "parked_car", "truck", "bus", "taxi", "motorcycle", "scooter", "bicycle",
    "person", "people", "pedestrian", "cargo", "goods", "load", "boxes",
}
TEMPORARY_FACILITY_ALIASES = {
    "temporary_facility", "cone", "cones", "traffic_cone", "rubber_cone", "barricade",
    "movable_barricade", "temporary_barricade", "construction_fence", "construction", "temporary_fence",
}

ASSESSMENT_FIELDS = ("sidewalk", "fixed_obstruction", "vehicle_entrance", "curb_line", "stop_exception")


class VLMError(ValueError):
    pass


class LocalizationError(VLMError):
    pass


class AssessmentError(VLMError):
    pass


# ------------------------------------------------------------------ JSON 추출
def extract_json_object(text):
    """응답에서 첫 JSON 객체를 꺼냅니다. 코드 블록(```json) 감싸기만 허용하고 내용은 고치지 않습니다."""
    if not isinstance(text, str):
        raise VLMError("모델 응답이 문자열이 아닙니다.")
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start = cleaned.find("{")
    if start < 0:
        raise VLMError("응답에 JSON 객체가 없습니다.")
    depth, quoted, escaped = 0, False, False
    for index, char in enumerate(cleaned[start:], start):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(cleaned[start:index + 1])
                except ValueError as error:
                    raise VLMError(f"JSON 문법 오류: {error.msg}") from None
                if not isinstance(value, dict):
                    raise VLMError("JSON 객체가 필요합니다.")
                return value
    raise VLMError("JSON 객체가 닫히지 않았습니다 (응답이 잘렸을 수 있음).")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _bool_or_none(value, name, error_class):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "null", "unknown"}:
        return {"true": True, "false": False}.get(value.strip().lower())
    raise error_class(f"{name}: true/false/null이 필요합니다.")


def _evidence(value, name, error_class, required=True):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise error_class(f"{name}: 사진 근거 문장이 필요합니다.")
    return value.strip()[:1000]


# ------------------------------------------------------------------ 1차: 연석 위치
def localization_question(box=None):
    """box: 계산한 탐색 상자 [x1,y1,x2,y2] (0~1000). 없으면 화면 가운데 기준 (계산 불가 시 대체)."""
    if box is not None:
        return _box_question(box)
    return _center_question()


def _box_question(box):
    return f"""이 사진은 카카오 로드뷰를 후보 도로변 정면(도로에 수직인 방향)으로 캡처한 것입니다.
노란 점선 상자는 '후보 보도가 보일 수 있는 범위'입니다. 상자 위쪽 끝은 찍어도 되는 가장 먼 거리입니다.
빨간 원·카카오 핀·흰 화살표는 프로그램 표시이며 판독 기준으로 쓰지 마세요.

할 일: 노란 상자 안에서 승객이 차에서 내려 서게 될 '보도 바닥' 한 점을 찾습니다.
1. 화면 가운데 근처에서, 차도 다음에 나오는 카메라 쪽 보도를 찾으세요. 정가운데일 필요는 없습니다.
2. 점은 차량·사람·벽·펜스·화단·차도 위가 아니라, 실제로 보이는 보도 바닥(타일·포장면) 위에 찍으세요.
   가까운 보도가 세워진 차량에 가려졌으면, 그 차 뒤(위쪽)나 옆으로 보이는 같은 보도 바닥에 찍으세요.
   보도는 땅이므로 항상 지평선(빨간 원 높이)보다 아래에 있습니다.
   상자 위쪽 끝보다 멀리(위쪽에) 보이는 보도, 반대편 도로의 보도, 사진에서 멀리 작게 보이는 보도는 찍지 마세요.
   차에 가려져 가까운 보도를 상자 안에서 찾을 수 없으면 occluded로 답하세요.
3. 점은 연석 선(노란 선·경계석·차도와 보도의 경계) 바로 위에 찍지 마세요. 연석에서 보도 안쪽으로 들어간,
   연석과 보도 뒤 끝(난간·화단·벽·건물 앞) 사이의 가운데쯤 보도 바닥에 찍으세요.
   연석을 따라 난간이 서 있으면 연석과 난간 사이 보도 바닥의 가운데에 찍습니다(난간 위나 난간 기둥이 아님).
   sidewalk_region은 찍은 점 주변에서 연석 바로 뒤부터 보도 뒤 끝까지, 그 보도의 깊이 전체를 덮는 사각형으로 답하세요.
   보도 뒤의 화단 벽·난간·펜스·건물 벽·계단·건물 앞마당이나 반대편 도로의 보도가 아닙니다.
4. sidewalk_status를 다음 중 하나로 정하세요.
   - "visible": 보도 바닥을 찾아 찍음
   - "occluded": 보도가 있을 수 있지만 차량 등에 가려져 상자 주변에서도 보도 바닥을 찾을 수 없음
   - "absent": 상자 범위의 차도 옆에 사람이 설 보도가 아예 없음 (차도 바로 옆이 벽·화단·주차장·건물 등)
   가려져서 안 보이는 것을 "absent"로 답하면 안 됩니다. 확실히 없을 때만 "absent"입니다.
5. visible이면 sidewalk_point(보도 바닥 위 한 점)와 sidewalk_region(연석 바로 뒤부터 보도 뒤 끝까지 보도 깊이 전체)을 답하고,
   그 바로 앞(화면 아래쪽) 차도와 보도 사이 연석이 보이면 curb_point / curb_region도 답하세요 (안 보이면 null).
6. 좌표는 왼쪽 위 (0,0), 오른쪽 아래 (1000,1000) 기준 정수이며, 각 point는 자기 region 안에 있어야 합니다.
7. layer_order에는 찍은 지점의 세로선에서 화면 아래(가까운 쪽)부터 위(먼 쪽) 순서로 실제로 보이는 층만 적으세요.
   허용 값: road, curb, curb_railing, sidewalk, planter, property_fence, wall, driveway, building, other.

다른 설명 없이 아래 키를 가진 JSON 객체 하나만 출력하세요. 값은 모두 이 사진을 직접 보고 정하세요:
- sidewalk_status: 위 4번의 세 값 중 하나
- sidewalk_point: 사진에서 직접 찾은 보도 바닥 점의 [x, y] 좌표, 찍지 못하면 null
- sidewalk_region: 사진에서 직접 찾은 보도 영역의 [x1, y1, x2, y2] 좌표, 찍지 못하면 null
- curb_point / curb_region: 사진에서 직접 찾은 연석의 점·영역 좌표, 안 보이면 null
- layer_order: 위 7번 규칙대로 이 사진에서 실제로 보이는 층의 배열, 찍지 못하면 빈 배열
- evidence: 찍은 지점에 실제로 보이는 것(바닥 재질·색, 주변 물체)을 한국어 한두 문장으로. 찍지 못했으면 그 이유"""


def _center_question():
    c = CENTER_X
    return f"""이 사진은 카카오 로드뷰를 후보 도로변 정면(도로에 수직인 방향)으로 캡처한 것입니다.
사진 속 빨간 원·카카오 핀·흰 화살표는 프로그램 표시이며 실물이 아닙니다. 판독 기준으로 쓰지 마세요.

할 일: 승객이 차에서 내려 서게 될 '보도 바닥' 한 점을 찾습니다.
1. 기준은 화면 가운데 세로선(x = {c})입니다. 그 세로선을 따라 화면 아래(카메라에 가까운 쪽)부터 위로 올라가며,
   차도 다음에 처음 나오는 보도 바닥(타일·포장면)을 찾으세요. 카메라에 가장 가까운 보도여야 합니다.
   보도 뒤의 화단 벽·난간·펜스·건물 벽·계단·건물 앞마당이나 반대편 도로의 보도가 아닙니다.
2. 가운데 세로선의 보도가 차량·사람 등에 가려졌으면, 가운데에서 가까운 쪽부터 좌우로 찾아
   보이는 같은 보도 바닥에 찍으세요 (x는 {c - MAX_CENTER_SHIFT}~{c + MAX_CENTER_SHIFT} 범위).
3. sidewalk_status를 다음 중 하나로 정하세요.
   - "visible": 보도 바닥을 찾아 찍음
   - "occluded": 보도가 있을 수 있지만 차량 등에 가려져 주변에서도 보도 바닥을 찾을 수 없음
   - "absent": 차도 옆에 사람이 설 보도가 아예 없음 (차도 바로 옆이 벽·화단·주차장·건물 등)
   가려져서 안 보이는 것을 "absent"로 답하면 안 됩니다. 확실히 없을 때만 "absent"입니다.
4. visible이면 sidewalk_point(보도 바닥 위 한 점)와 sidewalk_region(그 주변 보도 바닥 영역)을 답하고,
   그 바로 앞(화면 아래쪽) 차도와 보도 사이 연석이 보이면 curb_point / curb_region도 답하세요 (안 보이면 null).
5. 좌표는 왼쪽 위 (0,0), 오른쪽 아래 (1000,1000) 기준 정수이며, 각 point는 자기 region 안에 있어야 합니다.
6. layer_order에는 찍은 지점의 세로선에서 화면 아래(가까운 쪽)부터 위(먼 쪽) 순서로 실제로 보이는 층만 적으세요.
   허용 값: road, curb, curb_railing, sidewalk, planter, property_fence, wall, driveway, building, other.

다른 설명 없이 JSON 하나만 출력하세요:
{{"sidewalk_status": "visible", "sidewalk_point": [x, y], "sidewalk_region": [x1, y1, x2, y2], "curb_point": [x, y], "curb_region": [x1, y1, x2, y2], "layer_order": ["road", "curb", "sidewalk", "property_fence", "building"], "evidence": "찍은 보도와 연석의 위치·모양을 한국어 한두 문장으로"}}
보도를 찍지 못하면: {{"sidewalk_status": "occluded 또는 absent", "sidewalk_point": null, "sidewalk_region": null, "curb_point": null, "curb_region": null, "layer_order": [], "evidence": "이유를 한국어 한두 문장으로"}}"""


def _coords(value, count, name):
    if not isinstance(value, list) or len(value) != count or not all(_number(v) for v in value):
        raise LocalizationError(f"{name}: 숫자 {count}개 배열이 필요합니다.")
    if not all(0 <= v <= NORM_MAX for v in value):
        raise LocalizationError(f"{name}: 좌표가 0~{NORM_MAX} 범위를 벗어났습니다 ({value}).")
    return [float(v) for v in value]


def _point_in_region(point, region, name):
    x1, y1, x2, y2 = region
    if not (x1 < x2 and y1 < y2):
        raise LocalizationError(f"{name}_region이 유효한 사각형이 아닙니다 ({region}).")
    if not (x1 <= point[0] <= x2 and y1 <= point[1] <= y2):
        raise LocalizationError(f"{name}_point {point}가 {name}_region {region} 밖에 있습니다.")


def lift_from_curb(point, region, curb_point=None, top_limit=None):
    """판독점 높이 보정: 점이 보도 가운데보다 연석 쪽(아래)이면 보도 가운데로 올립니다. 위로만 옮깁니다.

    보도 가운데 = 보도 먼 끝(sidewalk_region 위쪽)과 가까운 끝의 중간.
    가까운 끝은 연석 y와 sidewalk_region 아래쪽 중 더 위(먼) 쪽입니다. 모델이 보도 영역을 탐색 상자 아래 끝
    (차도·화면 아래)까지 잡는 경우가 많아 연석 y를 우선합니다. 차에 가려진 보도면 영역 아래쪽(차 윗면)이 됩니다.
    실제 판독에서 점이 공통적으로 연석 쪽에 너무 낮게 찍혀 보정합니다.
    """
    _, far, _, near = region
    if curb_point is not None:
        near = min(near, curb_point[1])
    if near <= far:
        return point
    middle = (far + near) / 2
    middle = max(middle, HORIZON_Y + 10, top_limit if top_limit is not None else 0)
    if point[1] <= middle:
        return point
    return [point[0], round(middle, 1)]


def parse_localization(text, box=None):
    """1차 응답 검증. 실패하면 LocalizationError (추측해서 위치를 정하지 않음).

    box가 있으면 보도 지점이 탐색 상자(여유 BOX_MARGIN 포함) 안에 있어야 하고,
    없으면 화면 가운데에서 MAX_CENTER_SHIFT 안이어야 합니다.
    """
    try:
        data = extract_json_object(text)
    except VLMError as error:
        raise LocalizationError(f"위치 응답 형식 오류: {error}") from None
    status = data.get("sidewalk_status")
    if status not in SIDEWALK_STATUSES:
        raise LocalizationError("sidewalk_status: visible/occluded/absent 중 하나가 필요합니다.")
    evidence = _evidence(data.get("evidence"), "evidence", LocalizationError)
    empty = {"version": LOCALIZATION_VERSION, "sidewalk_status": status, "target_visible": False,
             "sidewalk_point": None, "sidewalk_region": None, "curb_point": None, "curb_region": None,
             "curb_note": None, "model_point": None, "lifted": False, "reading_point": None, "center_shift": None, "layer_order": [], "evidence": evidence}
    if status != "visible":
        return empty
    point = _coords(data.get("sidewalk_point"), 2, "sidewalk_point")
    region = _coords(data.get("sidewalk_region"), 4, "sidewalk_region")
    _point_in_region(point, region, "sidewalk")
    shift = point[0] - CENTER_X
    if point[1] <= HORIZON_Y:
        raise LocalizationError(
            f"보도 지점 y={point[1]:.0f}가 지평선(y={HORIZON_Y})보다 위에 있습니다. 보도 바닥은 지평선 아래에 있어야 합니다.")
    if box is not None:
        bx1, by1, bx2, by2 = box
        if not (bx1 - BOX_MARGIN_X <= point[0] <= bx2 + BOX_MARGIN_X and by1 - BOX_MARGIN_TOP <= point[1] <= by2 + BOX_MARGIN_Y):
            raise LocalizationError(
                f"보도 지점 {point}가 계산한 탐색 상자 {[round(v) for v in box]} 밖에 있어 다른 곳을 찍은 것으로 보입니다.")
    elif abs(shift) > MAX_CENTER_SHIFT:
        raise LocalizationError(
            f"보도 지점 x={point[0]:.0f}가 화면 가운데({CENTER_X})에서 {MAX_CENTER_SHIFT} 넘게 벗어났습니다.")
    # 연석은 표시·확대 범위용 보조 정보입니다. 좌표가 이상하면 연석만 버리고 보도 판독은 계속합니다.
    curb_point = curb_region = curb_note = None
    if data.get("curb_point") is not None or data.get("curb_region") is not None:
        try:
            curb_point = _coords(data.get("curb_point"), 2, "curb_point")
            curb_region = _coords(data.get("curb_region"), 4, "curb_region")
            _point_in_region(curb_point, curb_region, "curb")
            if curb_point[1] <= point[1]:
                raise LocalizationError(
                    f"연석 y={curb_point[1]:.0f}가 보도 지점 y={point[1]:.0f}보다 화면 아래가 아님")
            if abs(curb_point[0] - point[0]) > SIDEWALK_X_TOLERANCE:
                raise LocalizationError(
                    f"연석 x={curb_point[0]:.0f}가 보도 지점 x={point[0]:.0f}에서 {SIDEWALK_X_TOLERANCE} 넘게 벗어남")
        except LocalizationError as error:
            curb_point = curb_region = None
            curb_note = f"연석 좌표를 쓰지 않음 ({error}). 보도 지점으로만 판독"
    layers = data.get("layer_order") or []
    if not isinstance(layers, list) or not all(isinstance(v, str) for v in layers):
        raise LocalizationError("layer_order: 문자열 배열이 필요합니다.")
    layers = [v.strip().lower() for v in layers]
    unknown = [v for v in layers if v not in LAYER_VALUES]
    if unknown:
        raise LocalizationError(f"layer_order에 정의되지 않은 값이 있습니다: {', '.join(unknown)}")
    if "sidewalk" not in layers:
        raise LocalizationError("보도를 찍었다고 했지만 layer_order에 sidewalk가 없습니다.")
    model_point = list(point)
    point = lift_from_curb(point, region, curb_point, None if box is None else box[1])
    return {**empty, "target_visible": True, "sidewalk_point": point, "sidewalk_region": region,
            "model_point": model_point, "lifted": point != model_point,
            "curb_point": curb_point, "curb_region": curb_region, "curb_note": curb_note,
            "reading_point": point, "center_shift": round(shift, 1), "layer_order": layers}


# ------------------------------------------------------------------ 2차: 관계 기반 판독
def assessment_question(localization, curb_line_needed=True):
    layers = ", ".join(localization.get("layer_order") or [])
    line_note = "" if curb_line_needed else (
        "\n(GIS가 규제선을 이미 판정했지만, 사진에서 보이는 선은 그대로 기록하세요.)")
    return f"""로드뷰 전체 장면 사진 한 장을 봅니다.
하늘색 원과 십자선이 '판독 대상 보도 지점'(승객이 내려 처음 서는 곳)이고,
주황색 사각형이 그 바로 앞 '표시된 연석'(차도와 보도의 경계)입니다. 주황색 사각형이 없으면 하늘색 지점 바로 앞 연석을 보세요.
빨간 원·카카오 핀은 지리 좌표 참고 표시일 뿐 판독 대상 지점이 아닙니다.

1차 위치 탐색 결과: 층 순서(가까운 쪽→먼 쪽) = {layers}. 근거: {localization.get('evidence', '')}

판독 원칙:
- 사진 전체가 아니라 하늘색 보도 지점과 그 앞 주황색 연석 사이 승객 동선에 직접 관련된 것만 판독하세요.
  단, 도로 가장자리 규제선(curb_line)의 종류는 예외입니다. 아래 '규제선 판독' 규칙을 따르세요.
- 구조물이 있으면 '있는가'가 아니라 표시된 연석과의 관계(relation)를 판단하세요:
  curb_edge = 차도와 보도 사이 또는 연석 바로 위 / inside_sidewalk = 보도 내부 /
  property_boundary = 보도 뒤 건물·대지 경계 / background = 대상 지점과 무관한 배경 / none = 구조물 없음 / unknown = 구분 불가
- fixed_obstruction.value=true는 영구 고정 구조물이 curb_edge에 있고 실제로 차도→보도 진입을 막을 때만 쓰세요.
- 보도 뒤 건물 경계의 난간·벽·화단·펜스는 차도→보도 진입을 막지 않으므로 value=false, relation=property_boundary입니다.
- 난간이 이어지더라도 표시 지점 정면에 열린 개구부가 있으면 opening_at_target=true입니다.
- 사람·자동차·트럭·오토바이·적재물은 고정 차단물이 아닙니다 (temporary_occupancy에 기록).
- 공사 펜스·이동식 콘·이동식 바리케이드는 type=temporary_facility입니다.
- 보이지 않거나 가려진 것은 '없음(false/none)'이 아니라 null입니다.
- sidewalk: 표시 지점 바로 뒤(먼 쪽)에 사람이 서서 걸을 수 있는 보도 포장면이 이어지면 true, 차도·벽·화단·차량 진입로만 있으면 false.
- vehicle_entrance: 표시 지점 바로 뒤가 주차장·건물·차고로 들어가는 차량 진출입로이면 true입니다.
  단서: 차단기(바), 주차 정산·발권 부스, 'IN'/'OUT'·'입구'·'출구'·'P' 표시, 건물 안으로 내려가거나 들어가는 경사로,
  보도 턱을 낮춘 넓은 차량 통로, 진출입로 양옆의 황색·흑색 줄무늬 경계석. 차량 통로가 표시 지점과 떨어져 있으면 false.
- type 허용 값: metal_railing, permanent_fence, wall, planter, bollard, other_fixed, temporary_facility, none, unknown
- 규제선 판독 (curb_line): 선 종류는 표시 지점 근처만 보고 정하지 마세요.
  표시된 연석 바로 앞 도로 가장자리 선을 사진 왼쪽 끝부터 오른쪽 끝까지 따라가며 판단하세요.
  선이 일정한 간격으로 칠해진 구간과 비어 있는 구간이 번갈아 나오면 점선(yellow_dashed)이고,
  보이는 전체 구간에서 끊김 없이 이어질 때만 실선(yellow_solid)입니다. 두 줄이 나란히 이어지면 복선(yellow_double)입니다.
  화살표 표시·차량·그림자·나무·사람에 가려진 부분은 끊김으로 보지 마세요.
  선이 보이는 구간이 짧아 끊김 여부를 알 수 없으면 실선이라고 단정하지 말고 null로 답하세요.
  evidence에는 선을 어디서 어디까지 따라갔고 끊김이 있었는지 적으세요.
- curb_line 허용 값: white, yellow_dashed, yellow_solid, yellow_double, none, null (null = 도로 가장자리가 보이지 않음){line_note}
- stop_exception: 황색 실선이 있을 때 시간·차종 조건부 정차 허용 표지가 보이면 true, 없으면 false, 판독 대상이 아니거나 안 보이면 null.
- evidence에는 사진에서 확인한 위치·모양을 한국어 한두 문장으로 구체적으로 적으세요.

각 항목은 먼저 evidence에 표시 지점 주변에서 실제로 보이는 것을 적고, 그 관찰에 맞게 값을 정하세요.
다른 설명 없이 JSON 하나만 출력하세요 (값은 예시이며 사진에 맞게 바꾸세요):
{{"sidewalk": {{"evidence": "...", "value": true}},
 "fixed_obstruction": {{"evidence": "...", "type": "none", "relation": "none", "opening_at_target": true, "value": false}},
 "vehicle_entrance": {{"evidence": "...", "value": false}},
 "curb_line": {{"evidence": "...", "value": "none"}},
 "stop_exception": {{"evidence": "...", "value": null}},
 "temporary_occupancy": {{"evidence": "...", "value": false}}}}"""


def _field(data, name):
    item = data.get(name)
    if not isinstance(item, dict) or "value" not in item:
        raise AssessmentError(f"{name}: value와 evidence가 있는 객체가 필요합니다.")
    return item


def _enum(value, allowed, name, aliases=None):
    if value is None:
        return None
    if not isinstance(value, str):
        raise AssessmentError(f"{name}: 문자열이 필요합니다.")
    v = value.strip().lower().replace(" ", "_").replace("-", "_")
    if v in {"null", ""}:
        return None
    if aliases and v in aliases:
        return aliases[v]
    if v not in allowed:
        raise AssessmentError(f"{name}: 허용되지 않은 값 '{value}'")
    return v


def parse_assessment(text):
    """2차 응답 검증 → 정규화된 관측값. 실패하면 AssessmentError."""
    try:
        data = extract_json_object(text)
    except VLMError as error:
        raise AssessmentError(f"조건 응답 형식 오류: {error}") from None
    result = {"version": ASSESSMENT_VERSION}

    item = _field(data, "sidewalk")
    result["sidewalk"] = {"value": _bool_or_none(item["value"], "sidewalk.value", AssessmentError),
                          "evidence": _evidence(item.get("evidence"), "sidewalk.evidence", AssessmentError)}

    item = _field(data, "fixed_obstruction")
    raw_type = item.get("type")
    excluded = None
    norm_type = raw_type.strip().lower().replace(" ", "_").replace("-", "_") if isinstance(raw_type, str) else raw_type
    if isinstance(norm_type, str) and norm_type in TRANSIENT_OCCUPANCY_ALIASES:
        excluded, obstruction_type = norm_type, "none"
    elif isinstance(norm_type, str) and norm_type in TEMPORARY_FACILITY_ALIASES:
        obstruction_type = "temporary_facility"
    else:
        obstruction_type = _enum(raw_type, OBSTRUCTION_TYPES, "fixed_obstruction.type")
    value = _bool_or_none(item["value"], "fixed_obstruction.value", AssessmentError)
    obstruction = {
        "value": False if excluded else value,
        "type": obstruction_type if obstruction_type is not None else ("none" if value is False else "unknown"),
        "relation": _enum(item.get("relation"), RELATIONS, "fixed_obstruction.relation") or (
            "none" if value is False else "unknown"),
        "opening_at_target": _bool_or_none(item.get("opening_at_target"), "fixed_obstruction.opening_at_target",
                                           AssessmentError),
        "evidence": _evidence(item.get("evidence"), "fixed_obstruction.evidence", AssessmentError),
    }
    if excluded:
        obstruction["excluded_transient"] = excluded
        obstruction["reported_value"] = value
    result["fixed_obstruction"] = obstruction

    item = _field(data, "vehicle_entrance")
    result["vehicle_entrance"] = {"value": _bool_or_none(item["value"], "vehicle_entrance.value", AssessmentError),
                                  "evidence": _evidence(item.get("evidence"), "vehicle_entrance.evidence", AssessmentError)}

    item = _field(data, "curb_line")
    line = item["value"]
    if isinstance(line, str) and line.strip().lower() in {"unknown", "not_visible"}:
        line = None
    result["curb_line"] = {"value": _enum(line, CURB_LINES, "curb_line.value"),
                           "evidence": _evidence(item.get("evidence"), "curb_line.evidence", AssessmentError)}

    item = _field(data, "stop_exception")
    result["stop_exception"] = {"value": _bool_or_none(item["value"], "stop_exception.value", AssessmentError),
                                "evidence": _evidence(item.get("evidence"), "stop_exception.evidence",
                                                      AssessmentError, required=False)}

    item = data.get("temporary_occupancy")
    if isinstance(item, dict) and "value" in item:
        result["temporary_occupancy"] = {
            "value": _bool_or_none(item["value"], "temporary_occupancy.value", AssessmentError),
            "evidence": _evidence(item.get("evidence"), "temporary_occupancy.evidence", AssessmentError, required=False)}
    else:
        result["temporary_occupancy"] = {"value": None, "evidence": ""}
    if excluded:
        note = f"고정 차단물 항목에 보고된 '{excluded}'는 일시 점유물로 분리함"
        occ = result["temporary_occupancy"]
        result["temporary_occupancy"] = {"value": True, "evidence": "; ".join(filter(None, [occ["evidence"], note]))}
    return result
