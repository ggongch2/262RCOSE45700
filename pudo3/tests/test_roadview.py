import re
import unittest

import support  # noqa: F401  (sys.path)
from support import CANDIDATE, capture

from pudo3 import roadview
from pudo3.roadview import RoadviewError, redact, validate_capture, viewer_html


class FrontCameraTest(unittest.TestCase):
    """1. 카메라 pan/tilt/zoom이 기존 정면 설정과 동일함."""

    def test_viewer_uses_candidate_heading_level_default_zoom(self):
        html = viewer_html("SECRET_KEY_123", CANDIDATE)
        self.assertIn("const frontPan=c.heading;", html)
        self.assertIn("const frontTilt=0;", html)
        self.assertIn("const frontZoom=0;", html)
        self.assertIn("rv.setViewpoint({pan:frontPan,tilt:frontTilt,zoom:frontZoom});", html)
        self.assertIn("view_mode:'curb-front'", html)
        self.assertEqual((roadview.FRONT_TILT, roadview.FRONT_ZOOM, roadview.VIEW_MODE), (0, 0, "curb-front"))

    def test_kakao_geographic_marker_is_kept(self):
        html = viewer_html("k", CANDIDATE)
        self.assertIn("viewpointFromCoords(position,0)", html)
        self.assertIn("new kakao.maps.Marker({position:targetViewpoint,map:rv})", html)
        self.assertIn('id="vlm-target-anchor"', html)

    def test_matches_existing_pudo_front_settings(self):
        """기존 pudo/roadview.py(읽기 전용 참고)의 정면 설정 줄이 그대로 들어 있어야 함."""
        from pathlib import Path
        original = (Path(support.ROOT) / "pudo" / "roadview.py")
        if not original.is_file():
            self.skipTest("기존 pudo/roadview.py 없음")
        text = original.read_text(encoding="utf-8")
        for line in ("const frontPan=c.heading;", "const frontTilt=0;", "const frontZoom=0;",
                     "rv.setViewpoint({pan:frontPan,tilt:frontTilt,zoom:frontZoom});"):
            self.assertIn(line, text)
            self.assertIn(line, viewer_html("k", CANDIDATE))

    def test_validate_capture_limits(self):
        ok = validate_capture(capture(), CANDIDATE)
        self.assertAlmostEqual(ok["distance_m"], 8.8, delta=0.3)
        validate_capture(capture(viewpoint={"pan": 92.0, "tilt": -2.0, "zoom": 0.1}), CANDIDATE)
        for bad in ({"pan": 92.5, "tilt": 0, "zoom": 0}, {"pan": 90, "tilt": 2.5, "zoom": 0},
                    {"pan": 90, "tilt": 0, "zoom": 0.2}):
            with self.assertRaises(RoadviewError):
                validate_capture(capture(viewpoint=bad), CANDIDATE)
        with self.assertRaises(RoadviewError):
            validate_capture(capture(view_mode="target"), CANDIDATE)
        # 0°/360° 경계
        north = {**CANDIDATE, "heading": 359}
        validate_capture(capture(viewpoint={"pan": 1.0, "tilt": 0, "zoom": 0}), north)


class RoadviewErrorTest(unittest.TestCase):
    def test_stage_names_are_distinguished(self):
        html = viewer_html("k", CANDIDATE)
        for stage in (roadview.STAGE_SDK_SCRIPT, roadview.STAGE_MAPS_LOAD, roadview.STAGE_PANO_ID,
                      roadview.STAGE_PANO_INIT, roadview.STAGE_MARKER):
            self.assertIn(stage, html)
        self.assertIn("SDK 스크립트 로드 실패", html)
        self.assertIn("파노라마 ID 조회 실패", html)

    def test_errors_never_contain_api_key(self):
        error = RoadviewError("failed https://dapi.kakao.com/v2/maps/sdk.js?autoload=false&appkey=SECRET123 x",
                              roadview.STAGE_SDK_SCRIPT)
        self.assertNotIn("SECRET123", str(error))
        self.assertTrue(str(error).startswith("[SDK 스크립트 로드]"))
        self.assertNotIn("SECRET", redact("appkey=SECRET&foo=1"))

    def test_marker_is_reference_only(self):
        from pudo3.roadview import camera_offset, validate_target_pixel
        self.assertIsNone(validate_target_pixel(None))
        self.assertIsNone(validate_target_pixel({"x": 1500, "y": 10, "width": 1280, "height": 720}))
        html = viewer_html("k", CANDIDATE)
        self.assertIn("target_marker:!!point,target_pixel:point", html)
        self.assertIn("finish(null)", html)
        self.assertNotIn("curb-oblique", html)
        # heading 90(동쪽 도로변) 후보, 카메라는 서쪽 약 8.8m: 도로변 쪽 거리 ≈ 8.8, 도로를 따라 ≈ 0
        offset = camera_offset([127.02750, 37.4979], CANDIDATE)
        self.assertAlmostEqual(offset["toward_curb_m"], 8.8, delta=0.3)
        self.assertAlmostEqual(offset["along_road_m"], 0.0, delta=0.1)
        offset = camera_offset([127.0276, 37.49780], CANDIDATE)   # 카메라가 남쪽 약 11m
        self.assertAlmostEqual(abs(offset["along_road_m"]), 11.1, delta=0.3)

    def test_blank_image_is_reported(self):
        import io
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), (0, 0, 0)).save(buffer, format="PNG")
        with self.assertRaises(RoadviewError) as ctx:
            roadview.check_not_blank(buffer.getvalue())
        self.assertIn("비어 있습니다", str(ctx.exception))

    def test_no_fixed_20_second_total_timeout(self):
        from pathlib import Path
        source = Path(roadview.__file__).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"captureStartedAt\s*>=\s*20000", source))
        self.assertGreater(roadview.STALL_SECONDS, 20)


if __name__ == "__main__":
    unittest.main()
