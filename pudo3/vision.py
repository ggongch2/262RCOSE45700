"""이미지 처리: 디코딩, 0~1000 정규화 좌표 변환, 스냅 위치 표시 이미지와 연석 주변 확대 이미지.

모든 이미지는 메모리에서 만듭니다. 파일 저장은 vlm.py에서 진단 옵션이 켜진 경우에만 합니다.
"""
import base64
import hashlib
import io

NORM = 1000                     # Qwen3-VL 기본 상대 좌표계 (0~1000)
MAX_IMAGE_PIXELS = 16_000_000

# 확대 영역: 보도 판독 지점을 중심으로, 바로 앞 연석·차도 가장자리와 보도 뒤 가장자리
# (난간·벽·화단·펜스·차량 출입구)를 함께 담습니다. 비율은 원본 이미지 크기 대비.
CROP_WIDTH_RATIO = 0.46
CROP_HEIGHT_RATIO = 0.56
CROP_ABOVE_RATIO = 0.55         # 확대 영역 높이 중 보도 지점보다 위쪽(먼 쪽) 비율
CURB_MARGIN_RATIO = 0.06        # 연석 아래로 최소한 이만큼(이미지 높이 대비) 차도를 포함
DETAIL_MIN_WIDTH = 896          # 확대 이미지를 이 너비 이상으로 키워 세부를 보이게 함

SNAP_COLOR = (0, 200, 255)      # 보도 판독 지점: 하늘색 원+십자선 (카카오 마커는 빨간 원)
CURB_COLOR = (255, 140, 0)      # 바로 앞 연석: 주황색 사각형
SNAP_OUTLINE = (0, 0, 0)


