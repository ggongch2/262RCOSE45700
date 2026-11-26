"""QwenRunner (Qwen3.5 4bit): 실제 transformers·대형 모델 없이 가짜 모듈로 검증.

생각 켬(2차) 이어쓰기 검사는 CPU torch 텐서를 쓰므로 torch가 없으면 건너뜁니다.
"""
import contextlib
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401

from pudo3.config import (DEFAULT_MODEL_PATH, MODEL_PATH_ENV, ModelNotFoundError, require_model_dir,
                          require_vision_model, resolve_model_path)
from pudo3.vlm_inference import (FORCE_CLOSE, QwenRunner, build_messages, decode_new_tokens, split_thinking,
                                 strip_thinking)

try:
    import torch as real_torch
except ImportError:          # pragma: no cover
    real_torch = None

PROMPT = [101, 102, 103, 104]
TABLE = {7: '{"ok"', 8: ": true}", 50: "생각", 51: "더", 98: "(마무리)", 99: "</think>",
         101: "<프롬프트>", 102: "", 103: "", 104: ""}


def _ids(row):
    return [int(t) for t in row]


class FakeTensor(list):
    pass


class FakeInputs(dict):
    def to(self, device):
        self["moved_to"] = device
        return self


class FakeProcessor:
    def __init__(self, tensor=False):
        self.decoded = None
        self.messages = None
        self.tensor = tensor
        self.template_calls = []
        self.tokenizer = self._tokenize

    def _tokenize(self, text, **kwargs):
        assert text == FORCE_CLOSE
        return {"input_ids": real_torch.tensor([[98, 99]])}

    def apply_chat_template(self, messages, **kwargs):
        self.messages, self.template_kwargs = messages, kwargs
        self.template_calls.append(kwargs)
        if kwargs.get("tokenize") is False:
            return "생각 켬" if kwargs.get("enable_thinking") else "생각 끔"
        if self.tensor:
            ids = real_torch.tensor([PROMPT])
            return FakeInputs(input_ids=ids, attention_mask=real_torch.ones_like(ids),
                              mm_token_type_ids=real_torch.zeros_like(ids), pixel_values="PIX")
        return FakeInputs(input_ids=[list(PROMPT)], attention_mask=[[1, 1, 1, 1]])

    def batch_decode(self, sequences, **kwargs):
        self.decoded = [_ids(s) for s in sequences]
        return ["".join(TABLE.get(t, "?") for t in s) for s in self.decoded]


class FakeModel:
    device = "cuda:0"

    def __init__(self, script=None):
        self.generate_kwargs = None
        self.calls = []
        self.evaluated = False
        self.script = list(script or [])

    def eval(self):
        self.evaluated = True

    def generate(self, **kwargs):
        self.generate_kwargs = kwargs
        self.calls.append(kwargs)
        ids = kwargs["input_ids"]
        if self.script:                         # 텐서 모드: 정해진 토큰을 max_new_tokens까지만 붙임
            new = self.script.pop(0)[:kwargs["max_new_tokens"]]
            return real_torch.cat([ids, real_torch.tensor([new])], dim=1)
        return [ids[0] + [7, 8]]


def fake_modules(fail_sdpa=False, cuda=True, model=None):
    calls = {"model": [], "processor": [], "quant": []}
    model = model or FakeModel()

    class AutoModelForImageTextToText:
        @staticmethod
        def from_pretrained(path, **kwargs):
            calls["model"].append((path, kwargs))
            if fail_sdpa and kwargs["attn_implementation"] == "sdpa":
                raise ValueError("SDPA not supported")
            return model

    class AutoProcessor:
        processor = None

        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls["processor"].append((path, kwargs))
            return cls.processor or FakeProcessor()

    def BitsAndBytesConfig(**kwargs):
        calls["quant"].append(kwargs)
        return ("QUANT", kwargs)

    base = real_torch if real_torch is not None else types.SimpleNamespace()
    torch = types.SimpleNamespace(
        float16="float16", inference_mode=contextlib.nullcontext,
        cat=getattr(base, "cat", None), ones_like=getattr(base, "ones_like", None),
        cuda=types.SimpleNamespace(is_available=lambda: cuda, memory_allocated=lambda i: 0))
    transformers = types.SimpleNamespace(AutoProcessor=AutoProcessor, BitsAndBytesConfig=BitsAndBytesConfig,
                                         AutoModelForImageTextToText=AutoModelForImageTextToText)
    modules = {"torch": torch, "transformers": transformers, "bitsandbytes": types.SimpleNamespace()}
    return modules, calls, model


