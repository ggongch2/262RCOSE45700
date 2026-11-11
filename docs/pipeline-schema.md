# 현재 v0 파이프라인의 데이터 계약

현재 실행 중인 코드와 로컬 데이터 기준입니다. 후보 생성 → 지도 판정 → 웹과 Google 메타데이터 연결은 동작을 검증했습니다. 사진 판독 결과는 `photo-observations.v1` 계약으로 검증해 판정에 합칩니다. 그 밖의 단계는 공통 자료형이나 JSON Schema로 강제 검증하지 않습니다. 사진에서 판독 결과를 만드는 모델 단계, Mapillary 연결, 정답셋 평가는 아직 구현하지 않았습니다.

## 처리 구조

```text
[준비] data/ + sv_out/metadata.csv
       ↓ datasets.load_inputs → 후보 Feature 목록 + MapData (미터 좌표)
       ↓ measure.measure_all   ← 지리 계산은 여기서만. 기준값 미사용, 모르면 null
       data/prepared/measurements.json (pudo-measure.v2) + data/prepared/layers.json
[판정] 측정값 (+ 선택: --photo-results 사진 판독 JSON)
       ↓ rules.evaluate_all(thresholds=TH, rules_off=())   ← 파일·지리 계산 없음
       ↓ export.write_results
       pudo_v0.geojson / pudo_v0_data.js / pudo_v0_standalone.html
```

`pipeline.run_pipeline`은 측정값을 읽고(없거나 입력보다 오래되면 `measure.prepare`로 다시 만들고) 판정과 저장을 연결합니다. 구분 기준은 "운영 기준이 바뀌면 이 값도 바뀌나"입니다. 바뀌면 판정(`rules.py`), 데이터가 바뀌어야만 바뀌면 준비(`measure.py`)입니다.

### 측정값 (`data/prepared/measurements.json`)

`{schema_version: "pudo-measure.v2", generated_at, count, candidates: [...]}`. 후보 하나의 필드입니다. 거리는 m이며 null은 모름(자료 없음)입니다.

| 필드 | 내용 |
|---|---|
| `id`, `coordinates`, `heading`, `side`, `centerline` | 후보 식별·위치 |
| `road` | `{name, type, osm_way, oneway}` |
| `position` | `{estimated: true, offset_m, basis}`. 연석 위치는 중심선에서 옮긴 추정값이며 `basis`는 옮긴 근거(정밀도로지도 차로 수 / OSM 차로 수 / 지적도 도로 폭 / 근거 없음) |
| `lanes` | `{value, source}`. 정밀도로지도·OSM에 없으면 value null |
| `width` | `{value, source}`. 지적도 도로 필지 폭(보도 포함) |
| `planned_road` | `{grade, min_width}` 또는 null. 도시계획시설 도로 등급 |
| `sidewalk` | `{value, source}`. 현재 자료 없음 → null (사진 판독 결과가 판정에서 반영) |
| `intersection` | `{inside, distance, estimated: true}`. 교차로 면 안 여부와 가장자리까지 거리 |
| `crosswalk`, `bus_stop`, `hydrant` | 가장 가까운 시설까지 거리 |
| `child_zone` | `{mode: "tgis", state: in/near/out, label}` 또는 `{mode: "radius", distance, label}` |
| `safety_zone` | `{covered, distance}`. 정밀도로지도 안전지대 |
| `reg_line` | `{tgis_no_stop, tgis_no_park, hd_covered, hd_no_stop, hd_bus, hd_no_park}`. 규제선까지 거리 |
| `taxi_stand` | `{distance, label, estimated: true}` 또는 null(승차대 파일 없음). 같은 도로·같은 쪽(도로 중심선 기준 연석 쪽, 일방통행 차도는 차도 안 포함) 60m 안의 가장 가까운 서울시 택시승차대까지 **도로를 따라 잰 거리**. 없으면 `distance` null |
| `image` | `{pano_id}` 또는 null. 후보 ID와 조회 좌표가 맞는 Google 사진 |

## 원본 입력

