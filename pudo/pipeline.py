"""기본 조건으로 결과 파일 만들기: 준비(측정값, 필요할 때만) → 판정 → 저장.

준비는 data/prepared/의 측정값이 없거나 입력 데이터보다 오래되었을 때만 다시 합니다.
"""

import argparse

from .config import DATA_DIR, SV_DIR, WEB_DIR
from .export import print_summary, write_results
from .measure import load_prepared
from .rules import evaluate_all
from .vision import PhotoContractError, load_assessments, validate_confidence_threshold


def run_pipeline(*, data_dir, sv_dir, web_dir, photo_results=None, photo_min_confidence=0.8, refresh=False):
    validate_confidence_threshold(photo_min_confidence)
    measurements, layers = load_prepared(data_dir, sv_dir, refresh=refresh)
    photos = None
    if photo_results is not None:
        photos = load_assessments(photo_results)
        if photos.keys() - {m["id"] for m in measurements}:
            raise PhotoContractError("사진 판독 결과에 현재 후보 집합에 없는 candidate_id가 있습니다.")
    result = evaluate_all(measurements, photo_assessments=photos, photo_min_confidence=photo_min_confidence)
    outputs = write_results(result, data_dir, web_dir, layers=layers)
    print_summary(result)
    print("저장:", ", ".join(str(path) for path in outputs))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="기본 조건으로 판정 결과와 웹 데이터 생성")
    parser.add_argument("--photo-results", help="공통 계약에 맞는 사진 판독 결과 JSON (모델 호출 없음)")
    parser.add_argument("--photo-min-confidence", type=float, default=0.8, help="사진 관측값 적용 기준, 기본 0.8 (임시 운영값)")
    parser.add_argument("--refresh", action="store_true", help="측정값을 다시 준비 (입력이 바뀌지 않았어도)")
    args = parser.parse_args(argv)
    try:
        return run_pipeline(
            data_dir=DATA_DIR, sv_dir=SV_DIR, web_dir=WEB_DIR, refresh=args.refresh,
            photo_results=args.photo_results, photo_min_confidence=args.photo_min_confidence,
        )
    except PhotoContractError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
