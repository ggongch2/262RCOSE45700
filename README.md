# P1. 강남역 자율주행차 승하차(PUDO) 지점 도출

실전SW프로젝트 (카카오모빌리티 × 고려대). 강남역 반경 500m의 도로변 후보를 지도 데이터로 판정하고, 운영자가 근거를 검토해 승인·반려하는 v0 프로토타입입니다.

## 빠른 시작: 담당자 화면 켜기

처음 한 번은 1~4단계를 모두 하고, 그다음부터는 5단계만 하면 됩니다. 명령은 모두 **저장소 폴더**(`pyproject.toml`이 있는 곳)에서 실행합니다. Windows PowerShell 기준이며, macOS·Linux는 괄호 안의 명령을 씁니다.

**1. 코드 받기**

```powershell
git pull
```

**2. 데이터 넣기**

팀 드라이브의 `PUDO_data_YYYYMMDD.zip`을 저장소 폴더에 두고 압축을 풉니다. 저장소 폴더 바로 아래에 `data\` 폴더가 생겨야 합니다(`PUDO_data_...\data\`처럼 한 단계 더 들어가면 안 됩니다). 공유 묶음에는 수집한 원본만 들어 있고, 후보와 측정값은 4·5단계에서 만듭니다.

**3. 설치 (처음 한 번)**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # (macOS·Linux: source .venv/bin/activate)
python -m pip install -e .
```

- 성공하면 프롬프트 앞에 `(.venv)`가 붙습니다. **터미널을 새로 열 때마다 `.venv\Scripts\Activate.ps1`을 다시 실행**해야 합니다. 이걸 빼먹으면 `No module named 'numpy'` 같은 오류가 납니다.
- `Activate.ps1`이 "스크립트를 실행할 수 없습니다"로 막히면 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`를 한 번 실행한 뒤 다시 시도합니다.
- Python 3.11 이상이 필요합니다(`python --version`).

**4. 후보 만들기 (데이터를 새로 받았을 때 한 번)**

```powershell
python -m pudo candidates
python -m pudo check              # data\ 파일이 모두 "있음"인지 확인
```

**5. 화면 켜기**

```powershell
python -m pudo serve
```

브라우저가 `http://127.0.0.1:8000`으로 자동으로 열립니다. 처음 켤 때는 후보별 측정값을 계산하느라 몇 초 걸립니다. 끌 때는 터미널에서 `Ctrl+C`를 누릅니다. 이 주소는 자기 컴퓨터에서만 열립니다.

### 화면에서 하는 일

- **적용 규칙**: 체크를 끄면 그 규칙(교차로, 횡단보도, 어린이보호구역, 보도 등)을 빼고 다시 판정합니다. 끈 규칙 때문에 막혀 있던 곳은 굵은 테두리 초록 점으로 표시되고, 판정 필터의 "규칙 해제로 가능이 된 곳"으로 모아 볼 수 있습니다.
- **기준값 바꾸기**: 펼쳐서 거리 기준(횡단보도 10m, 소화전 5m, 통과 폭 5.5m 등)을 바꾸면 Python이 바로 다시 판정합니다. 바꾼 칸은 파란 테두리로 표시되고, "기본값으로"로 되돌립니다.
- **현재 조건 HTML 저장**: 지금 조건의 결과를 서버 없이 열리는 파일 하나로 내려받습니다. 결과를 팀원이나 멘토에게 보낼 때 씁니다.
- 지도의 점을 누르면 항목별 값·기준·결과와 불가·확인 사유가 나옵니다. 승인·반려·메모는 그 브라우저에 저장되고 "GeoJSON 내보내기"로 파일에 담깁니다.

`web/pudo_v0_standalone.html`(또는 저장한 결과 HTML)을 그냥 열면 서버 없이 결과를 볼 수 있습니다. 이때는 규칙 켜고 끄기만 되고 기준값은 바꿀 수 없습니다.

## 개발 환경

