"""승하차 지점과 도로 중심선의 같은 쪽에 있는 이름 기준점."""
import math
import re

import shapely
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points, substring
from shapely.strtree import STRtree

from .geo import to_lonlat, to_meters

OSM_POI_FILE = "osm_poi_gangnam.json"
SEARCH_M = 60
CURB_PROBE_M = 4
BUILDING_SIDE_M = 25  # 먼 건물까지 “옆”으로 안내하지 않도록 외곽선 검색 반경을 제한합니다.
SHOP_SEARCH_M = 25
SHOP_PRIORITY = 4
STRUCTURE_PRIORITY = 7
SIMILAR_DISTANCE_M = 5
GEOMETRY_TOLERANCE_M = 0.05
CENTER_MATCH_M = 2
LEGACY_CENTER_MATCH_M = 25
CENTER_ANGLE_DEG = 30


def _subway_anchors(elements):
    for node in elements:
        tags = node.get("tags", {})
        ref = tags.get("ref")
        if (node["type"] != "node" or tags.get("railway") != "subway_entrance" or not ref
                or tags.get("access") in {"private", "no", "customers"}
                or tags.get("entrance") in {"no", "exit", "service", "emergency", "garage"}):
            continue
        name = tags.get("name:ko") or tags.get("name") or ""
        label = next((label for label in (name, tags.get("description:ko", ""), tags.get("description", ""))
                      if re.search(rf"(?<!\d){re.escape(ref)}\s*번\s*출구", label)), None)
        station = tags.get("station") or name or "지하철"
        yield 1, label or f"{station} {ref}번 출구", Point(to_meters(node["lon"], node["lat"]))


def _anchors(layers, buses, junctions, buildings, osm_elements):
    yield from _subway_anchors(osm_elements)
    yield from buildings
    for name, point in junctions:
        yield 6, name, point
    for position, props in buses:
        if props.get("정류소"):
            label = f"{props['정류소']} 버스정류장"
            if props.get("번호"):
                label += f" ({props['번호']})"
            yield 2, label, Point(position)
    for layer, label in (("C 소화전", "소화전"), ("C 횡단보도", "횡단보도")):
        for feature in layers[layer]["features"]:
            props = feature["properties"]
            detail = props.get("주소") or props.get("관리번호")
            yield STRUCTURE_PRIORITY, f"{detail} {label}" if detail else label, Point(to_meters(*feature["geometry"]["coordinates"]))


def _rank_options(options):
    """가까운 후보부터 묶고, 거리 차가 작은 묶음 안에서 유형을 우선합니다."""
    by_distance = sorted(options, key=lambda item: (item[0] == STRUCTURE_PRIORITY, item[1]))
    group = []
    for option in by_distance:
        if group and (
            (option[0] == STRUCTURE_PRIORITY) != (group[0][0] == STRUCTURE_PRIORITY)
            or option[1] - group[0][1] > SIMILAR_DISTANCE_M
        ):
            yield from sorted(group, key=lambda item: item[:5])
            group = []
        group.append(option)
    yield from sorted(group, key=lambda item: item[:5])


