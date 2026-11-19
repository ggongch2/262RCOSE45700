"""후보점 생성 (카카오 안내 p.18 1단계). data/map1000/(1:1000 수치지형도)으로 만든다.

  - 1:1000 도로중심선을 15m 간격으로 샘플링하고, 중심선에서 직각으로 차도 면(도로경계 - 보도)의 끝(차도 끝)을 찾는다.
  - 후보 = 차도 끝에서 1.5m 안쪽(차 중심). 양방향은 양쪽, 일방통행은 OSM 진행 방향 우측(방향을 모르면 양쪽).
  - 차도 면이 없는 좁은 길(1:1000은 중심선만 그림)은 적힌 도로폭의 절반을 차도 끝으로 본다.
  - 교차로 안처럼 차도 끝을 찾지 못하면 후보를 만들지 않는다.
  - 차도 가운데 섬(중앙 버스승강장처럼 보도로 그려졌지만 사방이 차도인 면)은 차도 끝으로 보지 않고 건너간다.
  - 도로 유형(road_type)은 1:1000 도로명 끝 글자(대로·로·길)로 정한다 (도로명주소법 시행령 제6조).
  - OSM은 일방통행 진행 방향에만 쓴다. 1:1000에는 일방통행 여부만 있고 진행 방향이 없음
    (선을 그린 방향은 진행 방향과 맞는 곳·반대인 곳이 섞여 있음). osm_way = 방향을 가져온 OSM 길.

입력: data/map1000/ (+ 있으면 data/osm_roads_gangnam.json)
출력: data/candidates_v1.geojson  (id, heading, side, road_type, oneway, lanes, road_width_m, curb_offset_m, position_source, centerline, ...)
실행: python -m pudo candidates
"""
import argparse
import json
import math
from collections import defaultdict

import shapely
from shapely.geometry import Point, LineString
from shapely.strtree import STRtree

from .config import DATA_DIR, DRIVABLE, RADIUS_M, SAMPLE_STEP_M
from .geo import to_meters as m, to_lonlat as inv

STEP, RADIUS = SAMPLE_STEP_M, RADIUS_M


MAP1000_CURB_RAY_M = 40      # 중심선에서 차도 끝을 찾는 최대 거리
MAP1000_MEDIAN_M = 12        # 중심선이 중앙분리대 위에 있을 때, 이 거리 안에서 시작하는 차도만 같은 쪽 차도로 봄
MAP1000_GAP_M = 2            # 차도 사이 틈이 이보다 작으면 이어진 차도로 봄 (작은 교통섬·면 경계)
ISLAND_TOUCH_M = 1.0         # 보도 면을 이만큼 넓혀도 도로(차도+보도) 밖으로 나가지 않으면 차도 가운데 섬(중앙 버스승강장 등)으로 봄
CURB_INSET_M = 1.5           # 차도 끝에서 차 중심까지
OSM_MATCH_M = 20             # 이 거리 안, 방향 차이 30° 이하의 OSM 길을 같은 도로로 봄
DIRECTION_LOOK_M = 2         # 도로선 위 한 점의 진행 방향을 잴 때 앞뒤로 보는 거리
RAY_END_TOLERANCE_M = 0.5    # 차도 끝이 가로선 끝(MAP1000_CURB_RAY_M)에서 이 거리 안이면 끝까지 차도 = 교차로·광장 안
BEYOND_ROAD_M = 3            # 잰 차도 끝이 도로폭(보도 포함)/2 + 이 값보다 멀면 옆 도로 면까지 간 것으로 보고 도로폭/2를 씀


def road_class(name):
    """도로명 끝 글자로 도로 등급: 대로(폭 40m 이상·8차로 이상) / 로(12~40m·2~8차로) / 길(그 외).
    도로명주소법 시행령 제6조의 도로 구분. 이름이 없으면 '이름 없음'."""
    name = (name or "").strip()
    if not name:
        return "이름 없음"
    for suffix in ("대로", "로", "길"):
        if name.endswith(suffix):
            return suffix
    return "기타"


