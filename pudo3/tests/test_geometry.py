"""지도 좌표 → 로드뷰 화면 탐색 상자 계산. 실제 판독 기록(P0453, P0634) 수치로 확인."""
import json
import unittest

import support  # noqa: F401
from support import FakeRunner, assessment_json, capture, localization_json, png_bytes

from pudo3 import geometry
from pudo3.geometry import FOCAL_PX, project, search_box
from pudo3.vlm_inference import run_two_stage
from pudo3.vlm_policy import LocalizationError, localization_question, parse_localization

# 실제 기록에서 가져온 값 (P0453: 모델이 찾은 보도 [669, 750], P0634: 차에 가려 왼쪽 x=207에서 찾음)
P0453 = {"coordinates": None, "heading": 70}
P0453_CAMERA_OFFSET = (1.23, 5.03 - 1.5)   # (오른쪽 m, 후보까지 정면 m)


def candidate_at(camera, right, forward, heading):
    """카메라 기준 (오른쪽, 정면) 미터 위치의 후보를 만듭니다."""
    import math
    h = math.radians(heading)
    dx = right * math.cos(h) + forward * math.sin(h)
    dy = -right * math.sin(h) + forward * math.cos(h)
    lat = camera[1] + dy / 111320
    lon = camera[0] + dx / (111320 * math.cos(math.radians(lat)))
    return {"id": "T", "coordinates": [lon, lat], "heading": heading}


class GeometryTest(unittest.TestCase):
    def test_curb_inset_matches_candidates(self):
        from pudo3.candidates import CURB_INSET_M
        self.assertEqual(geometry.CURB_INSET_M, CURB_INSET_M)

    def test_projection_matches_calibration(self):
        # 기존 캡처: 후보가 정면에서 19.2° 오른쪽일 때 마커 x = 640 + 216.6
        import math
        x, y = project(math.tan(math.radians(19.2)), 1.0, 0.0)
        self.assertAlmostEqual(x - 640, 216.6, delta=1.0)   # 기록된 각도가 0.1° 단위로 반올림됨
        self.assertEqual(y, 360)
        self.assertEqual(FOCAL_PX, 623.5)

    def test_real_case_p0453_box_contains_found_sidewalk(self):
        camera = [127.02, 37.49]
        cand = candidate_at(camera, *P0453_CAMERA_OFFSET, 70)
        box = search_box(camera, cand, 70)
        self.assertTrue(box["valid"])
        # 실제로 찾은 보도 [669, 750]는 상자 + 검증 여유 안이라 받아들여짐
        found = localization_json(sidewalk_point=[669, 750], sidewalk_region=[630, 720, 700, 780],
                                  curb_point=None, curb_region=None)
        self.assertEqual(parse_localization(found, box=box["box"])["reading_point"], [669.0, 750.0])

    def test_real_case_p0634_box_stays_at_center(self):
        """촬영 지점이 도로를 따라 4.8m 옆이어도 판독 기준은 화면 가운데 (좌우로 옮기지 않음)."""
        camera = [127.02, 37.49]
        cand = candidate_at(camera, -4.77, 5.77 - 1.5, 159)
        box = search_box(camera, cand, 159)
        self.assertTrue(box["valid"])
        self.assertEqual(box["box"][0], 200.0)
        self.assertEqual(box["box"][2], 800.0)
        self.assertEqual(box["predicted_point"][0], 500.0)
        self.assertAlmostEqual(box["right_m"], -4.77, delta=0.05)   # 참고 기록만

    def test_camera_past_curb_is_invalid(self):
        camera = [127.02, 37.49]
        cand = candidate_at(camera, 0.5, -1.5, 90)   # 후보가 카메라 뒤/옆
        box = search_box(camera, cand, 90)
        self.assertFalse(box["valid"])
        self.assertIn("계산할 수 없음", box["reason"])

    def test_predicted_point_inside_sidewalk(self):
        """예상 지점은 연석 뒤 2m (연석보다 화면 위쪽)."""
        camera = [127.02, 37.49]
        box = search_box(camera, candidate_at(camera, 0, 3.5, 90), 90)
        curb_y = project(0, 5.0, geometry.CAMERA_HEIGHT_M)[1] / 720 * 1000
        self.assertEqual(geometry.SIDEWALK_STEP_M, 2.0)
        self.assertLess(box["predicted_point"][1], curb_y - 40)

    def test_x_never_shifts(self):
        camera = [127.02, 37.49]
        for right in (-8, 0, 8):
            box = search_box(camera, candidate_at(camera, right, 4.0, 90), 90)
            self.assertEqual((box["box"][0], box["box"][2], box["predicted_point"][0]), (200.0, 800.0, 500.0))
            # 위쪽 끝 = 연석 뒤 8m 거리 (h=1.8m): 360 + 623.5*1.8/(5.5+8) px
            self.assertAlmostEqual(box["box"][1], (360 + 623.5 * 1.8 / 13.5) / 720 * 1000, delta=0.2)


