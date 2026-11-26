"""pudo3 담당자 화면 로컬 서버 (127.0.0.1 전용).

  GET  /                        화면 (pudo3/web/index.html)
  GET  /pudo_v0_data.js         기본 조건 판정 결과 + 지도 근거 레이어
  GET  /api/meta                기준값, 규칙 목록, VLM 모델 정보
  GET  /api/vlm/result?candidate_id=P0001   저장된 최신 Qwen 판독 기록
  POST /api/evaluate            {thresholds, rules_off} → 판정 FeatureCollection
  POST /api/export              같은 입력 → 단일 HTML
  POST /api/vlm/roadview        {candidate_id, thresholds} → 로드뷰 캡처 + Qwen 2단계 판독 + Python 판정
  POST /api/vlm/locate          {candidate_id} → 로드뷰 캡처 + 1차 보도 판독 지점만 (2차·판정 없음)
  POST /api/vlm/reassess        {record_id, point:[x,y]} → 직접 지정한 보도 지점으로 2차 판독만 다시
"""
import argparse
import json
import math
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .config import (DATA_DIR, DEFAULT_MODEL_ID, MODEL_PATH_ENV, RUNTIME_DIR, SV_DIR, TH, WEB_DIR,
                     ModelNotFoundError, require_vision_model, resolve_model_path)

MAX_BODY = 64 * 1024


class RequestError(ValueError):
    pass


def parse_conditions(body):
    try:
        payload = json.loads(body or b"{}")
    except (ValueError, UnicodeError):
        raise RequestError("JSON 본문이 필요합니다.") from None
    if not isinstance(payload, dict):
        raise RequestError("JSON 객체가 필요합니다.")
    overrides = payload.get("thresholds") or {}
    rules_off = payload.get("rules_off") or []
    if not isinstance(overrides, dict) or not isinstance(rules_off, list):
        raise RequestError("thresholds는 객체, rules_off는 배열이어야 합니다.")
    unknown = set(overrides) - set(TH)
    if unknown:
        raise RequestError(f"모르는 기준값: {', '.join(sorted(unknown))}")
    thresholds = dict(TH)
    for name, value in overrides.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise RequestError(f"{name}: 0 이상의 숫자가 필요합니다.")
        thresholds[name] = value
    from .rules import RULE_NAMES
    if any(not isinstance(code, str) or code not in RULE_NAMES for code in rules_off):
        raise RequestError("rules_off에 모르는 규칙 코드가 있습니다.")
    return thresholds, rules_off


class App:
    """측정값을 들고 판정 요청에 답합니다. HTTP와 무관하게 테스트할 수 있습니다."""

    def __init__(self, measurements, layers, web_dir, vlm=None):
        self.measurements, self.layers, self.web_dir = measurements, layers, web_dir
        self.vlm = vlm

    def _photos(self):
        return self.vlm.summaries() if self.vlm else None

    def evaluate(self, thresholds=None, rules_off=()):
        from .rules import evaluate_all
        return evaluate_all(self.measurements, thresholds=thresholds, rules_off=rules_off,
                            photo_summaries=self._photos())

    def index_html(self):
        return (self.web_dir / "index.html").read_text(encoding="utf-8")

    def meta(self):
        from .rules import RULE_NAMES
        vlm = None
        if self.vlm:
            vlm = {**self.vlm.model_info(), "loaded": getattr(self.vlm.runner, "loaded", None),
                   "pipeline": "Qwen3.5 4bit 2단계: 보도 위치 탐색(생각 끔) → 관계 기반 조건 판독(생각 켬) → Python 판정"}
        return {"thresholds": TH, "rules": RULE_NAMES, "vlm": vlm is not None, "vlm_info": vlm}

    def gis_context(self, candidate_id, thresholds):
        from .rules import evaluate_all
        measurements = [m for m in self.measurements if m["id"] == candidate_id]
        if not measurements:
            raise RequestError("모르는 후보 ID입니다.")
        feature = evaluate_all(measurements, thresholds=thresholds, rules_off=())["features"][0]
        p = feature["properties"]
        return {"candidate_id": p["id"], "coordinates": feature["geometry"]["coordinates"],
                "heading": p["heading"], "verdict": p["verdict"], "checks": p["checks"],
                "fail_reasons": p["fail_reasons"], "check_reasons": p["check_reasons"],
                "basis": "GIS 계산·전체 규칙 적용·사진 미포함"}

    def default_script(self):
        from .export import data_script
        return data_script(self.evaluate(), thresholds=TH, layers=self.layers)

    def export_html(self, thresholds, rules_off):
        from .export import data_script, standalone_html
        script = data_script(self.evaluate(thresholds, rules_off), thresholds=thresholds, layers=self.layers,
                             rules_off=rules_off)
        return standalone_html(self.index_html(), script)