| 파일 | 구조 | 주요 소비 코드 |
|---|---|---|
| `osm_roads_gangnam.json` | Overpass JSON의 `elements[]`: way, tags, nodes, geometry | 후보 생성, 교차로 계산 |
| `layers_gangnam.json` | 레이어 이름 → GeoJSON FeatureCollection | 시설 거리, 학교 반경(T-GIS가 없을 때), 서울시 규제선 |
| `hdmap/lt_l_a2link.geojson` | 선 GeoJSON, `properties.laneno` | 후보 생성, 정밀도로지도 커버리지 |
| `hdmap/lt_l_b2surfacelinemark.geojson` | 선 GeoJSON, `properties.kind/type` | 규제선·버스차로·안전지대 |
| `tgis/A064_P_signs.geojson` | 점 GeoJSON, T-GIS 안전표지 원본 속성(`MRK_NUM`, `RMK` 등) | 어린이보호구역 근거·경계 |
| `tgis/A054_P_text_marks.geojson` | 점 GeoJSON, T-GIS 노면 문자표시 원본 속성(`CTT` 등) | 어린이보호구역 근거 |
| `vworld_road_parcels_gangnam.geojson` | 도로 필지 MultiPolygon GeoJSON | 도로 폭 추정 |
| `vworld_upis_roads_gangnam.geojson` | 도시계획시설 도로 면, `properties.grad_se`(광로·대로·중로·소로), `road_ty`(1~3류) | P1 폭 근거 (선택) |
| `child_protection_zones.json` | `{items: [...]}`, 전국어린이보호구역표준데이터 API 행(`trgetFcltyNm`, `fcltyKnd`, `latitude`, `longitude`) | 보호구역 지정 시설 (선택) |

외부 입력마다 구조가 다르며, 로더가 판정에 필요한 좌표와 속성으로 변환합니다. 외부 GeoJSON 좌표는 `[경도, 위도]`의 WGS84이며, `MapData`의 점·선은 강남역 기준 근사 미터 좌표입니다. 웹 지도 중심 `PUDO_META.center`만 Leaflet 형식인 `[위도, 경도]`입니다.

H6 어린이보호구역은 세 단계 근거를 씁니다. T-GIS 표지·노면 문자로 복원한 구간 안이면 보호구역 안(항상 불가)입니다. 지정 시설 목록(`child_protection_zones.json`, 없으면 `child_protection_zones.csv`)이 있으면 시설 좌표를 70m 안의 가장 가까운 도로에 붙여 150m까지 퍼진 도로를 따로 만들고, 그 안의 후보는 확인 필요로 둡니다. 정문 위치와 지정 범위를 몰라 간선도로까지 번질 수 있기 때문입니다. T-GIS 파일이 없으면 대상시설 반경 300m 규칙으로 돌아갑니다.

P1 통과 폭(편도 1차로 양방향 도로)은 지적도 도로 필지 폭과 도시계획시설 도로 등급의 법정 최소 폭(「도시·군계획시설의 결정·구조 및 설치기준에 관한 규칙」 제9조)을 함께 봅니다. 등급은 도로 중심점과 진행 방향 앞뒤 10m가 모두 같은 면 안에 있을 때만 그 도로의 것으로 봅니다. 지적도 폭이 없으면 등급 하한으로 채우고, 지적도 폭은 기준 미달인데 등급 하한은 기준 이상이면 불가 대신 자료 불일치(확인 필요)로 둡니다. 소로 3류(8m 미만)는 하한이 없어 폭 근거로 쓰지 않습니다. 판정 출력의 `map.planned_road`에 등급명을 남깁니다.

H1은 교차로 면까지의 거리로 판정합니다(면 안이면 제32조 1호 불가, 가장자리 5m 이내면 2호 불가). 교차로 면은 각 way를 차도 반폭(후보 생성 때의 가장자리 이동 거리 + 1.5m 중앙값)만큼 넓힌 뒤 같은 이름의 도로를 하나로 합치고(상하행 사이 20m까지 메움), 교차 노드 40m 안에서 서로 다른 도로가 겹치는 부분입니다. 교차 노드는 OSM 차도에서 두 개 이상의 서로 다른 way가 공유하는 노드입니다. 닫힌 way의 시작·끝 노드와, 두 way의 끝이 `CONTINUATION_TURN_DEG`(20°) 미만으로 꺾이며 이어지는 분할 지점은 제외합니다. 다른 길과 직각에 가깝게 이어지는 끝점은 도로 모퉁이로 보고 유지합니다.