class BoxLocalizationTest(unittest.TestCase):
    BOX = [380.0, 520.0, 640.0, 720.0]
    SEARCH = {"valid": True, "box": BOX, "predicted_point": [500.0, 600.0], "reason": None}

    def test_question_uses_box(self):
        q = localization_question(self.BOX)
        self.assertIn("노란 점선 상자", q)
        self.assertIn("정가운데일 필요는 없습니다", q)
        self.assertIn("차 뒤(위쪽)나 옆으로 보이는 같은 보도 바닥", q)
        self.assertNotIn("노란 +", q)
        self.assertIn("연석과 난간 사이 보도 바닥의 가운데", q)
        # 상자 좌표 숫자·가운데 x값·JSON 예시 값을 주지 않음 (모델이 복사하지 않도록)
        self.assertNotIn("380", q)
        self.assertNotIn("x = 500", q)
        self.assertNotIn('["road", "curb", "sidewalk", "property_fence", "building"]', q)
        self.assertNotIn('"sidewalk_status": "visible"', q)
        self.assertIn('"absent"', q)

    def test_point_must_be_in_box(self):
        parse_localization(localization_json(), box=self.BOX)
        with self.assertRaises(LocalizationError) as ctx:   # 상자에서 크게 벗어남
            parse_localization(localization_json(sidewalk_point=[900, 600], sidewalk_region=[860, 520, 960, 660],
                                                 curb_point=None, curb_region=None), box=self.BOX)
        self.assertIn("탐색 상자", str(ctx.exception))
        # 가려져 상자 옆으로 조금 옮긴 것은 허용 (가로 여유)
        parse_localization(localization_json(sidewalk_point=[720, 600], sidewalk_region=[680, 520, 760, 660],
                                             curb_point=None, curb_region=None), box=self.BOX)

    def test_far_sidewalk_rejected(self):
        """상자 위쪽 끝(연석 뒤 8m)보다 멀리 보이는 보도는 거부."""
        camera = [127.02, 37.49]
        box = search_box(camera, candidate_at(camera, 0, 4.0, 90), 90)["box"]
        far_y = box[1] - 40
        with self.assertRaises(LocalizationError):
            parse_localization(localization_json(sidewalk_point=[500, far_y], sidewalk_region=[400, far_y - 20, 600, far_y + 20],
                                                 curb_point=None, curb_region=None), box=box)
        q = localization_question(box)
        self.assertIn("멀리 작게 보이는 보도는 찍지 마세요", q)

    def test_point_above_horizon_rejected(self):
        """보도는 땅이므로 지평선(y=500)보다 아래. 위면 하늘·건물·나무로 보고 거부."""
        with self.assertRaises(LocalizationError) as ctx:
            parse_localization(localization_json(sidewalk_point=[500, 480], sidewalk_region=[400, 460, 600, 520],
                                                 curb_point=None, curb_region=None), box=self.BOX)
        self.assertIn("지평선", str(ctx.exception))

    def test_taxi_case_sidewalk_behind_vehicle_accepted(self):
        """실제 P0941: 택시 뒤로 보이는 보도(지평선 조금 아래, 가운데에서 옆)를 받아들임."""
        camera = [127.02, 37.49]
        box = search_box(camera, candidate_at(camera, -4.57, 9.08, 158), 158)["box"]
        result = parse_localization(localization_json(sidewalk_point=[300, 570], sidewalk_region=[220, 540, 380, 600],
                                                      curb_point=None, curb_region=None), box=box)
        self.assertEqual(result["reading_point"], [300.0, 570.0])

    def test_stage1_gets_marked_image_and_box(self):
        runner = FakeRunner(localization_json(), assessment_json())
        out = run_two_stage(runner, png_bytes(), capture(), search=self.SEARCH)
        self.assertIn("노란 점선 상자", runner.calls[0]["question"])
        self.assertIs(runner.calls[0]["images"][0], out["images"]["search"])
        self.assertEqual(out["search"], self.SEARCH)
        self.assertEqual(out["decision"]["verdict"], "가능")

    def test_invalid_search_skips_model(self):
        runner = FakeRunner()
        out = run_two_stage(runner, png_bytes(), capture(),
                            search={"valid": False, "reason": "계산상 후보 보도가 정면 화면 밖에 있음"})
        self.assertEqual(runner.calls, [])
        self.assertEqual(out["decision"]["verdict"], "판단 불가")
        self.assertIn("화면 밖", out["decision"]["reason"])


if __name__ == "__main__":
    unittest.main()
