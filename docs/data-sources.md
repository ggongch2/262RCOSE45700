# 데이터 출처와 보관 위치

`data/`는 Git에서 제외합니다. 팀 드라이브에 같은 폴더 구조로 보관하고, 이 문서로 파일마다 출처와 받는 방법을 관리합니다. 자동 수집 파일의 수집 시점·건수·해시는 `data/download_manifest.json`에 기록됩니다.

## 폴더 구조

```
data/
  candidates_v1.geojson, pudo_v0.geojson   파이프라인 산출물 (pudo candidates / pipeline으로 재생성)
  layers_gangnam.json, osm_roads_gangnam.json, vworld_*.geojson, hdmap/, tgis/
                                           pudo download로 수집
  child_protection_zones.json              공공데이터포털 API (pudo download, env data_go_kr_api 있을 때)
  taxi_stands.xlsx                         서울시 택시승차대 운영 목록 (사람이 내려받아 이름만 바꿔 둠)
  tgis/lane_facilities_2023.xlsx           서울시 차선시설물현황 2023-07 (사람이 내려받아 이름만 바꿔 둠)
  raw/                                     사람이 내려받은 원본
    ngii_hdmap/                            국토지리정보원 정밀도로지도
      정밀도로지도 설명 및 안내자료(2023. 5.).pdf   레이어·코드표 설명서
      정밀도로지도_벡터만_강남서초외.zip            아래 원본 4개에서 지도 레이어(UTMK 정표고)와 META만 추린 것 (37MB)
      정밀도로지도_*.zip                          원본 (합계 약 5GB, 주행 궤적 포함). 공유 불필요, 아래 링크에서 재다운로드
      vector_only/                                벡터만 zip을 푼 폴더
    datagokr/                              공공데이터포털 파일 다운로드
```

## 판정에 쓰는 데이터

