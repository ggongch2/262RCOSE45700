"""순서형 정차 판독: 보도 → 고정 장애물 → 주차장 입구 → 차선."""
import io
import json
import unittest

import support  # noqa: F401
from support import FakeRunner, capture, png_bytes, stop_check, stop_check_json

from pudo3.stop_check import decide_stop, parse_stop_check, stop_question
from pudo3.vlm_inference import run_stop_check
from pudo3.vlm_policy import AssessmentError


def judge(**overrides):
    return decide_stop(parse_stop_check(stop_check_json(**overrides)))


class DecideOrderTest(unittest.TestCase):
    def test_all_clear_is_possible(self):
        result = judge()
        self.assertEqual(result["verdict"], "가능")
        self.assertEqual([c["status"] for c in result["conditions"]], ["pass"] * 4)

    def test_no_sidewalk_is_impossible(self):
        result = judge(sidewalk={"value": "absent", "evidence": "차도 옆이 바로 벽"})
        self.assertEqual(result["verdict"], "불가")
        self.assertEqual(result["reason"], "불가: 후보 앞에 사람이 설 수 있는 보도가 없음")

    def test_sidewalk_hidden_is_undecidable_not_absent(self):
        result = judge(sidewalk={"value": "not_visible", "evidence": "트럭이 가림"})
        self.assertEqual(result["verdict"], "판단 불가")
        self.assertIn("보도 없음과 다름", result["reason"])

    def test_barrier_without_opening_is_impossible(self):
        for kind, name in (("metal_railing", "금속 난간"), ("fence", "펜스"), ("planter", "화단(관목 띠)"),
                           ("wall", "벽"), ("guardrail", "가드레일")):
            result = judge(barrier={"value": True, "type": kind, "opening": False, "evidence": "연석 따라 이어짐"})
            self.assertEqual(result["verdict"], "불가", kind)
            self.assertIn(name, result["reason"])
            self.assertIn("개구부가 없음", result["reason"])

    def test_barrier_with_opening_passes(self):
        result = judge(barrier={"value": True, "type": "fence", "opening": True, "evidence": "펜스 사이 출입구"})
        self.assertEqual(result["verdict"], "가능")
        self.assertTrue(any("개구부" in n["note"] for n in result["notes"]))

    def test_barrier_opening_unknown_is_undecidable(self):
        result = judge(barrier={"value": True, "type": "metal_railing", "opening": None, "evidence": "난간"})
        self.assertEqual(result["verdict"], "판단 불가")
        self.assertIn("개구부가 있는지", result["reason"])

    def test_spaced_trees_poles_bollards_and_cars_are_not_barriers(self):
        for kind in ("tree", "street_tree", "pole", "bollard", "car", "person", "cone"):
            result = judge(barrier={"value": True, "type": kind, "opening": False, "evidence": kind})
            self.assertEqual(result["verdict"], "가능", kind)
        self.assertIn("띄엄띄엄 선 가로수", stop_question())

    def test_parking_entrance_is_impossible(self):
        result = judge(parking_entrance={"value": True, "evidence": "주차장 입구 차단기"})
        self.assertEqual(result["verdict"], "불가")
        self.assertEqual(result["reason"], "불가: 후보 앞이 주차장·건물 차량 출입구임")
        unknown = judge(parking_entrance={"value": None, "evidence": "가림"})
        self.assertEqual(unknown["verdict"], "판단 불가")

    def test_lines(self):
        for line, verdict in (("yellow_solid", "불가"), ("yellow_double", "불가"), ("yellow_dashed", "가능"),
                              ("none", "가능"), ("white", "가능")):
            self.assertEqual(judge(curb_line={"value": line, "evidence": "선"})["verdict"], verdict, line)
        self.assertEqual(judge(curb_line={"value": None, "evidence": "가림"})["verdict"], "판단 불가")
        hidden = decide_stop(parse_stop_check(stop_check_json(curb_line={"value": None, "evidence": "가림"})),
                             curb_line_required=False)
        self.assertEqual(hidden["verdict"], "가능")

    def test_reason_follows_order(self):
        """불가 사유가 여러 개면 앞 순서(보도 → 장애물 → 입구 → 차선)가 먼저."""
        result = judge(barrier={"value": True, "type": "wall", "opening": False, "evidence": "벽"},
                       parking_entrance={"value": True, "evidence": "입구"},
                       curb_line={"value": "yellow_solid", "evidence": "실선"})
        self.assertIn("벽", result["reason"])
        self.assertEqual([f["field"] for f in result["fail_reasons"]], ["barrier", "parking_entrance", "curb_line"])

    def test_impossible_beats_undecidable(self):
        result = judge(sidewalk={"value": "not_visible", "evidence": "가림"},
                       curb_line={"value": "yellow_solid", "evidence": "실선"})
        self.assertEqual(result["verdict"], "불가")


