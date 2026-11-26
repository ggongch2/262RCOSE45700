"""Qwen3.5 로컬 실행기(4bit)와 2단계(보도 위치 → 승하차 조건) 판독.

- 모델은 로컬 경로에서 local_files_only=True로만 읽습니다. 자동 다운로드하지 않습니다.
- Tesla T4: bitsandbytes 4bit(NF4, 계산 FP16), 단일 GPU(device_map={"": 0}), SDPA 우선 → 미지원 시 eager 폴백.
  이미지 입력부(visual)와 lm_head는 양자화하지 않고 FP16으로 둡니다.
- 1차(보도 위치)는 생각 끔(enable_thinking=False) → 바로 JSON.
- 2차(관계 추론)는 생각 켬(enable_thinking=True) → <think>…</think> 뒤의 JSON만 판독에 씁니다.
  생각이 THINKING_BUDGET 토큰을 넘으면 생각을 닫는 문장을 붙여 답(JSON)을 쓰게 합니다.
- 최초 요청 때 지연 로딩하고 이후 재사용합니다. GPU 판독은 lock으로 직렬화합니다.
- 별도 분할·탐지 모델 없이 같은 모델을 두 번 호출합니다.
"""
import threading
import time
from pathlib import Path

from .config import DEFAULT_MODEL_ID, require_vision_model
from .curb_paint import apply_measure, measure_curb_paint
from .geometry import curb_band
from .stop_check import decide_stop, parse_stop_check, stop_question
from .photo_decision import decide, undecidable
from .vision import (annotate_full, annotate_search, curb_strip, decode_rgb, marker_norm, norm_to_pixel,
                     region_to_pixel)
from .vlm_policy import (AssessmentError, LocalizationError, VLMError, assessment_question,
                         localization_question, parse_assessment, parse_localization)

LOCALIZATION_MAX_TOKENS = 640
ASSESSMENT_MAX_TOKENS = 1024      # 2차 답(JSON) 길이 (생각 제외)
STOP_CHECK_MAX_TOKENS = 1024      # 순서형 정차 판독 답(JSON) 길이 (생각 제외)
THINKING_BUDGET = 2048            # 2차 생각 최대 길이. 넘으면 생각을 닫고 답을 쓰게 함
MAX_FULL_SIDE = 1280
THINK_END = "</think>"
FORCE_CLOSE = ("\n\nConsidering the limited time, I have to give the solution based on the thinking directly now."
               "\n</think>\n\n")
QUANT = {"load_in_4bit": True, "bnb_4bit_quant_type": "nf4", "bnb_4bit_use_double_quant": True,
         "llm_int8_skip_modules": ["visual", "lm_head"]}


PROGRESS_EVERY = 256              # 생성 중 이 토큰 수마다 터미널에 진행 표시


def _quiet(message):
    pass


class TokenProgress:
    """generate(streamer=...)용: 새 토큰 수를 세어 PROGRESS_EVERY마다 진행을 알립니다 (첫 put은 프롬프트라 제외)."""

    def __init__(self, progress, label, start=0):
        self.progress, self.label, self.count = progress, label, start
        self.prompt_seen = False

    def put(self, value):
        if not self.prompt_seen:
            self.prompt_seen = True
            return
        before = self.count
        self.count += int(value.numel()) if hasattr(value, "numel") else 1
        if self.count // PROGRESS_EVERY > before // PROGRESS_EVERY:
            self.progress(f"{self.label} 생성 중… {self.count}토큰")

    def end(self):
        pass


def build_messages(images, question):
    """Qwen 채팅 형식: 이미지(PIL RGB) 항목들 + 질문 텍스트. None 이미지는 뺍니다."""
    content = [{"type": "image", "image": image} for image in images if image is not None]
    content.append({"type": "text", "text": question})
    return [{"role": "user", "content": content}]


def decode_new_tokens(processor, input_ids, generated_ids):
    """generate() 결과에서 입력 프롬프트 토큰을 잘라내고 새로 생성된 토큰만 디코딩합니다."""
    trimmed = [output[len(prompt):] for prompt, output in zip(input_ids, generated_ids)]
    return processor.batch_decode(trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]


def split_thinking(text):
    """'생각…</think>답' → (생각, 답). </think>가 없으면 생각이 끝나지 않은 것: (전체, None)."""
    if THINK_END not in text:
        return text.replace("<think>", "").strip(), None
    thinking, answer = text.rsplit(THINK_END, 1)
    return thinking.replace("<think>", "").strip(), answer.strip()


