import unittest

from shapely.geometry import box

from pudo.datasets import Map1000
from pudo.geo import to_lonlat
from pudo.poi import PoiIndex


def entrance(id, x, y, **tags):
    lon, lat = to_lonlat(x, y)
    return {"type": "node", "id": id, "lon": lon, "lat": lat, "tags": tags}


def stop(x, y, name, number):
    return {"geometry": {"coordinates": to_lonlat(x, y)},
            "properties": {"정류소": name, "번호": number}}


class PoiTests(unittest.TestCase):
    def setUp(self):
        self.layers = {
            "C 버스정류소": {"features": [stop(2, 3, "맞은편", "1"), stop(20, -3, "같은 보도", "2")]},
            "C 소화전": {"features": []},
            "C 횡단보도": {"features": []},
        }
        self.source = Map1000([], box(-20, -2, 40, 2),
                              [(box(-20, -6, 10, -2), 4), (box(10, -6, 40, -2), 4),
                               (box(-20, 2, 40, 6), 4)])

    def test_priority_precedes_distance_and_opposite_side_is_excluded(self):
        elements = [
            entrance(1, 30, -4, railway="subway_entrance", ref="1", description="강남역 1번출구"),
            entrance(2, 2, -3, entrance="service", brand="스타벅스"),
        ]
        ranked, fallback = PoiIndex(self.source, self.layers, [], elements).suggest((0, 0), 180)
        self.assertEqual([item["name"] for item in ranked], ["강남역 1번출구", "같은 보도 버스정류장 (2)"])
        self.assertEqual(fallback["distance_m"], 2)
        self.assertEqual(fallback["bearing"], 0)

    def test_disconnected_same_side_is_excluded_and_list_stops_at_five(self):
        self.source.sidewalks[1] = (box(11, -6, 40, -2), 4)
        elements = [
            entrance(i, i, -3, railway="subway_entrance", ref=str(i)) for i in range(1, 8)
        ]
        ranked, _ = PoiIndex(self.source, self.layers, [], elements).suggest((0, 0), 180)
        self.assertEqual(len(ranked), 5)
        self.assertTrue(all(item["priority"] == 1 for item in ranked))
        ranked, _ = PoiIndex(self.source, self.layers, []).suggest((0, 0), 180)
        self.assertEqual(ranked, [])

    def test_no_curb_evidence_produces_no_candidates(self):
        ranked, fallback = PoiIndex(self.source, self.layers, []).suggest((0, 0), 90)
        self.assertEqual(ranked, [])
        self.assertIsNone(fallback)
