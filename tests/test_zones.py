import unittest

from shapely.geometry import LineString

from pudo.geo import to_lonlat
from pudo.zones import build_child_zone, map1000_edges, zone_evidence


def line(points, name):
    """1:1000 도로중심선 한 줄 (선, 차로수, 일방, 도로명, 도로폭)."""
    return (LineString(points), 2, False, name, 8)


def point(x, y, **properties):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": list(to_lonlat(x, y))}, "properties": properties}


def covered(zone, x, y):
    from shapely.geometry import Point
    return zone is not None and zone.distance(Point(x, y)) < 1


class ZoneTests(unittest.TestCase):
    def setUp(self):
        # 동서 도로 '학교길'(-400~400, 1:1000처럼 두 토막)과 x=0에서 만나는 남북 도로 '옆길'(한 줄, 중간에서 만남)
        self.roads = map1000_edges([
            line([(-400, 0), (0, 0)], "학교길"), line([(0, 0), (400, 0)], "학교길"),
            line([(0, -300), (0, 300)], "옆길"),
        ], snap=2)

    def test_two_marks_on_same_road_fill_between_and_stop_at_boundary(self):
        seeds = [((-100, 3), "노면 '어린이'"), ((100, 3), "노면 '보호구역'")]
        boundaries = [((-130, 5), "표지 '어린이보호구역 해제'")]
        zone = build_child_zone(self.roads, seeds, boundaries)
        self.assertTrue(covered(zone, 0, 0) and covered(zone, 140, 0))   # 사이 + 앞뒤 50m
        self.assertTrue(covered(zone, -125, 0))
        self.assertFalse(covered(zone, -140, 0))                         # 해제 표지 바깥
        self.assertFalse(covered(zone, 200, 0))                          # 근거에서 50m 넘음
        self.assertFalse(covered(zone, 0, 60))                           # 다른 이름 도로로 퍼지지 않음

    def test_far_apart_marks_are_not_joined(self):
        zone = build_child_zone(self.roads, [((-350, 0), "노면 '어린이'"), ((350, 0), "노면 '어린이'")], [])
        self.assertFalse(covered(zone, 0, 0))

    def test_designated_facility_snaps_farther_than_marks_and_spreads_both_ways(self):
        # 시설 좌표(건물 중심)는 도로에서 40m 떨어져도 붙고, 표지·노면표시는 20m까지만 붙음
        facility = build_child_zone(self.roads, [((100, 40), "지정 시설 어린이집 (어린이집)")], [])
        self.assertTrue(covered(facility, 100, 0) and covered(facility, 240, 0) and covered(facility, -40, 0))
        self.assertFalse(covered(facility, 300, 0))
        self.assertTrue(covered(facility, 0, 40) and not covered(facility, 0, 60))   # 다른 도로로도 남은 거리(50m)만큼 퍼짐
        self.assertIsNone(build_child_zone(self.roads, [((100, 40), "노면 '어린이'")], []))
        self.assertIsNone(build_child_zone(self.roads, [((100, 80), "지정 시설 어린이집 (어린이집)")], []))

    def test_map1000_lines_are_joined_by_position(self):
        # 끝점이 1m 어긋나도 같은 노드, 끝점이 다른 선 중간에 닿으면(T자) 그 선을 나눠 이음, 중간끼리 교차는 잇지 않음
        edges = map1000_edges([
            line([(0, 0), (100, 0)], "가로"), line([(101, 0), (200, 0)], "가로"),
            line([(50, 1), (50, 80)], "골목"), line([(150, -50), (150, 50)], "고가"),
        ], snap=2)
        nodes = {}
        for geometry, a, b, name in edges:
            nodes.setdefault(name, []).append((round(geometry.length), a, b))
        self.assertEqual(sorted(length for length, *_ in nodes["가로"]), [50, 50, 99])   # 첫 토막이 T자 자리에서 나뉨
        joined = {a for _, a, _ in nodes["가로"]} | {b for _, _, b in nodes["가로"]}
        self.assertIn(nodes["골목"][0][1], joined)                                    # 골목 시작점이 가로와 같은 노드
        self.assertNotIn(nodes["고가"][0][1], joined)
        self.assertEqual(len(nodes["고가"]), 1)                                        # 중간 교차는 나누지 않음

    def test_evidence_classification(self):
        marks = [point(0, 0, CTT="어린이"), point(50, 0, CTT="보호구역"), point(52, 3, CTT="노인"), point(90, 0, CTT="30")]
        signs = [point(10, 0, MRK_NUM="324", RMK=""), point(20, 0, MRK_NUM="751", RMK="어린이보호구역 해제"),
                 point(30, 0, MRK_NUM="751", RMK="50해제"), point(40, 0, MRK_NUM="403", RMK="불법 주정차 특별 단속구역 여기부터 200m"),
                 point(60, 0, MRK_NUM="751", RMK="30/여기부터속도를줄이시오/LED")]
        seeds, boundaries = zone_evidence(signs, marks, [])
        self.assertEqual(sorted(label for _, label in seeds), ["노면 '어린이'", "어린이보호 표지"])
        self.assertEqual(len(boundaries), 2)


if __name__ == "__main__":
    unittest.main()
