"""황색 규제선 색 측정, 카카오 화살표 숨김, 2차 판독에 측정값 반영."""
import io
import json
import unittest

import support  # noqa: F401
from support import CANDIDATE, FakeRunner, assessment, capture, localization_json

from pudo3.curb_paint import apply_measure, measure_curb_paint
from pudo3.roadview import HIDE_UI_CSS, viewer_html
from pudo3.vlm_inference import run_two_stage
from pudo3.vlm_policy import parse_assessment

YELLOW = (235, 190, 30)
STRIP = (0, 600, 1280, 720)


def road(draw_line=None):
    """회색 차도 위에 선 그리기. draw_line(draw) 콜백."""
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (1280, 720), (90, 90, 95))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 590, 1280, 610), fill=(190, 190, 190))       # 연석
    if draw_line:
        draw_line(draw)
    return image


def dashed(draw, y=650, segments=((0, 100), (550, 930), (1185, 1280))):
    for x1, x2 in segments:
        draw.rectangle((x1, y, x2, y + 8), fill=YELLOW)


class MeasureTest(unittest.TestCase):
    def test_dashed_line_like_p0453(self):
        """P0453 실제 캡처와 같은 배치: 칠해진 구간 3개 + 차도가 보이는 빈 구간 2개 → 점선."""
        result = measure_curb_paint(road(dashed), STRIP)
        self.assertEqual(result["value"], "yellow_dashed")
        self.assertEqual(len(result["visible_gaps"]), 2)
        self.assertIn("점선", result["evidence"])

    def test_dashed_in_shadow_gap(self):
        """빈 구간이 그림자(어두운 차도)여도 노란색이 없으면 진짜 빈 구간."""
        def line(draw):
            draw.rectangle((150, 620, 540, 700), fill=(35, 35, 40))   # 그림자
            dashed(draw)
        self.assertEqual(measure_curb_paint(road(line), STRIP)["value"], "yellow_dashed")

    def test_solid_line(self):
        result = measure_curb_paint(road(lambda d: d.rectangle((0, 650, 1280, 658), fill=YELLOW)), STRIP)
        self.assertEqual(result["value"], "yellow_solid")

    def test_solid_line_slanted(self):
        result = measure_curb_paint(road(lambda d: d.line((0, 700, 1280, 640), fill=YELLOW, width=8)), STRIP)
        self.assertEqual(result["value"], "yellow_solid")

    def test_double_line(self):
        def line(draw):
            draw.rectangle((0, 645, 1280, 651), fill=YELLOW)
            draw.rectangle((0, 660, 1280, 666), fill=YELLOW)
        self.assertEqual(measure_curb_paint(road(line), STRIP)["value"], "yellow_double")

    def test_gap_hidden_by_white_arrow_is_not_a_break(self):
        """빈 구간이 흰 화살표·흰 페인트로 덮였으면 끊김으로 세지 않음 → 실선도 점선도 단정하지 않음."""
        def line(draw):
            draw.rectangle((0, 650, 1280, 658), fill=YELLOW)
            draw.rectangle((500, 630, 800, 680), fill=(250, 250, 250))
        result = measure_curb_paint(road(line), STRIP)
        self.assertIsNone(result["value"])
        self.assertEqual(len(result["hidden_gaps"]), 1)

    def test_gap_hidden_by_car_is_not_a_break(self):
        def line(draw):
            draw.rectangle((0, 650, 1280, 658), fill=YELLOW)
            draw.rectangle((400, 615, 900, 720), fill=(150, 20, 20))     # 빨간 차
        self.assertIsNone(measure_curb_paint(road(line), STRIP)["value"])

    def test_no_yellow_is_no_line(self):
        """노란 칠이 없으면 '규제선 없음' (정차에 문제 없음). 흰 선만 있어도 마찬가지."""
        result = measure_curb_paint(road(lambda d: d.rectangle((0, 650, 1280, 658), fill=(240, 240, 240))), STRIP)
        self.assertEqual(result["value"], "none")
        self.assertIn("규제선 없음", result["evidence"])
        self.assertEqual(measure_curb_paint(road(), STRIP)["value"], "none")

    def test_short_visible_segment_is_undecided(self):
        result = measure_curb_paint(road(lambda d: d.rectangle((500, 650, 800, 658), fill=YELLOW)), STRIP)
        self.assertIsNone(result["value"])


