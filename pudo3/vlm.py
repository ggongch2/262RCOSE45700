"""선택한 후보 1개: 카카오 로드뷰 캡처 → Qwen3.5 2단계 판독 (1차 생각 끔 · 2차 생각 켬) → Python 판정 → pudo3/runtime/ 저장.

기존 Kanana 결과(data/vlm/)와 섞지 않습니다.
  pudo3/runtime/results/<후보>-<시각>-<id>.json   판독 기록 (1회 1파일)
  pudo3/runtime/qwen_results.json                 후보별 최신 판독 요약 색인
  pudo3/runtime/diagnostics/                      진단 이미지·원본 응답 (--save-diagnostics일 때만)
  pudo3/runtime/logs/roadview-errors.log          로드뷰 오류 단계 기록 (키 제외)
"""
import base64
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import DEFAULT_MODEL_ID
from .roadview import RoadviewError, redact, validate_capture, validate_target_pixel
from .vision import b64_png, encode_png, sha256
from .geometry import search_box
from .vlm_inference import QwenRunner, run_locate, run_manual, run_stop_check
from .vlm_policy import VLMError

RECORD_SCHEMA = "pudo3-qwen-photo.v4"
MEMORY_LIMIT = 30
LOCATE_SCHEMA = "pudo3-qwen-locate.v1"
INDEX_SCHEMA = "pudo3-qwen-index.v1"


def _now():
    return datetime.now(timezone.utc)


def curb_line_required(gis_context):
    """GIS가 규제선(H5)을 이미 판정했으면 사진 규제선 미확인을 판단 불가 사유로 보지 않습니다."""
    if not gis_context:
        return True
    h5 = next((c for c in gis_context.get("checks", []) if c.get("item") == "H5 규제선"), None)
    return h5 is None or h5.get("pass") is None


