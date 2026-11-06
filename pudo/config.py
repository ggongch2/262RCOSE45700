"""프로젝트 경로, API 키 로딩, 공통 공간 분석 설정."""

import os
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
SV_DIR = ROOT / "sv_out"

API_KEY_NAMES = {
    "google": ("GOOGLE_MAPS_KEY", "google_api"),
    "mapillary": ("MLY_TOKEN", "mapillary_api"),
    "vworld": ("VWORLD_API_KEY", "vworld_api"),
}


def api_key(provider, root=None, environ=None):
    """환경변수 > .env > env 순서로 키를 찾습니다. 키는 출력하지 않습니다."""
    names = API_KEY_NAMES[provider]
    environ = os.environ if environ is None else environ
    for name in names:
        if environ.get(name):
            return environ[name]
    root = ROOT if root is None else Path(root)
    for filename in (".env", "env"):
        # API 키의 $ 문자를 보존하며 파일 내용으로 환경변수를 변경하지 않습니다.
        values = dotenv_values(root / filename, encoding="utf-8-sig", interpolate=False)
        for name in names:
            if values.get(name):
                return values[name]
    return None


def require_files(*paths):
    """입력 부재 시 데이터 복원 방법을 포함한 오류를 제공합니다."""
    missing = [Path(path) for path in paths if not Path(path).is_file()]
    if missing:
        names = "\n".join(f"  - {path}" for path in missing)
        raise SystemExit(
            f"필요한 입력 파일이 없습니다:\n{names}\n"
            "pudo download로 수집하거나 보유한 원본 지도 데이터를 로컬 data/에 복사하세요. "
            "후보 파일은 pudo candidates로 생성합니다."
        )


CENTER_LON, CENTER_LAT = 127.0276, 37.4979
RADIUS_M = 500
SAMPLE_STEP_M = 15
LANE_WIDTH_M = 3.25
# 두 도로 끝이 이 각도 미만으로 꺾이며 이어지면 같은 길의 분할 지점으로 보고 교차로에서 제외.
# 강남역 데이터에서 직선 이음은 0~13°, 모퉁이는 34° 이상으로 나뉜다.
CONTINUATION_TURN_DEG = 20
DRIVABLE = {
    "primary", "primary_link", "secondary", "secondary_link", "tertiary",
    "residential", "service", "living_street", "unclassified",
}

# 지도 기반 v0 임계값. 어린이보호구역은 지정 구간 자료를 확보하기 전의 임시 규칙.
TH = {
    "H1 교차로": 5,  # 도로교통법 제32조 2호: 교차로 가장자리·도로 모퉁이로부터 5m. 교차로 면은 차도 폭으로 추정
    "H2 횡단보도": 10,
    "H3 정류장": 10,
    "H4 소화전": 5,
    "H5 규제선": 5,  # 규제선까지의 거리가 이 값 이내이면 해당 구간으로 판단
    "H5 정밀도로지도 규제선": 6,  # 정밀도로지도 규제선·버스전용차로선·황색점선까지의 거리 (v0 값 유지)
    "H6 학교": 300,  # 임시: 대상시설 반경 300m (보호구역 규칙 제3조의 지정 범위). 실제 지정 구간으로 교체 예정
    "H7 안전지대": 10,  # 정밀도로지도 안전지대 선(531)까지의 거리
    "P1 통과폭": 5.5,  # 차량 폭 2.5m + 잔여 통과 폭 3.0m
    "COORD_ERR": 7,  # 출처 간 좌표 오차. 임계값 직후 7m 구간은 확인 필요
}