def strip_thinking(text):
    """생각 끔 응답에 혹시 남은 <think>…</think>를 떼고 답만 돌려줍니다."""
    return text.rsplit(THINK_END, 1)[1].strip() if THINK_END in text else text


def dependency_message(error):
    """transformers/torch/bitsandbytes import 실패 → 원인과 해결 명령을 담은 메시지 (설치는 하지 않음)."""
    from importlib import metadata

    def version(name):
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            return None

    tf = version("transformers")
    if tf is None:
        return "transformers가 설치되어 있지 않습니다. 해결: python -m pip install -r pudo3/requirements-vlm.txt"
    if int(tf.split(".")[0]) < 5:
        return (f"Qwen3.5에는 transformers 5 이상이 필요한데 {tf}가 설치되어 있습니다. "
                "pudo3 전용 가상환경에서 python -m pip install -r pudo3/requirements-vlm.txt")
    if version("bitsandbytes") is None:
        return "4bit 실행에 필요한 bitsandbytes가 없습니다. 해결: python -m pip install bitsandbytes"
    return f"Qwen3.5 실행 패키지를 불러오지 못했습니다 ({type(error).__name__}: {error}). pudo3/requirements-vlm.txt를 확인하세요."


def check_dependencies():
    """서버 시작 시 미리 확인. 모델은 로드하지 않습니다."""
    try:
        import torch  # noqa: F401
        import bitsandbytes  # noqa: F401
        from transformers import AutoModelForImageTextToText, AutoProcessor, BitsAndBytesConfig  # noqa: F401
    except ImportError as error:
        raise VLMError(dependency_message(error)) from None


def check_thinking_switch(model_path):
    """채팅 템플릿이 enable_thinking 끄기/켜기를 지원하는지 (가중치 없이 템플릿만) 확인. 문제 있으면 설명 문자열."""
    from transformers import AutoProcessor
    processor = AutoProcessor.from_pretrained(str(model_path), local_files_only=True)
    messages = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
    off, on = (processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=flag)
               for flag in (False, True))
    if off == on:
        return "이 모델의 채팅 템플릿은 enable_thinking 전환을 지원하지 않습니다 (1차도 생각할 수 있음)."
    return None


