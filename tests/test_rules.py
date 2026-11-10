import copy
import unittest

from pudo.config import CENTER_LAT, CENTER_LON, TH
from pudo.datasets import MapData
from pudo.geo import distance_to_points, distance_to_segment, distance_to_segments
from pudo.measure import measure
from pudo.rules import evaluate, evaluate_all
from support import judge
from shapely.geometry import LineString, box


def candidate():
    return {
        "type": "Feature", "geometry": {"type": "Point", "coordinates": [CENTER_LON, CENTER_LAT]},
        "properties": {"id": "P0000", "road_type": "primary", "lanes": 2, "lanes_source": "hdmap",
                       "oneway": False, "heading": 90, "road_width_m": 10},
    }


def clear_map():
    return MapData(intersections=[(100, 100)], intersection_area=box(90, 90, 110, 110), crosswalks=[(100, 100)],
                   hydrants=[(100, 100)], buses=[(100, 100)], schools=[((400, 0), "학교")])


SIDEWALK_KNOWN = ("A1",)   # 보도 자료가 아직 없어 '가능'을 보려면 A1 규칙을 끕니다


class RuleTests(unittest.TestCase):
    def test_child_zone_is_hard_fail_and_inputs_are_not_mutated(self):
        feature, data = candidate(), clear_map()
        data.schools = [((100, 0), "학교")]
        original_feature, original_data, original_thresholds = copy.deepcopy((feature, data, TH))
        first, last = judge(feature, data), judge(feature, data)
        self.assertEqual(first, last)
        self.assertEqual(first["properties"]["verdict"], "불가")
        self.assertTrue(any(reason.startswith("H6") for reason in first["properties"]["fail_reasons"]))
        self.assertEqual((feature, data, TH), (original_feature, original_data, original_thresholds))

    def test_hydrant_rule_fails(self):
        data = clear_map()
        data.hydrants = [(1, 0)]
        data.intersection_area = box(3, -10, 20, 10)
        result = judge(candidate(), data)["properties"]
        self.assertEqual(result["verdict"], "불가")
        self.assertTrue(any(reason.startswith("H4") for reason in result["fail_reasons"]))

    def test_threshold_and_coordinate_error_boundaries(self):
        for distance, expected in ((5, "불가"), (6, "확인 필요"), (12, "확인 필요"), (13, "가능")):
            with self.subTest(distance=distance):
                data = clear_map()
                data.hydrants = [(distance, 0)]
                self.assertEqual(judge(candidate(), data, rules_off=SIDEWALK_KNOWN)["properties"]["verdict"], expected)

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

    def test_intersection_uses_area_edge_per_road_traffic_act(self):
        # 제32조 1호 교차로 안, 2호 가장자리 5m 이내는 불가. 완화 모드에서도 풀지 않음
        for edge, expected in ((-5, "불가"), (5, "불가"), (6, "확인 필요"), (13, "가능")):
            with self.subTest(edge=edge):
                data = clear_map()
                data.intersection_area = box(edge, -10, edge + 20, 10) if edge > 0 else box(-5, -10, 15, 10)
                self.assertEqual(judge(candidate(), data, rules_off=SIDEWALK_KNOWN)["properties"]["verdict"], expected)
        data = clear_map(); data.intersection_area = box(-5, -10, 15, 10)
        self.assertIn("H1 교차로 안 (도로교통법 제32조 1호)", judge(candidate(), data)["properties"]["fail_reasons"])

    def test_missing_intersection_area_requires_confirmation(self):
        data = clear_map(); data.intersection_area = None
        self.assertIn("H1 교차로 데이터 없음", judge(candidate(), data)["properties"]["check_reasons"])

    def test_evaluate_all_matches_single_evaluation(self):
        feature, data = candidate(), clear_map()
        data.schools = [((100, 0), "학교")]
        m = measure(feature, data)
        self.assertEqual(evaluate_all([m])["features"][0], evaluate(m))

    def test_rules_off_removes_only_that_rules_reasons(self):
        feature, data = candidate(), clear_map()
        data.schools = [((100, 0), "학교")]
        result = judge(feature, data, rules_off=("H6", "A1"))["properties"]
        self.assertEqual(result["verdict"], "가능")
        self.assertEqual(result["rules_off"], ["A1", "H6"])
        self.assertFalse(next(c for c in result["checks"] if c["item"] == "H6 어린이보호구역")["pass"])   # 근거는 남김