def make_handler(app):
    from .roadview import RoadviewError, redact
    from .vlm_policy import VLMError

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type, extra=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, value):
            self._send(status, json.dumps(value, ensure_ascii=False), "application/json; charset=utf-8")

        def _body(self, limit=MAX_BODY):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise RequestError("올바른 Content-Length가 필요합니다.") from None
            if length < 0 or length > limit:
                raise RequestError("요청 본문이 너무 큽니다.")
            return self.rfile.read(length)

        def do_GET(self):
            parts = urlsplit(self.path)
            path = parts.path
            if path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, app.index_html(), "text/html; charset=utf-8")
            elif path == "/pudo_v0_data.js":
                self._send(HTTPStatus.OK, app.default_script(), "text/javascript; charset=utf-8")
            elif path == "/api/meta":
                self._json(HTTPStatus.OK, app.meta())
            elif path == "/api/vlm/result":
                if app.vlm is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "--vlm 옵션으로 서버를 실행하세요."})
                    return
                candidate_id = (parse_qs(parts.query).get("candidate_id") or [""])[0]
                self._json(HTTPStatus.OK, {"record": app.vlm.latest(candidate_id)})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "없는 주소입니다."})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path.startswith("/api/vlm/"):
                self._vlm_post(path)
                return
            try:
                thresholds, rules_off = parse_conditions(self._body())
            except RequestError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            if path == "/api/evaluate":
                self._json(HTTPStatus.OK, app.evaluate(thresholds, rules_off))
            elif path == "/api/export":
                self._send(HTTPStatus.OK, app.export_html(thresholds, rules_off), "text/html; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="pudo3_result.html"'})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "없는 주소입니다."})

        def _vlm_post(self, path):
            if app.vlm is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "--vlm 옵션으로 서버를 실행하세요."})
                return
            origin = self.headers.get("Origin")
            if origin and (urlsplit(origin).netloc != self.headers.get("Host") or urlsplit(origin).scheme != "http"):
                self._json(HTTPStatus.FORBIDDEN, {"error": "같은 웹 화면에서 요청하세요."})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON 본문이 필요합니다."})
                return
            if path not in ("/api/vlm/roadview", "/api/vlm/reassess", "/api/vlm/locate"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "없는 주소입니다."})
                return
            try:
                body = self._body()
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise RequestError("JSON 객체가 필요합니다.")
                if path == "/api/vlm/reassess":
                    self._json(HTTPStatus.OK, app.vlm.reassess(payload))
                    return
                if path == "/api/vlm/locate":
                    self._json(HTTPStatus.OK, app.vlm.locate(payload))
                    return
                thresholds, _ = parse_conditions(body)
                context = app.gis_context(payload.get("candidate_id"), thresholds)
                result = app.vlm.analyze_roadview(payload, gis_context=context)
                result["gis_context"] = context
                self._json(HTTPStatus.OK, result)
            except RoadviewError as error:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": f"로드뷰 실패 {error}", "stage": error.stage})
            except (RequestError, VLMError, ValueError, UnicodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": redact(error)})
            except Exception as error:
                import traceback
                traceback.print_exc()
                print(f"VLM 오류: {type(error).__name__}: {redact(error)}", flush=True)
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"error": f"VLM 실행 실패 ({type(error).__name__}). 서버 창의 오류를 확인하세요."})

        def log_message(self, format, *args):
            pass

    return Handler


def create_server(app, port=8000):
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m pudo3 serve", description="pudo3 담당자 화면 로컬 서버 (127.0.0.1 전용)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    parser.add_argument("--refresh", action="store_true", help="측정값을 pudo3/runtime/prepared/에 다시 준비")
    parser.add_argument("--vlm", action="store_true", help=f"선택한 후보의 로드뷰를 Qwen3.5({DEFAULT_MODEL_ID}, 4bit)로 판독 (GPU 필요)")
    parser.add_argument("--vlm-model", help=f"로컬 Qwen3.5 모델 폴더 (우선순위: 이 옵션 > {MODEL_PATH_ENV} > 기본 경로)")
    parser.add_argument("--save-diagnostics", action="store_true",
                        help="스냅 표시·확대 이미지와 원본 응답을 pudo3/runtime/diagnostics/에 저장")
    args = parser.parse_args(argv)

    model_path = None
    if args.vlm:
        model_path = resolve_model_path(args.vlm_model)
        try:
            require_vision_model(model_path)
        except ModelNotFoundError as error:
            raise SystemExit(str(error)) from None
        from .vlm_inference import check_dependencies, check_thinking_switch
        from .vlm_policy import VLMError
        print("Qwen3.5 실행 패키지 확인 중…", flush=True)
        try:
            check_dependencies()
        except VLMError as error:
            raise SystemExit(str(error)) from None
        try:
            problem = check_thinking_switch(model_path)
        except Exception as error:      # 템플릿 확인 실패는 경고만 (판독 때 다시 드러남)
            problem = f"채팅 템플릿을 확인하지 못했습니다 ({type(error).__name__}: {error})"
        print(f"주의: {problem}" if problem else "생각 전환 확인: 1차 생각 끔 · 2차 생각 켬", flush=True)
    elif args.vlm_model:
        parser.error("--vlm-model은 --vlm과 함께 쓰세요.")

    from .measure import load_prepared
    print("측정값 준비 중… (pudo3/runtime/prepared/, 입력이 바뀌었을 때만 다시 계산)", flush=True)
    measurements, layers = load_prepared(DATA_DIR, SV_DIR, refresh=args.refresh, runtime_dir=RUNTIME_DIR)
    vlm = None
    if args.vlm:
        from .vlm import create_session
        vlm = create_session(measurements, RUNTIME_DIR, model_path, save_diagnostics=args.save_diagnostics)
        print(f"VLM: {DEFAULT_MODEL_ID} · 로컬 경로 {model_path} (첫 판독 요청 때 로딩)", flush=True)
    app = App(measurements, layers, WEB_DIR, vlm=vlm)
    try:
        server = create_server(app, args.port)
    except OSError:
        parser.error(f"포트 {args.port}을 열 수 없습니다. 이미 실행 중이면 --port 8001처럼 바꿔 주세요.")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"후보 {len(measurements)}개. 화면: {url}  (끄려면 Ctrl+C)", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 껐습니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