class ImageError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def decode_rgb(image_bytes):
    """PNG/JPEG/WebP 바이트 → PIL RGB 이미지."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(image_bytes)) as opened:
            if opened.format not in {"PNG", "JPEG", "WEBP"} or opened.width * opened.height > MAX_IMAGE_PIXELS:
                raise ImageError("PNG/JPEG/WebP, 1600만 화소 이하 이미지가 필요합니다.")
            return ImageOps.exif_transpose(opened).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ImageError("이미지 파일을 읽을 수 없습니다.") from None


def encode_png(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def b64_png(image):
    return base64.b64encode(encode_png(image)).decode()


def marker_norm(target_pixel):
    """카카오 마커 화면 위치 → 0~1000 정규화 (x만 좌우 기준으로 쓰고 y는 참고 기록용)."""
    return [round(target_pixel["x"] / target_pixel["width"] * NORM, 1),
            round(target_pixel["y"] / target_pixel["height"] * NORM, 1)]


def norm_to_pixel(point, size):
    """0~1000 정규화 좌표 → 원본 이미지 픽셀 좌표."""
    width, height = size
    x, y = point
    return (min(width - 1, max(0, x / NORM * width)), min(height - 1, max(0, y / NORM * height)))


def region_to_pixel(region, size):
    x1, y1 = norm_to_pixel(region[:2], size)
    x2, y2 = norm_to_pixel(region[2:], size)
    return (x1, y1, x2, y2)


def crop_box(point_px, size, curb_px=None):
    """보도 판독 지점을 중심으로 하되, 바로 앞 연석과 차도 가장자리가 반드시 들어가는 확대 영역."""
    width, height = size
    crop_w = width * CROP_WIDTH_RATIO
    crop_h = height * CROP_HEIGHT_RATIO
    x, y = point_px
    top = y - crop_h * CROP_ABOVE_RATIO
    if curb_px is not None:
        bottom_needed = curb_px[1] + height * CURB_MARGIN_RATIO
        if bottom_needed > top + crop_h:
            top = bottom_needed - crop_h
    left = min(max(0, x - crop_w / 2), max(0, width - crop_w))
    top = min(max(0, top), max(0, height - crop_h))
    return (int(left), int(top), int(min(width, left + crop_w)), int(min(height, top + crop_h)))


def _draw_snap(draw, x, y, scale):
    r = 16 * scale
    arm = 34 * scale
    for color, w in ((SNAP_OUTLINE, int(6 * scale) + 2), (SNAP_COLOR, max(2, int(3 * scale)))):
        draw.ellipse((x - r, y - r, x + r, y + r), outline=color, width=w)
        draw.line((x - arm, y, x - r * 0.5, y), fill=color, width=w)
        draw.line((x + r * 0.5, y, x + arm, y), fill=color, width=w)
        draw.line((x, y - arm, x, y - r * 0.5), fill=color, width=w)
        draw.line((x, y + r * 0.5, x, y + arm), fill=color, width=w)


def _draw_curb(draw, region, scale):
    draw.rectangle(region, outline=SNAP_OUTLINE, width=max(3, int(4 * scale)))
    draw.rectangle(region, outline=CURB_COLOR, width=max(2, int(2 * scale)))


def annotate_full(image, point_px, curb_region_px=None):
    """전체 이미지: 보도 판독 지점(하늘색 원+십자선)과 바로 앞 연석 영역(주황색 사각형)."""
    from PIL import ImageDraw
    marked = image.copy()
    draw = ImageDraw.Draw(marked)
    scale = max(1.0, image.width / 1280)
    if curb_region_px is not None:
        _draw_curb(draw, curb_region_px, scale)
    _draw_snap(draw, *point_px, scale)
    return marked


def detail_crop(image, point_px, curb_px=None, curb_region_px=None):
    """보도 판독 지점 중심 확대 이미지와 그 원본 내 영역. 확대 이미지에도 같은 표시를 합니다."""
    from PIL import Image, ImageDraw
    box = crop_box(point_px, image.size, curb_px)
    crop = image.crop(box)
    factor = max(1.0, DETAIL_MIN_WIDTH / max(1, crop.width))
    if factor > 1:
        crop = crop.resize((round(crop.width * factor), round(crop.height * factor)), Image.LANCZOS)
    draw = ImageDraw.Draw(crop)
    local = lambda x, y: ((x - box[0]) * factor, (y - box[1]) * factor)
    if curb_region_px is not None:
        _draw_curb(draw, (*local(*curb_region_px[:2]), *local(*curb_region_px[2:])), max(1.0, factor * 0.8))
    _draw_snap(draw, *local(*point_px), max(1.0, factor * 0.8))
    return crop, box


# 연석 띠: 규제선 종류(실선/점선)는 선이 끊기는지를 화면 좌우 끝까지 따라가야 알 수 있으므로
# 연석 높이를 중심으로 이미지 전체 너비의 가로 띠를 잘라 키웁니다.
STRIP_ABOVE_RATIO = 0.07        # 연석 위(보도 쪽) 높이, 이미지 높이 대비
STRIP_BELOW_RATIO = 0.10        # 연석 아래(차도 쪽) 높이
STRIP_MIN_WIDTH = 1600


def curb_strip(image, curb_px, reading_px=None):
    """연석을 따라 이미지 전체 너비로 자른 띠 이미지와 원본 내 영역. 판독 지점 위치에 하늘색 세로 표시."""
    from PIL import Image, ImageDraw
    width, height = image.size
    y = curb_px[1]
    top = max(0, int(y - height * STRIP_ABOVE_RATIO))
    bottom = min(height, int(y + height * STRIP_BELOW_RATIO))
    if bottom - top < 8:
        top, bottom = max(0, bottom - 8), min(height, top + 8)
    box = (0, top, width, bottom)
    strip = image.crop(box)
    factor = max(1.0, STRIP_MIN_WIDTH / max(1, strip.width))
    if factor > 1:
        strip = strip.resize((round(strip.width * factor), round(strip.height * factor)), Image.LANCZOS)
    x = (reading_px or curb_px)[0] * factor
    draw = ImageDraw.Draw(strip)
    for color, w in ((SNAP_OUTLINE, 6), (SNAP_COLOR, 3)):
        draw.line((x, 0, x, strip.height * 0.25), fill=color, width=w)
    return strip, box


SEARCH_COLOR = (255, 220, 0)    # 계산한 탐색 상자: 노란 점선 (1차 판독 입력에만 그림)


def _dashed_rect(draw, box, color, width, dash=14, gap=9):
    x1, y1, x2, y2 = box
    for (ax, ay, bx, by) in ((x1, y1, x2, y1), (x2, y1, x2, y2), (x2, y2, x1, y2), (x1, y2, x1, y1)):
        length = max(abs(bx - ax), abs(by - ay))
        steps = max(1, int(length // (dash + gap)) + 1)
        for i in range(steps):
            s = i * (dash + gap) / max(length, 1)
            e = min(1.0, (i * (dash + gap) + dash) / max(length, 1))
            if s >= 1:
                break
            draw.line((ax + (bx - ax) * s, ay + (by - ay) * s, ax + (bx - ax) * e, ay + (by - ay) * e),
                      fill=color, width=width)


def annotate_search(image, box_px, predicted_px=None):
    """1차 판독 입력: 계산한 탐색 상자(노란 점선)와 예상 지점(작은 노란 +)."""
    from PIL import ImageDraw
    marked = image.copy()
    draw = ImageDraw.Draw(marked)
    scale = max(1.0, image.width / 1280)
    _dashed_rect(draw, box_px, SNAP_OUTLINE, int(5 * scale))
    _dashed_rect(draw, box_px, SEARCH_COLOR, int(3 * scale))
    if predicted_px is not None:
        x, y = predicted_px
        a = 9 * scale
        for color, w in ((SNAP_OUTLINE, int(5 * scale)), (SEARCH_COLOR, int(2 * scale))):
            draw.line((x - a, y, x + a, y), fill=color, width=w)
            draw.line((x, y - a, x, y + a), fill=color, width=w)
    return marked
