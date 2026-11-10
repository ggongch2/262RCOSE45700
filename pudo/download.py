"""강남역 지도 입력 수집: OSM, VWorld, 서울 T-GIS, 공공데이터포털, 저장소의 시설 레이어 스냅샷."""

import argparse
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .config import CENTER_LAT, CENTER_LON, DATA_DIR, DRIVABLE, ROOT, api_key
from .geo import CX, CY

VWORLD_DATA = "https://api.vworld.kr/req/data"
VWORLD_WFS = "https://api.vworld.kr/req/wfs"
OVERPASS = "https://overpass-api.de/api/interpreter"
CHILD_ZONE_API = "https://api.data.go.kr/openapi/tn_pubr_public_child_prtc_zn_api"   # 공공데이터포털 15012891
SEOUL_DATASET = "https://data.seoul.go.kr/dataList/{}/S/1/datasetView.do"
SEOUL_FILE = "https://datafile.seoul.go.kr/bigfile/iot/inf/nio_download.do?&useCache=false"
# 서울시 교통안전시설물(T-GIS) 원본: (데이터셋, 압축 파일 이름 접두어, 필요한 shp 이름)
TGIS_LAYERS = {
    "tgis/A064_P_signs.geojson": ("OA-15540", "A064_P", "A064_P"),        # 안전표지 (어린이보호구역 시작·해제 등)
    "tgis/A054_P_text_marks.geojson": ("OA-15535", "A054_P", "A054_P"),  # 노면 문자표시 ("어린이 보호구역" 등)
}
REQUIRED_LAYERS = (
    "C 횡단보도", "C 소화전", "C 버스정류소",
    "C 학교(보호구역 300m 버퍼)", "D 규제차선 (T-GIS)",
)


class DownloadError(Exception):
    """키나 요청 URL을 포함하지 않는 다운로드 오류."""