class QwenRunner:
    ATTENTION_ORDER = ("sdpa", "eager")

    def __init__(self, model_path, model_id=DEFAULT_MODEL_ID, log=print):
        self.model_path = Path(model_path)
        self.model_id = model_id
        self.model = self.processor = None
        self.attn_implementation = None
        self.gpu_lock = threading.Lock()
        self.log = log

    @property
    def loaded(self):
        return self.model is not None

    def info(self):
        return {"model_id": self.model_id, "model_path": str(self.model_path), "loaded": self.loaded,
                "dtype": "4bit nf4 (계산 float16)", "attn_implementation": self.attn_implementation,
                "thinking": {"localization": False, "assessment": True, "budget_tokens": THINKING_BUDGET}}

    def _load_model(self, transformers, torch):
        quant = transformers.BitsAndBytesConfig(bnb_4bit_compute_dtype=torch.float16, **QUANT)
        errors = []
        for attn in self.ATTENTION_ORDER:
            try:
                model = transformers.AutoModelForImageTextToText.from_pretrained(
                    str(self.model_path), local_files_only=True, dtype=torch.float16,
                    quantization_config=quant, device_map={"": 0}, attn_implementation=attn)
            except (ValueError, ImportError, NotImplementedError) as error:
                errors.append(f"{attn}: {type(error).__name__}")
                self.log(f"Qwen3.5 attn_implementation='{attn}' 사용 불가 ({type(error).__name__}: {error}). "
                         "다음 방식으로 폴백합니다.", flush=True)
                continue
            self.attn_implementation = attn
            return model
        raise VLMError("Qwen3.5 모델을 sdpa·eager 어느 방식으로도 불러오지 못했습니다: " + ", ".join(errors))

    def load(self):
        """지연 로딩. 모델 폴더가 없으면 다운로드하지 않고 명확한 오류를 냅니다."""
        if self.model is not None:
            return
        require_vision_model(self.model_path)
        try:
            import torch
            import transformers
            from transformers import AutoModelForImageTextToText, AutoProcessor, BitsAndBytesConfig  # noqa: F401
        except ImportError as error:
            raise VLMError(dependency_message(error)) from None
        if not torch.cuda.is_available():
            raise VLMError("PyTorch에서 GPU를 사용할 수 없습니다.")
        self.log(f"Qwen3.5 로딩: {self.model_path} (4bit NF4, 단일 GPU)", flush=True)
        processor = AutoProcessor.from_pretrained(str(self.model_path), local_files_only=True)
        model = self._load_model(transformers, torch)
        model.eval()
        self.processor, self.model = processor, model
        used = torch.cuda.memory_allocated(0) / 1e9 if hasattr(torch.cuda, "memory_allocated") else None
        self.log(f"Qwen3.5 로딩 완료 (attn={self.attn_implementation}"
                 + (f", GPU 사용 {used:.1f}GB)" if used is not None else ")"), flush=True)

    def _inputs(self, images, question, thinking):
        inputs = self.processor.apply_chat_template(
            build_messages(images, question), tokenize=True, add_generation_prompt=True, return_dict=True,
            return_tensors="pt", enable_thinking=thinking)
        return inputs.to(self.model.device)

    def _generate(self, inputs, max_new_tokens, streamer=None):
        extra = {} if streamer is None else {"streamer": streamer}
        return self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False, num_beams=1, **extra)

    def generate(self, images, question, max_new_tokens, progress=None):
        """생각 끔 판독 호출 (1차). 동시 요청은 gpu_lock으로 직렬화합니다."""
        with self.gpu_lock:
            self.load()
            import torch
            inputs = self._inputs(images, question, thinking=False)
            streamer = None if progress is None else TokenProgress(progress, "1차")
            with torch.inference_mode():
                generated = self._generate(inputs, max_new_tokens, streamer)
            return strip_thinking(decode_new_tokens(self.processor, inputs["input_ids"], generated))

    def generate_thinking(self, images, question, answer_tokens, thinking_budget=THINKING_BUDGET, progress=None):
        """생각 켬 판독 호출 (2차). 반환 {"thinking", "answer", "cut", "tokens"}.

        1) 생각+답을 thinking_budget 토큰까지 생성. 그 안에 끝나면 완료.
        2) 덜 끝났으면 이어서 answer_tokens만큼 더 생성. 생각이 안 닫혔으면(cut) 생각을 닫는 문장을 붙인 뒤 이어감.
        """
        with self.gpu_lock:
            self.load()
            import torch
            inputs = self._inputs(images, question, thinking=True)
            prompt = inputs["input_ids"]
            prompt_len = len(prompt[0])
            cut = False
            streamer = None if progress is None else TokenProgress(progress, "2차(생각 포함)")
            with torch.inference_mode():
                generated = self._generate(inputs, thinking_budget, streamer)
                new = list(generated[0][prompt_len:])
                if len(new) >= thinking_budget:     # 예산을 다 씀 → 아직 끝나지 않음
                    text = decode_new_tokens(self.processor, prompt, generated)
                    sequence = generated
                    if THINK_END not in text:
                        cut = True
                        if progress is not None:
                            progress(f"2차 생각이 {thinking_budget}토큰 상한에 닿아 생각을 닫고 답을 쓰게 함")
                        close = self.processor.tokenizer(FORCE_CLOSE, add_special_tokens=False,
                                                         return_tensors="pt")["input_ids"].to(generated.device)
                        sequence = torch.cat([generated, close], dim=1)
                    more = dict(inputs)
                    extra = sequence.shape[1] - prompt.shape[1]
                    for key, value in inputs.items():     # 토큰 길이에 맞춰야 하는 보조 입력 (예: mm_token_type_ids)
                        if (key not in ("input_ids", "attention_mask") and hasattr(value, "shape")
                                and tuple(value.shape) == tuple(prompt.shape)):
                            more[key] = torch.cat([value, value.new_zeros((value.shape[0], extra))], dim=1)
                    more["input_ids"] = sequence
                    more["attention_mask"] = torch.ones_like(sequence)
                    if streamer is not None:
                        streamer = TokenProgress(progress, "2차(답 이어쓰기)", start=len(new))
                    generated = self._generate(more, answer_tokens, streamer)
                    new = list(generated[0][prompt_len:])
            text = decode_new_tokens(self.processor, prompt, generated)
            thinking, answer = split_thinking(text)
            return {"thinking": thinking, "answer": answer if answer is not None else "", "cut": cut,
                    "tokens": len(new)}