class MeasurementTests(unittest.TestCase):
    def test_measurement_does_not_depend_on_thresholds(self):
        feature, data = candidate(), clear_map()
        m = measure(feature, data)
        self.assertEqual(round(m["hydrant"], 6), round(distance_to_points((0, 0), [(100, 100)]), 6))
        strict = {**TH, "H4 소화전": 200}
        self.assertEqual(judge(feature, data, thresholds=strict)["properties"]["verdict"], "불가")
        self.assertEqual(measure(feature, data), m)

    def test_unknown_lanes_and_sidewalk_stay_unknown(self):
        feature = candidate()
        feature["properties"].update(road_type="tertiary", lanes=2, lanes_source="default")
        m = measure(feature, clear_map())
        self.assertIsNone(m["lanes"]["value"]); self.assertIsNone(m["sidewalk"]["value"])
        self.assertTrue(m["position"]["estimated"])
        result = judge(feature, clear_map())["properties"]
        self.assertIn("P1 차로 수 모름 (정밀도로지도·OSM에 없음)", result["check_reasons"])
        self.assertIn("A1 보도 유무 모름 (사진 필요)", result["check_reasons"])
        self.assertEqual(result["verdict"], "확인 필요")


class DesignatedFacilityAndWidthTests(unittest.TestCase):
    def test_tgis_zone_fails_but_facility_only_zone_needs_check(self):
        feature = candidate()
        data = clear_map(); data.child_zone = LineString([(-50, 0), (50, 0)])
        data.child_zone_evidence = [((10, 0), "노면 '어린이'")]
        self.assertEqual(judge(feature, data)["properties"]["verdict"], "불가")
        data = clear_map(); data.child_zone_facility = LineString([(-50, 0), (50, 0)])
        data.child_zone_evidence = [((0, 40), "지정 시설 어린이집 (어린이집)")]
        result = judge(feature, data)["properties"]
        self.assertEqual(result["verdict"], "확인 필요")
        self.assertTrue(any(r.startswith("H6 어린이보호구역 지정 시설 (지정 시설 어린이집") for r in result["check_reasons"]))
        check = next(c for c in result["checks"] if c["item"] == "H6 어린이보호구역")
        self.assertIsNone(check["pass"])

    def one_lane(self, width):
        feature = candidate()
        feature["properties"].update(lanes=1, road_type="residential", road_width_m=width)
        return feature

    def test_planned_grade_fills_missing_width_and_flags_contradiction(self):
        # candidate()는 heading 90(연석이 동쪽) → 도로는 남북 방향
        data = clear_map(); data.planned_roads = [(box(-10, -100, 10, 100), "중로3류", 12)]
        missing = judge(self.one_lane(None), data)["properties"]
        p1 = next(c for c in missing["checks"] if c["item"] == "P1 통과 폭")
        self.assertTrue(p1["pass"]); self.assertIn("중로3류", p1["value"])
        self.assertEqual(missing["map"]["planned_road"], "중로3류")
        narrow = judge(self.one_lane(4.0), data)["properties"]
        self.assertEqual(narrow["fail_reasons"], [])
        self.assertTrue(any(r.startswith("P1 폭 자료 불일치") for r in narrow["check_reasons"]))
        data.planned_roads = [(box(-10, -100, 10, 100), "소로3류", 0)]   # 8m 미만: 하한 없음
        self.assertTrue(any(r.startswith("P1 통과 폭 부족") for r in judge(self.one_lane(4.0), data)["properties"]["fail_reasons"]))

    def test_planned_grade_ignored_when_road_only_crosses_the_area(self):
        # 교차로에서 가로지르는 대로 면에 중심점만 걸린 경우 (앞뒤 10m가 면 밖)
        data = clear_map(); data.planned_roads = [(box(-100, -5, 100, 5), "광로2류", 50)]
        result = judge(self.one_lane(None), data)["properties"]
        self.assertIsNone(result["map"]["planned_road"])
        self.assertIn("P1 도로 폭 없음 (단지 내 통로일 수 있음)", result["check_reasons"])


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