def model_dir(vision=True):
    folder = tempfile.TemporaryDirectory()
    config = {"model_type": "qwen3_5", "vision_config": {}} if vision else {"model_type": "qwen3_5_text"}
    (Path(folder.name) / "config.json").write_text(json.dumps(config))
    return folder


class ModelPathTest(unittest.TestCase):
    def test_priority_cli_env_default(self):
        env = {MODEL_PATH_ENV: "/env/qwen"}
        self.assertEqual(resolve_model_path("/cli/qwen", env), Path("/cli/qwen"))
        self.assertEqual(resolve_model_path(None, env), Path("/env/qwen"))
        self.assertEqual(resolve_model_path(None, {}), DEFAULT_MODEL_PATH)
        self.assertEqual(DEFAULT_MODEL_PATH.name, "Qwen3.5-9B")

    def test_missing_model_clear_error_without_download(self):
        """모델 경로가 없을 때 자동 다운로드하지 않고 명확한 오류 출력."""
        missing = "/nonexistent/Qwen3.5-9B"
        with self.assertRaises(ModelNotFoundError) as ctx:
            require_model_dir(missing)
        self.assertEqual(str(ctx.exception),
                         f"Qwen3.5 모델을 찾을 수 없습니다: {missing}. 모델을 먼저 다운로드하세요.")
        modules, calls, _ = fake_modules()
        runner = QwenRunner(missing, log=lambda *a, **k: None)
        with patch.dict("sys.modules", modules):
            with self.assertRaises(ModelNotFoundError):
                runner.load()
        self.assertEqual(calls["model"], [], "from_pretrained를 호출하면 안 됨 (다운로드 위험)")
        self.assertEqual(calls["processor"], [])

    def test_text_only_model_rejected(self):
        folder = model_dir(vision=False)
        self.addCleanup(folder.cleanup)
        with self.assertRaises(ModelNotFoundError) as ctx:
            require_vision_model(folder.name)
        self.assertIn("vision_config", str(ctx.exception))

    def test_server_exits_with_message_when_model_missing(self):
        from pudo3 import server
        with patch.dict("os.environ", {MODEL_PATH_ENV: "/nonexistent/qwen"}):
            with self.assertRaises(SystemExit) as ctx:
                server.main(["--vlm", "--no-browser"])
        self.assertIn("Qwen3.5 모델을 찾을 수 없습니다: /nonexistent/qwen. 모델을 먼저 다운로드하세요.",
                      str(ctx.exception.code))

    def test_old_transformers_message(self):
        from importlib import metadata
        from pudo3.vlm_inference import dependency_message
        versions = {"transformers": "4.57.6", "bitsandbytes": "0.45.0"}
        with patch.object(metadata, "version", side_effect=lambda name: versions[name]):
            message = dependency_message(ImportError("x"))
        self.assertIn("transformers 5 이상", message)
        self.assertIn("4.57.6", message)

    def test_no_hub_download_calls_in_source(self):
        source = "".join(Path(support.ROOT, "pudo3", name).read_text(encoding="utf-8")
                         for name in ("vlm_inference.py", "vlm.py", "server.py", "config.py"))
        for forbidden in ("snapshot_download", "hf_hub_download", "trust_remote_code",
                          "get_class_from_dynamic_module", "bfloat16", "flash_attention_2"):
            self.assertNotIn(forbidden, source)


class ThinkingTextTest(unittest.TestCase):
    def test_split_thinking(self):
        self.assertEqual(split_thinking("<think>\n보도 확인\n</think>\n\n{\"a\": 1}"), ("보도 확인", '{"a": 1}'))
        self.assertEqual(split_thinking("보도 확인</think>{}"), ("보도 확인", "{}"))
        self.assertEqual(split_thinking("끝나지 않은 생각"), ("끝나지 않은 생각", None))

    def test_strip_thinking(self):
        self.assertEqual(strip_thinking('<think>\n\n</think>\n\n{"a": 1}'), '{"a": 1}')
        self.assertEqual(strip_thinking('{"a": 1}'), '{"a": 1}')


