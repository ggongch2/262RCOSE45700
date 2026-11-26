"""테스트 공용: 가짜 후보·캡처·이미지·Qwen 응답 (대형 모델을 로드하지 않음)."""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CANDIDATE = {"id": "P0001", "coordinates": [127.0276, 37.4979], "heading": 90}


def capture(**overrides):
    value = {"status": "ready", "pano_id": "1234567", "stage": "정면 로드뷰 준비 완료",
             "camera": [127.02750, 37.4979], "query_heading": 90,
             "viewpoint": {"pan": 90.0, "tilt": 0.0, "zoom": 0},
             "target_bearing": 95.0, "view_mode": "curb-front", "target_marker": True,
             "target_pixel": {"x": 640.0, "y": 260.0, "width": 1280, "height": 720}}
    value.update(overrides)
    return value


def png_bytes(size=(1280, 720)):
    from PIL import Image, ImageDraw
    image = Image.new("RGB", size, (120, 120, 120))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 500, size[0], size[1]), fill=(60, 60, 60))      # 차도
    draw.rectangle((0, 480, size[0], 500), fill=(200, 200, 200))       # 연석
    draw.rectangle((0, 300, size[0], 480), fill=(170, 150, 130))       # 보도
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def localization_json(**overrides):
    value = {"sidewalk_status": "visible", "sidewalk_point": [500, 600], "sidewalk_region": [380, 540, 640, 660],
             "curb_point": [500, 680], "curb_region": [360, 640, 680, 720],
             "layer_order": ["road", "curb", "sidewalk", "property_fence", "building"],
             "evidence": "화면 가운데 앞쪽 연석 바로 뒤 회색 보도 바닥"}
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False)


def assessment(**overrides):
    value = {
        "sidewalk": {"value": True, "evidence": "표시 지점 뒤에 보도가 연결됨"},
        "fixed_obstruction": {"value": False, "type": "none", "relation": "none", "opening_at_target": True,
                              "evidence": "표시 지점의 차도와 보도 사이에 고정 차단물이 없음"},
        "vehicle_entrance": {"value": False, "evidence": "차량 출입구와 겹치지 않음"},
        "curb_line": {"value": "none", "evidence": "도로 가장자리 규제선 없음"},
        "stop_exception": {"value": None, "evidence": "판독 대상 아님"},
    }
    for key, item in overrides.items():
        value[key] = {**value.get(key, {}), **item} if isinstance(item, dict) else item
    return value


def stop_check(**overrides):
    value = {
        "sidewalk": {"value": "present", "evidence": "차도 다음에 회색 보도 바닥이 이어짐"},
        "barrier": {"value": False, "type": "none", "opening": None, "evidence": "차도와 보도 사이에 막는 시설 없음"},
        "parking_entrance": {"value": False, "evidence": "차량 출입구 없음"},
        "curb_line": {"value": "none", "evidence": "연석 앞 선 없음"},
        "temporary_occupancy": {"value": False, "evidence": "주차 차량 없음"},
    }
    for key, item in overrides.items():
        value[key] = {**value.get(key, {}), **item} if isinstance(item, dict) else item
    return value


def stop_check_json(**overrides):
    return json.dumps(stop_check(**overrides), ensure_ascii=False)


def assessment_json(**overrides):
    return json.dumps(assessment(**overrides), ensure_ascii=False)


class FakeRunner:
    """generate() 호출을 기록하고 미리 준비한 응답을 순서대로 반환."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, images, question, max_new_tokens, progress=None):
        self.calls.append({"images": images, "question": question, "max_new_tokens": max_new_tokens,
                           "thinking": False})
        return self.responses.pop(0)

    def generate_thinking(self, images, question, answer_tokens, progress=None):
        self.calls.append({"images": images, "question": question, "max_new_tokens": answer_tokens,
                           "thinking": True})
        response = self.responses.pop(0)
        if isinstance(response, dict):
            return response
        return {"thinking": "가짜 생각", "answer": response, "cut": False, "tokens": 10}

    def info(self):
        return {"model_id": "Qwen/Qwen3.5-9B", "model_path": "/models/Qwen3.5-9B",
                "dtype": "4bit nf4 (계산 float16)", "attn_implementation": "sdpa", "loaded": True}