class PoiIndex:
    def __init__(self, source, layers, buses, junctions=(), buildings=(), osm_elements=(), *, building_boundaries=(), shops=()):
        self.source = source
        merged = shapely.union_all([area for area, _ in source.sidewalks])
        self.walks = list(shapely.get_parts(merged)) if not merged.is_empty else []
        self.walk_tree = STRtree(self.walks)
        self.anchors = [(priority, name, point, None)
                        for priority, name, point in _anchors(layers, buses, junctions, buildings, osm_elements)]
        self.anchors.extend((SHOP_PRIORITY, name, point, details) for name, point, details in shops)
        self.anchor_tree = STRtree([point for _, _, point, _ in self.anchors])
        self.building_boundaries = list(building_boundaries)
        self.building_tree = STRtree([boundary for _, boundary in self.building_boundaries])

    def _curb(self, point, outward):
        probe = LineString([point, (point.x + CURB_PROBE_M * outward[0], point.y + CURB_PROBE_M * outward[1])])
        cuts = [probe.intersection(self.walks[i]) for i in self.walk_tree.query(probe, predicate="intersects")]
        curb = min((nearest_points(point, cut)[1] for cut in cuts if cut.length), key=point.distance, default=None)
        if curb is not None and not point.equals(curb):
            if LineString([point, curb]).difference(self.source.carriageway).length > GEOMETRY_TOLERANCE_M:
                return None
        return curb

    def _road(self, center, heading, max_distance):
        road_direction = (heading + 90) % 180
        matches = []
        for index in self.source.line_tree.query(center.buffer(max_distance)):
            line = self.source.lines[index][0]
            distance = line.distance(center)
            at = line.project(center)
            a, b = line.interpolate(max(0, at - 2)), line.interpolate(min(line.length, at + 2))
            direction = math.degrees(math.atan2(b.x - a.x, b.y - a.y)) % 180
            diff = abs(direction - road_direction)
            if distance <= max_distance and min(diff, 180 - diff) <= CENTER_ANGLE_DEG:
                matches.append((distance, min(diff, 180 - diff), index))
        return min(matches)[2] if matches else None

    def _same_side(self, start, anchor, road_index, road_name):
        link = LineString([start, anchor])
        if link.length <= 2 * GEOMETRY_TOLERANCE_M:
            return True
        # 끝점은 허용하고, 도엽 경계에서 끊긴 같은 도로의 중심선도 검사합니다.
        interior = substring(link, GEOMETRY_TOLERANCE_M, link.length - GEOMETRY_TOLERANCE_M)
        return not any(i == road_index or (road_name and self.source.lines[i][3] == road_name)
                       for i in self.source.line_tree.query(interior, predicate="intersects"))

    def _building_side(self, origin, start, outward, road_index, road_name):
        options = []
        for idx in self.building_tree.query(origin.buffer(BUILDING_SIDE_M).envelope):
            name, boundary = self.building_boundaries[idx]
            anchor = nearest_points(origin, boundary)[1]
            distance = origin.distance(anchor)
            dx, dy = anchor.x - origin.x, anchor.y - origin.y
            if distance > BUILDING_SIDE_M or dx * outward[0] + dy * outward[1] <= 0:
                continue
            if self._same_side(start, anchor, road_index, road_name):
                options.append((3, distance, name, anchor.x, anchor.y, {"kind": "building_side"}))
        return min(options, default=None)

    def suggest(self, position, heading, centerline=None):
        point = Point(position)
        angle = math.radians(heading)
        outward = math.sin(angle), math.cos(angle)
        curb = self._curb(point, outward)
        fallback = None
        if curb is not None:
            fallback = {"name": "승하차 지점 옆 보도", "distance_m": round(point.distance(curb), 1),
                        "bearing": round((heading + 180) % 360),
                        "coordinates": to_lonlat(curb.x, curb.y), "curb": to_lonlat(point.x, point.y)}

        center = Point(centerline) if centerline is not None else point
        road_index = self._road(center, heading, CENTER_MATCH_M if centerline is not None else LEGACY_CENTER_MATCH_M)
        if road_index is None:
            return [], fallback

        road, _, _, road_name, _ = self.source.lines[road_index]
        start = point
        if road.distance(point) <= GEOMETRY_TOLERANCE_M:
            # 좁은 도로에서는 후보와 중심점이 같을 수 있어 후보의 바깥 방향에서 검사합니다.
            start = Point(point.x + 2 * GEOMETRY_TOLERANCE_M * outward[0],
                          point.y + 2 * GEOMETRY_TOLERANCE_M * outward[1])
        origin = curb if curb is not None else point
        options = []
        for idx in self.anchor_tree.query(origin.buffer(SEARCH_M)):
            priority, name, anchor, details = self.anchors[idx]
            distance = origin.distance(anchor)
            if distance > SEARCH_M:
                continue
            if priority == SHOP_PRIORITY:
                dx, dy = anchor.x - origin.x, anchor.y - origin.y
                if distance > SHOP_SEARCH_M or dx * outward[0] + dy * outward[1] <= 0:
                    continue
            if self._same_side(start, anchor, road_index, road_name):
                options.append((priority, distance, name, anchor.x, anchor.y, details))
        building_side = self._building_side(origin, start, outward, road_index, road_name)
        if building_side:
            options.append(building_side)
        unique = {}
        shop_added = False
        for priority, distance, name, x, y, details in _rank_options(options):
            is_shop = priority == SHOP_PRIORITY
            if name in unique or (is_shop and shop_added):
                continue
            is_building_side = details and details["kind"] == "building_side"
            label = f"{name} 옆" if is_building_side else name
            if is_building_side and road_name:
                label += f" · {road_name}"
            item = {"name": label, "priority": priority, "distance_m": round(distance, 1),
                    "coordinates": to_lonlat(x, y), "curb": to_lonlat(origin.x, origin.y)}
            if details:
                item.update(details)
            unique[name] = item
            shop_added |= is_shop
        return list(unique.values())[:5], fallback
