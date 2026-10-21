# P1. 강남역 자율주행차 승하차(PUDO) 지점 도출

실전SW프로젝트 (카카오모빌리티 × 고려대). 기획안은 `기획안_초안_P1_PUDO.md` (같은 내용 `.docx`).

## 지금 돌려보기 (1주차 뼈대 v0)

```powershell
python scripts/make_candidates.py  # 후보점 2,440개 생성 → data/candidates_v1.geojson
python scripts/pipeline_v0.py      # 판정 → data/pudo_v0.geojson, web/pudo_v0_standalone.html
start web/index.html               # 지도 + 지점 상세 화면 (인터넷 필요: 지도 타일)
start data_preview/gangnam_preview.html   # 원본 데이터 레이어를 겹쳐 보는 페이지
```

v0는 지도 데이터만 쓴다. 사진 인식(보도·연석·현재 노면)은 2주차.

## 폴더

| 경로 | 내용 |
|---|---|
| `scripts/pipeline_v0.py` | 후보점 → 합법·물리·승객 접근 판정 → GeoJSON. 임계값은 파일 위 `TH` 딕셔너리 |
| `scripts/streetview_fetch.py` | Google 거리뷰 메타데이터(무료)로 사진 유무·촬영일 확인, `--fetch-images`로 사진 요청 |
| `scripts/mapillary_coverage.py` | Mapillary 사진 위치·촬영일 수집 |
| `scripts/make_candidates.py` → `data/candidates_v1.geojson` | 후보점 2,440개 (OSM 도로 15m 간격, 방향별 우측 연석 차로) |
| `data/layers_gangnam.json` | 횡단보도·소화전·정류소·학교·규제차선·표지 (강남역 600m) |
| `data/hdmap/` | 정밀도로지도 (VWorld WFS): 차로링크, 노면선, 표지 |
| `data/vworld_road_parcels_gangnam.geojson` | 지적도 도로 필지 (도로 폭 계산용) |
| `data/pudo_v0.geojson` | v0 판정 결과 |
| `sv_out/` | Google·Mapillary 커버리지 실측, 샘플 사진 24장 |
| `web/` | 운영자 화면 (Leaflet, 서버 없이 열림) |

## 키

`env` 파일(커밋 안 됨)에 한 줄씩:

```
google_api=...
mapillary_api=MLY|...
vworld_api=...
```

## 데이터 출처와 라이선스

| 데이터 | 제공 | 라이선스 |
|---|---|---|
| 횡단보도 · 버스정류소 · 소방용수시설 · 교통안전시설(규제선·표지) | 서울 열린데이터광장 (서울특별시) | 공공누리 1유형 (출처 표시) |
| 어린이보호구역 표준데이터 | 공공데이터포털 | 공공누리 (출처 표시) |
| 도로망 | © OpenStreetMap contributors | ODbL |
| 연속지적도 · 정밀도로지도 | 국토교통부 VWorld, 국토지리정보원 | 공공누리 1유형 (출처 표시) |
| 거리뷰 메타데이터 | Google Maps Platform | 파노라마 ID만 저장 (정책상 허용 범위). 사진·촬영일·파노라마 좌표는 저장하지 않음 |
| 거리뷰 메타데이터 | Mapillary | CC BY-SA |

거리뷰 사진 파일은 저장소에 두지 않는다. 운영자 화면은 카카오맵·구글맵 사이트로 가는 링크만 쓴다.
