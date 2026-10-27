# P1. 강남역 자율주행차 승하차(PUDO) 지점 도출

실전SW프로젝트 (카카오모빌리티 × 고려대). 강남역 반경 500m의 도로변 후보를 지도 데이터로 판정하고, 운영자가 근거를 검토해 승인·반려하는 v0 프로토타입입니다.

## 먼저 데모 보기

`web/pudo_v0_standalone.html`을 브라우저에서 엽니다. 후보와 판정 데이터가 내장되어 있어 Python 실행이나 API 키가 필요하지 않습니다. Leaflet CDN과 지도 타일을 불러오므로 인터넷 연결은 필요합니다.

`web/index.html`은 파이프라인이 생성하는 `web/pudo_v0_data.js`가 있어야 동작합니다. 승인·반려·메모는 해당 브라우저에 저장되며, GeoJSON 내보내기로 공유합니다.

## 개발 환경

Python 3.11 이상. 저장소 루트에서 실행합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pudo check
```

Windows PowerShell에서는 `python -m venv .venv`, `.venv\Scripts\Activate.ps1`로 가상환경을 만들고 활성화한 뒤 같은 설치 명령을 실행합니다.

`check`는 API 키 설정 여부와 필요한 파일의 존재 여부만 출력합니다. 키 값은 출력하지 않고, 외부 API를 호출하지 않습니다.

### API 키

이미 전달받은 루트 `env` 파일을 그대로 사용합니다. 새 환경에서는 `.env.example`을 `.env`로 복사하고 값을 입력합니다.

```dotenv
google_api=...
mapillary_api=MLY|...
vworld_api=...
```

환경변수 → `.env` → `env` 순서로 읽으며, 다음 이름을 지원합니다. 파일은 실행 위치와 관계없이 저장소 루트에서 찾습니다.

| 서비스 | 환경변수 이름 | 기존 env 이름 |
|---|---|---|
| Google Street View | `GOOGLE_MAPS_KEY` | `google_api` |
| Mapillary | `MLY_TOKEN` | `mapillary_api` |
| VWorld | `VWORLD_API_KEY` | `vworld_api` |

VWorld 키는 지도 다운로드에 사용합니다. `env`와 `.env`는 Git에서 제외합니다.

### 지도 데이터 준비

루트 `env`에 VWorld 키를 넣은 뒤 다음 명령으로 지도 입력을 준비합니다. 결과 `data/`는 Git에서 제외합니다.

```bash
python -m pudo download
```

OSM 도로망, VWorld 도로 필지와 정밀도로지도를 수집합니다. OSM은 후보 생성 대상 차도만 남기며 보행로 접속부를 차량 교차로에 포함하지 않습니다. 시설·규제 레이어는 저장소 `data_preview/gangnam_preview.html`에 내장된 기존 스냅샷을 복원합니다. 시설 레이어는 이번 실행에서 최신 API로 갱신한 데이터가 아닙니다. 수집 시점·출처·건수·파일 해시는 `data/download_manifest.json`에 기록합니다. 이미 존재하는 파일은 유지하고, `--refresh`를 명시하면 다시 수집합니다. VWorld 페이지와 WFS 반환 상한을 처리하여 일부 결과만 저장되는 것을 방지합니다.

과거 결과를 동일하게 재현하려면 보유한 당시 원본을 아래 로컬 경로로 복사하세요. 새 수집 결과는 데이터 갱신과 조회 범위에 따라 기존 후보 ID·개수와 달라질 수 있습니다. 다운로드와 실행 결과는 현재 작업 폴더에만 저장하며 드라이브 연동은 사용하지 않습니다.

```text
data/
├── osm_roads_gangnam.json
├── layers_gangnam.json
├── vworld_road_parcels_gangnam.geojson
└── hdmap/
    ├── lt_l_a2link.geojson
    └── lt_l_b2surfacelinemark.geojson
