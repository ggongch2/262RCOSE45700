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
        self.assertIn("H5 규제선 자료 불일치 (서울시 2021 없음, 정밀도로지도 있음) → 사진 확인", result["properties"]["check_reasons"])

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


class TaxiStandTests(unittest.TestCase):
    """H8: 승차대에서 도로를 따라 택시 1대(6m) 안은 불가, 2대(12m) 안은 확인 필요. 길 건너편은 보지 않음."""

    def verdict(self, stand, **properties):
        feature, data = candidate(), clear_map()
        feature["properties"].update(properties)
        data.taxi_stands = [(stand, "테스트 승차대")]
        return judge(feature, data, rules_off=SIDEWALK_KNOWN)["properties"]

    def test_distance_along_the_road_sets_verdict(self):
        # candidate()는 heading 90: 연석이 동쪽(+x), 도로는 남북(y) 방향
        for along, expected in ((5, "불가"), (9, "확인 필요"), (13, "가능")):
            with self.subTest(along=along):
                result = self.verdict((2, along))
                self.assertEqual(result["verdict"], expected)
                if expected != "가능":
                    reasons = result["fail_reasons"] + result["check_reasons"]
                    self.assertTrue(any(reason.startswith("H8") for reason in reasons))

    def test_threshold_change_and_rule_off(self):
        feature, data = candidate(), clear_map()
        data.taxi_stands = [((2, 5), "테스트 승차대")]
        relaxed = dict(TH, **{"H8 택시승강장": 3, "H8 택시승강장 확인": 4})
        self.assertEqual(judge(feature, data, thresholds=relaxed, rules_off=SIDEWALK_KNOWN)["properties"]["verdict"], "가능")
        self.assertEqual(judge(feature, data, rules_off=("A1", "H8"))["properties"]["verdict"], "가능")

    def test_opposite_side_of_two_way_road_is_ignored(self):
        self.assertEqual(self.verdict((-2, 1))["verdict"], "가능")

    def test_stand_inside_oneway_carriageway_belongs_to_its_curb(self):
        # 중앙분리대로 나뉜 대로의 한쪽 차도: 승차대 좌표가 차도 안에 찍혀도 그 연석의 승차대
        self.assertEqual(self.verdict((-2, 1), oneway=True, curb_offset_m=5)["verdict"], "불가")

    def test_missing_stand_file_requires_confirmation(self):
        feature, data = candidate(), clear_map()
        data.taxi_stands = None
        result = judge(feature, data, rules_off=SIDEWALK_KNOWN)["properties"]
        self.assertEqual(result["verdict"], "확인 필요")
        self.assertIn("H8 택시승차대 자료 없음 (data/taxi_stands.xlsx)", result["check_reasons"])

    def test_reads_seoul_stand_workbook(self):
        import tempfile
        import zipfile
        from pathlib import Path
        from pudo.datasets import _taxi_stands

        def cell(ref, text):
            return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'
        rows = [["□ 서울시 택시승차대 운영목록 (2026년)"],
                ["순번", "ID주소\n(신관리번호)", "타입", "좌표\n(Poi_X)", "좌표\n(Poi_Y)", "위치명"],
                ["1", "23-051021", "쉘터형", str(CENTER_LON), str(CENTER_LAT), "테스트 앞"],
                ["2", "01-011002", "쉘터형", "126.97524", "37.57042", "멀리 있음"],
                ["3", "x", "폴형", "127/02437", "37.5", "좌표 오타"]]
        body = "".join(f'<row r="{i + 1}">' + "".join(cell(f"{chr(65 + j)}{i + 1}", v) for j, v in enumerate(row)) + "</row>"
                       for i, row in enumerate(rows))
        sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                 + body + "</sheetData></worksheet>")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "taxi_stands.xlsx"
            with zipfile.ZipFile(path, "w") as book:
                book.writestr("xl/worksheets/sheet1.xml", sheet)
            stands = _taxi_stands(path)
            self.assertIsNone(_taxi_stands(Path(temp) / "없음.xlsx"))
        self.assertEqual(len(stands), 1)
        self.assertAlmostEqual(stands[0][0][0], 0, places=3)
        self.assertEqual(stands[0][1], "테스트 앞 (쉘터형, 23-051021)")


class Lane2023Tests(unittest.TestCase):
    """H5: 정밀도로지도가 없는 도로의 2021 서울시 금지선을 2023 서울시 차선 목록으로 확인."""

    def judge_line(self, confirmed):
        feature, data = candidate(), clear_map()
        data.lines = {"010": [[(-50, 1), (50, 1)]]}
        data.lines_2023 = None if confirmed is None else {"010": [confirmed]}
        return judge(feature, data, rules_off=SIDEWALK_KNOWN)["properties"]

    def test_confirmed_line_is_fail_without_estimate(self):
        result = self.judge_line(True)
        self.assertEqual(result["verdict"], "불가")
        self.assertIn("H5 주정차금지 구간 (2021 선 + 2023 목록)", result["fail_reasons"])
        self.assertFalse(next(c for c in result["checks"] if c["item"] == "H5 규제선")["estimated"])

    def test_line_missing_in_2023_requires_photo(self):
        result = self.judge_line(False)
        self.assertEqual(result["verdict"], "확인 필요")
        self.assertIn("H5 2021 주정차금지선이 2023 목록에 없음 → 사진 확인", result["check_reasons"])

    def test_without_2023_list_keeps_single_source_fail(self):
        self.assertIn("H5 주정차금지 구간 (2021 서울시 자료, 단일 출처)", self.judge_line(None)["fail_reasons"])

    def test_reads_seoul_lane_workbook_and_matches_lines(self):
        import tempfile
        import zipfile
        from pathlib import Path
        from pyproj import Transformer
        from pudo.datasets import _confirm_2023, _lane_points_2023

        x, y = Transformer.from_crs(4326, 5186, always_xy=True).transform(CENTER_LON, CENTER_LAT)

        def cell(ref, text):
            return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'
        rows = [["", "차선 시설물 현황"],
                ["", "순번", "자치구", "차선종류", "차선중분류", "도로구분", "X좌표", "Y좌표"],
                ["", "1", "강남구", "주정차금지", "황색실선", "구도", str(x), str(y)],
                ["", "2", "강남구", "차선", "백색점선", "구도", str(x), str(y)]]
        body = "".join(f'<row r="{i + 1}">' + "".join(cell(f"{chr(65 + j)}{i + 1}", v) for j, v in enumerate(row) if v) + "</row>"
                       for i, row in enumerate(rows))
        sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + body + "</sheetData></worksheet>"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "lanes.xlsx"
            with zipfile.ZipFile(path, "w") as book:
                book.writestr("xl/worksheets/sheet1.xml", sheet)
            points = _lane_points_2023(path)
            self.assertIsNone(_lane_points_2023(Path(temp) / "없음.xlsx"))
        self.assertEqual(list(points), ["010"])
        self.assertEqual(len(points["010"]), 1)
        lines = {"010": [[(-20, 1), (20, 1)], [(-20, 30), (20, 30)]]}
        self.assertEqual(_confirm_2023(lines, points), {"010": [True, False]})
        self.assertIsNone(_confirm_2023(lines, None))


if __name__ == "__main__":
    unittest.main()