def request_json(endpoint, params, *, post=False):
    encoded = urllib.parse.urlencode(params).encode()
    url = endpoint if post else endpoint + "?" + encoded.decode()
    request = urllib.request.Request(
        url, data=encoded if post else None,
        headers={"User-Agent": "Gangnam-PUDO/0.1", "Accept": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=50) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (429, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise DownloadError(f"{urllib.parse.urlparse(endpoint).hostname}: HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise DownloadError(
                f"{urllib.parse.urlparse(endpoint).hostname}: 네트워크 연결 실패 ({type(error).__name__})"
            ) from None
        except (ValueError, UnicodeError):
            raise DownloadError(f"{urllib.parse.urlparse(endpoint).hostname}: JSON이 아닌 응답") from None


def collection(features):
    return {"type": "FeatureCollection", "features": features}


def unique_features(features):
    seen, result = set(), []
    for feature in features:
        identity = feature.get("id") or feature.get("properties", {}).get("pnu")
        if identity is None:
            identity = json.dumps(feature, sort_keys=True, ensure_ascii=False)
        if identity not in seen:
            seen.add(identity)
            result.append(feature)
    return result


def fetch_vworld_data(key, layer, bbox, label):
    """2D 데이터 API를 페이지 끝까지 조회합니다. (피처 목록, 페이지 수)."""
    features, total, page = [], None, 1
    while total is None or len(features) < total:
        response = request_json(VWORLD_DATA, {
            "service": "data", "request": "GetFeature", "version": "2.0",
            "data": layer, "key": key, "format": "json",
            "size": 1000, "page": page, "geometry": "true", "attribute": "true",
            "crs": "EPSG:4326", "geomFilter": "BOX(" + ",".join(map(str, bbox)) + ")",
        }).get("response", {})
        if response.get("status") != "OK":
            code = response.get("error", {}).get("code", response.get("status", "unknown"))
            # API 오류 본문은 요청 URL/키를 포함할 수 있어 코드만 사용합니다.
            raise DownloadError(f"VWorld {label}: API 오류 ({str(code).replace(key, '[REDACTED]')})")
        records = response.get("record", {})
        total = int(records["total"])
        current = response.get("result", {}).get("featureCollection", {}).get("features", [])
        if not current:
            raise DownloadError(f"VWorld {label}: 페이지가 비어 전체 수집을 완료하지 못했습니다.")
        before = len(features)
        features = unique_features(features + current)
        if len(features) == before:
            raise DownloadError(f"VWorld {label}: 중복 페이지만 반환되어 수집을 중단했습니다.")
        page += 1
    return features, page - 1


def fetch_parcels(key, bbox):
    """연속지적도에서 지목 '도' 필지만 선택."""
    features, pages = fetch_vworld_data(key, "LP_PA_CBND_BUBUN", bbox, "지적도")
    roads = [f for f in features if str(f.get("properties", {}).get("jibun", "")).strip().endswith("도")]
    if not roads:
        raise DownloadError("VWorld 지적도: 도로 필지를 찾지 못했습니다.")
    result = collection(roads)
    result["pudo_source"] = {"all_parcels": len(features), "road_parcels": len(roads), "pages": pages}
    return result


def fetch_upis_roads(key, bbox):
    """도시계획시설(도로) 면. 규모별 등급(광로·대로·중로·소로 1~3류)을 폭 근거로 씁니다."""
    features, _ = fetch_vworld_data(key, "LT_C_UPISUQ151", bbox, "도시계획시설 도로")
    return collection(features)


def fetch_child_zones(key, radius_m=2000):
    """전국어린이보호구역표준데이터 API 전체를 받아 판정 영역 주변의 지정 시설만 남깁니다.

    API의 지역 필터가 동작하지 않아(기관명 NODATA, 시군구 SQL 오류) 전체를 페이지 단위로 받습니다.
    """
    items, page, total = [], 1, None
    while total is None or len(items) < total:
        response = request_json(CHILD_ZONE_API, {"serviceKey": key, "pageNo": page, "numOfRows": 1000, "type": "json"})
        header = response.get("response", response).get("header", {})
        if header.get("resultCode") != "00":
            raise DownloadError(f"어린이보호구역 API: 오류 ({header.get('resultCode', 'unknown')})")
        body = response.get("response", response).get("body") or {}
        current = (body.get("items") or {}).get("item") or []
        total = int(body.get("totalCount", 0))
        if not current:
            raise DownloadError("어린이보호구역 API: 페이지가 비어 전체 수집을 완료하지 못했습니다.")
        items += current
        page += 1
    nearby = []
    for item in items:
        try:
            lon, lat = float(item["longitude"]), float(item["latitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.hypot((lon - CENTER_LON) * CX, (lat - CENTER_LAT) * CY) <= radius_m:
            nearby.append(item)
    if not nearby:
        raise DownloadError("어린이보호구역 API: 판정 영역 주변 시설이 없습니다.")
    return {"source": CHILD_ZONE_API, "filter": f"판정 중심 반경 {radius_m}m", "total": len(items),
            "count": len(nearby), "items": nearby}


def fetch_hdmap(key, layer, bbox, depth=0):
    """WFS 결과가 상한에 걸리면 영역을 분할하고 ID로 중복 제거."""
    response = request_json(VWORLD_WFS, {
        "SERVICE": "WFS", "REQUEST": "GetFeature", "VERSION": "1.1.0",
        "TYPENAME": layer, "BBOX": ",".join(map(str, bbox)), "SRSNAME": "EPSG:4326",
        "OUTPUT": "application/json", "MAXFEATURES": 1000, "KEY": key,
    })
    if response.get("type") != "FeatureCollection":
        raise DownloadError(f"VWorld {layer}: 정상 GeoJSON 응답이 아닙니다.")
    features = response.get("features", [])
    raw_total = response.get("numberMatched", response.get("totalFeatures"))
    total = int(raw_total) if str(raw_total).isdigit() else None
    if (total is not None and total > len(features)) or (total is None and len(features) >= 1000):
        if depth >= 8:
            raise DownloadError(f"VWorld {layer}: 반환 상한으로 전체 수집 여부를 확인할 수 없습니다.")
        west, south, east, north = bbox
        mid_x, mid_y = (west + east) / 2, (south + north) / 2
        tiles = ((west, south, mid_x, mid_y), (mid_x, south, east, mid_y),
                 (west, mid_y, mid_x, north), (mid_x, mid_y, east, north))
        merged = unique_features([
            feature for tile in tiles for feature in fetch_hdmap(key, layer, tile, depth + 1)["features"]
        ])
        if total is not None and len(merged) < total:
            raise DownloadError(f"VWorld {layer}: 영역 분할 후에도 일부 객체가 누락됐습니다.")
        return collection(merged)
    return collection(features)


def fetch_roads(bbox):
    west, south, east, north = bbox
    query = f'[out:json][timeout:40];way["highway"]({south},{west},{north},{east});out geom;'
    response = request_json(OVERPASS, {"data": query}, post=True)
    return prepare_roads(response)


def prepare_roads(response):
    """후보 생성 대상 차도만 남깁니다. 보행로 접속부는 차량 교차로에 포함하지 않습니다."""
    elements = response.get("elements", [])
    if response.get("remark") or not elements:
        raise DownloadError("OSM: 도로망이 없거나 쿼리가 완전히 실행되지 않았습니다.")
    for way in elements:
        if way.get("type") != "way" or not way.get("geometry") or len(way["nodes"]) != len(way["geometry"]):
            raise DownloadError("OSM: 노드 ID와 좌표가 포함된 도로망이 필요합니다.")
    roads = [way for way in elements if way.get("tags", {}).get("highway") in DRIVABLE]
    if not roads:
        raise DownloadError("OSM: 후보 생성 대상 차도가 없습니다.")
    response = {**response, "elements": roads, "pudo_source": {
        "received_ways": len(elements), "drivable_ways": len(roads), "highways": sorted(DRIVABLE),
    }}
    return response


def _seoul_file_list(dataset):
    """열린데이터광장 데이터셋 화면에서 (파일 번호, 파일 이름) 목록과 파일 묶음 번호를 읽습니다."""
    request = urllib.request.Request(SEOUL_DATASET.format(dataset), headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=50) as response:
            page = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DownloadError(f"서울 열린데이터광장 {dataset}: 연결 실패 ({type(error).__name__})") from None
    files = re.findall(r"downloadFile\('(\d+)'\);\" title=\"([^\"]+)\"", page)
    sequences = re.findall(r'name="infSeq" value="(\d+)"', page)
    if not files or not sequences:
        raise DownloadError(f"서울 열린데이터광장 {dataset}: 파일 목록을 찾지 못했습니다.")
    return files, sequences[-1]


def fetch_tgis(dataset, prefix, member, bbox):
    """T-GIS shp 압축 파일을 받아 bbox 안의 객체만 WGS84 GeoJSON으로 변환합니다."""
    import io
    import zipfile

    import shapefile
    from pyproj import Transformer

    files, sequence = _seoul_file_list(dataset)
    matches = [number for number, name in files if name.startswith(prefix) and name.endswith(".zip")]
    if not matches:
        raise DownloadError(f"서울 열린데이터광장 {dataset}: {prefix} 압축 파일이 없습니다.")
    body = urllib.parse.urlencode({"infId": dataset, "seqNo": "", "seq": matches[0], "infSeq": sequence}).encode()
    request = urllib.request.Request(SEOUL_FILE, data=body, headers={
        "User-Agent": "Mozilla/5.0", "Referer": SEOUL_DATASET.format(dataset)})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DownloadError(f"서울 열린데이터광장 {dataset}: 파일 다운로드 실패 ({type(error).__name__})") from None
    except zipfile.BadZipFile:
        raise DownloadError(f"서울 열린데이터광장 {dataset}: 압축 파일이 아닌 응답") from None
    names = {Path(name).name: name for name in archive.namelist()}
    if f"{member}.shp" not in names:   # 압축 안에 압축이 한 번 더 있는 경우
        inner = [name for name in archive.namelist() if name.lower().endswith(".zip")]
        if not inner:
            raise DownloadError(f"서울 열린데이터광장 {dataset}: {member}.shp가 없습니다.")
        archive = zipfile.ZipFile(io.BytesIO(archive.read(inner[0])))
        names = {Path(name).name: name for name in archive.namelist()}
    parts = {ext: io.BytesIO(archive.read(names[f"{member}.{ext}"])) for ext in ("shp", "shx", "dbf")}
    to_wgs84 = Transformer.from_crs(5186, 4326, always_xy=True)   # T-GIS: KGD2002 중부원점 (EPSG:5186)
    to_tm = Transformer.from_crs(4326, 5186, always_xy=True)
    west, south = to_tm.transform(bbox[0], bbox[1])
    east, north = to_tm.transform(bbox[2], bbox[3])

    def convert(coordinates):
        if isinstance(coordinates[0], (int, float)):
            return [round(value, 7) for value in to_wgs84.transform(coordinates[0], coordinates[1])]
        return [convert(item) for item in coordinates]

    features = []
    reader = shapefile.Reader(**parts, encoding="cp949", encodingErrors="replace")
    for record in reader.iterShapeRecords():
        points = record.shape.points
        if not points:
            continue
        xs, ys = [x for x, _ in points], [y for _, y in points]
        if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
            continue
        geometry = record.shape.__geo_interface__
        properties = {key: value.strip() if isinstance(value, str) else value for key, value in record.record.as_dict().items()}
        features.append({"type": "Feature", "properties": properties,
                         "geometry": {"type": geometry["type"], "coordinates": convert(geometry["coordinates"])}})
    if not features:
        raise DownloadError(f"서울 열린데이터광장 {dataset}: 조회 범위에 객체가 없습니다.")
    result = collection(features)
    result["pudo_source"] = {"dataset": dataset, "file": dict(files)[matches[0]], "crs_from": "EPSG:5186"}
    return result


def restore_layers(preview):
    text = Path(preview).read_text(encoding="utf-8")
    match = re.search(r"\bconst\s+DATA\s*=\s*", text)
    if not match:
        raise DownloadError("미리보기 HTML에 시설 레이어가 없습니다.")
    layers, _ = json.JSONDecoder().raw_decode(text[match.end():])
    for name in REQUIRED_LAYERS:
        if not layers.get(name, {}).get("features"):
            raise DownloadError(f"미리보기 스냅샷에 필요한 레이어가 없습니다: {name}")
    return layers


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="강남역 원본 지도 수집 및 시설 스냅샷 복원")
    parser.add_argument("--out", type=Path, default=DATA_DIR, help="입력 데이터 저장 폴더")
    parser.add_argument("--refresh", action="store_true", help="이미 존재하는 파일도 다시 수집")
    args = parser.parse_args(argv)
    key = api_key("vworld")
    if not key:
        parser.error("env의 vworld_api 또는 환경변수 VWORLD_API_KEY를 설정하세요.")
    # 판정 영역 500m보다 넓은 600m 주변을 수집합니다. 시설은 기존 확장 스냅샷을 복원합니다.
    bbox = [round(CENTER_LON - 600 / CX, 7), round(CENTER_LAT - 600 / CY, 7),
            round(CENTER_LON + 600 / CX, 7), round(CENTER_LAT + 600 / CY, 7)]
    preview = ROOT / "data_preview" / "gangnam_preview.html"
    tasks = [
        ("layers_gangnam.json", "repository_snapshot", "data_preview/gangnam_preview.html", lambda: restore_layers(preview)),
        ("osm_roads_gangnam.json", "download", OVERPASS, lambda: fetch_roads(bbox)),
        ("vworld_road_parcels_gangnam.geojson", "download", VWORLD_DATA, lambda: fetch_parcels(key, bbox)),
        ("hdmap/lt_l_a2link.geojson", "download", VWORLD_WFS, lambda: fetch_hdmap(key, "lt_l_a2link", bbox)),
        ("hdmap/lt_l_b2surfacelinemark.geojson", "download", VWORLD_WFS, lambda: fetch_hdmap(key, "lt_l_b2surfacelinemark", bbox)),
        ("vworld_upis_roads_gangnam.geojson", "download", VWORLD_DATA, lambda: fetch_upis_roads(key, bbox)),
    ] + [
        (filename, "download", SEOUL_DATASET.format(dataset), lambda d=dataset, p=prefix, m=member: fetch_tgis(d, p, m, bbox))
        for filename, (dataset, prefix, member) in TGIS_LAYERS.items()
    ]
    zone_key = api_key("datagokr")
    if zone_key:
        tasks.append(("child_protection_zones.json", "download", CHILD_ZONE_API, lambda: fetch_child_zones(zone_key)))
    else:
        print("건너뜀: child_protection_zones.json (env의 data_go_kr_api 없음, 보호구역 지정 시설 없이 판정)", flush=True)
    manifest_path = args.out / "download_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {"files": {}}
    manifest.update({"center": [CENTER_LON, CENTER_LAT], "bbox": bbox, "crs": "EPSG:4326", "complete": False})
    errors = []
    for filename, method, source, fetch in tasks:
        path = args.out / filename
        if path.is_file() and not args.refresh:
            print(f"유지: {filename}", flush=True)
            continue
        try:
            data = fetch()
            if "features" in data and not data["features"]:
                raise DownloadError(f"{filename}: 수집 결과가 비어 있습니다.")
            save_json(path, data)
            if filename == "layers_gangnam.json":
                counts = {name: len(fc["features"]) for name, fc in data.items()}
            else:
                counts = len(data.get("features", data.get("elements", data.get("items", []))))
            manifest["files"][filename] = {
                "method": method, "source": source,
                "saved_at": datetime.now(timezone.utc).isoformat(), "count": counts,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            print(f"저장: {filename} ({counts})", flush=True)
        except (DownloadError, OSError, ValueError, KeyError) as error:
            # I/O 오류도 파일 내용이나 자격 증명을 출력하지 않도록 정리합니다.
            message = type(error).__name__
            if isinstance(error, DownloadError):
                message = str(error).replace(key, "[REDACTED]")
                if zone_key:
                    message = message.replace(zone_key, "[REDACTED]")
            errors.append(f"{filename}: {message}")
            print(f"실패: {filename}: {message}", flush=True)
        save_json(manifest_path, manifest)
    manifest["complete"] = not errors
    manifest["errors"] = errors
    save_json(manifest_path, manifest)
    if errors:
        raise SystemExit("일부 입력 수집이 실패했습니다. 성공한 파일은 유지하며 재실행 시 누락 파일만 받습니다.")
    print(f"완료: {args.out} (출처·건수: download_manifest.json)")


if __name__ == "__main__":
    main()