```

후보 생성에는 OSM 도로망, 차로링크, 도로 필지가 필요합니다. 판정에는 후보 파일, 시설 레이어, OSM 도로망이 필요하고, 정밀도로지도와 거리뷰 메타데이터는 있으면 함께 사용합니다.

지적도의 유효하지 않은 도형은 후보 생성 시 분석용으로 보정하고 내려받은 원본은 유지합니다. 과거 거리뷰 메타데이터는 후보 ID와 조회 좌표가 모두 맞을 때만 연결합니다.

## 실행

```bash
python -m pudo candidates   # → data/candidates_v1.geojson
python -m pudo pipeline     # → data/pudo_v0.geojson, web/pudo_v0_data.js, 단독 데모 HTML
```

사진 판독 결과가 있으면 `--photo-results`로 함께 넣습니다. 파일은 [`schemas/photo-observations.v1.schema.json`](schemas/photo-observations.v1.schema.json) 형식을 따라야 하며, 파이프라인은 모델을 호출하지 않고 이 파일만 읽습니다. 관측값은 신뢰도가 `--photo-min-confidence`(기본 0.8, 임시 운영값) 이상일 때만 판정에 반영합니다.

```bash
python -m pudo pipeline --photo-results photo_results.json
```

데이터가 없으면 누락된 파일과 복원 방법을 안내합니다. 아래 명령은 외부 API를 호출하므로 필요한 작업에서만 실행합니다.

```bash
python -m pudo streetview data/candidates_v1.geojson
python -m pudo streetview --demo
python -m pudo mapillary
```

Google은 기본적으로 메타데이터만 조회합니다. 이미지를 요청하려면 `--fetch-images`를 명시합니다. 결과는 기본적으로 저장소의 `sv_out/`에 저장하며 `--out`으로 변경할 수 있습니다. Mapillary의 `--out`은 GeoJSON 파일 경로를 받습니다.

기존 `python scripts/make_candidates.py`, `python scripts/pipeline_v0.py`, `python scripts/streetview_fetch.py`, `python scripts/mapillary_coverage.py` 명령도 유지됩니다.

## 구조

```text
pudo/             Python 구현
  config.py       경로, API 키 로딩, 판정 임계값 TH
  geo.py          공통 좌표 변환·거리 계산
  download.py     OSM·VWorld 지도 수집, 시설 스냅샷 복원
  candidates.py   방향별 도로변 후보 생성
  datasets.py     지도·후보·사진 메타데이터 로딩
  rules.py        명시적 모드(strict/lenient)별 판정
  vision.py       사진 판독 결과 검증과 판정 병합 (모델 호출 없음)
  export.py       GeoJSON·웹 파일 저장과 요약
  pipeline.py     로딩 → 판정 → 출력 실행 흐름
  streetview.py   Google 거리뷰 조회
  mapillary.py    Mapillary 커버리지 조사
  __main__.py     통합 CLI
scripts/          기존 실행 명령을 유지하는 진입점
web/              운영자 화면 및 데이터 내장 데모
data_preview/     원본 지도 레이어 확인 화면
sv_out/           커버리지 조사 결과 및 검토 자료
data/             원본·중간 지도 데이터 (Git 제외)
docs/             기획안 및 데이터 기술 스펙
schemas/          사진 판독 결과 입력 JSON Schema
tests/            설정 및 실행 회귀 검증
```

자세한 범위와 판단 기준은 [전체 기획안](docs/기획안_초안_P1_PUDO.md), 데이터 구성은 [데이터 기술 스펙](docs/기획안_⑤_데이터기술스펙.md)을 참고하세요.

실제 파일의 필드·좌표 순서·결측값·모드별 출력과 통합되지 않은 단계는 [현재 파이프라인 데이터 계약](docs/pipeline-schema.md)에 정리했습니다. 현재 v0의 단계 간 연결은 검증했으며 전 단계 공통 스키마의 런타임 검증은 아직 없습니다.

## 현재 구현 범위와 검증

지도 기반 후보 생성·판정, 운영자 검토, GeoJSON 내보내기가 구현되어 있습니다. 사진 판독 결과를 받아 판정에 합치는 입력 계약도 구현되어 있습니다. 사진에서 이 결과를 만드는 VLM 판독 단계와 정답셋 평가는 후속 작업입니다. 교차로 중심 반경과 학교 반경은 임시 규칙이며, 판정 결과에는 기본 모드와 임시 규칙 완화 모드가 함께 들어갑니다. 임계값은 `pudo/config.py`의 `TH`에서 관리합니다.

```bash
python -m unittest discover -s tests -v
```

검증은 임시 데이터와 모의 API 응답을 사용하며 실제 키나 API 호출이 필요하지 않습니다.

## 데이터 출처와 라이선스

| 데이터 | 제공 | 라이선스 |
|---|---|---|
| 횡단보도 · 버스정류소 · 소방용수시설 · 교통안전시설(규제선·표지) | 서울 열린데이터광장 (서울특별시) | 공공누리 1유형 (출처 표시) |
| 어린이보호구역 표준데이터 | 공공데이터포털 | 공공누리 (출처 표시) |
| 도로망 | © OpenStreetMap contributors | ODbL |
| 연속지적도 · 정밀도로지도 | 국토교통부 VWorld, 국토지리정보원 | 공공누리 1유형 (출처 표시) |
| 거리뷰 메타데이터 | Google Maps Platform | 파노라마 ID만 저장. 촬영일·파노라마 좌표는 저장하지 않음 |
| 거리뷰 메타데이터 | Mapillary | CC BY-SA |

거리뷰 사진 파일은 저장소에 두지 않습니다. 운영자 화면은 카카오맵·구글맵 사이트로 가는 링크를 사용합니다.