## 후보 생성 → 지도 판정 / 거리뷰 조회

`data/candidates_v1.geojson`은 FeatureCollection이며, Feature 하나가 후보 하나입니다. 위치는 `geometry.type="Point"`, `geometry.coordinates=[lon, lat]`입니다.

| properties 필드 | 실제 형태 | 의미 |
|---|---|---|
| `id` | string | 해당 후보 집합 내 식별자 |
| `heading` | number | 우측 도로변을 바라보는 방위각, 도 단위 |
| `side` | `R` / `L` | OSM 선의 정방향 / 역방향에서 생성한 쪽 |
| `road_type`, `name` | string | 도로 유형과 이름 |
| `osm_way` | integer | OSM way ID |
| `oneway` | boolean | 일방통행 여부 |
| `lanes` | integer | 후보 생성에 사용한 차로 수 |
| `lanes_source` | `hdmap` / `osm` / `default` | 차로 수의 출처 |
| `road_width_m` | number / null | 지적도 기반 도로 폭, m |
| `curb_offset_m` | number | 중심선에서 후보까지 이동 거리, m |
| `centerline` | `[lon, lat]` | 이동 전 도로 중심선 위치 |

`streetview.load_geojson`은 후보 또는 판정 GeoJSON의 위치·ID·heading을 조회 입력으로 바꿉니다. 새 도로망을 수집하면 같은 `id`가 다른 지점을 가리킬 수 있습니다.

## Google 거리뷰 → 지도 판정

`sv_out/metadata.csv`의 헤더는 `id,lon,lat,status,pano_id,copyright`입니다. CSV이므로 읽을 때는 모두 문자열입니다. `lon/lat`은 자체 후보의 조회 좌표입니다. Google의 촬영일·파노라마 좌표는 저장하지 않으며 판정 출력의 `captured_at`도 현재는 null입니다.

로더는 `status=OK`이고 Google 출처인 행을 선택합니다. 판정 시 ID에 더해 조회 좌표가 후보에서 1m 이내인지 확인한 후 `pano_id`를 출력의 `image_id`에 연결합니다. Mapillary GeoJSON은 조사 산출물로 존재하지만 이 연결에는 사용하지 않습니다.

## 사진 판독 결과 → 지도 판정

`pipeline --photo-results <파일>`로 넣는 선택 입력입니다. 형식은 `schemas/photo-observations.v1.schema.json`이며 `vision.parse_assessments`가 같은 규칙으로 검증합니다. 모델 응답 원문이 아니라 판독 결과를 이 형식으로 정리한 파일이어야 합니다. 형식이 다르면 추측해서 변환하지 않고 오류로 중단합니다.

```text
{schema_version: "photo-observations.v1", results: [
  {candidate_id, coordinates: [lon, lat], heading, image_id,
   image_source: "google"|"mapillary"|"own", model,
   observations: {sidewalk, curb_blocked, curb_line, construction?, occupied?}}
]}
```

관측값 하나는 `{value, confidence: 0~1|null, evidence: string}`입니다. `sidewalk`·`curb_blocked`·`construction`·`occupied`의 값은 true/false/null, `curb_line`은 `white`·`yellow_dashed`·`yellow_solid`·`yellow_double`·`none`·null입니다. 앞의 세 관측값은 필수이고 나머지 둘은 선택입니다. `candidate_id`가 중복되거나 현재 후보 집합에 없으면 실행을 중단합니다.

사진 결과는 다음 조건을 모두 만족할 때만 그 후보에 적용합니다. 하나라도 다르면 반영하지 않고 `V0 사진 대응 확인 필요` 사유를 추가합니다.

- `candidate_id`가 후보 ID와 같음
- `coordinates`가 후보 좌표에서 1m 이내
- `heading`이 후보 방향과 5° 이내
- `image_source="google"`이고 거리뷰 메타데이터가 연결된 경우 `image_id`가 그 `pano_id`와 같음

관측값은 값이 있고 신뢰도가 `--photo-min-confidence`(기본 0.8) 이상일 때만 사용합니다. 그렇지 않으면 해당 항목을 확인 필요로 둡니다. 반영 방식은 다음과 같습니다.

