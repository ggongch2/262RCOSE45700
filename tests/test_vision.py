import copy
import tempfile
import unittest
from pathlib import Path

from pudo.datasets import MapData
from shapely.geometry import box
from pudo.measure import measure
from pudo.rules import evaluate_all
from support import judge as _judge
from pudo.config import CENTER_LAT, CENTER_LON
from pudo.vision import PhotoContractError, SCHEMA_VERSION, load_assessments, parse_assessments


def photo_row(**values):
    observations = {
        "sidewalk": {"value": True, "confidence": 0.95, "evidence": "테스트 입력: 보도가 보임"},
        "curb_blocked": {"value": False, "confidence": 0.95, "evidence": "테스트 입력: 통로가 열림"},
        "curb_line": {"value": "yellow_dashed", "confidence": 0.95, "evidence": "테스트 입력: 점선이 보임"},
    }
    for name, value in values.items():
        observations[name] = {"value": value, "confidence": 0.95, "evidence": "인위적으로 만든 검증용 값"}
    return {"candidate_id": "TEST", "coordinates": [CENTER_LON, CENTER_LAT], "heading": 90,
            "image_id": "synthetic-image", "image_source": "own", "model": "test-fixture-no-model",
            "observations": observations}


def fixture(row=None, road_type="primary"):
    feature = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [CENTER_LON, CENTER_LAT]},
               "properties": {"id": "TEST", "road_type": road_type, "oneway": False, "lanes": 2,
                              "lanes_source": "map1000", "heading": 90}}
    data = MapData(intersections=[(100, 100)], intersection_area=box(90, 90, 110, 110), crosswalks=[(100, 100)], hydrants=[(100, 100)],
                   buses=[(100, 100)], schools=[((400, 0), "학교")])
    data.photos = parse_assessments({"schema_version": SCHEMA_VERSION, "results": [row]}) if row is not None else {}
    return feature, data


def judge(feature, data, **options):
    return _judge(feature, data, photos=data.photos, **options)


