"""담당자 화면용 로컬 서버: 화면에서 기준값·규칙을 바꾸면 rules.py로 다시 판정합니다.

시작할 때 측정값(data/prepared/)을 한 번 읽어 두고(없거나 오래되면 준비), 요청마다 판정만 합니다.
127.0.0.1에만 열어 같은 컴퓨터에서만 접속됩니다. 외부 공개는 하지 않습니다.

  GET  /                  화면 (web/index.html)
  GET  /pudo_v0_data.js   기본 조건 판정 결과 + 지도 근거 레이어
  GET  /api/meta          기본 기준값, 규칙 목록
  POST /api/evaluate      {thresholds: {...}, rules_off: [...]} → 판정 FeatureCollection
  POST /api/export        같은 입력 → 현재 조건 결과가 들어간 단일 HTML (공유용)
"""

import argparse
import json
import math
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import DATA_DIR, SV_DIR, TH, WEB_DIR
from .export import data_script, standalone_html
from .measure import load_prepared
from .rules import RULE_NAMES, evaluate_all
from .vision import load_assessments

MAX_BODY = 64 * 1024


class RequestError(ValueError):
    pass


def parse_conditions(body):
    """요청 본문 → (기준값 전체, 끈 규칙). 모르는 기준값 이름이나 숫자가 아닌 값은 거절합니다."""
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
    if any(not isinstance(code, str) or code not in RULE_NAMES for code in rules_off):
        raise RequestError("rules_off에 모르는 규칙 코드가 있습니다.")
    return thresholds, rules_off


class App:
    """측정값을 들고 판정 요청에 답합니다. HTTP와 무관하게 테스트할 수 있습니다."""

    def __init__(self, measurements, layers, web_dir, photos=None, photo_min_confidence=0.8):
        self.measurements, self.layers, self.web_dir = measurements, layers, web_dir
        self.photos, self.photo_min_confidence = photos, photo_min_confidence

    def evaluate(self, thresholds=None, rules_off=()):
        return evaluate_all(self.measurements, thresholds=thresholds, rules_off=rules_off,
                            photo_assessments=self.photos, photo_min_confidence=self.photo_min_confidence)

    def index_html(self):
        return (self.web_dir / "index.html").read_text(encoding="utf-8")

    def default_script(self):
        return data_script(self.evaluate(), thresholds=TH, layers=self.layers)

    def export_html(self, thresholds, rules_off):
        script = data_script(self.evaluate(thresholds, rules_off), thresholds=thresholds, layers=self.layers, rules_off=rules_off)
        return standalone_html(self.index_html(), script)


def make_handler(app):
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

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise RequestError("요청 본문이 너무 큽니다.")
            return self.rfile.read(length)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, app.index_html(), "text/html; charset=utf-8")
            elif path == "/pudo_v0_data.js":
                self._send(HTTPStatus.OK, app.default_script(), "text/javascript; charset=utf-8")
            elif path == "/api/meta":
                self._json(HTTPStatus.OK, {"thresholds": TH, "rules": RULE_NAMES})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "없는 주소입니다."})

        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                thresholds, rules_off = parse_conditions(self._body())
            except RequestError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            if path == "/api/evaluate":
                self._json(HTTPStatus.OK, app.evaluate(thresholds, rules_off))
            elif path == "/api/export":
                self._send(HTTPStatus.OK, app.export_html(thresholds, rules_off), "text/html; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="pudo_result.html"'})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "없는 주소입니다."})

        def log_message(self, format, *args):   # 요청마다 출력하지 않습니다
            pass

    return Handler


def create_server(app, port=8000):
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))


def main(argv=None):
    parser = argparse.ArgumentParser(description="담당자 화면 로컬 서버 (127.0.0.1 전용)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    parser.add_argument("--refresh", action="store_true", help="측정값을 다시 준비")
    parser.add_argument("--photo-results", help="사진 판독 결과 JSON (선택)")
    parser.add_argument("--photo-min-confidence", type=float, default=0.8)
    args = parser.parse_args(argv)
    print("측정값 준비 중… (입력이 바뀌었을 때만 다시 계산)", flush=True)
    measurements, layers = load_prepared(DATA_DIR, SV_DIR, refresh=args.refresh)
    photos = load_assessments(args.photo_results) if args.photo_results else None
    app = App(measurements, layers, WEB_DIR, photos, args.photo_min_confidence)
    try:
        server = create_server(app, args.port)
    except OSError:
        parser.error(f"포트 {args.port}을 열 수 없습니다. 이미 실행 중이면 그 창을 쓰거나 --port 8001처럼 바꿔 주세요.")
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