def _fit(image):
    if max(image.size) > MAX_FULL_SIDE:
        image = image.copy()
        image.thumbnail((MAX_FULL_SIDE, MAX_FULL_SIDE))
    return image


# 연석이 안 보일 때 연석 띠 높이를 보도 지점 아래 이만큼(이미지 높이 대비)으로 잡음
CURB_GUESS_BELOW_RATIO = 0.06


def _empty_result(image, marker, search):
    return {"marker_norm": marker, "search": search, "reading_source": "auto",
            "localization": None, "localization_raw": None, "localization_error": None,
            "assessment": None, "assessment_raw": None, "assessment_error": None,
            "snapped_curb_point": None, "snapped_curb_pixel": None, "curb_region_pixel": None,
            "snapped_sidewalk_point": None, "snapped_sidewalk_pixel": None, "sidewalk_region_pixel": None,
            "reading_point": None, "reading_pixel": None, "curb_strip_box": None,
            "detail_crop_box": None, "image_size": list(image.size), "images": {"original": image},
            "assessment_thinking": None, "assessment_thinking_cut": None, "curb_line_measure": None,
            "timing": {"localization_s": None, "assessment_s": None}}


def _timed(out, key, call, progress=_quiet, label=""):
    started = time.monotonic()
    progress(f"{label} 시작")
    try:
        return call()
    finally:
        out["timing"][key] = round(time.monotonic() - started, 1)
        progress(f"{label} 끝 ({out['timing'][key]}초)")


def run_locate(runner, image_bytes, capture, *, search=None, progress=None):
    """위치 찾기만: 1차 보도 판독 지점 탐색까지 하고 끝냅니다 (2차 판독·가능/불가 판정 없음)."""
    image = _fit(decode_rgb(image_bytes))
    target_pixel = capture.get("target_pixel")
    out = _empty_result(image, marker_norm(target_pixel) if target_pixel else None, search)
    out["mode"] = "locate"
    if search is not None and not search.get("valid"):
        out["localization_error"] = f"후보 보도 위치를 계산하지 못함 — {search.get('reason')}"
        return out
    box = search["box"] if search else None
    stage1_image = image
    if box is not None:
        stage1_image = annotate_search(image, region_to_pixel(box, image.size))   # 예상 지점 +는 그리지 않음
        out["images"]["search"] = stage1_image
    progress = progress or _quiet
    raw = _timed(out, "localization_s",
                 lambda: runner.generate([stage1_image], localization_question(box), LOCALIZATION_MAX_TOKENS,
                                         progress=progress), progress, "1차 보도 위치 찾기 (생각 끔)")
    out["localization_raw"] = raw
    try:
        localization = parse_localization(raw, box=box)
    except LocalizationError as error:
        out["localization_error"] = f"위치 응답 검증 실패: {error}"
        return out
    out["localization"] = localization
    if not localization["target_visible"]:
        return out
    reading_px = norm_to_pixel(localization["reading_point"], image.size)
    out.update(snapped_sidewalk_point=localization["sidewalk_point"],
               snapped_sidewalk_pixel=[round(reading_px[0], 1), round(reading_px[1], 1)],
               sidewalk_region_pixel=[round(v, 1) for v in region_to_pixel(localization["sidewalk_region"], image.size)],
               reading_point=localization["reading_point"],
               reading_pixel=[round(reading_px[0], 1), round(reading_px[1], 1)])
    if localization["curb_point"] is not None:
        curb_px = norm_to_pixel(localization["curb_point"], image.size)
        out.update(snapped_curb_point=localization["curb_point"],
                   snapped_curb_pixel=[round(curb_px[0], 1), round(curb_px[1], 1)],
                   curb_region_pixel=[round(v, 1) for v in region_to_pixel(localization["curb_region"], image.size)])
    out["images"]["located"] = annotate_full(image, reading_px, None if localization["curb_point"] is None else
                                             region_to_pixel(localization["curb_region"], image.size))
    return out