class ApplyMeasureTest(unittest.TestCase):
    def parsed(self, line):
        return parse_assessment(json.dumps(assessment(curb_line={"value": line, "evidence": "모델 근거"}),
                                           ensure_ascii=False))

    def test_measure_overrides_model(self):
        result = apply_measure(self.parsed("yellow_solid"), {"value": "yellow_dashed", "evidence": "색 측정: 점선"})
        self.assertEqual(result["curb_line"]["value"], "yellow_dashed")
        self.assertEqual(result["curb_line"]["source"], "color_measure")
        self.assertEqual(result["curb_line"]["model_value"], "yellow_solid")

    def test_undecided_measure_keeps_model(self):
        result = apply_measure(self.parsed("yellow_solid"), {"value": None, "evidence": "보류"})
        self.assertEqual(result["curb_line"]["value"], "yellow_solid")
        self.assertNotIn("source", result["curb_line"])

    def test_no_yellow_overrides_model_solid_and_passes(self):
        """모델이 황색 실선이라 해도 노란 칠이 없으면 규제선 없음 → 규제선 때문에 불가가 되지 않음."""
        from pudo3.photo_decision import decide
        from support import localization_json as loc_json
        from pudo3.vlm_policy import parse_localization
        result = apply_measure(self.parsed("yellow_solid"), {"value": "none", "evidence": "색 측정: 노란 칠 없음"})
        self.assertEqual(result["curb_line"]["value"], "none")
        decision = decide(parse_localization(loc_json()), result)
        self.assertEqual(decision["verdict"], "가능")

    def test_no_yellow_keeps_model_white(self):
        result = apply_measure(self.parsed("white"), {"value": "none", "evidence": "x"})
        self.assertEqual(result["curb_line"]["value"], "white")
        self.assertTrue(result["curb_line"]["measure_agrees"])

    def test_solid_and_double_are_impossible_dashed_is_fine(self):
        """실선·복선 = 정차 금지(불가), 점선 = 문제 없음."""
        from pudo3.photo_decision import decide
        from support import localization_json as loc_json
        from pudo3.vlm_policy import parse_localization
        loc = parse_localization(loc_json())
        for value, verdict in (("yellow_solid", "불가"), ("yellow_double", "불가"), ("yellow_dashed", "가능")):
            result = apply_measure(self.parsed("none"), {"value": value, "evidence": "색 측정"})
            self.assertEqual(decide(loc, result)["verdict"], verdict, value)

    def test_agreement_marked(self):
        result = apply_measure(self.parsed("yellow_dashed"), {"value": "yellow_dashed", "evidence": "x"})
        self.assertTrue(result["curb_line"]["measure_agrees"])


class TwoStageMeasureTest(unittest.TestCase):
    def test_dashed_photo_overrides_model_solid(self):
        """모델이 실선이라 해도 사진의 노란 선이 점선으로 측정되면 실선 불가가 나오지 않음."""
        image = road(lambda d: dashed(d, y=660))   # 연석 y≈619px 아래, 연석 띠 안
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        loc = localization_json(sidewalk_point=[500, 780], sidewalk_region=[200, 700, 800, 850],
                                curb_point=[500, 860], curb_region=[200, 830, 800, 890])
        runner = FakeRunner(loc, json.dumps(assessment(curb_line={"value": "yellow_solid", "evidence": "끊김 없음"}),
                                            ensure_ascii=False))
        out = run_two_stage(runner, buffer.getvalue(), capture())
        self.assertEqual(out["curb_line_measure"]["value"], "yellow_dashed")
        self.assertEqual(out["assessment"]["curb_line"]["value"], "yellow_dashed")
        self.assertNotIn("황색 실선", out["decision"]["reason"])


class HideKakaoUiTest(unittest.TestCase):
    def test_arrows_and_controls_hidden_marker_and_copyright_kept(self):
        html = viewer_html("TESTKEY", CANDIDATE)
        self.assertIn(HIDE_UI_CSS, html)
        for prefix in ('[id^="_ar_"]', '[id^="_at_"]', '[id^="_box_util_"]'):
            self.assertIn(prefix, HIDE_UI_CSS)
        self.assertNotIn("copyright", HIDE_UI_CSS)
        self.assertIn("kakao.maps.Marker", html)


if __name__ == "__main__":
    unittest.main()