| 관측값 | checks 항목 | 반영 |
|---|---|---|
| `sidewalk` | A1 보도 | 도로 유형 추정값을 대체. 보도 없음이면 불가 |
| `curb_blocked` | A2 연석 연결 | 막힘이면 불가 |
| `curb_line` | V1 사진 규제선 | 황색 실선·복선이면 불가. 그 외 값이면 지도 규제선 자료 불일치를 해소. 단 버스전용차로 확인 사유는 유지 |
| `construction`, `occupied` | V2 공사, V3 일시적 점유 | 있음이면 확인 필요 |

사진은 지도 판정의 불가 사유를 지우지 않습니다. 지도는 규제선으로 불가인데 사진에는 금지선이 없으면 불가를 유지하고 불일치 사유를 추가합니다. `map.curb_line`은 지도 기준 값으로 남고 사진 값은 `vision`과 `checks`에만 들어갑니다.

## 지도 판정 → 웹 / GeoJSON 내보내기

출력은 같은 Point FeatureCollection을 유지합니다. 후보의 `id`, `name`, `road_type`, `oneway`, `heading`, `side`, `centerline`, `curb_offset_m`을 properties에 전달하고 다음 필드를 추가합니다.

| properties 필드 | 실제 형태 |
|---|---|
| `verdict` | `가능` / `불가` / `확인 필요` |
| `score` | number / null, 가능일 때 0~1 |
| `needs_check` | boolean |
| `fail_reasons`, `check_reasons` | string 배열 |
| `checks` | 항목별 판정 객체 배열 |
| `map` | 아래의 지도 속성 객체 |
| `image_id`, `image_source`, `captured_at` | string / null |
| `review`, `note`, `reviewed_at` | string / null, 자동 판정 시 null |
| `rules_off` | 판정에서 뺀 규칙 코드 배열 (기본 판정은 빈 배열) |
| `vision` | 사진 결과가 있는 후보에만 존재. 입력 행 + `schema_version`, `min_confidence`, `status`(`applied` / `review` / `rejected`), `used_fields`, `review_reasons` |

`checks[]` 객체는 `{item: string, value: string|null, threshold: string, pass: boolean|null, estimated: boolean}`입니다. `value`와 `threshold`는 화면 표시용 문자열이며, 모든 거리·조건을 기계 판독용 수치로 제공하는 구조는 아닙니다.

`map` 필드는 `dist_intersection_m: number|null`, `lanes: integer|null`(모르면 null), `road_width_m: number|null`, `planned_road: string|null`, `curb_line: string`, `position_basis: string`입니다. 현재 `curb_line`은 `yellow_solid`, `yellow_dashed`, `unknown` 중 하나입니다.

웹은 GeoJSON과 동일한 내용을 `const PUDO`로 읽습니다. 검토 데이터는 브라우저에서 ID별 `{review, note, at}`로 보관하고, 내보낼 때 properties의 `review`, `note`, `reviewed_at`으로 변환합니다.

## 문서 예시와의 차이 / 남은 범위

- 기획안 C.2의 평탄한 `lon/lat`, `dist_crosswalk_m`, `sidewalk` 등은 현재 외부 후보 스키마와 다릅니다. 실제 위치는 geometry에 있고, 교차로 외 거리와 보도 판정은 대부분 checks의 표시 문자열에 들어갑니다.
- 판정 출력 GeoJSON에는 명시적 `schema_version`과 실행 시점의 공통 스키마 검증기가 없습니다. 위 계약은 문서와 연결 테스트로 확인하는 수준입니다. 실행 시점 검증은 사진 판독 입력에만 있습니다.
- 사진에서 `photo-observations.v1` 파일을 만드는 모델 단계는 미구현입니다. 신뢰도 기준 0.8은 정답셋으로 정하기 전의 임시값입니다.
- Mapillary 사진은 입력 계약에서 `image_source="mapillary"`로 받을 수 있지만, 거리뷰 메타데이터처럼 후보에 자동 연결하는 단계는 미구현입니다.
- `verify_sheet_week1.csv`는 사람이 작성하는 한국어 열 이름의 검토 시트입니다. 이를 자동 평가에 쓰는 변환기와 공통 정답 스키마는 미구현입니다.

이번 역할 분리와 모드 인자화는 v0의 기존 파일 형식과 판정을 유지합니다. 전체 스키마 통합은 별도의 작업입니다.
