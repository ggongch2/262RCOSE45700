import unittest

from pudo.geo import to_lonlat
from pudo.zones import build_child_zone, zone_evidence


def way(identifier, nodes, points, name):
    return {"type": "way", "id": identifier, "nodes": nodes, "tags": {"highway": "residential", "name": name},
            "geometry": [dict(zip(("lon", "lat"), to_lonlat(x, y))) for x, y in points]}


def point(x, y, **properties):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": list(to_lonlat(x, y))}, "properties": properties}


def covered(zone, x, y):
    from shapely.geometry import Point
    return zone is not None and zone.distance(Point(x, y)) < 1


class ZoneTests(unittest.TestCase):
    def setUp(self):
        # 동서 도로 '학교길'(-400~400)과 x=0에서 만나는 남북 도로 '옆길'
        self.roads = [
            way(1, [1, 2, 3], [(-400, 0), (0, 0), (400, 0)], "학교길"),
            way(2, [4, 2, 5], [(0, -300), (0, 0), (0, 300)], "옆길"),
        ]

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
