import contextlib
import csv
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pudo import candidates, pipeline, streetview, mapillary
from pudo.config import ROOT
from pudo.datasets import load_map_data
from pudo.export import write_results
from pudo.geo import to_lonlat
from pudo.vision import PhotoContractError, SCHEMA_VERSION


def feature(geometry_type, coordinates, **properties):
    return {"type": "Feature", "geometry": {"type": geometry_type, "coordinates": coordinates}, "properties": properties}


def collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def write_fixture(root):
    """교차하는 양방향 도로와 시설을 담은 작은 입력. 실제 API/원본 데이터 불필요."""
    data = root / "data"
    (data / "hdmap").mkdir(parents=True)
    web = root / "web"
    web.mkdir()
    (web / "index.html").write_text((ROOT / "web" / "index.html").read_text())

    def way(identifier, nodes, points, highway, lanes):
        return {
            "type": "way", "id": identifier, "nodes": nodes,
            "tags": {"highway": highway, "lanes": str(lanes)},
            "geometry": [{"lon": lon, "lat": lat} for lon, lat in map(lambda xy: to_lonlat(*xy), points)],
        }

    horizontal = [to_lonlat(x, 0) for x in (-120, 0, 120)]
    vertical = [to_lonlat(0, y) for y in (-120, 0, 120)]
    regulation = [to_lonlat(x, -1.75) for x in (-120, -60)]
    layers = {
        "C 횡단보도": collection(feature("Point", to_lonlat(30, 0))),
        "C 소화전": collection(feature("Point", to_lonlat(-80, 0))),
        "C 버스정류소": collection(feature("Point", to_lonlat(70, 0))),
        "C 학교(보호구역 300m 버퍼)": collection(feature("Point", to_lonlat(-250, 0), 이름="테스트 초등학교")),
        "D 규제차선 (T-GIS)": collection(feature("LineString", regulation, code="010")),
    }
    inputs = {
        "osm_roads_gangnam.json": {"elements": [
            way(1, [1, 2, 3], [(-120, 0), (0, 0), (120, 0)], "primary", 2),
            way(2, [4, 2, 5], [(0, -120), (0, 0), (0, 120)], "residential", 1),
        ]},
        "layers_gangnam.json": layers,
        "hdmap/lt_l_a2link.geojson": collection(
            feature("MultiLineString", [horizontal], laneno=2),
            feature("MultiLineString", [vertical], laneno=1),
        ),
        "hdmap/lt_l_b2surfacelinemark.geojson": collection(
            feature("LineString", regulation, kind="515", type="111")
        ),
        "vworld_road_parcels_gangnam.geojson": collection(feature("MultiPolygon", [[[
            to_lonlat(x, y) for x, y in [(-150, -150), (150, -150), (150, 150), (-150, 150), (-150, -150)]
        ]]])),
    }
    for name, value in inputs.items():
        (data / name).write_text(json.dumps(value, ensure_ascii=False))
    return data, web


