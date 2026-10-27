import tempfile
import unittest
from pathlib import Path

from pudo.config import api_key


class ApiKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_existing_env_format(self):
        (self.root / "env").write_text(
            "google_api='test-google'\nmapillary_api=MLY|test\nvworld_api=test-vworld\n"
        )
        for provider, expected in (
            ("google", "test-google"), ("mapillary", "MLY|test"), ("vworld", "test-vworld")
        ):
            self.assertEqual(api_key(provider, self.root, {}), expected)

    def test_environment_overrides_both_files_and_aliases(self):
        (self.root / ".env").write_text("GOOGLE_MAPS_KEY=file-key\n")
        (self.root / "env").write_text("google_api=legacy-key\n")
        self.assertEqual(api_key("google", self.root, {"google_api": "shell-key"}), "shell-key")
        self.assertEqual(api_key("google", self.root, {"GOOGLE_MAPS_KEY": "shell-key"}), "shell-key")

    def test_dotenv_precedence_and_empty_value_fallback(self):
        (self.root / "env").write_text("google_api=legacy-key\nmapillary_api=MLY|test\n")
        (self.root / ".env").write_text("google_api=dotenv-key\nmapillary_api=\n")
        self.assertEqual(api_key("google", self.root, {}), "dotenv-key")
        self.assertEqual(api_key("mapillary", self.root, {}), "MLY|test")

    def test_bom_comments_and_literal_dollar_signs(self):
        (self.root / "env").write_text(
            '\ufeff# keys\nexport google_api="test-${LITERAL}" # comment\n', encoding="utf-8"
        )
        self.assertEqual(api_key("google", self.root, {}), "test-${LITERAL}")

    def test_missing_keys(self):
        self.assertIsNone(api_key("google", self.root, {}))


if __name__ == "__main__":
    unittest.main()
