"""연석 앞 황색 규제선을 색으로 직접 측정합니다 (모델 없이).

연석 띠 영역(원본 이미지 좌표)에서 노란 페인트 픽셀을 찾아 왼쪽→오른쪽으로 칠해진 구간을 잽니다.
- 칠해진 구간 사이에 '차도·연석 바닥이 보이는데 노란색이 없는' 빈 구간이 있으면 → 점선 (yellow_dashed)
- 두 줄이 나란히 이어지면 → 복선 (yellow_double)
- 화면 너비의 SOLID_SPAN_RATIO 이상을 끊김 없이 이어지면 → 실선 (yellow_solid)
- 노란 칠이 거의 없으면 → 규제선 없음 (none). 황색 선이 없으면 정차에 문제 없음 (점선도 문제 없음, 실선·복선만 금지)
- 그 밖(보이는 구간이 짧음, 빈 구간이 가려져 확인 불가) → None (모델 답을 그대로 씀)

빈 구간에서 선이 있어야 할 자리(칠해진 구간으로 맞춘 직선)가 흰색(흰 화살표·흰 차선)이거나
차량처럼 색이 있는 물체면 '가려짐'으로 보고 끊김으로 세지 않습니다. 회색·검은 차도(그림자 포함)면 '보이는 빈 구간'.
"""

# PIL HSV는 0~255 범위. 노란색(약 35°~70°) = 25~50
YELLOW_HUE = (25, 50)
YELLOW_MIN_SAT = 90
YELLOW_MIN_VAL = 55
MIN_COLUMN_PIXELS = 2       # 한 세로줄에 노란 픽셀이 이 개수 이상이면 '칠해짐'
BRIDGE_PX = 6               # 이보다 짧은 끊김은 같은 구간으로 이음 (잡음)
MIN_SEGMENT_PX = 20         # 이보다 짧은 노란 구간은 무시
MIN_PAINTED_PX = 60         # 노란 칠이 이보다 적으면 측정하지 않음
MIN_GAP_PX = 25             # 이보다 짧은 빈 구간은 끊김으로 세지 않음
GAP_VISIBLE_RATIO = 0.6     # 빈 구간 세로줄 중 이 비율 이상이 '보이는 바닥'이면 진짜 빈 구간
GROUND_MAX_SAT = 70         # 바닥(회색·검은 차도, 그림자): 채도 낮음
WHITE_MIN_VAL = 230         # 흰 화살표·흰 페인트: 아주 밝고 채도 낮음
DOUBLE_RATIO = 0.5          # 칠해진 세로줄 중 이 비율 이상에서 두 줄이 보이면 복선
SOLID_SPAN_RATIO = 0.5      # 끊김 없는 한 구간이 화면 너비의 이 비율 이상이면 실선
LINE_HALF_BAND = 2          # 빈 구간에서 선 자리 위아래로 볼 픽셀


def _runs(flags, bridge=0):
    """True 구간 [(시작, 끝+1)], bridge 이하 끊김은 이어 붙임."""
    runs, start = [], None
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(flags)])
    merged = []
    for run in runs:
        if merged and run[0] - merged[-1][1] <= bridge:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    return merged


