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
        pinned = '\nprovider = "p"\nquantization = "q"\n'
        for url in ["localhost:11434", "https://[::1", "http://localhost:99999/v1", "http://openrouter.ai/api/v1",
                    "http://local host/v1", "http://model.invalid/v1#frag", "http://model.invalid/v1?x=1"]:
            with self.assertRaises(ConfigError, msg=url):
                self.load(f'boards_file = "b.json"\n[model]\nbase_url = "{url}"\nslug = "q"' + pinned)

    def test_unreadable_toml_is_a_config_error(self):
        (self.home / "config.toml").write_bytes(b'boards_file = "\xff"\n')
        with self.assertRaises(ConfigError):
            load_config(self.home)

    def test_empty_exclusions_allowed(self):
        (self.home / "profiles").mkdir()
        (self.home / "profiles" / "ae.toml").write_text('titles = ["AE"]\nexclude_titles = []\nlocations = ["London"]\n'
                                                        'eligible_where = "the UK"\ntags = ["founding"]\n')
        self.assertEqual(load_profile(self.home).exclude_titles, ())

    def test_duplicate_tags_rejected(self):
        (self.home / "profiles").mkdir()
        (self.home / "profiles" / "ae.toml").write_text(
            'titles = ["AE"]\nlocations = ["London"]\neligible_where = "the UK"\ntags = ["founding", "founding"]\n')
        with self.assertRaises(ConfigError):
            load_profile(self.home)


if __name__ == "__main__":
    unittest.main()
