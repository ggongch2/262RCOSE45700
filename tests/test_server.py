import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from pudo import candidates
from pudo.measure import load_prepared
from pudo.server import App, RequestError, create_server, parse_conditions
from test_workflow import write_fixture


class ConditionTests(unittest.TestCase):
    def test_overrides_merge_with_defaults_and_bad_input_is_rejected(self):
        thresholds, off = parse_conditions(b'{"thresholds": {"H2 \\ud6a1\\ub2e8\\ubcf4\\ub3c4": 15}, "rules_off": ["H6"]}')
        self.assertEqual(thresholds["H2 횡단보도"], 15)
        self.assertEqual(thresholds["H4 소화전"], 5)
        self.assertEqual(off, ["H6"])
        for body in (b"not json", b'{"thresholds": {"\\uc5c6\\ub294 \\uae30\\uc900": 1}}', b'{"thresholds": {"H4 \\uc18c\\ud654\\uc804": -1}}',
                     b'{"thresholds": {"H4 \\uc18c\\ud654\\uc804": true}}', b'{"rules_off": ["X9"]}', b"[]"):
            with self.subTest(body=body), self.assertRaises(RequestError):
                parse_conditions(body)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        data, web = write_fixture(root)
        with patch.object(candidates, "DATA_DIR", data), contextlib.redirect_stdout(io.StringIO()):
            candidates.main([])
        measurements, layers = load_prepared(data, root / "sv_out")
        cls.app = App(measurements, layers, web)
        cls.server = create_server(cls.app, port=0)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.temp.cleanup()

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def test_only_localhost_is_bound(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_screen_data_and_meta(self):
        status, html = self.request("/")
        self.assertEqual(status, 200); self.assertIn('<script src="pudo_v0_data.js"></script>', html)
        _, script = self.request("/pudo_v0_data.js")
        self.assertIn("const PUDO=", script); self.assertIn("const PUDO_LAYERS=", script)
        meta = json.loads(self.request("/api/meta")[1])
        self.assertEqual(meta["thresholds"]["H4 소화전"], 5); self.assertIn("H6", meta["rules"])

    def test_evaluate_applies_thresholds_and_rules_off(self):
        default = json.loads(self.request("/api/evaluate", {})[1])
        self.assertEqual(default, self.app.evaluate())
        strict = json.loads(self.request("/api/evaluate", {"thresholds": {"H4 소화전": 500}})[1])
        self.assertTrue(all(f["properties"]["verdict"] == "불가" for f in strict["features"]))
        relaxed = json.loads(self.request("/api/evaluate", {"rules_off": ["H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "P1", "A1"]})[1])
        self.assertTrue(all(f["properties"]["verdict"] == "가능" for f in relaxed["features"]))

    def test_bad_conditions_return_400(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/evaluate", {"thresholds": {"없는 기준": 1}})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_export_embeds_current_conditions(self):
        _, html = self.request("/api/export", {"thresholds": {"H4 소화전": 500}, "rules_off": ["A1"]})
        self.assertNotIn('<script src="pudo_v0_data.js"></script>', html)
        self.assertIn('"rules_off": ["A1"]', html)
        self.assertIn('"H4 소화전": 500', html)


if __name__ == "__main__":
    unittest.main()