class PhotoContractTests(unittest.TestCase):
    def test_rejects_free_text_instead_of_guessing_model_response(self):
        for payload in ("보도가 있어요", {"text": "sidewalk exists"}, {"schema_version": "unknown", "results": []}):
            with self.subTest(payload=payload), self.assertRaises(PhotoContractError):
                parse_assessments(payload)

    def test_boolean_types_and_finite_confidence_are_required(self):
        for key, value in (("value", "true"), ("confidence", True), ("confidence", float("nan")), ("confidence", 2)):
            row = photo_row()
            row["observations"]["sidewalk"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(PhotoContractError):
                fixture(row)

    def test_unknown_values_and_unknown_confidence_are_explicitly_allowed(self):
        row = photo_row(sidewalk=None)
        row["observations"]["sidewalk"]["confidence"] = None
        feature, data = fixture(row)
        result = judge(feature, data)["properties"]
        self.assertEqual(result["verdict"], "확인 필요")
        self.assertNotIn("sidewalk", result["vision"]["used_fields"])

    def test_duplicate_ids_and_unknown_fields_are_rejected(self):
        row = photo_row()
        with self.assertRaises(PhotoContractError):
            parse_assessments({"schema_version": SCHEMA_VERSION, "results": [row, row]})
        row["observations"]["distance_to_hydrant"] = {"value": 20, "confidence": 1, "evidence": "추정"}
        with self.assertRaises(PhotoContractError):
            fixture(row)

    def test_invalid_json_has_a_contract_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "photo.json"
            path.write_text("this is not JSON")
            with self.assertRaises(PhotoContractError):
                load_assessments(path)


class PhotoRuleTests(unittest.TestCase):
    def test_no_photo_input_leaves_sidewalk_unknown(self):
        feature, data = fixture()
        result = judge(feature, data)["properties"]
        self.assertEqual(result["verdict"], "확인 필요")
        self.assertEqual(result["check_reasons"], ["A1 보도 유무 모름 (사진 필요)"])
        self.assertNotIn("vision", result)

    def test_sidewalk_observation_replaces_unknown(self):
        feature, data = fixture(road_type="residential")
        self.assertEqual(judge(feature, data)["properties"]["verdict"], "확인 필요")
        _, data = fixture(photo_row(), road_type="residential")
        result = judge(feature, data)["properties"]
        self.assertEqual(result["verdict"], "가능")
        sidewalk = next(check for check in result["checks"] if check["item"] == "A1 보도")
        self.assertTrue(sidewalk["pass"])
        self.assertFalse(sidewalk["estimated"])
        self.assertEqual(result["vision"]["model"], "test-fixture-no-model")

    def test_negative_observations_add_failure_evidence(self):
        for name, value, prefix in (("sidewalk", False, "A1"), ("curb_blocked", True, "A2"), ("curb_line", "yellow_solid", "H5")):
            feature, data = fixture(photo_row(**{name: value}))
            with self.subTest(name=name):
                result = judge(feature, data)["properties"]
                self.assertEqual(result["verdict"], "불가")
                self.assertTrue(any(reason.startswith(prefix) for reason in result["fail_reasons"]))

    def test_low_confidence_cannot_promote_unknown_sidewalk(self):
        row = photo_row()
        row["observations"]["sidewalk"]["confidence"] = 0.2
        feature, data = fixture(row, road_type="residential")
        result = judge(feature, data)["properties"]
        self.assertEqual(result["verdict"], "확인 필요")
        self.assertNotIn("sidewalk", result["vision"]["used_fields"])

    def test_high_confidence_curb_line_resolves_map_disagreement(self):
        feature, data = fixture(photo_row())
        data.hd_link = [[(-50, 2), (50, 2)]]
        data.lines = {"010": [[(-50, 1), (50, 1)]]}
        result = judge(feature, data)["properties"]
        self.assertEqual(result["verdict"], "가능")
        self.assertFalse(any(reason.startswith("H5 규제선 자료 불일치") for reason in result["check_reasons"]))

    def test_photo_does_not_erase_existing_map_failure(self):
        feature, data = fixture(photo_row())
        data.lines = {"010": [[(-50, 1), (50, 1)]]}
        result = judge(feature, data)["properties"]
        self.assertEqual(result["verdict"], "불가")
        self.assertTrue(any(reason.startswith("H5 주정차금지") for reason in result["fail_reasons"]))
        self.assertIn("H5 지도 불가 근거와 사진 판독 불일치", result["check_reasons"])

    def test_wrong_location_heading_or_image_cannot_be_applied(self):
        for change in ({"coordinates": [CENTER_LON + 0.001, CENTER_LAT]}, {"heading": 180}, {"image_source": "google"}):
            row = {**photo_row(), **change}
            feature, data = fixture(row)
            data.images = {"TEST": {"pano_id": "different-image", "lon": str(CENTER_LON), "lat": str(CENTER_LAT)}}
            with self.subTest(change=change):
                result = judge(feature, data)["properties"]
                self.assertEqual(result["verdict"], "확인 필요")
                self.assertEqual(result["vision"]["status"], "rejected")
                self.assertEqual(result["vision"]["used_fields"], [])

    def test_construction_or_occupation_requires_review(self):
        for field in ("construction", "occupied"):
            feature, data = fixture(photo_row(**{field: True}))
            self.assertEqual(judge(feature, data)["properties"]["verdict"], "확인 필요")

    def test_evaluation_does_not_mutate_measurement_or_observation_inputs(self):
        feature, data = fixture(photo_row())
        m = measure(feature, data)
        before = copy.deepcopy((m, data.photos))
        result = evaluate_all([m], photo_assessments=data.photos)
        self.assertEqual(result["features"][0]["properties"]["vision"]["status"], "applied")
        self.assertEqual((m, data.photos), before)


if __name__ == "__main__":
    unittest.main()
