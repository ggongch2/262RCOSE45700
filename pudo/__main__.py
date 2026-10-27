"""python -m pudo 또는 pudo 명령의 진입점."""

import argparse
import importlib

from .config import API_KEY_NAMES, DATA_DIR, ROOT, api_key

COMMANDS = {
    "download": ("download", "OSM·VWorld 지도 수집 및 시설 스냅샷 복원"),
    "candidates": ("candidates", "도로망에서 승하차 후보 생성"),
    "pipeline": ("pipeline", "지도 기반 판정 및 웹 데이터 생성"),
    "streetview": ("streetview", "Google 거리뷰 메타데이터 조회"),
    "mapillary": ("mapillary", "Mapillary 커버리지 조사"),
}


def check():
    print(f"프로젝트: {ROOT}")
    for filename in (".env", "env"):
        print(f"{filename}: {'있음' if (ROOT / filename).is_file() else '없음'}")
    for provider in API_KEY_NAMES:
        print(f"{provider} 키: {'설정됨' if api_key(provider) else '없음'}")
    for name in (
        "osm_roads_gangnam.json",
        "layers_gangnam.json",
        "hdmap/lt_l_a2link.geojson",
        "hdmap/lt_l_b2surfacelinemark.geojson",
        "vworld_road_parcels_gangnam.geojson",
        "candidates_v1.geojson",
    ):
        print(f"data/{name}: {'있음' if (DATA_DIR / name).is_file() else '없음'}")
    print("키 설정 여부만 확인했습니다. API 인증 및 네트워크 호출은 수행하지 않았습니다.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="강남역 PUDO 도구")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="키 설정 및 입력 데이터 확인 (네트워크 호출 없음)")
    for name, (_, description) in COMMANDS.items():
        sub.add_parser(name, help=description, add_help=False)
    args, rest = parser.parse_known_args(argv)
    if args.command == "check":
        if rest:
            parser.error("check 명령에는 추가 인자가 없습니다.")
        check()
        return
    module = importlib.import_module(f"pudo.{COMMANDS[args.command][0]}")
    module.main(rest)


if __name__ == "__main__":
    main()
