import unittest

from pudo.config import CENTER_LAT, CENTER_LON
from pudo.datasets import _intersections
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


if __name__ == "__main__":
    unittest.main()