설치는 위 3단계와 같습니다. `python -m pudo check`는 API 키 설정 여부와 필요한 파일의 존재 여부만 출력합니다. 키 값은 출력하지 않고, 외부 API를 호출하지 않습니다. 테스트는 `python -m unittest discover -s tests`로 실행합니다.

### API 키

API 키는 데이터를 새로 수집할 때(`python -m pudo download`, `streetview`, `mapillary`)만 필요합니다. 화면을 켜고 판정하는 데는 필요 없습니다. 

```dotenv
google_api=...
mapillary_api=MLY|...
vworld_api=...
data_go_kr_api=...
```

| 서비스 | 환경변수 이름 | 기존 env 이름 |
|---|---|---|
| Google Street View | `GOOGLE_MAPS_KEY` | `google_api` |
| Mapillary | `MLY_TOKEN` | `mapillary_api` |
| VWorld | `VWORLD_API_KEY` | `vworld_api` |
| 공공데이터포털 (어린이보호구역 표준데이터) | `DATA_GO_KR_API_KEY` | `data_go_kr_api` |

VWorld 키는 지도 다운로드에, 공공데이터포털 키(일반 인증키 Decoding)는 어린이보호구역 지정 시설 수집에 사용합니다. 공공데이터포털 키가 없으면 해당 파일만 건너뜁니다. 팀 드라이브의 데이터 묶음을 쓰면 키 없이 판정까지 실행할 수 있습니다.

### 지도 데이터 준비

루트 `env`에 VWorld 키를 넣은 뒤 다음 명령으로 지도 입력을 준비합니다. 

```bash
python -m pudo download
```

OSM 도로망, VWorld 도로 필지·도시계획시설 도로와 정밀도로지도를 수집합니다. `env`에 공공데이터포털 키(`data_go_kr_api`)가 있으면 어린이보호구역 지정 시설 목록도 받습니다. OSM은 후보 생성 대상 차도만 남기며 보행로 접속부를 차량 교차로에 포함하지 않습니다. 시설·규제 레이어는 저장소 `data_preview/gangnam_preview.html`에 내장된 기존 스냅샷을 복원합니다. 시설 레이어는 이번 실행에서 최신 API로 갱신한 데이터가 아닙니다. 어린이보호구역 구간 복원에 쓰는 서울시 T-GIS 안전표지(OA-15540)와 노면 문자표시(OA-15535)는 서울 열린데이터광장에서 키 없이 원본 shp를 받아 강남역 주변만 WGS84로 변환해 저장합니다. 수집 시점·출처·건수·파일 해시는 `data/download_manifest.json`에 기록합니다. 이미 존재하는 파일은 유지하고, `--refresh`를 명시하면 다시 수집합니다. 

```text
data/
├── osm_roads_gangnam.json
├── layers_gangnam.json
├── vworld_road_parcels_gangnam.geojson
├── hdmap/
│   ├── lt_l_a2link.geojson
│   └── lt_l_b2surfacelinemark.geojson
└── tgis/
    ├── A064_P_signs.geojson
    └── A054_P_text_marks.geojson
```

후보 생성에는 OSM 도로망, 차로링크, 도로 필지가 필요합니다. 판정에는 후보 파일, 시설 레이어, OSM 도로망이 필요하고, 정밀도로지도와 거리뷰 메타데이터는 있으면 함께 사용합니다.

지적도의 유효하지 않은 도형은 후보 생성 시 분석용으로 보정하고 내려받은 원본은 유지합니다. 과거 거리뷰 메타데이터는 후보 ID와 조회 좌표가 모두 맞을 때만 연결합니다.

## 실행

```bash
python -m pudo candidates   # → data/candidates_v1.geojson
python -m pudo prepare      # 준비: 후보별 측정값 → data/prepared/ (판정 없음)
python -m pudo pipeline     # 기본 조건 판정 → data/pudo_v0.geojson, web/pudo_v0_data.js, 단독 데모 HTML
python -m pudo serve        # 담당자 화면: 조건을 바꿔 가며 판정 (127.0.0.1 전용)
```

