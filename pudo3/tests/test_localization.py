import json
import unittest

import support  # noqa: F401
from support import FakeRunner, assessment_json, capture, localization_json, png_bytes

from pudo3.vlm_inference import run_two_stage
from pudo3.vlm_policy import LocalizationError, localization_question, parse_localization


def status_json(status, evidence):
    return json.dumps({"sidewalk_status": status, "sidewalk_point": None, "sidewalk_region": None,
                       "curb_point": None, "curb_region": None, "layer_order": [], "evidence": evidence},
                      ensure_ascii=False)


class ParseLocalizationTest(unittest.TestCase):
    def test_valid_json(self):
        """3. 유효한 Qwen 위치 JSON 파싱 (코드 블록 감싸기 허용). 판독 지점 = 보도 지점."""
        result = parse_localization("```json\n" + localization_json() + "\n```")
        self.assertEqual(result["sidewalk_status"], "visible")
        self.assertEqual(result["reading_point"], [500.0, 600.0])
        self.assertEqual(result["curb_point"], [500.0, 680.0])
        self.assertEqual(result["center_shift"], 0.0)

    def test_curb_optional(self):
        result = parse_localization(localization_json(curb_point=None, curb_region=None,
                                                      layer_order=["road", "sidewalk"]))
        self.assertIsNone(result["curb_point"])
        self.assertEqual(result["reading_point"], [500.0, 600.0])

    def test_point_near_curb_is_lifted_to_sidewalk_middle(self):
        """점이 보도 가운데보다 연석 쪽이면 보도 먼 끝과 연석의 가운데로 올림 (실제: 공통적으로 너무 낮게 찍힘)."""
        result = parse_localization(localization_json(sidewalk_point=[500, 650], sidewalk_region=[380, 520, 640, 660]))
        self.assertTrue(result["lifted"])
        self.assertEqual(result["model_point"], [500.0, 650.0])
        self.assertEqual(result["reading_point"], [500.0, 590.0])   # (520 + min(660, 연석 680)) / 2
        kept = parse_localization(localization_json(sidewalk_point=[500, 580], sidewalk_region=[380, 520, 640, 660]))
        self.assertFalse(kept["lifted"], "가운데보다 먼 쪽 점은 그대로 (아래로 내리지 않음)")
        self.assertEqual(kept["reading_point"], [500.0, 580.0])

    def test_region_running_to_image_bottom_uses_curb(self):
        """P0453: 보도 영역을 화면 아래 끝(999)까지 잡아도 연석 y를 가까운 끝으로 써서 보도 가운데로 올림."""
        result = parse_localization(localization_json(
            sidewalk_point=[669, 812], sidewalk_region=[193, 610, 806, 999],
            curb_point=[669, 897], curb_region=[193, 856, 806, 999]), box=[200, 619.6, 800, 998.6])
        self.assertEqual(result["reading_point"], [669.0, 753.5])

    def test_occluded_sidewalk_uses_region_bottom_not_curb(self):
        """차 뒤로 보이는 보도: 영역 아래쪽(차 윗면)이 연석보다 위면 영역 안에서 가운데."""
        result = parse_localization(localization_json(
            sidewalk_point=[500, 600], sidewalk_region=[380, 560, 640, 610],
            curb_point=[500, 680], curb_region=[360, 640, 680, 720]))
        self.assertEqual(result["reading_point"], [500.0, 585.0])
        no_curb = parse_localization(localization_json(sidewalk_point=[500, 650], sidewalk_region=[380, 520, 640, 660],
                                                       curb_point=None, curb_region=None,
                                                       layer_order=["road", "sidewalk"]))
        self.assertEqual(no_curb["reading_point"], [500.0, 590.0])

    def test_question_asks_mid_sidewalk(self):
        q = localization_question([370, 600, 630, 900])
        self.assertIn("연석 선(노란 선·경계석·차도와 보도의 경계) 바로 위에 찍지 마세요", q)
        self.assertIn("가운데쯤 보도 바닥", q)

    def test_bad_curb_is_dropped_not_fatal(self):
        """연석 좌표가 이상하면 연석만 버리고 보도 지점으로 계속 (실제 사례: 보도와 같은 좌표를 복사)."""
        cases = [
            {"curb_point": [500, 600], "curb_region": [380, 520, 640, 660]},   # 보도와 같은 좌표
            {"curb_point": [500, 560], "curb_region": [360, 540, 680, 580]},   # 보도보다 먼 쪽
            {"curb_point": [700, 680], "curb_region": [660, 640, 760, 720]},   # 다른 좌우 위치
            {"curb_point": [200, 680]},                                         # 영역 밖
            {"curb_point": [float("nan"), 3]},                                  # 숫자 아님
        ]
        for bad in cases:
            result = parse_localization(localization_json(**bad))
            self.assertIsNone(result["curb_point"], bad)
            self.assertIn("연석 좌표를 쓰지 않음", result["curb_note"])
            self.assertEqual(result["reading_point"], [500.0, 600.0])

    def test_shift_from_center_when_occluded_at_center(self):
        """가운데가 가려지면 주변 보도로 옮겨 찍는 것을 허용 (최대 350)."""
        result = parse_localization(localization_json(sidewalk_point=[760, 600], sidewalk_region=[700, 520, 840, 660],
                                                      curb_point=[760, 680], curb_region=[700, 640, 840, 720]))
        self.assertEqual(result["center_shift"], 260.0)
        with self.assertRaises(LocalizationError):
            parse_localization(localization_json(sidewalk_point=[900, 600], sidewalk_region=[860, 520, 960, 660],
                                                 curb_point=None, curb_region=None))

    def test_out_of_range_rejected(self):
        """4. 범위를 벗어난 좌표 거부."""
        for bad in ({"sidewalk_point": [500, 1001]}, {"sidewalk_point": [-1, 600]},
                    {"sidewalk_region": [380, 520, 640, 1200]}, {"sidewalk_point": [float("nan"), 3]}):
            with self.assertRaises(LocalizationError, msg=bad):
                parse_localization(localization_json(**bad))

    def test_point_outside_region_rejected(self):
        """5. point가 region 밖이면 거부."""
        with self.assertRaises(LocalizationError) as ctx:
            parse_localization(localization_json(sidewalk_point=[200, 600]))
        self.assertIn("밖", str(ctx.exception))

    def test_geometry_rejected(self):
        with self.assertRaises(LocalizationError):   # 사각형 뒤집힘
            parse_localization(localization_json(sidewalk_region=[640, 520, 380, 660]))
        with self.assertRaises(LocalizationError):   # 보도를 찍었는데 layer_order에 sidewalk 없음
            parse_localization(localization_json(layer_order=["road", "curb"]))
        with self.assertRaises(LocalizationError):
            parse_localization("보도는 화면 아래에 있습니다.")
        with self.assertRaises(LocalizationError):
            parse_localization(localization_json(sidewalk_status="maybe"))

    def test_absent_and_occluded(self):
        self.assertEqual(parse_localization(status_json("absent", "차도 옆이 바로 벽"))["sidewalk_status"], "absent")
        result = parse_localization(status_json("occluded", "트럭이 가림"))
        self.assertFalse(result["target_visible"])
        self.assertIsNone(result["reading_point"])

    def test_question_uses_center_not_marker(self):
        q = localization_question()
        self.assertIn("화면 가운데 세로선(x = 500)", q)
        self.assertIn("판독 기준으로 쓰지 마세요", q)
        self.assertIn('"absent"', q)
        self.assertIn("가려져서 안 보이는 것을", q)


