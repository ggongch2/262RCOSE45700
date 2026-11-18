"""승하차 지점과 지도상 같은 보도로 연결되는 이름 기준점."""
import math
import re
from collections import defaultdict

import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points
from shapely.strtree import STRtree

from .geo import to_lonlat, to_meters

OSM_POI_FILE = "osm_poi_gangnam.json"
SEARCH_M = 60
CURB_PROBE_M = 4
GEOMETRY_TOLERANCE_M = 0.05
LARGE_BUILDING_M2 = 500
CHAINS = {"스타벅스", "맥도날드", "버거킹", "롯데리아", "올리브영", "다이소",
          "이디야커피", "메가MGC커피", "투썸플레이스", "파리바게뜨", "GS25", "CU", "세븐일레븐",
          "Starbucks", "McDonald's", "Burger King", "Daiso", "7-Eleven"}


def _name(tags):
    return tags.get("name:ko") or tags.get("name") or ""


def _osm_anchors(elements):
    buildings = defaultdict(list)
    for way in elements:
        tags = way.get("tags", {})
        if way["type"] == "way":
            geometry = way.get("geometry", ())
            area = Polygon([to_meters(p["lon"], p["lat"]) for p in geometry]).area if len(geometry) >= 4 else 0
            for node in way.get("nodes", ()):
                buildings[node].append((tags, area))

    for node in elements:
        if node["type"] != "node":
            continue
        tags = node.get("tags", {})
        point = Point(to_meters(node["lon"], node["lat"]))
        if tags.get("access") in {"private", "no", "customers"} or tags.get("entrance") in {
            "no", "exit", "service", "emergency", "garage",
        }:
            continue
        if tags.get("railway") == "subway_entrance" and tags.get("ref"):
            label = next((label for label in (_name(tags), tags.get("description:ko", ""), tags.get("description", ""))
                          if re.search(rf"(?<!\d){re.escape(tags['ref'])}\s*번\s*출구", label)), None)
            station = tags.get("station") or _name(tags) or "지하철"
            yield 1, label or f"{station} {tags['ref']}번 출구", point
        elif tags.get("entrance"):
            for place, area in [(tags, 0), *buildings[node["id"]]]:
                name = _name(place)
                brand = place.get("brand:ko") or place.get("brand")
                if brand in CHAINS:
                    yield 4, name or brand, point
                elif name and (area >= LARGE_BUILDING_M2 or place.get("amenity") in {"hospital", "university", "library", "theatre"}
                               or place.get("shop") == "mall"):
                    yield 3, name, point
                street, number = place.get("addr:street"), place.get("addr:housenumber")
                if street and number:
                    yield 5, f"{street} {number}", point


def _junctions(roads):
    names_at = defaultdict(set)
    points = {}
    for way in roads:
        tags = way.get("tags", {})
        name = _name(tags)
        if not name or tags.get("bridge") == "yes" or tags.get("tunnel") == "yes":
            continue
        for node, coord in zip(way.get("nodes", ()), way.get("geometry", ())):
            names_at[node].add(name)
            points[node] = Point(to_meters(coord["lon"], coord["lat"]))
    for node, names in names_at.items():
        if len(names) == 2:
            yield 6, " · ".join(sorted(names)) + " 교차로", points[node]


def _anchors(layers, roads, osm_elements):
    yield from _osm_anchors(osm_elements)
    yield from _junctions(roads)
    for layer, priority, label in (("C 버스정류소", 2, "버스정류장"),
                                   ("C 소화전", 7, "소화전"),
                                   ("C 횡단보도", 7, "횡단보도")):
        for feature in layers[layer]["features"]:
            props = feature["properties"]
            if priority == 2:
                if not props.get("정류소"):
                    continue
                label_text = f"{props['정류소']} {label}"
                if props.get("번호"):
                    label_text += f" ({props['번호']})"
            else:
                detail = props.get("주소") or props.get("관리번호")
                label_text = f"{detail} {label}" if detail else label
            yield priority, label_text, Point(to_meters(*feature["geometry"]["coordinates"]))


class PoiIndex:
    def __init__(self, source, layers, roads, osm_elements=()):
        self.carriageway = source.carriageway
        # 붙어 있는 보도 면은 합치되, 끊긴 구간이나 차도 위로 보도를 확장하지 않습니다.
        merged = shapely.union_all([area for area, _ in source.sidewalks])
        self.walks = list(shapely.get_parts(merged)) if not merged.is_empty else []
        self.walk_tree = STRtree(self.walks)
        self.anchors = list(_anchors(layers, roads, osm_elements))
        self.anchor_tree = STRtree([point for _, _, point in self.anchors])

    def suggest(self, position, heading):
        point = Point(position)
        angle = math.radians(heading)
        probe = LineString([position, (point.x + CURB_PROBE_M * math.sin(angle),
                                       point.y + CURB_PROBE_M * math.cos(angle))])
        entries = []
        for index in self.walk_tree.query(probe, predicate="intersects"):
            cut = probe.intersection(self.walks[index])
            if cut.length:
                entries.append((point.distance(cut), index))
        if not entries:
            return [], None
        _, index = min(entries)
        walk = self.walks[index]
        curb = nearest_points(point, probe.intersection(walk))[1]
        inner_probe = LineString([point, curb])
        # 후보에서 보도까지 짧게 차도 가장자리를 지나는 구간도 중간에 끊기면 배제합니다.
        if not point.equals(curb) and not self.carriageway.covers(inner_probe):
            if inner_probe.difference(self.carriageway).length > GEOMETRY_TOLERANCE_M:
                return [], None

        options = []
        for idx in self.anchor_tree.query(curb.buffer(SEARCH_M)):
            priority, name, anchor = self.anchors[idx]
            if priority == 6:
                # 노드는 교차로 중앙에 있으므로 인접한 현재 보도 모서리의 위치로 표시합니다.
                nearest = nearest_points(anchor, walk)[1]
                if anchor.distance(nearest) > 20:
                    continue
                anchor = nearest
            distance = curb.distance(anchor)
            if distance > SEARCH_M or not walk.covers(anchor):
                continue
            if distance and LineString([curb, anchor]).difference(walk).length > GEOMETRY_TOLERANCE_M:
                continue
            options.append((priority, distance, name, anchor.x, anchor.y))
        options.sort()
        unique = {}
        for priority, distance, name, x, y in options:
            unique.setdefault(name, {"name": name, "priority": priority, "distance_m": round(distance, 1),
                                     "coordinates": to_lonlat(x, y), "curb": to_lonlat(curb.x, curb.y)})
        fallback = {"name": "승하차 지점 옆 보도", "distance_m": round(point.distance(curb), 1),
                    "bearing": round((heading + 180) % 360),
                    "coordinates": to_lonlat(curb.x, curb.y), "curb": to_lonlat(point.x, point.y)}
        return list(unique.values())[:5], fallback
