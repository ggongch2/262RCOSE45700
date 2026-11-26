"""python -m pudo3 진입점."""

import argparse
import importlib

from .config import DATA_DIR, DEFAULT_MODEL_ID, MODEL_PATH_ENV, RUNTIME_DIR, api_key, resolve_model_path

COMMANDS = {
    "serve": ("server", "담당자 화면 로컬 서버 (127.0.0.1 전용). --vlm으로 Qwen3.5 판독"),
    "prepare": ("measure", "후보·측정값을 pudo3/runtime/prepared/에 생성 (data/는 읽기 전용)"),
}


def check():
    print(f"입력 data/: {DATA_DIR} ({'있음' if DATA_DIR.is_dir() else '없음'}, 읽기 전용)")
    print(f"결과 runtime/: {RUNTIME_DIR}")
    print(f"카카오 JavaScript 키: {'설정됨' if api_key('kakao') else '없음'}")
    path = resolve_model_path()
    print(f"VLM 모델: {DEFAULT_MODEL_ID} · 경로 {path} ({'있음' if (path / 'config.json').is_file() else '없음'})"
          f" · 덮어쓰기: --vlm-model 또는 {MODEL_PATH_ENV}")
    print("키 값과 네트워크 호출은 사용하지 않았습니다.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m pudo3", description="강남역 PUDO v3 (Qwen3.5 4bit)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="입력·모델 경로·키 설정 확인 (네트워크 호출 없음)")
    for name, (_, description) in COMMANDS.items():
        sub.add_parser(name, help=description, add_help=False)
    args, rest = parser.parse_known_args(argv)
    if args.command == "check":
        check()
        return
    importlib.import_module(f"pudo3.{COMMANDS[args.command][0]}").main(rest)


if __name__ == "__main__":
    main()