class ParseTest(unittest.TestCase):
    def test_aliases(self):
        data = stop_check(sidewalk={"value": True, "evidence": "보도"},
                          barrier={"value": "true", "type": "hedge", "opening": "false", "evidence": "관목"})
        check = parse_stop_check(json.dumps(data, ensure_ascii=False))
        self.assertEqual(check["sidewalk"]["value"], "present")
        self.assertEqual(check["barrier"]["type"], "planter")
        self.assertIs(check["barrier"]["opening"], False)

    def test_invalid(self):
        with self.assertRaises(AssessmentError):
            parse_stop_check(stop_check_json(sidewalk={"value": "maybe", "evidence": "?"}))
        with self.assertRaises(AssessmentError):
            parse_stop_check("JSON 아님")

    def test_question_has_order_and_no_point(self):
        q = stop_question()
        for word in ("sidewalk", "barrier", "parking_entrance", "curb_line"):
            self.assertIn(word, q)
        self.assertLess(q.index("1. sidewalk"), q.index("2. barrier"))
        self.assertLess(q.index("2. barrier"), q.index("3. parking_entrance"))
        self.assertLess(q.index("3. parking_entrance"), q.index("4. curb_line"))
        self.assertNotIn("sidewalk_point", q)
        self.assertNotIn("상자", q, "모델 입력에 상자를 그리지 않으므로 질문에도 없음")
        self.assertIn("사진 가운데", q)


class RunStopCheckTest(unittest.TestCase):
    def test_single_thinking_call_and_color_measure(self):
        from pudo3.geometry import search_box
        meta = capture()
        from support import CANDIDATE
        search = search_box(meta["camera"], CANDIDATE, meta["viewpoint"]["pan"])
        runner = FakeRunner(stop_check_json(curb_line={"value": "yellow_solid", "evidence": "모델은 실선"}))
        out = run_stop_check(runner, png_bytes(), meta, search=search)
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(runner.calls[0]["thinking"])
        self.assertEqual(len(runner.calls[0]["images"]), 1)
        from PIL import Image
        sent = runner.calls[0]["images"][0]
        self.assertEqual(sent.tobytes(), Image.open(io.BytesIO(png_bytes())).convert("RGB").tobytes(),
                         "원본 캡처를 그대로 넣음 (상자 표시 없음)")
        if search["valid"]:
            # 테스트 사진에는 노란 칠이 없음 → 색 측정 '규제선 없음'이 모델의 '실선'을 대신함
            self.assertEqual(out["curb_line_measure"]["value"], "none")
            self.assertEqual(out["decision"]["verdict"], "가능")
        self.assertEqual(out["mode"], "stop_check")

    def test_invalid_search_is_undecidable_without_model(self):
        runner = FakeRunner()
        out = run_stop_check(runner, png_bytes(), capture(), search={"valid": False, "reason": "화면 밖"})
        self.assertEqual(out["decision"]["verdict"], "판단 불가")
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