class QwenPhotoSession:
    def __init__(self, measurements, runtime_dir, runner, roadview=None, save_diagnostics=False, clock=_now,
                 log=None):
        self.candidates = {m["id"]: m for m in measurements}
        self.runtime_dir = Path(runtime_dir)
        self.results_dir = self.runtime_dir / "results"
        self.diagnostics_dir = self.runtime_dir / "diagnostics"
        self.index_path = self.runtime_dir / "qwen_results.json"
        self.log_path = self.runtime_dir / "logs" / "roadview-errors.log"
        self.runner = runner
        self.roadview = roadview
        self.save_diagnostics = save_diagnostics
        self.clock = clock
        self.log = log or (lambda message: None)
        self.lock = threading.Lock()     # 후보 판독 전체(로드뷰+2회 추론)를 직렬화
        self.memory = {}                 # record_id → 원본 (직접 지정 재판독용, 메모리 전용)
        self.index = {}
        if self.index_path.exists():
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            if payload.get("schema_version") == INDEX_SCHEMA:
                self.index = payload.get("candidates", {})

    # -------------------------------------------------------------- 저장
    def _write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _log_error(self, candidate_id, error):
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(f"{self.clock().isoformat()}\t{candidate_id}\t{redact(error)}\n")
        except OSError:
            pass

    def summaries(self):
        return {cid: {k: row[k] for k in ("verdict", "reason", "generated_at", "record_id")}
                for cid, row in self.index.items()}

    def latest(self, candidate_id):
        row = self.index.get(candidate_id)
        if not row:
            return None
        path = self.results_dir / row["file"]
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # -------------------------------------------------------------- 진행 표시 (터미널)
    def _progress(self, candidate_id):
        started = time.monotonic()

        def report(message):
            self.log(f"[{candidate_id} +{time.monotonic() - started:.0f}초] {message}")
        if not getattr(self.runner, "loaded", True):
            report("모델 로딩이 먼저 진행됩니다 (서버를 켠 뒤 첫 판독만, 몇 분 걸림)")
        return report

    def _capture(self, candidate, progress):
        progress("로드뷰 캡처 시작")
        started = time.monotonic()
        try:
            image, metadata = self.roadview(candidate)
        except RoadviewError as error:
            progress(f"로드뷰 실패: {redact(str(error))}")
            raise
        progress(f"로드뷰 캡처 끝 ({time.monotonic() - started:.0f}초)")
        return image, metadata

    def _done(self, progress, result):
        decision = result["record"]["decision"]
        progress(f"판독 끝: {decision['verdict']} — {decision['reason']}")
        return result

    # -------------------------------------------------------------- 판독
    def model_info(self):
        info = self.runner.info() if hasattr(self.runner, "info") else {}
        return {"model_id": info.get("model_id", DEFAULT_MODEL_ID), "model_path": info.get("model_path"),
                "dtype": info.get("dtype", "4bit nf4 (계산 float16)"), "attn_implementation": info.get("attn_implementation"),
                "thinking": info.get("thinking")}

    def analyze_roadview(self, payload, gis_context=None):
        if not isinstance(payload, dict) or not isinstance(payload.get("candidate_id"), str):
            raise VLMError("후보 ID가 필요합니다.")
        candidate = self.candidates.get(payload["candidate_id"])
        if candidate is None:
            raise VLMError("후보를 지도에서 선택하세요.")
        with self.lock:
            progress = self._progress(candidate["id"])
            if self.roadview is None:
                from .roadview import KakaoRoadview
                self.roadview = KakaoRoadview()
            try:
                image, metadata = self._capture(candidate, progress)
                metadata = validate_capture(metadata, candidate)
                metadata["target_pixel"] = validate_target_pixel(metadata.get("target_pixel"))
            except RoadviewError as error:
                self._log_error(candidate["id"], error)
                raise
            search = search_box(metadata["camera"], candidate, metadata["viewpoint"]["pan"])
            stage = run_stop_check(self.runner, image, metadata, search=search,
                                   curb_line_required=curb_line_required(gis_context), progress=progress)
            return self._done(progress, self._finish(candidate, image, metadata, stage, gis_context))

    def locate(self, payload):
        """위치 찾기만: 로드뷰 캡처 → 1차 보도 판독 지점. 결과는 runtime/locate/에 따로 저장 (판정 색인과 분리)."""
        if not isinstance(payload, dict) or not isinstance(payload.get("candidate_id"), str):
            raise VLMError("후보 ID가 필요합니다.")
        candidate = self.candidates.get(payload["candidate_id"])
        if candidate is None:
            raise VLMError("후보를 지도에서 선택하세요.")
        with self.lock:
            progress = self._progress(candidate["id"])
            if self.roadview is None:
                from .roadview import KakaoRoadview
                self.roadview = KakaoRoadview()
            try:
                image, metadata = self._capture(candidate, progress)
                metadata = validate_capture(metadata, candidate)
                metadata["target_pixel"] = validate_target_pixel(metadata.get("target_pixel"))
            except RoadviewError as error:
                self._log_error(candidate["id"], error)
                raise
            search = search_box(metadata["camera"], candidate, metadata["viewpoint"]["pan"])
            stage = run_locate(self.runner, image, metadata, search=search, progress=progress)
            progress("위치 찾기 끝")
            images = stage.pop("images")
            now = self.clock()
            record = {
                "schema_version": LOCATE_SCHEMA, "record_id": uuid.uuid4().hex, "generated_at": now.isoformat(),
                "candidate_id": candidate["id"], "candidate_coordinates": candidate["coordinates"],
                "pano_id": metadata["pano_id"], "camera": metadata["camera"], "distance_m": metadata["distance_m"],
                "camera_offset": metadata.get("camera_offset"), "query_heading": metadata.get("query_heading"),
                "viewpoint": metadata["viewpoint"], "view_mode": metadata.get("view_mode"),
                "target_pixel": metadata["target_pixel"], "image_size": stage["image_size"],
                "search": stage["search"], "reading_source": "auto",
                "localization": stage["localization"], "localization_raw": stage["localization_raw"],
                "localization_error": stage["localization_error"], "timing": stage["timing"],
                "reading_point": stage["reading_point"], "reading_pixel": stage["reading_pixel"],
                "sidewalk_region_pixel": stage["sidewalk_region_pixel"],
                "snapped_curb_point": stage["snapped_curb_point"], "curb_region_pixel": stage["curb_region_pixel"],
                "model": self.model_info(), "image_sha256": sha256(image),
            }
            stamp = record["generated_at"].replace(":", "").replace("-", "").split(".")[0]
            name = f"{candidate['id']}-{stamp}-{record['record_id'][:8]}"
            self._write_json(self.runtime_dir / "locate" / f"{name}.json", record)
            if self.save_diagnostics:
                folder = self.diagnostics_dir / f"locate-{name}"
                folder.mkdir(parents=True, exist_ok=True)
                for key, img in images.items():
                    (folder / f"{key}.png").write_bytes(encode_png(img))
            return {"record": record, "images": {"original": base64.b64encode(image).decode()}}

    def _finish(self, candidate, image, metadata, stage, gis_context, manual_from=None):
        record = self._record(candidate, image, metadata, stage, gis_context)
        if manual_from:
            record["manual_from_record"] = manual_from
        images = stage.pop("images")
        self._save(record, images)
        self._remember(record["record_id"], candidate, image, metadata, gis_context, stage.get("search"))
        return {"record": record,
                "images": {name: b64_png(img) for name, img in images.items()
                           if name != "original"} | {"original": base64.b64encode(image).decode()}}

    def _remember(self, record_id, candidate, image, metadata, gis_context, search):
        """직접 지정 재판독용 원본을 메모리에만 보관 (파일로 저장하지 않음, 최근 MEMORY_LIMIT건)."""
        self.memory[record_id] = (candidate["id"], image, metadata, gis_context, search)
        while len(self.memory) > MEMORY_LIMIT:
            self.memory.pop(next(iter(self.memory)))

    def reassess(self, payload):
        """사진에서 직접 클릭한 보도 지점으로 2차 판독만 다시 실행."""
        if not isinstance(payload, dict):
            raise VLMError("JSON 객체가 필요합니다.")
        record_id, point = payload.get("record_id"), payload.get("point")
        if not isinstance(record_id, str) or record_id not in self.memory:
            raise VLMError("이 판독의 원본 사진이 서버 메모리에 없습니다 (서버 재시작 등). 로드뷰 판독을 다시 누른 뒤 위치를 지정하세요.")
        if (not isinstance(point, list) or len(point) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1000 for v in point)):
            raise VLMError("판독 지점은 0~1000 범위의 [x, y]여야 합니다.")
        candidate_id, image, metadata, gis_context, search = self.memory[record_id]
        candidate = self.candidates[candidate_id]
        with self.lock:
            progress = self._progress(candidate_id)
            progress(f"직접 지정한 지점 {[round(v) for v in point]}으로 2차 다시 판독")
            stage = run_manual(self.runner, image, point, metadata, search=search,
                               curb_line_required=curb_line_required(gis_context), progress=progress)
            return self._done(progress, self._finish(candidate, image, metadata, stage, gis_context,
                                                     manual_from=record_id))

    def _record(self, candidate, image, metadata, stage, gis_context):
        now = self.clock()
        record_id = uuid.uuid4().hex
        decision = stage["decision"]
        return {
            "schema_version": RECORD_SCHEMA,
            "record_id": record_id,
            "generated_at": now.isoformat(),
            "candidate_id": candidate["id"],
            "candidate_coordinates": candidate["coordinates"],
            "pano_id": metadata["pano_id"],
            "camera": metadata["camera"],
            "distance_m": metadata["distance_m"],
            "camera_offset": metadata.get("camera_offset"),
            "query_heading": metadata.get("query_heading", candidate["heading"]),
            "viewpoint": metadata["viewpoint"],
            "view_mode": metadata.get("view_mode"),
            "target_bearing": metadata.get("target_bearing"),
            "target_pixel": metadata["target_pixel"],
            "marker_norm": stage["marker_norm"],
            "search": stage.get("search"),
            "mode": stage.get("mode", "two_stage"),
            "reading_source": stage.get("reading_source", "auto"),
            "snapped_curb_point": stage["snapped_curb_point"],
            "snapped_curb_pixel": stage["snapped_curb_pixel"],
            "curb_region_pixel": stage["curb_region_pixel"],
            "snapped_sidewalk_point": stage["snapped_sidewalk_point"],
            "snapped_sidewalk_pixel": stage["snapped_sidewalk_pixel"],
            "sidewalk_region_pixel": stage["sidewalk_region_pixel"],
            "reading_point": stage["reading_point"],
            "reading_pixel": stage["reading_pixel"],
            "detail_crop_box": stage["detail_crop_box"],
            "curb_strip_box": stage["curb_strip_box"],
            "image_size": stage["image_size"],
            "localization": stage["localization"],
            "localization_raw": stage["localization_raw"],
            "localization_error": stage["localization_error"],
            "assessment": stage["assessment"],
            "assessment_raw": stage["assessment_raw"],
            "assessment_error": stage["assessment_error"],
            "assessment_thinking": stage["assessment_thinking"],
            "assessment_thinking_cut": stage["assessment_thinking_cut"],
            "curb_line_measure": stage["curb_line_measure"],
            "timing": stage["timing"],
            "decision": decision,
            "model": self.model_info(),
            "image_sha256": sha256(image),
            "capture": {k: metadata.get(k) for k in ("elapsed_ms", "network_idle", "image_encoding")},
            "gis_context": None if not gis_context else {
                k: gis_context.get(k) for k in ("verdict", "fail_reasons", "check_reasons")},
        }

    def _save(self, record, images):
        stamp = record["generated_at"].replace(":", "").replace("-", "").split(".")[0]
        name = f"{record['candidate_id']}-{stamp}-{record['record_id'][:8]}.json"
        self._write_json(self.results_dir / name, record)
        self.index[record["candidate_id"]] = {
            "verdict": record["decision"]["verdict"], "reason": record["decision"]["reason"],
            "generated_at": record["generated_at"], "record_id": record["record_id"], "file": name}
        self._write_json(self.index_path, {"schema_version": INDEX_SCHEMA, "candidates": self.index})
        if self.save_diagnostics:
            folder = self.diagnostics_dir / name[:-5]
            folder.mkdir(parents=True, exist_ok=True)
            for key, image in images.items():
                (folder / f"{key}.png").write_bytes(encode_png(image))
            (folder / "raw_responses.txt").write_text(
                f"[localization]\n{record['localization_raw']}\n\n[assessment thinking]\n"
                f"{record['assessment_thinking']}\n\n[assessment]\n{record['assessment_raw']}\n",
                encoding="utf-8")


def create_session(measurements, runtime_dir, model_path, *, save_diagnostics=False):
    return QwenPhotoSession(measurements, runtime_dir, QwenRunner(model_path), log=lambda m: print(m, flush=True),
                            save_diagnostics=save_diagnostics)