class TokenProgressTest(unittest.TestCase):
    def test_counts_new_tokens_and_reports_every_256(self):
        from pudo3.vlm_inference import TokenProgress
        lines = []
        counter = TokenProgress(lines.append, "2차")
        counter.put("PROMPT")                 # 첫 put은 프롬프트 → 세지 않음
        for _ in range(600):
            counter.put(7)
        self.assertEqual(counter.count, 600)
        self.assertEqual(lines, ["2차 생성 중… 256토큰", "2차 생성 중… 512토큰"])


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.folder = model_dir()
        self.addCleanup(self.folder.cleanup)
        self.log = []

    def runner(self):
        return QwenRunner(self.folder.name, log=lambda *a, **k: self.log.append(a[0]))

    def test_load_options_4bit_and_lazy_reuse(self):
        modules, calls, model = fake_modules()
        runner = self.runner()
        self.assertFalse(runner.loaded)
        with patch.dict("sys.modules", modules):
            runner.generate(["IMG"], "질문", 32)
            runner.generate(["IMG"], "질문", 32)
        self.assertEqual(len(calls["model"]), 1, "모델은 한 번만 로드")
        path, kwargs = calls["model"][0]
        self.assertEqual(path, self.folder.name)
        quant = kwargs.pop("quantization_config")
        self.assertEqual(kwargs, {"local_files_only": True, "dtype": "float16", "device_map": {"": 0},
                                  "attn_implementation": "sdpa"})
        self.assertEqual(quant[0], "QUANT")
        self.assertTrue(quant[1]["load_in_4bit"])
        self.assertEqual(quant[1]["bnb_4bit_quant_type"], "nf4")
        self.assertEqual(quant[1]["bnb_4bit_compute_dtype"], "float16")
        self.assertIn("visual", quant[1]["llm_int8_skip_modules"])
        self.assertTrue(calls["processor"][0][1]["local_files_only"])
        self.assertTrue(model.evaluated)
        self.assertFalse(model.generate_kwargs["do_sample"])
        self.assertEqual(model.generate_kwargs["num_beams"], 1)

    def test_stage1_thinking_off(self):
        """1차(generate)는 항상 enable_thinking=False."""
        modules, _, _ = fake_modules()
        runner = self.runner()
        with patch.dict("sys.modules", modules):
            runner.generate(["IMG"], "질문", 32)
        self.assertIs(runner.processor.template_kwargs["enable_thinking"], False)

    def test_sdpa_falls_back_to_eager(self):
        modules, calls, _ = fake_modules(fail_sdpa=True)
        runner = self.runner()
        with patch.dict("sys.modules", modules):
            runner.load()
        self.assertEqual([c[1]["attn_implementation"] for c in calls["model"]], ["sdpa", "eager"])
        self.assertEqual(runner.attn_implementation, "eager")
        self.assertTrue(any("폴백" in line for line in self.log))

    def test_decodes_only_new_tokens(self):
        modules, _, _ = fake_modules()
        runner = self.runner()
        with patch.dict("sys.modules", modules):
            text = runner.generate(["IMG"], "질문", 32)
        self.assertEqual(runner.processor.decoded, [[7, 8]])
        self.assertEqual(text, '{"ok": true}')
        processor = FakeProcessor()
        self.assertEqual(decode_new_tokens(processor, [[1, 2, 3]], [[1, 2, 3, 7, 8]]), '{"ok": true}')

    def test_chat_template_message_structure(self):
        modules, _, _ = fake_modules()
        runner = self.runner()
        with patch.dict("sys.modules", modules):
            runner.generate(["FULL", "DETAIL"], "질문", 8)
        content = runner.processor.messages[0]["content"]
        self.assertEqual(runner.processor.messages[0]["role"], "user")
        self.assertEqual([c["type"] for c in content], ["image", "image", "text"])
        self.assertEqual([c.get("image") for c in content[:2]], ["FULL", "DETAIL"])
        self.assertTrue(runner.processor.template_kwargs["add_generation_prompt"])
        self.assertEqual(build_messages(["FULL", None], "q")[0]["content"],
                         [{"type": "image", "image": "FULL"}, {"type": "text", "text": "q"}])

    def test_concurrent_generation_is_serialized(self):
        modules, _, model = fake_modules()
        runner = self.runner()
        active, peak = [0], [0]
        original = model.generate

        def slow_generate(**kwargs):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            threading.Event().wait(0.05)
            active[0] -= 1
            return original(**kwargs)

        model.generate = slow_generate
        with patch.dict("sys.modules", modules):
            threads = [threading.Thread(target=runner.generate, args=(["I"], "q", 4)) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(peak[0], 1)

    def test_gpu_required(self):
        from pudo3.vlm_policy import VLMError
        modules, _, _ = fake_modules(cuda=False)
        with patch.dict("sys.modules", modules):
            with self.assertRaises(VLMError):
                self.runner().load()


@unittest.skipIf(real_torch is None, "torch 없음")
class ThinkingGenerationTest(unittest.TestCase):
    """2차(generate_thinking): 생각 켬, </think> 뒤만 답, 예산 초과 시 생각을 닫고 이어서 답."""

    def setUp(self):
        self.folder = model_dir()
        self.addCleanup(self.folder.cleanup)

    def run_script(self, script, budget=5, answer_tokens=4):
        model = FakeModel(script)
        modules, _, _ = fake_modules(model=model)
        modules["transformers"].AutoProcessor.processor = FakeProcessor(tensor=True)
        modules.pop("torch")                    # 실제 torch(CPU)를 쓰고 GPU 확인만 바꿈
        runner = QwenRunner(self.folder.name, log=lambda *a, **k: None)
        with patch.dict("sys.modules", modules), \
                patch.object(real_torch.cuda, "is_available", return_value=True), \
                patch.object(real_torch.cuda, "memory_allocated", return_value=0):
            result = runner.generate_thinking(["IMG"], "질문", answer_tokens, thinking_budget=budget)
        return runner, model, result

    def test_finishes_within_budget(self):
        runner, model, result = self.run_script([[50, 99, 7, 8]])
        self.assertIs(runner.processor.template_calls[0]["enable_thinking"], True)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(result, {"thinking": "생각", "answer": '{"ok": true}', "cut": False, "tokens": 4})

    def test_answer_truncated_continues(self):
        """생각은 닫혔는데 답이 예산에 걸려 잘림 → 이어서 생성 (생각 닫기 문장 없음)."""
        runner, model, result = self.run_script([[50, 99, 7, 7, 7], [8]], budget=4)
        self.assertEqual(len(model.calls), 2)
        self.assertFalse(result["cut"])
        self.assertEqual(model.calls[1]["max_new_tokens"], 4)
        self.assertEqual(_ids(model.calls[1]["input_ids"][0]), PROMPT + [50, 99, 7, 7])
        self.assertEqual(result["answer"], '{"ok"{"ok": true}')

    def test_thinking_over_budget_forced_close(self):
        """생각이 예산을 넘음 → 생각 닫기 문장(</think>)을 붙이고 답을 이어 씀. 보조 입력 길이도 맞춤."""
        runner, model, result = self.run_script([[50, 51, 50, 51, 50, 51], [7, 8]], budget=4)
        self.assertTrue(result["cut"])
        second = model.calls[1]
        self.assertEqual(_ids(second["input_ids"][0]), PROMPT + [50, 51, 50, 51, 98, 99])
        self.assertEqual(second["attention_mask"].shape, second["input_ids"].shape)
        self.assertEqual(second["mm_token_type_ids"].shape, second["input_ids"].shape)
        self.assertEqual(second["pixel_values"], "PIX")
        self.assertEqual(result["answer"], '{"ok": true}')
        self.assertIn("생각더", result["thinking"])


class TemplateCheckTest(unittest.TestCase):
    def test_thinking_switch_detected(self):
        from pudo3.vlm_inference import check_thinking_switch
        folder = model_dir()
        self.addCleanup(folder.cleanup)
        modules, _, _ = fake_modules()
        with patch.dict("sys.modules", modules):
            self.assertIsNone(check_thinking_switch(folder.name))

            class Same(FakeProcessor):
                def apply_chat_template(self, messages, **kwargs):
                    return "같음"
            modules["transformers"].AutoProcessor.processor = Same()
            self.assertIn("enable_thinking", check_thinking_switch(folder.name))


if __name__ == "__main__":
    unittest.main()
