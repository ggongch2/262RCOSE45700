"""지도 좌표로 로드뷰 화면 속 후보 보도 위치를 계산합니다 (모델 없이).

카카오 로드뷰 화면은 일반 원근(핀홀) 투영입니다. 기존 캡처 3건의 카카오 마커 위치로 확인한
초점거리 FOCAL_PX ≈ 623.5px (1280×720 화면, 가로 시야각 약 91.5°)를 씁니다.
카카오 마커는 지면 높이를 반영하지 않아 항상 지평선(y=360)에 그려지므로, 높이는 여기서
카메라 높이와 거리로 직접 계산합니다.

  x = cx + f · L / F        L: 화면 오른쪽 방향 거리(m), F: 화면 정면 방향 거리(m)
  y = cy + f · h / F        h: 카메라 높이(m), tilt=0

후보 좌표 자체도 지도에서 추정한 값이라 오차가 있으므로 점 하나가 아니라 '탐색 상자'를 만듭니다.

탐색 상자는 화면 가운데를 중심으로 좌우로 넓게(정가운데일 필요 없음), 위쪽은 연석 뒤 MAX_BEHIND_CURB_M까지
(차에 가려져도 차 뒤로 보이는 가까운 보도는 포함, 너무 멀리 보이는 보도는 제외), 아래쪽은 가장 가까운 보도 높이까지입니다.
촬영 위치가 도로를 따라 옆으로 떨어진 거리(right_m)와 예상 지점은 참고로만 기록합니다(사진에 그리지 않음).
"""
import math

CURB_INSET_M = 1.5          # candidates.CURB_INSET_M과 같은 값: 후보 점(차 중심)에서 차도 끝(연석)까지 (테스트로 고정)

FOCAL_PX = 623.5            # 1280×720 기준. 보정: 기존 캡처 3건 (오프셋 -14.6°, 12.4°, 19.2°)에서 동일
SCREEN = (1280, 720)
CAMERA_HEIGHT_M = 2.2       # P0453 판독(연석 y=875)으로 역산한 값. 직접 수정 판독이 쌓이면 다시 보정
CAMERA_HEIGHT_RANGE_M = (1.8, 2.7)
CENTER_HALF_WIDTH = 300     # 탐색 상자 가로 반폭 (0~1000 기준, 500 ± 이 값). 정가운데일 필요는 없음
HORIZON_GAP_NORM = 15       # 상자 위쪽 끝은 최소한 지평선(빨간 마커 높이, y=500)보다 이만큼 아래
MAX_BEHIND_CURB_M = 8.0     # 연석 뒤 이 거리보다 멀리 보이는 보도는 찍지 않음 (차 뒤 보도는 포함)
SIDEWALK_NEAR_M = -0.5      # 연석보다 이만큼 앞(차도 쪽)부터
SIDEWALK_FAR_M = 4.0        # 연석 뒤 이만큼까지를 보도 탐색 범위로 봄 (난간 뒤 보도까지 포함)
SIDEWALK_STEP_M = 2.0       # 예상 지점: 연석 뒤 2m (연석 선에 붙지 않게 보도 안쪽). 1m는 실제 판독에서 연석에 너무 붙었음
MIN_FORWARD_M = 0.8         # 이보다 가까우면 투영이 불안정하므로 계산하지 않음
NORM = 1000


def _ground_offset(camera, candidate, pan_deg):
    """카메라 → 후보: (오른쪽 L, 정면 F) 미터. 화면 방향(pan) 기준."""
    lon, lat = candidate["coordinates"]
    dx = (lon - camera[0]) * 111320 * math.cos(math.radians(lat))
    dy = (lat - camera[1]) * 111320
    p = math.radians(pan_deg)
    forward = dx * math.sin(p) + dy * math.cos(p)
    right = dx * math.cos(p) - dy * math.sin(p)
    return right, forward