class WorkflowTests(unittest.TestCase):
    def test_photo_result_file_is_loaded_and_reflected_in_exports(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temp)
            data, web = write_fixture(root)
            with patch.object(candidates, "DATA_DIR", data):
                candidates.main([])
            selected = json.loads((data / "candidates_v1.geojson").read_text())["features"][0]
            photo_path = root / "photo_results.json"
            row = {
                "candidate_id": selected["properties"]["id"], "coordinates": selected["geometry"]["coordinates"],
                "heading": selected["properties"]["heading"], "image_id": "synthetic-image",
                "image_source": "own", "model": "test-fixture-no-model",
                "observations": {name: {"value": value, "confidence": 0.95, "evidence": "검증용 가상 입력"}
                                 for name, value in (("sidewalk", True), ("curb_blocked", True), ("curb_line", "yellow_dashed"))},
            }
            photo_path.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "results": [row]}))
            result = pipeline.run_pipeline(data_dir=data, web_dir=web, sv_dir=root / "sv_out", photo_results=photo_path)
            properties = result["features"][0]["properties"]
            self.assertIn("A2 사진 판독: 연석 접근이 막힘", properties["fail_reasons"])
            self.assertEqual(properties["vision"]["model"], "test-fixture-no-model")
            self.assertEqual(json.loads((data / "pudo_v0.geojson").read_text()), result)
            self.assertNotIn("vision", result["features"][1]["properties"])
            # 잘못된 후보 ID가 들어오면 기존 출력 파일을 덮어쓰기 전에 거절합니다.
            before = (data / "pudo_v0.geojson").read_bytes()
            row["candidate_id"] = "UNKNOWN-CANDIDATE"
            photo_path.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "results": [row]}))
            with self.assertRaises(PhotoContractError):
                pipeline.run_pipeline(data_dir=data, web_dir=web, sv_dir=root / "sv_out", photo_results=photo_path)
            self.assertEqual((data / "pudo_v0.geojson").read_bytes(), before)

    def test_invalid_parcel_geometry_is_repaired_without_changing_source(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temp)
            data, _ = write_fixture(root)
            path = data / "vworld_road_parcels_gangnam.geojson"
            # 교차하는 외곽선: 공급자 데이터에서 발견되는 유효하지 않은 폴리곤을 재현합니다.
            invalid = collection(feature("MultiPolygon", [[[
                to_lonlat(x, y) for x, y in [(-150, -150), (150, 150), (-150, 150), (150, -150), (-150, -150)]
            ]]]))
            path.write_text(json.dumps(invalid))
            original = path.read_bytes()
            with patch.object(candidates, "DATA_DIR", data):
                candidates.main([])
            self.assertEqual(path.read_bytes(), original)
            self.assertGreater(len(json.loads((data / "candidates_v1.geojson").read_text())["features"]), 0)

    def test_candidates_to_pipeline_outputs_and_modes(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temp)
            data, web = write_fixture(root)
            with patch.object(candidates, "DATA_DIR", data):
                candidates.main([])
            generated = json.loads((data / "candidates_v1.geojson").read_text())["features"]
            self.assertGreater(len(generated), 0)
            self.assertEqual({f["properties"]["side"] for f in generated}, {"L", "R"})
            self.assertEqual(len(generated), len({f["properties"]["id"] for f in generated}))
            image_queries = streetview.load_geojson(data / "candidates_v1.geojson")
            self.assertEqual([row["id"] for row in image_queries], [f["properties"]["id"] for f in generated])
            self.assertEqual([row["heading"] for row in image_queries], [f["properties"]["heading"] for f in generated])
            # 새 후보 ID가 과거 ID와 같아도 조회 좌표가 다르면 사진을 연결하지 않습니다.
            sv = root / "sv_out"
            sv.mkdir()
            with (sv / "metadata.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["id", "lon", "lat", "status", "copyright", "pano_id"])
                writer.writeheader()
                for index, candidate in enumerate(generated[:2]):
                    lon, lat = candidate["geometry"]["coordinates"]
                    writer.writerow({"id": candidate["properties"]["id"], "lon": lon + index * 0.001,
                                     "lat": lat, "status": "OK", "copyright": "Google", "pano_id": "test-pano"})
            with patch.multiple(pipeline, DATA_DIR=data, WEB_DIR=web, SV_DIR=root / "sv_out"):
                pipeline.main([])
            result = json.loads((data / "pudo_v0.geojson").read_text())
            self.assertEqual(len(result["features"]), len(generated))
            self.assertEqual(result["features"][0]["properties"]["image_id"], "test-pano")
            self.assertIsNone(result["features"][1]["properties"]["image_id"])
            self.assertTrue(any(f["properties"]["verdict"] != f["properties"]["verdict_lenient"] for f in result["features"]))
            for source, judged in zip(generated, result["features"]):
                self.assertEqual(judged["geometry"], source["geometry"])
                self.assertEqual(judged["properties"]["id"], source["properties"]["id"])
                self.assertEqual(judged["properties"]["heading"], source["properties"]["heading"])
                for check in judged["properties"]["checks"]:
                    self.assertEqual(set(check), {"item", "value", "threshold", "pass", "estimated"})
                    self.assertTrue(check["pass"] is None or isinstance(check["pass"], bool))
                    self.assertIsInstance(check["estimated"], bool)
                    self.assertTrue(check["value"] is None or isinstance(check["value"], str))
            standalone = (web / "pudo_v0_standalone.html").read_text()
            self.assertIn("const PUDO=", standalone)
            self.assertNotIn('<script src="pudo_v0_data.js"></script>', standalone)
            embedded = re.search(r"const PUDO=(.*?);\s*const PUDO_META=", standalone, re.S)
            self.assertEqual(json.loads(embedded.group(1)), result)

    def test_loader_without_optional_hdmap_or_photo_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data, _ = write_fixture(root)
            (data / "hdmap" / "lt_l_a2link.geojson").unlink()
            loaded = load_map_data(data, root / "sv_out")
            self.assertTrue(loaded.intersections)
            self.assertEqual(loaded.hd_link, [])
            self.assertEqual(loaded.hd_reg, [])
            self.assertEqual(loaded.images, {})

    def test_export_without_html_template(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = collection()
            outputs = write_results(result, root / "data", root / "web")
            self.assertEqual({path.name for path in outputs}, {"pudo_v0.geojson", "pudo_v0_data.js"})
            self.assertEqual(json.loads(outputs[0].read_text()), result)

    def test_missing_inputs_are_explained_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(pipeline, "DATA_DIR", Path(temp)):
                with self.assertRaisesRegex(SystemExit, "pudo download"):
                    pipeline.main([])
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_streetview_key_forwarding_and_no_images_by_default(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            point = {"id": "test", "lon": 127.0276, "lat": 37.4979, "heading": 90}
            response = {"status": "OK", "pano_id": "test-pano", "date": "2020-01",
                        "copyright": "Google", "location": {"lng": point["lon"], "lat": point["lat"]}}
            with patch.object(streetview, "api_key", return_value="test-key"), \
                 patch.object(streetview, "demo_points", return_value=[point]), \
                 patch.object(streetview, "metadata", return_value=response) as metadata, \
                 patch.object(streetview, "fetch_image") as fetch:
                streetview.main(["--demo", "--out", temp])
            metadata.assert_called_once_with(point["lon"], point["lat"], key="test-key")
            fetch.assert_not_called()
            with (Path(temp) / "metadata.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["pano_id"], "test-pano")
            self.assertNotIn("date", rows[0])
            self.assertFalse((Path(temp) / "img").exists())

    def test_mapillary_key_forwarding_and_output(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            output = Path(temp) / "nested" / "coverage.geojson"
            with patch.object(mapillary, "api_key", return_value="MLY|test"), \
                 patch.object(mapillary, "collect_coverage", return_value=[]) as collect:
                mapillary.main(["--out", str(output)])
            collect.assert_called_once_with("MLY|test")
            self.assertEqual(json.loads(output.read_text()), collection())


if __name__ == "__main__":
    unittest.main()