def run_two_stage(runner, image_bytes, capture, *, curb_line_required=True, search=None, progress=None):
    """1차 보도 판독 지점 탐색 → 표시 이미지 생성 → 2차 관계 기반 조건 판독 → Python 판정.

    search: geometry.search_box 결과. valid이면 계산한 탐색 상자 안에서 보도를 찾고,
            없으면 화면 가운데 기준으로 찾습니다. 계산상 후보 보도가 화면 밖이면 모델 없이 '판단 불가'.
    카카오 마커는 판독 기준으로 쓰지 않습니다(있으면 참고 기록만).
    보도 없음(absent)이면 2차 없이 '불가', 가려짐(occluded)이면 '판단 불가'.
    """
    image = _fit(decode_rgb(image_bytes))
    target_pixel = capture.get("target_pixel")
    marker = marker_norm(target_pixel) if target_pixel else None
    out = _empty_result(image, marker, search)
    if search is not None and not search.get("valid"):
        out["decision"] = undecidable("curb_localization", f"후보 보도 위치를 계산하지 못함 — {search.get('reason')}")
        return out

    box = search["box"] if search else None
    stage1_image = image
    if box is not None:
        stage1_image = annotate_search(image, region_to_pixel(box, image.size))
        out["images"]["search"] = stage1_image
    progress = progress or _quiet
    raw = _timed(out, "localization_s",
                 lambda: runner.generate([stage1_image], localization_question(box), LOCALIZATION_MAX_TOKENS,
                                         progress=progress), progress, "1차 보도 위치 찾기 (생각 끔)")
    out["localization_raw"] = raw
    try:
        localization = parse_localization(raw, box=box)
    except LocalizationError as error:
        progress(f"1차 응답 검증 실패: {error}")
        out["localization_error"] = str(error)
        out["decision"] = undecidable("curb_localization", f"보도 판독 위치를 찾지 못함 — 위치 응답 검증 실패: {error}")
        return out
    out["localization"] = localization
    progress(f"1차 결과: {localization['sidewalk_status']}"
             + (f" 지점 {[round(v) for v in localization['reading_point']]}" if localization.get("reading_point") else ""))
    if not localization["target_visible"]:
        out["decision"] = decide(localization, None)
        progress("보도가 보이지 않아 2차 없이 판정")
        return out
    return _assess(runner, image, localization, out, curb_line_required, progress)


def run_stop_check(runner, image_bytes, capture, *, curb_line_required=True, search=None, progress=None):
    """순서형 정차 판독 (점 찍기 없음): 원본 캡처 사진 1장 그대로 → 모델 1회(생각 켬) → Python 순서 판정.

    search(지도 계산)는 모델 입력에 그리지 않고, 판독 가능 여부 확인과 차선 색 측정 높이 계산에만 씁니다.

    1 보도 → 2 차도–보도 사이 고정 장애물(개구부) → 3 주차장 입구 → 4 연석 앞 차선(색 측정 우선).
    """
    progress = progress or _quiet
    image = _fit(decode_rgb(image_bytes))
    target_pixel = capture.get("target_pixel")
    out = _empty_result(image, marker_norm(target_pixel) if target_pixel else None, search)
    out["mode"] = "stop_check"
    if search is not None and not search.get("valid"):
        out["decision"] = undecidable("curb_localization", f"후보 앞 판독 범위를 계산하지 못함 — {search.get('reason')}")
        return out
    result = _timed(out, "assessment_s", lambda: runner.generate_thinking(
        [image], stop_question(), STOP_CHECK_MAX_TOKENS, progress=progress),
        progress, "정차 판독 (보도 → 장애물 → 주차장 입구 → 차선, 생각 켬)")
    progress(f"생성 {result['tokens']}토큰" + (" (생각 상한에서 끊음)" if result["cut"] else ""))
    raw = result["answer"]
    out.update(assessment_raw=raw, assessment_thinking=result["thinking"], assessment_thinking_cut=result["cut"])
    try:
        check = parse_stop_check(raw)
    except AssessmentError as error:
        out["assessment_error"] = str(error)
        out["decision"] = undecidable("assessment", f"판독 JSON 검증 실패: {error}")
        progress(f"판독 응답 검증 실패: {error}")
        return out
    band = curb_band(search, image.size) if search else None
    if band is not None:
        measure = measure_curb_paint(image, band)
        out["curb_line_measure"] = measure
        out["curb_strip_box"] = list(band)
        model_line = check["curb_line"]["value"]
        check = apply_measure(check, measure)
        progress(f"차선 색 측정: {measure['value'] or '판단 보류'}"
                 + (f" (모델 답 {model_line} → 측정값 사용)" if check["curb_line"].get("source") == "color_measure" else ""))
    out["assessment"] = check
    out["decision"] = decide_stop(check, curb_line_required=curb_line_required)
    progress("결과: " + " / ".join(f"{c['label']} {c['value']}" for c in out["decision"]["conditions"]))
    return out


