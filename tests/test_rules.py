import copy
import unittest

from pudo.config import CENTER_LAT, CENTER_LON, TH
from pudo.datasets import MapData
from pudo.geo import distance_to_points, distance_to_segment, distance_to_segments
from pudo.rules import evaluate_candidates, judge


def candidate():
    return {
        "type": "Feature", "geometry": {"type": "Point", "coordinates": [CENTER_LON, CENTER_LAT]},
        "properties": {"id": "P0000", "road_type": "primary", "lanes": 2, "lanes_source": "hdmap",
                       "oneway": False, "heading": 90, "road_width_m": 10},
    }


def clear_map():
    return MapData(intersections=[(100, 100)], crosswalks=[(100, 100)], hydrants=[(100, 100)],
                   buses=[(100, 100)], schools=[((400, 0), "학교")])


class RuleTests(unittest.TestCase):
    def test_modes_are_explicit_independent_and_do_not_mutate_inputs(self):
        feature, data = candidate(), clear_map()
        data.intersections = [(10, 0)]
        data.schools = [((100, 0), "학교")]
        original_feature, original_data, original_thresholds = copy.deepcopy((feature, data, TH))
        first = judge(feature, data, mode="strict")
        lenient = judge(feature, data, mode="lenient")
        last = judge(feature, data, mode="strict")
        self.assertEqual(first, last)
        self.assertEqual(first["properties"]["verdict"], "불가")
        self.assertEqual(lenient["properties"]["verdict"], "확인 필요")
        self.assertEqual(lenient["properties"]["fail_reasons"], [])
        self.assertEqual(len(lenient["properties"]["check_reasons"]), 2)
        self.assertEqual((feature, data, TH), (original_feature, original_data, original_thresholds))

    def test_lenient_does_not_relax_hydrant_rule(self):
        data = clear_map()
        data.hydrants = [(1, 0)]
        data.intersections = [(10, 0)]
        for mode in ("strict", "lenient"):
            result = judge(candidate(), data, mode=mode)["properties"]
            self.assertEqual(result["verdict"], "불가")
            self.assertTrue(any(reason.startswith("H4") for reason in result["fail_reasons"]))

    def test_threshold_and_coordinate_error_boundaries(self):
        for distance, expected in ((5, "불가"), (6, "확인 필요"), (12, "확인 필요"), (13, "가능")):
            with self.subTest(distance=distance):
                data = clear_map()
                data.hydrants = [(distance, 0)]
                self.assertEqual(judge(candidate(), data)["properties"]["verdict"], expected)

    def test_unknown_data_requires_confirmation(self):
        data = clear_map()
        data.hydrants = []
        properties = judge(candidate(), data)["properties"]
        self.assertEqual(properties["verdict"], "확인 필요")
        check = next(check for check in properties["checks"] if check["item"] == "H4 소화전")
        self.assertIsNone(check["value"])
        self.assertIsNone(check["pass"])

    def test_custom_thresholds_do_not_change_defaults_or_later_calls(self):
        feature, data = candidate(), clear_map()
        data.hydrants = [(8, 0)]
        before = judge(feature, data)
        custom = {**TH, "H4 소화전": 10}
        self.assertEqual(judge(feature, data, thresholds=custom)["properties"]["verdict"], "불가")
        self.assertEqual(judge(feature, data), before)
        self.assertEqual(TH["H4 소화전"], 5)

    def test_zero_distance_to_hdmap_line_counts_as_inside(self):
        feature, data = candidate(), clear_map()
        data.hd_link = [[(-50, 0), (50, 0)]]
        data.hd_reg = [[(-50, 0), (50, 0)]]  # 후보점이 규제선 위에 있음 (거리 0)
        result = judge(feature, data)
        self.assertEqual(result["properties"]["verdict"], "확인 필요")
        self.assertIn("H5 규제선 자료 불일치 (2021 없음, 2023 있음) → 사진 확인", result["properties"]["check_reasons"])

    def test_hdmap_curb_threshold_is_configurable(self):
        feature, data = candidate(), clear_map()
        data.hd_link = [[(-50, 0), (50, 0)]]
        data.hd_reg = [[(-50, 5.5), (50, 5.5)]]
        self.assertTrue(any(r.startswith("H5 규제선 자료 불일치") for r in judge(feature, data)["properties"]["check_reasons"]))
        narrow = {**TH, "H5 정밀도로지도 규제선": 5}
        self.assertFalse(any(r.startswith("H5") for r in judge(feature, data, thresholds=narrow)["properties"]["check_reasons"]))

    def test_invalid_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "판정 모드"):
            judge(candidate(), clear_map(), mode="unknown")

    def test_combined_output_preserves_v0_mode_fields(self):
        feature, data = candidate(), clear_map()
        data.intersections = [(10, 0)]
        strict = judge(feature, data, mode="strict")["properties"]
        lenient = judge(feature, data, mode="lenient")["properties"]
        result = evaluate_candidates([feature], data)["features"][0]["properties"]
        self.assertEqual(result["verdict"], strict["verdict"])
        self.assertEqual(result["checks"], strict["checks"])
        self.assertEqual(result["needs_check"], strict["needs_check"])
        self.assertEqual(result["verdict_lenient"], lenient["verdict"])
        self.assertEqual(result["fail_reasons_lenient"], lenient["fail_reasons"])
        self.assertEqual(result["check_reasons_lenient"], lenient["check_reasons"])


class DistanceTests(unittest.TestCase):
    def test_projection_endpoints_and_degenerate_segments(self):
        self.assertEqual(distance_to_segment((5, 3), [(0, 0), (10, 0)]), 3)
        self.assertEqual(distance_to_segment((13, 4), [(0, 0), (10, 0)]), 5)
        self.assertEqual(distance_to_segment((3, 4), [(0, 0), (0, 0)]), 5)
        self.assertEqual(distance_to_segments((5, 0), [[(0, 0), (10, 0)]]), 0)

    def test_absent_geometry_is_unknown(self):
        self.assertIsNone(distance_to_points((0, 0), []))
        self.assertIsNone(distance_to_segments((0, 0), []))


if __name__ == "__main__":
    unittest.main()