def measure_curb_paint(image, strip_box):
    """image: PIL RGB 원본, strip_box: (x1, y1, x2, y2) 원본 픽셀. 반환 dict (value는 None일 수 있음)."""
    import numpy as np
    x0, y0, x1, y1 = (int(round(v)) for v in strip_box)
    crop = image.crop((x0, y0, x1, y1))
    hsv = np.asarray(crop.convert("HSV")).astype(int)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    yellow = (hue >= YELLOW_HUE[0]) & (hue <= YELLOW_HUE[1]) & (sat >= YELLOW_MIN_SAT) & (val >= YELLOW_MIN_VAL)
    height, width = yellow.shape
    result = {"value": None, "method": "color", "strip_box": [x0, y0, x1, y1], "segments": [],
              "visible_gaps": [], "hidden_gaps": [], "painted_px": 0, "double_ratio": 0.0, "evidence": ""}

    painted = yellow.sum(axis=0) >= MIN_COLUMN_PIXELS
    segments = [s for s in _runs(painted, BRIDGE_PX) if s[1] - s[0] >= MIN_SEGMENT_PX]
    painted_px = sum(e - s for s, e in segments)
    result["segments"] = [[s + x0, e + x0] for s, e in segments]
    result["painted_px"] = int(painted_px)
    if painted_px < MIN_PAINTED_PX:
        result["value"] = "none"
        result["evidence"] = "색 측정: 연석 띠에 노란 칠이 없음 → 황색 규제선 없음"
        return result

    # 선의 위치(세로줄마다 노란 픽셀 가운데)를 직선으로 맞춰 빈 구간에서 선이 있어야 할 자리를 계산
    xs, ys, doubles, counted = [], [], 0, 0
    for s, e in segments:
        for x in range(s, e):
            rows = np.flatnonzero(yellow[:, x])
            if len(rows) < MIN_COLUMN_PIXELS:
                continue
            xs.append(x)
            ys.append(float(np.median(rows)))
            counted += 1
            if len([r for r in _runs(yellow[:, x], 1) if r[1] - r[0] >= 2]) >= 2:
                doubles += 1
    slope, intercept = np.polyfit(xs, ys, 1) if len(set(xs)) >= 2 else (0.0, ys[0])
    result["double_ratio"] = round(doubles / max(1, counted), 2)

    visible, hidden = [], []
    for (_, gap_start), (gap_end, _) in zip(segments, segments[1:]):
        if gap_end - gap_start < MIN_GAP_PX:
            continue
        ground = 0
        for x in range(gap_start, gap_end):
            y = int(round(slope * x + intercept))
            lo, hi = max(0, y - LINE_HALF_BAND), min(height, y + LINE_HALF_BAND + 1)
            if lo >= hi:
                continue
            s_mean, v_mean = sat[lo:hi, x].mean(), val[lo:hi, x].mean()
            if s_mean <= GROUND_MAX_SAT and v_mean < WHITE_MIN_VAL:
                ground += 1
        (visible if ground >= GAP_VISIBLE_RATIO * (gap_end - gap_start) else hidden).append(
            [gap_start + x0, gap_end + x0])
    result["visible_gaps"], result["hidden_gaps"] = visible, hidden

    spans = ", ".join(f"{s}~{e}" for s, e in result["segments"])
    if result["double_ratio"] >= DOUBLE_RATIO:
        result["value"] = "yellow_double"
        result["evidence"] = f"색 측정: 노란 칠 세로줄의 {result['double_ratio']:.0%}에서 두 줄이 나란히 보임 (구간 {spans}px)"
    elif visible and len(segments) >= 2:
        result["value"] = "yellow_dashed"
        gaps = ", ".join(f"{s}~{e}" for s, e in visible)
        result["evidence"] = (f"색 측정: 노란 칠 구간 {len(segments)}개({spans}px) 사이에 차도 바닥이 보이는 빈 구간 "
                              f"{len(visible)}개({gaps}px) → 점선")
    elif not visible and max(e - s for s, e in segments) >= SOLID_SPAN_RATIO * width and not hidden:
        result["value"] = "yellow_solid"
        result["evidence"] = f"색 측정: 노란 칠이 끊김 없이 이어짐 (구간 {spans}px, 화면 너비 {width}px) → 실선"
    else:
        result["evidence"] = (f"색 측정: 노란 칠 구간 {spans}px. 보이는 구간이 짧거나 빈 구간이 가려져 "
                              "실선/점선을 정하지 못함 (모델 판독 사용)")
    return result


def apply_measure(assessment, measure):
    """색 측정 결과가 분명하면 2차 판독의 curb_line을 측정값으로 바꿉니다. 원래 모델 답은 model_value에 남깁니다."""
    if not assessment or not measure or measure.get("value") is None:
        return assessment
    line = assessment["curb_line"]
    if line["value"] == measure["value"] or (measure["value"] == "none" and line["value"] == "white"):
        line["measure_agrees"] = True
        return assessment
    assessment["curb_line"] = {"value": measure["value"], "evidence": measure["evidence"],
                               "source": "color_measure", "model_value": line["value"],
                               "model_evidence": line["evidence"]}
    return assessment
