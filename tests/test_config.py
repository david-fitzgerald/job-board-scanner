import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan.config import ConfigError, load_config, load_profile

OPENROUTER = 'boards_file = "b.json"\n[model]\nbase_url = "https://openrouter.ai/api/v1"\nslug = "x/y"\n'


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def load(self, text, key="sk-or-v1-abc"):
        (self.home / "config.toml").write_text(text)
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": key}):
            return load_config(self.home)

    def test_openrouter_needs_pinned_provider_and_a_clean_key(self):
        pinned = OPENROUTER + 'provider = "parasail"\nquantization = "fp8"\n'
        self.assertEqual(self.load(pinned).api_key, "sk-or-v1-abc")
        for text, key in [(OPENROUTER, "sk-or-v1-abc"), (pinned, ""), (pinned, "sk-or\n-v1"), (pinned, "sk or")]:
            with self.assertRaises(ConfigError, msg=(text, key)) as caught:
                self.load(text, key)
            self.assertNotIn(key.strip() or "\0", str(caught.exception))

    def test_key_never_sent_to_other_endpoints(self):
        config = self.load('boards_file = "b.json"\n[model]\nbase_url = "http://localhost:11434/v1"\nslug = "q"\n')
        self.assertIsNone(config.api_key)
        self.assertNotIn("sk-or", repr(config))

    def test_bad_base_url(self):
        with self.assertRaises(ConfigError):
            self.load('boards_file = "b.json"\n[model]\nbase_url = "localhost:11434"\nslug = "q"\n')

    def test_duplicate_tags_rejected(self):
        (self.home / "profiles").mkdir()
        (self.home / "profiles" / "ae.toml").write_text(
            'titles = ["AE"]\nlocations = ["London"]\neligible_where = "the UK"\ntags = ["founding", "founding"]\n')
        with self.assertRaises(ConfigError):
            load_profile(self.home)


if __name__ == "__main__":
    unittest.main()