| 데이터 | 용도 | 출처 | 받는 방법 | 로컬 경로 |
|---|---|---|---|---|
| OSM 도로망 | 후보점 생성, 교차로 | OpenStreetMap Overpass | `python -m pudo download` | `data/osm_roads_gangnam.json` |
| 횡단보도·소화전·정류장·학교 | H2~H4 거리 규칙 | 서울 열린데이터광장 (스냅샷) | `pudo download`가 `data_preview/` 스냅샷에서 복원 | `data/layers_gangnam.json` |
| 도로 필지 (지적도) | 이면도로 폭 | VWorld `LP_PA_CBND_BUBUN` | `pudo download` (env `vworld_api`) | `data/vworld_road_parcels_gangnam.geojson` |
| 정밀도로지도 차로링크·노면선 | 대로 차로 수, 규제선(2023) | VWorld WFS `lt_l_a2link`, `lt_l_b2surfacelinemark` | `pudo download` | `data/hdmap/` |
| T-GIS 안전표지·노면 문자 | 어린이보호구역 구간 복원 | [서울 OA-15540](https://data.seoul.go.kr/dataList/OA-15540/S/1/datasetView.do), [OA-15535](https://data.seoul.go.kr/dataList/OA-15535/S/1/datasetView.do) | `pudo download` (키 불필요) | `data/tgis/` |
| 어린이보호구역 지정 시설 | H6: 지정 시설 주변 도로를 확인 필요로 표시 | [전국어린이보호구역표준데이터](https://www.data.go.kr/data/15012891/standard.do) API `tn_pubr_public_child_prtc_zn_api` | 공공데이터포털 활용신청 → env `data_go_kr_api` → `pudo download` (전국을 받아 중심 2km만 저장) | `data/child_protection_zones.json` |
| 도시계획시설 도로 | P1: 공식 폭 등급 (광로·대로·중로·소로) | VWorld `LT_C_UPISUQ151` | `pudo download` (env `vworld_api`) | `data/vworld_upis_roads_gangnam.geojson` |
| 서울시 차선시설물현황 (2023-07) | H5: 2021 서울시 금지선이 2023년에도 있는지 확인 (정밀도로지도가 없는 도로) | [서울 OA-15537](https://data.seoul.go.kr/dataList/OA-15537/S/1/datasetView.do)의 `차선시설물현황_0724.xlsx`. 선마다 대표 좌표 점 하나(EPSG:5186)와 차선종류. 같은 데이터셋의 `A058_L_차선.zip`은 2025년에 다시 올라왔지만 안의 shp는 2021-08-09 판 | 엑셀을 `data/tgis/lane_facilities_2023.xlsx`로 이름만 바꿔 둠. 공유 zip에 포함 | `data/tgis/lane_facilities_2023.xlsx` |
| 서울시 택시승차대 | H8: 승차대 앞 택시 정차 구간 제외 (멘토 요청) | 서울시 택시승차대 운영 목록 2026년판 (`택시승차대 운영 목록_260223.xlsx`, 255개소, 좌표·쉘터형/폴형·도로명주소). 같은 내용의 [열린데이터광장 CSV](https://data.seoul.go.kr/)는 259개소로 더 오래되었고 좌표 오타(`127/02437`)가 있음 | 엑셀 원본을 `data/taxi_stands.xlsx`로 이름만 바꿔 둠. 머리글 `순번` 행과 `좌표(Poi_X)`=경도, `좌표(Poi_Y)`=위도를 읽음 | `data/taxi_stands.xlsx` |
| 표준노드링크 | 제한속도 (판정 미사용, 점수 후보) | VWorld `LT_L_MOCTLINK` | VWorld 2D 데이터 API로 수동 수집 | `data/vworld_moct_links_gangnam.geojson` |
| 정밀도로지도 원본 (강남구·서초구 등) | 코드표 확인, 안전표지·노면표시 레이어 | [국토정보플랫폼 정밀도로지도](https://map.ngii.go.kr/ms/pblictn/preciseRoadMap.do) | 공간정보 → 지도정보 → 정밀도로지도 → 벡터데이터 → 지역 선택다운로드 (로그인, 이용목적 입력) | `data/raw/ngii_hdmap/` |

택시승차대 목록에는 시설물 **점 하나**만 있고 택시가 서는 정차 구간(시작·끝)은 없습니다. 정밀도로지도 노면선(구간 코드 없음)과 T-GIS 노면 문자("택시" 0건)에도 없어, 판정은 승차대에서 도로를 따라 택시 1대(6m) 안을 불가, 2대(12m) 안을 확인 필요로 둡니다. 판정 영역 안 승차대는 3곳(테헤란로 116 동경빌딩 앞 쉘터형, 강남대로 지오다노강남점 앞 폴형, 강남대로 CGV 강남점 앞 폴형)이며, 서울시가 설치한 시설만 들어 있어 노면에만 칠한 택시 정차구역은 빠져 있을 수 있습니다.

정밀도로지도 원본은 강남역 구역의 동쪽 일부만 덮습니다. 강남대로·테헤란로는 다운로드 목록에 없고 VWorld API 데이터에만 있으므로, 판정에는 VWorld 데이터를 씁니다.

## 신청 후 대기 중

| 데이터 | 용도 | 신청 | 승인 후 |
|---|---|---|---|
| 도로명주소 전자지도 (강남구·서초구) | 모든 도로의 도로폭(`ROAD_BT`), 실폭도로 면 | [juso.go.kr 전자지도 신청](http://www.juso.go.kr/externalLink/goUrl.do?menuId=DT05). 11종 레이어 일괄 제공, 구청 이용목적 심사. 좌표계 UTM(GRS80) | `data/raw/juso/`에 보관 후 변환 |
| 경찰청_전국 보호구역 현황 | 어린이보호구역 실제 경계(geometry) | [공공데이터포털 15142010](https://www.data.go.kr/data/15142010/openapi.do) | 활용신청 현황의 End Point·상세기능 주소 확인 후 수집 |

## 정밀도로지도 코드표

출처: `정밀도로지도 설명 및 안내자료(2023. 5.).pdf` (국토지리정보원 스마트공간정보과)

**B2_SURFACELINEMARK 노면선표시**

- `Type` 선표시유형: 세 자리 = 색(1 황색, 2 백색, 3 청색) · 단겹(1 단선, 2 겹선) · 모양(1 실선, 2 점선, 3 좌점혼선, 4 우점혼선). 예: 111 황색 단선 실선, 112 황색 단선 점선, 121 황색 겹선 실선. 999 기타.
- `Kind` 선규제유형:

| 코드 | 의미 | 코드 | 의미 |
|---|---|---|---|
| 501 | 중앙선 | 506 | 진로변경제한선 |
| 5011 | 가변차선 | 515 | 주정차금지선 |
| 502 | 유턴구역선 | 525 | 유도선 |
| 503 | 차선 | 530 | 정지선 |
| 504 | 버스전용차선 | 531 | 안전지대 |
| 505 | 길가장자리구역선 | 535 | 자전거도로 |
| 599 | 기타선 | | |

판정 사용: 515+111/121 주정차금지(실선), 515+112 주차금지(점선), 504 버스전용, 531 안전지대.

H5 조합: 정밀도로지도가 덮는 도로는 서울시 2021 선과 정밀도로지도를 대조합니다. 덮지 않는 도로는 2021 선이 2023 서울시 차선 목록에도 있으면(같은 종류의 점이 선에서 3m 안) 불가, 없으면 확인 필요입니다. 2023 목록은 점뿐이라 2021년에 없던 새 선은 구간을 알 수 없어 쓰지 않습니다.

**B3_SURFACEMARK 노면표시**

- `Type`: 1 화살표, 5 횡단보도
- `Kind`: 524 정차금지대, 5321 횡단보도, 533 고원식횡단보도, 534 자전거횡단보도, 5371 직진, 5372 좌회전, 5373 우회전, 5374 좌우회전, 5379 전방향, 5381 직진및좌회전, 5382 직진및우회전, 5383 직진및유턴, 5391 유턴, 5392 좌회전및유턴, 5431 차로변경(좌로합류), 5432 차로변경(우로합류), 544 오르막경사면, 599 기타지시표시

**B1_SAFETYSIGN 안전표지**

- `Type`: 1 주의, 2 지시, 3 규제, 4 보조. 2021년부터 표지코드 속성이 삭제되어 주정차금지 표지를 구분할 수 없음 → 표지는 T-GIS 사용.

**A2_LINK 주행경로링크**

- `RoadRank`: 1 고속국도, 2 일반국도, 3 특별시도·광역시도, 4 국가지원지방도, 5 지방도, 6 시도, 7 군도, 8 구도, 9 기타도로
- `RoadType`: 1 일반도로, 2 터널, 3 교량, 4 지하차도, 5 고가차도
- `LinkType`: 1 교차로내주행경로, 4 버스전용차로, 5 가변차선차로, 6 일반주행차로, 13 교차로진입로, 14 교차로진출로, 99 기타 (2·3 톨게이트, 7~12 휴게소·졸음쉼터)

## 도로 구분 참고 기준

이면도로는 법령상 정의가 없습니다. 아래 두 공식 기준이 모두 12m를 경계로 씁니다.

- 도로명주소법 시행령 제6조: 대로 폭 40m 이상 또는 왕복 8차로 이상, 로 폭 12~40m 또는 왕복 2~8차로, 길 그 외
- 도시·군계획시설의 결정·구조 및 설치기준에 관한 규칙 제9조: 광로 40m 이상(1류 70+, 2류 50~70, 3류 40~50), 대로 25~40m(1류 35~40, 2류 30~35, 3류 25~30), 중로 12~25m(1류 20~25, 2류 15~20, 3류 12~15), 소로 12m 미만(1류 10~12, 2류 8~10, 3류 8 미만)