def _bearing(a, b):
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360


def _from_map1000(folder, osm_path):
    """1:1000 수치지형도 중심선·차도 면으로 후보 Feature 목록을 만듭니다."""
    from .datasets import _map1000
    source = _map1000(folder)
    osm = [e for e in json.loads(osm_path.read_text(encoding="utf-8"))["elements"]
           if e["type"] == "way" and e["tags"].get("highway") in DRIVABLE] if osm_path.is_file() else []
    osm_lines = [LineString([m(p["lon"], p["lat"]) for p in w["geometry"]]) for w in osm]
    osm_tree = STRtree(osm_lines) if osm_lines else None
    road = shapely.union_all([source.carriageway] + [area for area, _ in source.sidewalks])
    walk_tree = STRtree([area for area, _ in source.sidewalks]) if source.sidewalks else None

    def island(x, y):
        """(x, y)를 덮는 보도 면이 차도로 둘러싸인 섬인지 (강남대로 중앙 버스승강장처럼 1:1000에 보도로 그려진 곳)."""
        point = Point(x, y)
        for i in (walk_tree.query(point) if walk_tree is not None else []):
            area = source.sidewalks[i][0]
            if area.covers(point):
                return area.buffer(ISLAND_TOUCH_M).difference(road).area < ISLAND_TOUCH_M
        return False

    def osm_match(point, direction):
        """같은 방향 OSM 길: (way, 진행 방향과 1:1000 선 방향이 같은지)."""
        best = None
        for i in (osm_tree.query(point.buffer(OSM_MATCH_M)) if osm_tree is not None else []):
            line = osm_lines[i]; distance = line.distance(point)
            if distance > OSM_MATCH_M:
                continue
            at = line.project(point)
            a, b = line.interpolate(max(0.0, at - DIRECTION_LOOK_M)), line.interpolate(min(line.length, at + DIRECTION_LOOK_M))
            way_dir = _bearing((a.x, a.y), (b.x, b.y)); diff = abs((way_dir - direction + 180) % 360 - 180)
            if min(diff, 180 - diff) <= 30 and (best is None or distance < best[0]):
                best = (distance, osm[i], diff < 90)
        return (best[1], best[2]) if best else (None, None)

    def curb_distance(center, heading, road_width):
        """중심선 점에서 heading 쪽 차도 끝까지 거리. 찾지 못하면 None, 차도 면이 없으면 도로폭/2."""
        dx, dy = math.sin(math.radians(heading)), math.cos(math.radians(heading))
        ray = LineString([(center.x, center.y), (center.x + dx * MAP1000_CURB_RAY_M, center.y + dy * MAP1000_CURB_RAY_M)])
        cut = ray.intersection(source.carriageway)
        spans = sorted(
            (min(along), max(along)) for piece in getattr(cut, "geoms", [cut]) if piece.geom_type == "LineString" and piece.length > 0
            for along in [[(x - center.x) * dx + (y - center.y) * dy for x, y in piece.coords]])
        if not spans or spans[0][0] > MAP1000_MEDIAN_M:
            return (road_width / 2, "1:1000 도로폭/2 (차도 면 없음)") if road_width else (None, None)
        start, end = spans[0]
        for begin, finish in spans[1:]:
            middle = (begin + end) / 2
            if begin - end > MAP1000_GAP_M and not island(center.x + dx * middle, center.y + dy * middle):
                break   # 틈이 길가 보도·도로 밖이면 여기가 차도 끝. 차도 가운데 섬이면 건너서 계속
            end = max(end, finish)
        if end >= MAP1000_CURB_RAY_M - RAY_END_TOLERANCE_M:   # 끝까지 차도: 교차로·광장 안
            return None, None
        if road_width and end > road_width / 2 + BEYOND_ROAD_M:   # 도로폭(보도 포함)의 절반보다 멀면 옆 도로 면까지 간 것
            return road_width / 2, "1:1000 도로폭/2 (차도 끝 측정이 도로폭보다 큼)"
        return end, "1:1000 차도 끝"

    feats, skipped, k = [], 0, 0
    for line, total, oneway_1000, name, road_width in source.lines:
        if line.distance(Point(0, 0)) > RADIUS + 20:
            continue
        acc = STEP / 2
        while acc < line.length:
            center = line.interpolate(acc)
            a, b = line.interpolate(max(0.0, acc - DIRECTION_LOOK_M)), line.interpolate(min(line.length, acc + DIRECTION_LOOK_M))
            fwd = _bearing((a.x, a.y), (b.x, b.y))
            acc += STEP
            way, same_dir = osm_match(center, fwd)
            tags = way["tags"] if way else {}
            osm_oneway = str(tags.get("oneway", "no")).lower() in ("yes", "true", "1", "-1")
            # 일방통행 여부는 1:1000을 따릅니다 (OSM은 강남대로처럼 분리된 차도를 상·하행 일방 길로 따로 그림).
            # OSM은 일방통행일 때 진행 방향을 정하는 데만 씁니다.
            oneway = bool(oneway_1000)
            if oneway and osm_oneway:                 # 진행 방향 우측만
                travel = fwd if same_dir == (str(tags.get("oneway")).lower() != "-1") else (fwd + 180) % 360
                sides = [(travel, "R")]
            else:
                sides = [(fwd, "R"), ((fwd + 180) % 360, "L")]
            for side_dir, side in sides:
                heading = (side_dir + 90) % 360
                curb, basis = curb_distance(center, heading, road_width)
                if curb is None:
                    skipped += 1
                    continue
                off = max(0.0, curb - CURB_INSET_M)
                hx, hy = center.x + math.sin(math.radians(heading)) * off, center.y + math.cos(math.radians(heading)) * off
                if math.hypot(hx, hy) > RADIUS:
                    continue
                per_direction = None if total is None else (total if oneway else total // 2)
                road_type = road_class(name)
                feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": list(inv(hx, hy))},
                    "properties": {"id": f"P{k:04d}", "heading": round(heading), "side": side, "road_type": road_type,
                                   "name": name or "", "osm_way": way["id"] if way else None, "oneway": oneway,
                                   "lanes": per_direction, "lanes_source": "map1000", "road_width_m": road_width,
                                   "road_width_source": "1:1000 도로폭 (보도 포함)", "curb_offset_m": round(off, 1),
                                   "position_source": basis, "centerline": list(inv(center.x, center.y))}})
                k += 1
    return feats, skipped


