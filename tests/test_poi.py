import unittest

from shapely.geometry import LineString, Point, box

from pudo.datasets import Map1000
from pudo.geo import to_lonlat
from pudo.poi import PoiIndex, _rank_options


def entrance(id, x, y, **tags):
    lon, lat = to_lonlat(x, y)
    return {"type": "node", "id": id, "lon": lon, "lat": lat, "tags": tags}


def stop(x, y, name, number):
    return (x, y), {"정류소": name, "번호": number}


class PoiTests(unittest.TestCase):
    def setUp(self):
        self.layers = {
            "C 소화전": {"features": []},
            "C 횡단보도": {"features": []},
        }
        self.buses = [stop(2, 3, "맞은편", "1"), stop(20, -3, "같은 쪽", "2")]
        lines = [(LineString([(-20, 0), (40, 0)]), 2, False, "테스트로", 4)]
        self.source = Map1000(lines, box(-20, -2, 40, 2),
                              [(box(-20, -6, 10, -2), 4), (box(10, -6, 40, -2), 4),
                               (box(-20, 2, 40, 6), 4)])

    def test_distance_precedes_priority_and_opposite_side_is_excluded(self):
        elements = [
            entrance(1, 30, -4, railway="subway_entrance", ref="1", description="강남역 1번출구"),
            entrance(2, 2, -3, entrance="service", brand="스타벅스"),
        ]
        ranked, fallback = PoiIndex(self.source, self.layers, self.buses, osm_elements=elements).suggest((0, -1), 180, (0, 0))
        self.assertEqual([item["name"] for item in ranked], ["같은 쪽 버스정류장 (2)", "강남역 1번출구"])
        self.assertEqual(fallback["distance_m"], 1)
        self.assertEqual(fallback["bearing"], 0)

    def test_disconnected_same_side_is_included_and_list_stops_at_five(self):
        self.source.sidewalks[1] = (box(11, -6, 40, -2), 4)
        elements = [
            entrance(i, i, -3, railway="subway_entrance", ref=str(i)) for i in range(1, 8)
        ]
        ranked, _ = PoiIndex(self.source, self.layers, self.buses, osm_elements=elements).suggest((0, -1), 180, (0, 0))
        self.assertEqual(len(ranked), 5)
        self.assertTrue(all(item["priority"] == 1 for item in ranked))
        ranked, _ = PoiIndex(self.source, self.layers, self.buses).suggest((0, -1), 180, (0, 0))
        self.assertEqual([item["name"] for item in ranked], ["같은 쪽 버스정류장 (2)"])

    def test_outside_sidewalk_and_curb_gap_allow_same_side_anchors(self):
        self.source.sidewalks = []
        buildings = [(3, "건물 출입구", Point(10, -10)), (5, "테스트로 10", Point(10, -10))]
        junctions = [("가로 · 나로 · 다로 교차로", Point(15, 0))]
        ranked, fallback = PoiIndex(self.source, self.layers, [], junctions, buildings).suggest((0, -1), 180, (0, 0))
        self.assertEqual([item["priority"] for item in ranked], [3, 5, 6])
        self.assertEqual(ranked[-1]["coordinates"], to_lonlat(15, 0))
        self.assertIsNone(fallback)

    def test_building_boundary_suggests_a_roadside_landmark(self):
        buildings = [
            ("맞은편 건물", box(0, 3, 3, 7).exterior),
            ("같은 쪽 건물", box(1, -14, 8, -8).exterior),
            ("먼 건물", box(0, -55, 4, -50).exterior),
        ]
        entrances = [(3, "같은 쪽 건물", Point(20, -15))]
        index = PoiIndex(self.source, self.layers, [], buildings=entrances, building_boundaries=buildings)
        ranked, _ = index.suggest((0, -1), 180, (0, 0))
        self.assertEqual([item["name"] for item in ranked], ["같은 쪽 건물 옆 · 테스트로"])
        self.assertEqual(ranked[0]["kind"], "building_side")
        self.assertEqual(ranked[0]["coordinates"], to_lonlat(1, -8))

    def test_shop_candidates_use_nearest_same_side_and_keep_review_details(self):
        shops = [
            ("맞은편 가게", Point(1, 3), {"kind": "shop"}),
            ("두 번째 가게", Point(10, -8), {"kind": "shop"}),
            ("가까운 가게", Point(4, -8), {"kind": "shop", "address": "테스트로 5", "floor": "1"}),
            ("먼 가게", Point(0, -55), {"kind": "shop"}),
        ]
        index = PoiIndex(self.source, self.layers, [], shops=shops)
        ranked, _ = index.suggest((0, -1), 180, (0, 0))
        self.assertEqual([item["name"] for item in ranked], ["가까운 가게"])
        self.assertEqual(ranked[0]["address"], "테스트로 5")
        self.assertEqual(ranked[0]["kind"], "shop")

    def test_shop_csv_filters_radius_and_uses_latest_seoul_snapshot(self):
        import csv
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from pudo.datasets import SBIZ_SEOUL_CSV, _shop_anchors

        with TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / SBIZ_SEOUL_CSV.replace("*", "202606")).write_text("이전 판")
            with (directory / SBIZ_SEOUL_CSV.replace("*", "202609")).open(
                "w", encoding="utf-8-sig", newline=""
            ) as stream:
                writer = csv.writer(stream)
                writer.writerow(["상호명", "지점명", "경도", "위도"])
                writer.writerow(["가게", "강남점", *to_lonlat(0, 0)])
                writer.writerow(["범위 밖", "", *to_lonlat(550, 0)])
                writer.writerow(["잘못된 좌표", "", "nan", ""])
            self.assertEqual([name for name, _, _ in _shop_anchors(directory)], ["가게 강남점"])

    def test_similar_distances_use_type_priority_and_structures_come_last(self):
        options = [(priority, distance, name, 0, 0, None) for priority, distance, name in [
            (2, 44.5, "먼 정류장"),
            (3, 10.6, "가까운 건물"),
            (2, 14, "비슷한 거리의 정류장"),
            (1, 18, "지하철 출구"),
            (7, 2, "소화전"),
        ]]
        self.assertEqual([option[2] for option in _rank_options(options)],
                         ["비슷한 거리의 정류장", "가까운 건물", "지하철 출구", "먼 정류장", "소화전"])