처리는 세 단계로 나뉩니다. **수집**(`download`)은 원본을 받고, **준비**(`candidates`, `prepare`)는 후보 위치와 후보별 측정값(시설까지 거리, 보호구역 안/밖, 차로·폭 등)을 구하며, **판정**(`pipeline`)은 측정값에 기준값(`pudo/config.py`의 `TH`)과 규칙을 적용합니다. 준비는 기준값을 쓰지 않고, 데이터에 없는 값은 가정하지 않고 모름으로 둡니다. `pipeline`은 측정값이 없거나 입력보다 오래되었을 때만 준비를 다시 합니다.

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
  datasets.py     준비 입력 로딩 (지도·후보·거리뷰 메타데이터 → MapData)
  zones.py        어린이보호구역 구간 복원
  measure.py      준비: 후보별 측정값 (지리 계산, 기준값 미사용)
  rules.py        판정: 측정값 + 기준값 + 규칙 → 가능/확인 필요/불가
  vision.py       사진 판독 결과 검증과 판정 병합 (모델 호출 없음)
  export.py       GeoJSON·웹 파일 저장과 요약
  pipeline.py     측정값 로딩(필요하면 준비) → 판정 → 출력
  server.py       담당자 화면 로컬 서버 (화면의 조건 → rules.py 판정 → 결과)
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

지도 기반 후보 생성·판정, 운영자 검토, GeoJSON 내보내기가 구현되어 있습니다. 사진 판독 결과를 받아 판정에 합치는 입력 계약도 구현되어 있습니다. 사진에서 이 결과를 만드는 VLM 판독 단계와 정답셋 평가는 후속 작업입니다. 

교차로(H1)는 도로교통법 제32조 1·2호대로 교차로 안과 교차로 가장자리 5m를 금지하며, 교차로 면은 차도 폭으로 추정합니다. 어린이보호구역(H6, 제32조 8호)은 T-GIS 표지(시작·해제)와 노면 문자표시로 도로 구간을 복원한 추정값이며, T-GIS 파일이 없으면 학교 반경 300m 규칙으로 돌아갑니다. 공공데이터포털 어린이보호구역 지정 시설 목록이 있으면 시설 주변 도로를 표지 근거 없는 지정 시설 주변으로 보고 확인 필요로 둡니다. 택시승강장(H8)은 서울시 택시승차대 목록(`data/taxi_stands.xlsx`)의 승차대에서 같은 쪽 도로를 따라 택시 1대(6m) 안을 불가, 2대(12m) 안을 확인 필요로 둡니다(정차 구간 길이는 자료에 없음). 편도 1차로 도로의 통과 폭(P1)은 지적도 폭과 도시계획시설 도로 등급의 법정 최소 폭을 함께 봅니다. 보호구역 구간 안은 항상 불가입니다. 차로 수와 보도는 자료가 없으면 가정하지 않고 모름(확인 필요)으로 둡니다. 임계값은 `pudo/config.py`의 `TH`에서 관리합니다.

```bash
python -m unittest discover -s tests -v
```

## 데이터 출처와 라이선스

| 데이터 | 제공 | 라이선스 |
|---|---|---|
| 횡단보도 · 버스정류소 · 소방용수시설 · 교통안전시설(규제선·표지·노면 문자표시) | 서울 열린데이터광장 (서울특별시) | 공공누리 1유형 (출처 표시) |
| 어린이보호구역 표준데이터 | 공공데이터포털 | 공공누리 (출처 표시) |
| 도로망 | © OpenStreetMap contributors | ODbL |
| 연속지적도 · 정밀도로지도 | 국토교통부 VWorld, 국토지리정보원 | 공공누리 1유형 (출처 표시) |
| 거리뷰 메타데이터 | Google Maps Platform | 파노라마 ID만 저장. 촬영일·파노라마 좌표는 저장하지 않음 |
| 거리뷰 메타데이터 | Mapillary | CC BY-SA |

