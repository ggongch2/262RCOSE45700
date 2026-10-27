import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from pudo import download


def point(identifier, jibun="1대"):
    return {"type": "Feature", "id": identifier,
            "geometry": {"type": "Point", "coordinates": [127.02, 37.49]},
            "properties": {"jibun": jibun}}


def parcel_page(total, *features):
    return {"response": {"status": "OK", "record": {"total": str(total)},
                         "result": {"featureCollection": download.collection(list(features))}}}


class DownloadTests(unittest.TestCase):
    def test_parcels_collect_all_pages_before_road_filter(self):
        responses = [parcel_page(3, point("a", "1도"), point("b")), parcel_page(3, point("c", "2도"))]
        with patch.object(download, "request_json", side_effect=responses) as fetch:
            result = download.fetch_parcels("test-key", [127, 37, 128, 38])
        self.assertEqual([f["id"] for f in result["features"]], ["a", "c"])
        self.assertEqual(result["pudo_source"], {"all_parcels": 3, "road_parcels": 2, "pages": 2})
        self.assertEqual([call.args[1]["page"] for call in fetch.call_args_list], [1, 2])

    def test_repeated_parcel_page_fails_instead_of_silently_truncating(self):
        response = parcel_page(3, point("a", "1도"))
        with patch.object(download, "request_json", return_value=response):
            with self.assertRaisesRegex(download.DownloadError, "중복"):
                download.fetch_parcels("test-key", [127, 37, 128, 38])

    def test_wfs_splits_truncated_response_and_deduplicates(self):
        def response(total, *ids):
            return {**download.collection([point(id) for id in ids]), "numberMatched": total}
        responses = [response(3, "a"), response(2, "a", "b"), response(1, "b"), response(1, "c"), response(0)]
        with patch.object(download, "request_json", side_effect=responses) as fetch:
            result = download.fetch_hdmap("test-key", "lt_l_a2link", [127, 37, 128, 38])
        self.assertEqual([f["id"] for f in result["features"]], ["a", "b", "c"])
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(fetch.call_args_list[1].args[1]["BBOX"], "127,37,127.5,37.5")

    def test_wfs_incomplete_subdivision_fails(self):
        responses = [{**download.collection([point("a")]), "numberMatched": 2}]
        responses += [{**download.collection([]), "numberMatched": 0}] * 4
        with patch.object(download, "request_json", side_effect=responses):
            with self.assertRaisesRegex(download.DownloadError, "누락"):
                download.fetch_hdmap("test-key", "lt_l_a2link", [127, 37, 128, 38])

    def test_api_http_error_never_includes_secret_url(self):
        secret_url = "https://api.vworld.kr/req/data?key=test-secret"
        error = urllib.error.HTTPError(secret_url, 403, secret_url, {}, io.BytesIO(b"test-secret"))
        with patch.object(download.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(download.DownloadError) as raised:
                download.request_json(download.VWORLD_DATA, {"key": "test-secret"})
        self.assertNotIn("test-secret", str(raised.exception))
        self.assertIn("403", str(raised.exception))

    def test_overpass_partial_response_is_rejected(self):
        response = {"elements": [{"type": "way"}], "remark": "runtime timeout"}
        with patch.object(download, "request_json", return_value=response):
            with self.assertRaises(download.DownloadError):
                download.fetch_roads([127, 37, 128, 38])

    def test_osm_footways_do_not_become_vehicle_intersections(self):
        def way(highway):
            return {"type": "way", "tags": {"highway": highway}, "nodes": [1, 2],
                    "geometry": [{"lon": 127, "lat": 37}, {"lon": 127.01, "lat": 37}]}
        response = {"elements": [way("residential"), way("footway")]}
        with patch.object(download, "request_json", return_value=response):
            result = download.fetch_roads([127, 37, 128, 38])
        self.assertEqual(len(result["elements"]), 1)
        self.assertEqual(result["elements"][0]["tags"]["highway"], "residential")

    def test_preview_restores_json_without_executing_javascript(self):
        layers = {name: download.collection([point(name)]) for name in download.REQUIRED_LAYERS}
        with tempfile.TemporaryDirectory() as temp:
            preview = Path(temp) / "preview.html"
            preview.write_text('<script>const DATA=' + json.dumps(layers) + ';throw "must not execute";</script>')
            self.assertEqual(download.restore_layers(preview), layers)


if __name__ == "__main__":
    unittest.main()