def project(right_m, forward_m, height_m, size=SCREEN, focal=FOCAL_PX):
    width, height = size
    scale = focal * width / SCREEN[0]
    return (width / 2 + scale * right_m / forward_m, height / 2 + scale * height_m / forward_m)


def search_box(camera, candidate, pan_deg, size=SCREEN):
    """화면 가운데 세로선에서 후보 보도의 예상 높이와 탐색 상자 (0~1000 정규화).

    반환 {"valid": bool, "reason": str|None, "box": [x1,y1,x2,y2], "predicted_point": [x,y],
          "right_m", "forward_curb_m", 파라미터}
    """
    width, height = size
    right, forward = _ground_offset(camera, candidate, pan_deg)
    curb_f = forward + CURB_INSET_M
    base = {"right_m": round(right, 2), "forward_curb_m": round(curb_f, 2), "focal_px": FOCAL_PX,
            "camera_height_m": CAMERA_HEIGHT_M, "box": None, "predicted_point": None}
    near = curb_f + SIDEWALK_NEAR_M
    far = curb_f + SIDEWALK_FAR_M
    if near < MIN_FORWARD_M:
        return {**base, "valid": False,
                "reason": f"촬영 위치가 연석에 너무 가깝거나 지나쳐 있어(정면 거리 {curb_f:.1f}m) 화면 위치를 계산할 수 없음"}
    h_lo, h_hi = CAMERA_HEIGHT_RANGE_M
    # 위쪽 끝: 연석 뒤 MAX_BEHIND_CURB_M 거리까지 (차 뒤로 보이는 가까운 보도는 포함, 너무 먼 보도는 제외).
    # 카메라 높이를 낮게 잡을수록 같은 거리가 화면 위쪽에 오므로 h_lo로 계산해 여유를 둠. 지평선보다는 항상 아래.
    y1 = max(project(0, curb_f + MAX_BEHIND_CURB_M, h_lo, size)[1],
             height / 2 + HORIZON_GAP_NORM / NORM * height)
    y2 = min(height - 1.0, project(0, near, h_hi, size)[1])
    if y1 >= y2:
        return {**base, "valid": False, "reason": "계산상 보도가 화면 아래 밖에 있음"}
    x1 = width / 2 - CENTER_HALF_WIDTH / NORM * width
    x2 = width / 2 + CENTER_HALF_WIDTH / NORM * width
    px, py = width / 2, project(0, curb_f + SIDEWALK_STEP_M, CAMERA_HEIGHT_M, size)[1]
    py = min(max(py, y1), y2)
    to_norm = lambda x, y: [round(x / width * NORM, 1), round(y / height * NORM, 1)]
    return {**base, "valid": True, "reason": None,
            "box": to_norm(x1, y1) + to_norm(x2, y2), "predicted_point": to_norm(px, py)}


CURB_BAND_ABOVE = 0.04      # 연석 예상 높이 위로 (이미지 높이 대비) — 보도 쪽 점자블록은 최대한 빼기
CURB_BAND_BELOW = 0.12      # 연석 예상 높이 아래로 — 도로 가장자리 노란 선이 칠해지는 쪽


def curb_band(search, size=SCREEN):
    """황색 규제선 색 측정 영역 (원본 픽셀 x1,y1,x2,y2). 연석까지 정면 거리로 화면 높이를 계산.

    카메라 높이 범위(CAMERA_HEIGHT_RANGE_M) 전체를 덮고 위아래 여유를 둡니다. 화면 밖이면 None.
    """
    width, height = size
    curb_f = (search or {}).get("forward_curb_m")
    if not curb_f or curb_f < MIN_FORWARD_M:
        return None
    h_lo, h_hi = CAMERA_HEIGHT_RANGE_M
    top = project(0, curb_f, h_lo, size)[1] - CURB_BAND_ABOVE * height
    bottom = min(height, project(0, curb_f, h_hi, size)[1] + CURB_BAND_BELOW * height)
    top = max(height / 2, top)
    if bottom - top < 8:
        return None
    return (0, int(top), width, int(bottom))