def manual_localization(point):
    """직접 클릭한 보도 지점 (0~1000). 연석은 모름."""
    return {"version": "pudo3-manual-point.v1", "sidewalk_status": "visible", "target_visible": True,
            "sidewalk_point": [float(point[0]), float(point[1])], "sidewalk_region": None,
            "curb_point": None, "curb_region": None, "curb_note": None,
            "reading_point": [float(point[0]), float(point[1])], "center_shift": None,
            "layer_order": [], "evidence": "사용자가 사진에서 직접 지정한 보도 판독 지점"}


def run_manual(runner, image_bytes, point, capture, *, curb_line_required=True, search=None, progress=None):
    """직접 지정한 판독 지점으로 2차 판독만 다시 실행."""
    image = _fit(decode_rgb(image_bytes))
    target_pixel = capture.get("target_pixel")
    out = _empty_result(image, marker_norm(target_pixel) if target_pixel else None, search)
    out["reading_source"] = "manual"
    out["localization"] = manual_localization(point)
    return _assess(runner, image, out["localization"], out, curb_line_required, progress or _quiet)


def _assess(runner, image, localization, out, curb_line_required, progress=_quiet):
    reading_px = norm_to_pixel(localization["reading_point"], image.size)
    curb_px = curb_region_px = None
    if localization["curb_point"] is not None:
        curb_px = norm_to_pixel(localization["curb_point"], image.size)
        curb_region_px = region_to_pixel(localization["curb_region"], image.size)
    strip_anchor = curb_px or (reading_px[0], min(image.height - 1, reading_px[1] + image.height * CURB_GUESS_BELOW_RATIO))
    full_marked = annotate_full(image, reading_px, curb_region_px)
    # 2차 모델 입력은 전체 사진 1장. 연석 띠 영역은 황색 규제선 색 측정(Python)에만 씀 (모델·화면에는 안 넣음)
    _, strip_box = curb_strip(image, strip_anchor, reading_px)
    sidewalk_region = localization.get("sidewalk_region")
    out.update(snapped_curb_point=localization["curb_point"],
               snapped_curb_pixel=None if curb_px is None else [round(curb_px[0], 1), round(curb_px[1], 1)],
               curb_region_pixel=None if curb_region_px is None else [round(v, 1) for v in curb_region_px],
               snapped_sidewalk_point=localization["sidewalk_point"],
               snapped_sidewalk_pixel=[round(reading_px[0], 1), round(reading_px[1], 1)],
               sidewalk_region_pixel=None if sidewalk_region is None else
               [round(v, 1) for v in region_to_pixel(sidewalk_region, image.size)],
               reading_point=localization["reading_point"],
               reading_pixel=[round(reading_px[0], 1), round(reading_px[1], 1)],
               detail_crop_box=None, curb_strip_box=list(strip_box))
    out["images"].update(snapped_full=full_marked)

    result = _timed(out, "assessment_s", lambda: runner.generate_thinking(
        [full_marked], assessment_question(localization, curb_line_required), ASSESSMENT_MAX_TOKENS,
        progress=progress), progress, "2차 조건 판독 (생각 켬)")
    progress(f"2차 생성 {result['tokens']}토큰" + (" (생각 상한에서 끊음)" if result["cut"] else ""))
    raw = result["answer"]
    out.update(assessment_raw=raw, assessment_thinking=result["thinking"], assessment_thinking_cut=result["cut"])
    try:
        assessment = parse_assessment(raw)
    except AssessmentError as error:
        out["assessment_error"] = str(error)
        out["decision"] = undecidable("assessment", f"2차 조건 판독 JSON 검증 실패: {error}")
        return out
    # 황색 규제선은 색으로 직접 측정해서, 분명하면 모델 답 대신 씀 (점선을 실선으로 보는 오판 방지)
    measure = measure_curb_paint(image, strip_box)
    out["curb_line_measure"] = measure
    model_line = assessment["curb_line"]["value"]
    assessment = apply_measure(assessment, measure)
    progress(f"규제선 색 측정: {measure['value'] or '판단 보류'}"
             + (f" (모델 답 {model_line} → 측정값 사용)" if assessment["curb_line"].get("source") == "color_measure" else ""))
    out["assessment"] = assessment
    out["decision"] = decide(localization, assessment, curb_line_required=curb_line_required)
    return out
