"""QwenPhotoSession: 가짜 로드뷰·가짜 Qwen으로 저장 데이터와 서버 API 확인."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import support  # noqa: F401
from support import CANDIDATE, FakeRunner, assessment_json, capture, localization_json, png_bytes, stop_check_json

from pudo3.roadview import RoadviewError
from pudo3.vlm import RECORD_SCHEMA, QwenPhotoSession, curb_line_required

REQUIRED_KEYS = {"candidate_id", "pano_id", "camera", "distance_m", "query_heading", "viewpoint",
                 "target_pixel", "assessment", "decision", "mode", "curb_line_measure",
                 "model", "image_sha256", "generated_at", "schema_version"}


def fixed_clock():
    return datetime(2026, 11, 25, 3, 0, 0, tzinfo=timezone.utc)


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name) / "runtime"

    def session(self, runner, roadview=None, diagnostics=False):
        roadview = roadview or (lambda candidate: (png_bytes(), capture()))
        return QwenPhotoSession([CANDIDATE], self.runtime, runner, roadview=roadview,
                                save_diagnostics=diagnostics, clock=fixed_clock)

    def test_progress_messages_in_order(self):
        """터미널 진행 표시: 캡처 → 정차 판독 → 결과 → 판정 순서로 찍힘."""
        lines = []
        session = QwenPhotoSession([CANDIDATE], self.runtime, FakeRunner(stop_check_json()),
                                   roadview=lambda candidate: (png_bytes(), capture()), clock=fixed_clock,
                                   log=lines.append)
        session.analyze_roadview({"candidate_id": "P0001"})
        text = "\n".join(lines)
        order = ["로드뷰 캡처 시작", "로드뷰 캡처 끝", "정차 판독 (보도 → 장애물 → 주차장 입구 → 차선, 생각 켬) 시작",
                 "정차 판독 (보도 → 장애물 → 주차장 입구 → 차선, 생각 켬) 끝", "결과: 1. 보도 있음", "판독 끝: "]
        positions = [text.index(item) for item in order]
        self.assertEqual(positions, sorted(positions), text)
        self.assertTrue(all(line.startswith("[P0001 +") for line in lines))

    def test_record_contents_and_storage(self):
        runner = FakeRunner(stop_check_json())
        session = self.session(runner)
        result = session.analyze_roadview({"candidate_id": "P0001"})
        record = result["record"]
        self.assertTrue(REQUIRED_KEYS <= record.keys(), REQUIRED_KEYS - record.keys())
        self.assertEqual(record["schema_version"], RECORD_SCHEMA)
        self.assertEqual(record["model"]["model_id"], "Qwen/Qwen3.5-9B")
        self.assertEqual(record["model"]["model_path"], "/models/Qwen3.5-9B")
        self.assertEqual(record["target_pixel"]["y"], 260.0)
        self.assertEqual(record["mode"], "stop_check")
        self.assertIsNone(record["reading_point"], "보도에 점을 찍지 않음")
        self.assertEqual([c["thinking"] for c in runner.calls], [True], "모델 1회 호출, 생각 켬")
        self.assertEqual(len(runner.calls[0]["images"]), 1)
        self.assertEqual([c["label"][:2] for c in record["decision"]["conditions"]], ["1.", "2.", "3.", "4."])
        self.assertIn("along_road_m", record["camera_offset"])
        self.assertEqual(len(record["image_sha256"]), 64)
        self.assertEqual(record["decision"]["verdict"], "가능")
        self.assertEqual(set(result["images"]), {"original"})
        files = list((self.runtime / "results").glob("P0001-*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8"))["record_id"], record["record_id"])
        index = json.loads((self.runtime / "qwen_results.json").read_text(encoding="utf-8"))
        self.assertEqual(index["candidates"]["P0001"]["verdict"], "가능")
        self.assertFalse((self.runtime / "diagnostics").exists(), "진단 옵션 없이는 이미지 저장 안 함")
        # 다시 열면 최신 결과 복원
        reopened = self.session(FakeRunner())
        self.assertEqual(reopened.latest("P0001")["record_id"], record["record_id"])
        self.assertEqual(reopened.summaries()["P0001"]["verdict"], "가능")

    def test_diagnostics_saved_only_when_enabled(self):
        session = self.session(FakeRunner(stop_check_json()), diagnostics=True)
        session.analyze_roadview({"candidate_id": "P0001"})
        folders = list((self.runtime / "diagnostics").iterdir())
        self.assertEqual(len(folders), 1)
        self.assertEqual({p.name for p in folders[0].iterdir()},
                         {"original.png", "raw_responses.txt"})

    def test_manual_reassess(self):
        runner = FakeRunner(stop_check_json(), assessment_json(
            fixed_obstruction={"value": True, "type": "metal_railing", "relation": "curb_edge",
                               "opening_at_target": False, "evidence": "연석 위 난간"}))
        session = self.session(runner)
        first = session.analyze_roadview({"candidate_id": "P0001"})["record"]
        result = session.reassess({"record_id": first["record_id"], "point": [300, 650]})
        record = result["record"]
        self.assertEqual(record["reading_source"], "manual")
        self.assertEqual(record["reading_point"], [300.0, 650.0])
        self.assertEqual(record["manual_from_record"], first["record_id"])
        self.assertEqual(record["decision"]["verdict"], "불가")
        self.assertEqual(len(runner.calls), 2, "직접 지정은 지정 지점 조건 판독만 1회")
        self.assertEqual(session.latest("P0001")["record_id"], record["record_id"])
        from pudo3.vlm_policy import VLMError
        with self.assertRaises(VLMError):
            session.reassess({"record_id": first["record_id"], "point": [300, 1200]})
        with self.assertRaises(VLMError) as ctx:
            session.reassess({"record_id": "없는기록", "point": [300, 650]})
        self.assertIn("다시 누른 뒤", str(ctx.exception))

    def test_record_has_search_box(self):
        session = self.session(FakeRunner(stop_check_json()))
        record = session.analyze_roadview({"candidate_id": "P0001"})["record"]
        self.assertIn("valid", record["search"])
        self.assertEqual(record["reading_source"], "auto")

    def test_marker_off_screen_does_not_block(self):
        session = self.session(FakeRunner(stop_check_json()),
                               roadview=lambda c: (png_bytes(), capture(target_pixel=None)))
        record = session.analyze_roadview({"candidate_id": "P0001"})["record"]
        self.assertIsNone(record["target_pixel"])
        self.assertEqual(record["decision"]["verdict"], "가능")

    def test_invalid_response_saved_as_undecidable(self):
        session = self.session(FakeRunner("모르겠습니다"))
        record = session.analyze_roadview({"candidate_id": "P0001"})["record"]
        self.assertEqual(record["decision"]["verdict"], "판단 불가")
        self.assertIsNotNone(record["assessment_error"])

    def test_roadview_error_logged_without_key(self):
        def broken(candidate):
            raise RoadviewError("appkey=SECRET 실패", "SDK 스크립트 로드")
        session = self.session(FakeRunner(), roadview=broken)
        with self.assertRaises(RoadviewError):
            session.analyze_roadview({"candidate_id": "P0001"})
        log = (self.runtime / "logs" / "roadview-errors.log").read_text(encoding="utf-8")
        self.assertIn("SDK 스크립트 로드", log)
        self.assertNotIn("SECRET", log)

    def test_curb_line_required_follows_gis(self):
        self.assertTrue(curb_line_required(None))
        self.assertTrue(curb_line_required({"checks": [{"item": "H5 규제선", "pass": None}]}))
        self.assertFalse(curb_line_required({"checks": [{"item": "H5 규제선", "pass": True}]}))


class ServerAppTest(unittest.TestCase):
    def test_meta_reports_model(self):
        from pudo3.server import App
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        session = QwenPhotoSession([CANDIDATE], tmp.name, FakeRunner(), roadview=lambda c: None)
        meta = App([], {}, Path(tmp.name), vlm=session).meta()
        self.assertTrue(meta["vlm"])
        self.assertEqual(meta["vlm_info"]["model_id"], "Qwen/Qwen3.5-9B")
        self.assertIn("2단계", meta["vlm_info"]["pipeline"])

    def test_runtime_paths_inside_pudo3(self):
        from pudo3.config import PKG_DIR, RUNTIME_DIR, WEB_DIR
        from pudo3.measure import prepared_paths
        self.assertEqual(RUNTIME_DIR.parent, PKG_DIR)
        self.assertEqual(WEB_DIR.parent, PKG_DIR)
        for path in prepared_paths():
            self.assertTrue(str(path).startswith(str(RUNTIME_DIR)))


if __name__ == "__main__":
    unittest.main()


class LocateTest(unittest.TestCase):
    """위치 찾기만: 1차 1회 호출, 2차·판정 없음, runtime/locate/에 따로 저장."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name) / "runtime"

    def session(self, runner):
        return QwenPhotoSession([CANDIDATE], self.runtime, runner,
                                roadview=lambda c: (png_bytes(), capture()), clock=fixed_clock)

    def test_locate_only_runs_first_stage(self):
        runner = FakeRunner(localization_json())
        result = self.session(runner).locate({"candidate_id": "P0001"})
        record = result["record"]
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(record["reading_point"], [500.0, 600.0])
        self.assertNotIn("decision", record)
        self.assertNotIn("assessment", record)
        self.assertEqual(set(result["images"]), {"original"})
        self.assertEqual(len(list((self.runtime / "locate").glob("P0001-*.json"))), 1)
        self.assertFalse((self.runtime / "qwen_results.json").exists(), "판정 색인과 섞지 않음")
        self.assertFalse((self.runtime / "results").exists())

    def test_locate_statuses(self):
        for status in ("absent", "occluded"):
            text = json.dumps({"sidewalk_status": status, "evidence": "이유"}, ensure_ascii=False)
            record = self.session(FakeRunner(text)).locate({"candidate_id": "P0001"})["record"]
            self.assertEqual(record["localization"]["sidewalk_status"], status)
            self.assertIsNone(record["reading_point"])

    def test_locate_bad_response(self):
        record = self.session(FakeRunner("모르겠음")).locate({"candidate_id": "P0001"})["record"]
        self.assertIn("위치 응답 검증 실패", record["localization_error"])
        self.assertIsNone(record["reading_point"])