class TwoStageTest(unittest.TestCase):
    def test_marker_not_used_as_reading_point(self):
        """2. 카카오 마커 좌표를 판독 위치로 사용하지 않음 (기준은 화면 가운데의 보도)."""
        runner = FakeRunner(localization_json(), assessment_json())
        cap = capture(target_pixel={"x": 900.0, "y": 260.0, "width": 1280, "height": 720})
        out = run_two_stage(runner, png_bytes(), cap)
        self.assertEqual(out["reading_point"], [500.0, 600.0])
        self.assertAlmostEqual(out["reading_pixel"][0], 640.0, places=1)
        self.assertAlmostEqual(out["reading_pixel"][1], 432.0, places=1)
        question = runner.calls[0]["question"]
        self.assertNotIn("703", question)   # 마커 x(900/1280*1000) 미사용
        self.assertNotIn("361", question)   # 마커 y 미사용
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(runner.calls[1]["images"]), 1, "2차 입력은 전체 사진 1장")
        self.assertIsNone(out["detail_crop_box"])
        sx1, sy1, sx2, sy2 = out["curb_strip_box"]      # 색 측정용 영역은 계속 계산
        self.assertEqual((sx1, sx2), (0, 1280))
        self.assertTrue(sy1 <= out["snapped_curb_pixel"][1] <= sy2)
        self.assertIn("왼쪽 끝부터 오른쪽 끝까지", runner.calls[1]["question"])
        self.assertEqual(out["decision"]["verdict"], "가능")

    def test_works_without_marker(self):
        runner = FakeRunner(localization_json(curb_point=None, curb_region=None), assessment_json())
        out = run_two_stage(runner, png_bytes(), capture(target_pixel=None))
        self.assertIsNone(out["marker_norm"])
        self.assertIsNone(out["snapped_curb_pixel"])
        self.assertEqual(out["decision"]["verdict"], "가능")

    def test_localization_failure_is_undecidable(self):
        """6. 위치 탐색 실패 시 마커로 폴백하지 않고 판단 불가."""
        for bad in ("형식이 아닌 답변", localization_json(sidewalk_point=[999, 999])):
            runner = FakeRunner(bad)
            out = run_two_stage(runner, png_bytes(), capture())
            self.assertEqual(out["decision"]["verdict"], "판단 불가")
            self.assertIsNone(out["reading_point"])
            self.assertEqual(len(runner.calls), 1, "2차 호출을 하지 않아야 함")
            self.assertIn("보도 판독 위치를 찾지 못함", out["decision"]["reason"])

    def test_absent_sidewalk_is_impossible_without_second_call(self):
        runner = FakeRunner(status_json("absent", "차도 바로 옆이 건물 벽"))
        out = run_two_stage(runner, png_bytes(), capture())
        self.assertEqual(out["decision"]["verdict"], "불가")
        self.assertEqual(out["decision"]["reason"], "불가: 후보 바로 앞에 사람이 설 수 있는 보도가 없음")
        self.assertEqual(len(runner.calls), 1)

    def test_occluded_sidewalk_is_undecidable(self):
        runner = FakeRunner(status_json("occluded", "주차 트럭이 보도를 가림"))
        out = run_two_stage(runner, png_bytes(), capture())
        self.assertEqual(out["decision"]["verdict"], "판단 불가")
        self.assertIn("가려져", out["decision"]["reason"])
        self.assertIn("보도 없음과 다름", out["decision"]["reason"])
        self.assertEqual(len(runner.calls), 1)

    def test_assessment_json_failure_is_undecidable(self):
        runner = FakeRunner(localization_json(), '{"sidewalk": {"value": "maybe"}}')
        out = run_two_stage(runner, png_bytes(), capture())
        self.assertEqual(out["decision"]["verdict"], "판단 불가")
        self.assertIn("JSON 검증 실패", out["decision"]["reason"])


if __name__ == "__main__":
    unittest.main()
