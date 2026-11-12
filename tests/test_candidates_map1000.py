"""1:1000 수치지형도로 후보를 만드는 방식(candidates._from_map1000) 검증. 작은 shp를 직접 만들어 씁니다."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pudo import candidates
from pudo.geo import to_meters
from support import write_map1000

class Map1000CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        write_map1000(self.data / "map1000")
        with patch.object(candidates, "DATA_DIR", self.data):
            self.features, self.skipped = candidates._from_map1000(self.data / "map1000")
        self.props = [f["properties"] for f in self.features]

    def tearDown(self):
        self.temp.cleanup()

    def test_candidates_sit_inside_the_carriageway_edge(self):
        main = [p for p in self.props if p["name"] == "가로로"]
        self.assertTrue(main)
        for p in main:
            self.assertEqual(p["position_source"], "1:1000 차도 끝")
            self.assertAlmostEqual(p["curb_offset_m"], 7 - 1.5, delta=0.2)   # 차도 끝(보도 시작) 7m에서 1.5m 안쪽
            self.assertEqual(p["lanes"], 1)                                   # 차로수 2(양방향 합) → 편도 1
            self.assertEqual(p["road_width_m"], 20)
        self.assertEqual({p["side"] for p in main}, {"R", "L"})              # 양방향은 양쪽

    def test_both_sides_mirror_each_other(self):
        ys = sorted(round(to_meters(*f["geometry"]["coordinates"])[1]) for f in self.features if f["properties"]["name"] == "가로로")
        self.assertEqual({ys[0], ys[-1]}, {-6, 6})                            # 중심선에서 ±5.5m (반올림)

    def test_no_candidate_inside_the_crossing(self):
        for f in self.features:
            x, y = to_meters(*f["geometry"]["coordinates"])
            self.assertFalse(abs(x) < 4 and abs(y) < 7, (x, y))              # 두 차도가 겹치는 교차로 안에는 없음

    def test_without_osm_road_type_comes_from_name(self):
        self.assertTrue(all(p["road_type"] == "residential" and p["osm_way"] is None for p in self.props))


if __name__ == "__main__":
    unittest.main()
