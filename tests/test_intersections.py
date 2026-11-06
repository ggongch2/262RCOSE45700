import unittest

from pudo.config import CENTER_LAT, CENTER_LON
from pudo.datasets import _intersection_area, _intersections
from pudo.geo import to_lonlat


def way(identifier, nodes, points):
    """미터 좌표 points로 OSM way를 만듭니다."""
    return {"type": "way", "id": identifier, "nodes": nodes,
            "geometry": [dict(zip(("lon", "lat"), to_lonlat(x, y))) for x, y in points]}


def found(elements):
    return [(round(x), round(y)) for x, y in _intersections(elements)]


class IntersectionTests(unittest.TestCase):
    def test_crossing_and_t_junction_are_intersections(self):
        elements = [
            way(1, [1, 2, 3], [(-50, 0), (0, 0), (50, 0)]),
            way(2, [4, 2, 5], [(0, -50), (0, 0), (0, 50)]),   # 십자
            way(3, [3, 6], [(50, 0), (50, 50)]),               # 1번 끝에서 꺾임
        ]
        self.assertEqual(sorted(found(elements)), [(0, 0), (50, 0)])

    def test_straight_split_of_one_road_is_not_an_intersection(self):
        elements = [
            way(1, [1, 2], [(-50, 0), (0, 0)]),
            way(2, [2, 3], [(0, 0), (50, 5)]),  # 약 6° 꺾임: 속성 변화로 나뉜 같은 길
        ]
        self.assertEqual(found(elements), [])

    def test_corner_between_two_road_ends_is_kept(self):
        elements = [
            way(1, [1, 2], [(-50, 0), (0, 0)]),
            way(2, [2, 3], [(0, 0), (0, 50)]),  # 90° 모퉁이 (도로교통법 제32조 2호)
        ]
        self.assertEqual(found(elements), [(0, 0)])

    def test_closed_way_start_is_not_an_intersection_unless_joined(self):
        loop = way(1, [1, 2, 3, 1], [(0, 0), (30, 0), (30, 30), (0, 0)])
        self.assertEqual(found([loop]), [])
        self.assertEqual(found([loop, way(2, [1, 4], [(0, 0), (-50, 0)])]), [(0, 0)])

    def test_repeated_vertex_does_not_break_direction(self):
        elements = [
            way(1, [1, 2, 3], [(-50, 0), (0, 0), (0, 0)]),
            way(2, [3, 4], [(0, 0), (50, 0)]),
        ]
        self.assertEqual(found(elements), [])


class IntersectionAreaTests(unittest.TestCase):
    def test_crossing_area_is_overlap_of_carriageways(self):
        elements = [
            way(1, [1, 2, 3], [(-50, 0), (0, 0), (50, 0)]),
            way(2, [4, 2, 5], [(0, -50), (0, 0), (0, 50)]),
        ]
        area = _intersection_area(elements, {1: 6, 2: 4})
        self.assertAlmostEqual(area.area, 12 * 8, delta=1)   # 폭 12m × 8m 상자

    def test_divided_road_median_is_inside_one_intersection(self):
        tags = {"name": "대로"}
        elements = [
            {**way(1, [1, 2, 3], [(-50, 8), (0, 8), (50, 8)]), "tags": tags},     # 상행
            {**way(2, [4, 5, 6], [(50, -8), (0, -8), (-50, -8)]), "tags": tags},  # 하행
            way(3, [7, 2, 5, 8], [(0, 50), (0, 8), (0, -8), (0, -50)]),
        ]
        area = _intersection_area(elements, {1: 5, 2: 5, 3: 4})
        self.assertTrue(area.contains(to_point(0, 0)))   # 중앙분리대 위도 교차로 안
        self.assertFalse(area.contains(to_point(0, 20)))


def to_point(x, y):
    from shapely.geometry import Point
    return Point(x, y)


if __name__ == "__main__":
    unittest.main()
