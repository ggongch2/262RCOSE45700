"""지도 기반 v0 실행 흐름: 입력 로딩 → 두 모드 판정 → 결과 저장."""

import argparse

from .config import DATA_DIR, SV_DIR, WEB_DIR
from .datasets import load_inputs
from .export import overlay_layers, print_summary, write_results
from .rules import evaluate_candidates
from .vision import PhotoContractError, validate_confidence_threshold


def run_pipeline(*, data_dir, sv_dir, web_dir, photo_results=None, photo_min_confidence=0.8):
    validate_confidence_threshold(photo_min_confidence)
    candidates, map_data = load_inputs(data_dir, sv_dir, photo_results=photo_results)
    result = evaluate_candidates(candidates, map_data, photo_min_confidence=photo_min_confidence)
    outputs = write_results(result, data_dir, web_dir, layers=overlay_layers(map_data))
    print_summary(result)
    print("저장:", ", ".join(str(path) for path in outputs))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="지도 기반 판정 및 웹 데이터 생성")
    parser.add_argument("--photo-results", help="공통 계약에 맞는 사진 판독 결과 JSON (모델 호출 없음)")
    parser.add_argument("--photo-min-confidence", type=float, default=0.8, help="사진 관측값 적용 기준, 기본 0.8 (임시 운영값)")
    args = parser.parse_args(argv)
    try:
        return run_pipeline(
            data_dir=DATA_DIR, sv_dir=SV_DIR, web_dir=WEB_DIR,
            photo_results=args.photo_results, photo_min_confidence=args.photo_min_confidence,
        )
    except PhotoContractError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
