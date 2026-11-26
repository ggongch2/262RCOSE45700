"""pudo3 경로·API 키·모델 경로·공통 공간 분석 설정.

기존 pudo 패키지와 독립적입니다. 저장소의 data/는 읽기 전용 입력으로만 쓰고,
새로 만드는 모든 결과(측정값, 판독 JSON, 진단 이미지, 로그)는 pudo3/runtime/ 아래에 둡니다.
"""

import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ROOT = PKG_DIR.parent
DATA_DIR = ROOT / "data"              # 읽기 전용 입력
SV_DIR = ROOT / "sv_out"              # 읽기 전용 입력 (Google 메타데이터가 있으면 사용)
WEB_DIR = PKG_DIR / "web"
RUNTIME_DIR = PKG_DIR / "runtime"     # 모든 신규 결과

# ---------------------------------------------------------------- Qwen3.5 모델 (4bit, 1차 생각 끔 · 2차 생각 켬)
DEFAULT_MODEL_ID = "Qwen/Qwen3.5-9B"
DEFAULT_MODEL_PATH = ROOT / "models" / "Qwen3.5-9B"
MODEL_PATH_ENV = "PUDO3_VLM_MODEL_PATH"


class ModelNotFoundError(FileNotFoundError):
    """로컬 모델 디렉터리가 없을 때. 자동 다운로드는 하지 않습니다."""


def resolve_model_path(cli_path=None, environ=None):
    """모델 경로 우선순위: CLI > 환경변수 PUDO3_VLM_MODEL_PATH > 기본 경로."""
    environ = os.environ if environ is None else environ
    if cli_path:
        return Path(cli_path).expanduser().resolve()
    if environ.get(MODEL_PATH_ENV):
        return Path(environ[MODEL_PATH_ENV]).expanduser().resolve()
    return DEFAULT_MODEL_PATH


def model_missing_message(path):
    return f"Qwen3.5 모델을 찾을 수 없습니다: {path}. 모델을 먼저 다운로드하세요."


def require_model_dir(path):
    """모델 폴더와 필수 파일(config.json)이 있는지만 확인합니다. 네트워크를 쓰지 않습니다."""
    path = Path(path)
    if not path.is_dir() or not (path / "config.json").is_file():
        raise ModelNotFoundError(model_missing_message(path))
    return path


def require_vision_model(path):
    """config.json에 이미지 입력부(vision_config)가 있는지 확인합니다. 텍스트 전용 모델이면 사진을 볼 수 없습니다."""
    import json
    path = require_model_dir(path)
    config = json.loads((Path(path) / "config.json").read_text(encoding="utf-8"))
    if "vision_config" not in config:
        raise ModelNotFoundError(f"이 모델에는 이미지 입력부(vision_config)가 없어 사진을 판독할 수 없습니다: {path}")
    return config


# ---------------------------------------------------------------- API 키
API_KEY_NAMES = {
    "kakao": ("KAKAO_MAPS_JS_KEY", "kakao_js_key"),
    "google": ("GOOGLE_MAPS_KEY", "google_api"),
    "mapillary": ("MLY_TOKEN", "mapillary_api"),
    "vworld": ("VWORLD_API_KEY", "vworld_api"),
    "datagokr": ("DATA_GO_KR_API_KEY", "data_go_kr_api"),
}


def api_key(provider, root=None, environ=None):
    """환경변수 > .env > env 순서로 키를 찾습니다. 키는 출력하지 않습니다."""
    names = API_KEY_NAMES[provider]
    environ = os.environ if environ is None else environ
    for name in names:
        if environ.get(name):
            return environ[name]
    root = ROOT if root is None else Path(root)
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    for filename in (".env", "env"):
        values = dotenv_values(root / filename, encoding="utf-8-sig", interpolate=False)
        for name in names:
            if values.get(name):
                return values[name]
    return None


def require_files(*paths):
    missing = [Path(path) for path in paths if not Path(path).is_file()]
    if missing:
        names = "\n".join(f"  - {path}" for path in missing)
        raise SystemExit(
            f"필요한 입력 파일이 없습니다:\n{names}\n"
            "기존 data/ 폴더(읽기 전용 입력)에 원본 지도 데이터가 있는지 확인하세요."
        )


# ---------------------------------------------------------------- 공간 분석 (기존 v0 값 그대로)
CENTER_LON, CENTER_LAT = 127.0276, 37.4979
RADIUS_M = 500
SAMPLE_STEP_M = 15
CONTINUATION_TURN_DEG = 20
DRIVABLE = {
    "primary", "primary_link", "secondary", "secondary_link", "tertiary",
    "residential", "service", "living_street", "unclassified",
}

TH = {
    "H1 교차로": 5,
    "H2 횡단보도": 10,
    "H3 정류장": 10,
    "H4 소화전": 5,
    "H5 규제선": 5,
    "H5 정밀도로지도 규제선": 6,
    "H6 학교": 300,
    "H6 경계 확인": 7,
    "H7 안전지대": 10,
    "H8 택시승강장": 6,
    "H8 택시승강장 확인": 12,
    "P1 통과폭 일방": 5.5,
    "P1 통과폭 양방": 8.5,
    "H1~H4 오차 범위": 7,
}