def generate(data_dir):
    """data_dir/map1000 → data_dir/candidates_v1.geojson. 측정 준비(measure.prepare) 때마다 다시 만듭니다."""
    from pathlib import Path
    data_dir = Path(data_dir)
    if not (data_dir / "map1000").is_dir():
        raise SystemExit("필요한 입력이 없습니다: data/map1000/ (1:1000 수치지형도). 공유 데이터 묶음을 풀어 주세요.")
    feats, skipped = _from_map1000(data_dir / "map1000", data_dir / "osm_roads_gangnam.json")
    with (data_dir / "candidates_v1.geojson").open("w", encoding="utf-8") as stream:
        json.dump({"type": "FeatureCollection", "features": feats}, stream, ensure_ascii=False)
    by = defaultdict(int)
    for f in feats: by[f["properties"]["road_type"]] += 1
    print(f"후보 {len(feats)}개 (1:1000 수치지형도 기준. 차도 끝을 찾지 못해 건너뛴 점(교차로 안 등) {skipped}개). 도로 유형별: {dict(by)}")
    return feats


def main(argv=None):
    argparse.ArgumentParser(description="도로망에서 승하차 후보 생성").parse_args(argv)
    generate(DATA_DIR)
    print("저장: data/candidates_v1.geojson")


if __name__ == "__main__":
    main()
