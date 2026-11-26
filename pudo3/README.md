# pudo3 — Qwen3.5-9B 4bit 2단계 판독 (1차 생각 끔 · 2차 생각 켬)

`pudo2`와 같은 흐름이고 모델만 다릅니다. 1차(보도 위치)는 생각 없이 바로 JSON, 2차(관계 추론)는 생각 과정을 거친 뒤 JSON을 씁니다.

기존 `pudo/`를 바탕으로 만든 독립 패키지입니다. `pudo`를 import하지 않으며, 저장소의 `data/`는 읽기 전용 입력으로만 씁니다. 새로 생기는 결과는 모두 `pudo3/runtime/`에 저장됩니다.

## 설치

```bash
python -m pip install -r pudo3/requirements-vlm.txt
python -m playwright install chromium
```

transformers 5가 필요하므로 pudo2(transformers 4.57)와 **다른 가상환경**에 설치합니다.

모델은 직접 내려받습니다. 코드는 `local_files_only=True`로만 읽고, 자동으로 다운로드하지 않습니다.

## 실행

```bash
python -m pudo3 serve --vlm --no-browser
python -m pudo3 serve --vlm --vlm-model /home/ubuntu/gyeonglim/262RCOSE45700/models/Qwen3.5-9B --no-browser
PUDO3_VLM_MODEL_PATH=/path/to/model python -m pudo3 serve --vlm --no-browser
```

- 모델 경로 우선순위: `--vlm-model` > `PUDO3_VLM_MODEL_PATH` > `models/Qwen3.5-9B`
- 모델 폴더가 없으면 `Qwen3.5 모델을 찾을 수 없습니다: <경로>. 모델을 먼저 다운로드하세요.`를 출력하고 종료합니다.
- 시작할 때 모델에 이미지 입력부(`vision_config`)가 있는지, 채팅 템플릿이 생각 끄기/켜기(`enable_thinking`)를 지원하는지 확인합니다.
- `--save-diagnostics`: 탐색 상자·판독 지점 표시 이미지와 원본 응답을 `pudo3/runtime/diagnostics/`에 저장합니다.
- `--port`: 기본값은 8000입니다. 기존 `pudo` 서버와 동시에 띄우려면 `--port 8010`처럼 바꿉니다.
- `python -m pudo3 check`: 모델 경로와 키 설정 여부를 확인합니다. 네트워크를 호출하지 않습니다.

## 판독 흐름 (「정차 판독」 버튼)

1. 카카오 로드뷰를 후보 정면(pan = candidate.heading, tilt = 0, zoom = 0)으로 캡처합니다. 흰 이동 화살표·방향 글자·확대 버튼은 숨기고, 카카오 후보 마커와 저작권 표시는 둡니다.
2. 모델에는 원본 캡처 사진을 그대로 넣습니다 (상자·점 표시 없음). 지도 계산은 판독 가능 여부와 차선 색 측정 높이에만 씁니다.
3. **Qwen 1회 호출 (생각 켬, `stop_check.py`)** — 사진 가운데(후보 정면)를 보고 순서대로 답하게 합니다.
   1. 보도: 있음 / 없음 / 가려짐
   2. 차도–보도 사이 고정 장애물(난간·펜스·벽·화단 띠·가드레일)과 개구부. 띄엄띄엄 선 가로수·기둥·볼라드, 보도 뒤 건물 쪽 시설, 사람·차량·콘은 장애물이 아님
   3. 주차장·건물 차량 출입구
   4. 연석 앞 차선
4. 황색 차선은 `curb_paint.py`가 지도로 계산한 연석 높이 띠에서 노란 페인트를 색으로 재서, 분명하면 모델 답 대신 씁니다. 노란 칠이 없으면 '규제선 없음'.
5. **Python 판정 (`stop_check.decide_stop`)**: 보도 없음 → 불가, 장애물+개구부 없음 → 불가, 주차장 입구 → 불가, 황색 실선·복선 → 불가. 가려져 확인 못 한 항목이 있으면 판단 불가. 불가 사유가 있으면 불가가 우선이고, 사유는 1→4 순서로 표시합니다.

사진을 클릭해 위치를 지정하는 재판독은 이전 방식(지정 지점의 조건 판독)을 그대로 씁니다.

## 저장 위치 (`pudo3/runtime/`)

| 경로 | 내용 |
|---|---|
| `prepared/` | 후보와 측정값. `data/`에서 읽어 생성합니다. |
| `results/<후보>-<시각>-<id>.json` | 판독 기록 1건 (스키마 `pudo3-qwen-photo.v1`) |
| `qwen_results.json` | 후보별 최신 판독 색인 |
| `diagnostics/` | 진단 이미지와 원본 응답 (`--save-diagnostics`일 때만) |
| `logs/roadview-errors.log` | 로드뷰 실패 단계 (API 키 제외) |

## 테스트

```bash
python -m unittest discover -s pudo3/tests
```

테스트는 실제 모델을 로드하지 않습니다. `torch`, `transformers`, `bitsandbytes`는 가짜 모듈로 대체합니다.
