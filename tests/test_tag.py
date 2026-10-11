import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import store, tag
from jobscan.config import ConfigError, ModelConfig
from jobscan.fetch import Posting
from tests.fakes import FakeServer, model_reply

ALL = ("founding", "location_eligible", "segment", "language_required", "tech_company")
DEFS = tag.definitions(ALL, "the UK")
POSTING = Posting(key="ashby:acme:1", board="acme", company="Acme", title="Founding Account Executive",
                  location="London", url="https://jobs.ashbyhq.com/acme/1",
                  description="You’ll be our first sales hire —  selling to mid-market teams.")


def reply(**cells):
    base = {"founding": {"value": "no", "quote": None}, "location_eligible": {"value": "unknown", "quote": None},
            "segment": {"value": [], "quote": None}, "language_required": {"value": "no", "quote": None},
            "tech_company": {"value": "unknown", "quote": None}}
    return json.dumps({**base, **cells})


class QuoteTest(unittest.TestCase):
    def test_exact_and_normalised_quotes_verify(self):
        self.assertTrue(tag.verify_quote("Founding Account Executive", POSTING))
        self.assertTrue(tag.verify_quote("You'll be our first sales hire - selling", POSTING))
        self.assertTrue(tag.verify_quote("our first sales hire", POSTING))

    def test_fabricated_and_blank_quotes_fail(self):
        self.assertFalse(tag.verify_quote("you will be the first seller", POSTING))
        self.assertFalse(tag.verify_quote("   ", POSTING))


class ParseReplyTest(unittest.TestCase):
    def parse(self, content):
        return tag.parse_reply(content, DEFS, POSTING)

    def test_verified_affirmative(self):
        r = self.parse(reply(founding={"value": "yes", "quote": "our first sales hire"}))["founding"]
        self.assertEqual((r.value, r.verified, r.warn), ("yes", True, False))

    def test_fabricated_quote_warns_but_keeps_value(self):
        r = self.parse(reply(founding={"value": "yes", "quote": "the first seller ever"}))["founding"]
        self.assertEqual((r.value, r.verified, r.warn), ("yes", False, True))

    def test_expected_quote_missing_or_blank_warns(self):
        for quote in (None, "  "):
            r = self.parse(reply(location_eligible={"value": "no", "quote": quote}))["location_eligible"]
            self.assertEqual((r.quote, r.warn), (None, True))

    def test_defaults_unknowns_and_empty_lists_need_no_quote(self):
        results = self.parse(reply())
        self.assertFalse(any(r.warn for r in results.values()))
        self.assertEqual(results["segment"].value, ())

    def test_multi_value_canonical_order_without_duplicates(self):
        r = self.parse(reply(segment={"value": ["enterprise", "mid_market", "enterprise"],
                                      "quote": "mid-market teams"}))["segment"]
        self.assertEqual(r.value, ("enterprise", "mid_market"))

    def test_free_text_default_is_normalised(self):
        self.assertEqual(self.parse(reply(language_required={"value": "No", "quote": None}))
                         ["language_required"].value, "no")
        r = self.parse(reply(language_required={"value": "German", "quote": None}))["language_required"]
        self.assertEqual((r.value, r.warn), ("German", True))

    def test_invalid_replies_raise(self):
        for content in ["not json", "[]", json.dumps({"founding": {"value": "yes", "quote": None}}),
                        reply(founding={"value": "maybe", "quote": None}),
                        reply(segment={"value": "enterprise", "quote": None}),
                        reply(segment={"value": ["huge"], "quote": None}),
                        reply(language_required={"value": " ", "quote": None}),
                        reply(founding={"value": "yes", "quote": 3})]:
            with self.assertRaises(tag.ReplyError, msg=content):
                self.parse(content)


class DefinitionsTest(unittest.TestCase):
    def test_eligible_where_substituted_and_hashed(self):
        (uk,) = tag.definitions(("location_eligible",), "the UK")
        (de,) = tag.definitions(("location_eligible",), "Germany")
        self.assertIn("work from the UK", uk.definition)
        self.assertNotEqual(uk.hash, de.hash)

    def test_unknown_tag_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            tag.definitions(("founding", "climate"), "the UK")

    def test_schema_types_per_tag(self):
        props = tag.schema(DEFS)["properties"]
        self.assertEqual(props["founding"]["properties"]["value"], {"type": "string", "enum": ["yes", "no"]})
        self.assertEqual(props["language_required"]["properties"]["value"], {"type": "string"})
        self.assertEqual(props["segment"]["properties"]["value"]["type"], "array")


class CacheIdentityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open_db(Path(tmp.name))
        self.addCleanup(self.conn.close)
        self.stored = store.StoredPosting(posting=POSTING, input_hash=store.input_hash(POSTING), first_seen="t")
        self.model = ModelConfig("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash", "parasail", "fp8")
        store.save_results(self.conn, self.stored, DEFS, self.model.id, tag.parse_reply(reply(), DEFS, POSTING))

    def test_hit_on_same_identity(self):
        self.assertEqual(len(store.cached_results(self.conn, self.stored, DEFS, self.model.id)), len(DEFS))

    def test_miss_on_location_change_and_provider_change(self):
        moved = Posting(**{**vars(POSTING), "location": "Remote - UK"})
        moved_stored = store.StoredPosting(posting=moved, input_hash=store.input_hash(moved), first_seen="t")
        self.assertEqual(store.cached_results(self.conn, moved_stored, DEFS, self.model.id), {})
        other = ModelConfig(self.model.base_url, self.model.slug, "deepinfra", "fp8")
        self.assertEqual(store.cached_results(self.conn, self.stored, DEFS, other.id), {})


class TagPostingTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        self.model = ModelConfig(self.server.url, "test/model", "parasail", "fp8")
        for name in ("PAUSE", "RATE_LIMIT_PAUSE"):
            patcher = mock.patch.object(tag, name, 0)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_billed_invalid_reply_then_valid_retry_counts_both_costs(self):
        replies = iter([(200, {"choices": [{"message": {"content": "garbage"}}], "usage": {"cost": 0.001}}),
                        model_reply(json.loads(reply()), cost=0.002)])
        self.server.routes["/chat/completions"] = (200, lambda payload: next(replies))
        attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertIsNotNone(attempt.results)
        self.assertAlmostEqual(attempt.cost, 0.003)

    def test_request_pins_provider_and_strict_schema(self):
        self.server.routes["/chat/completions"] = (200, lambda payload: model_reply(json.loads(reply())))
        tag.tag_posting(POSTING, DEFS, self.model, None)
        (_, _, body), = self.server.requests
        self.assertEqual(body["provider"], {"only": ["parasail"], "allow_fallbacks": False,
                                            "require_parameters": True, "quantizations": ["fp8"]})
        self.assertEqual(body["temperature"], 0)
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertNotIn("reasoning", body)

    def test_unavailable_model_fails_after_three_tries(self):
        self.server.routes["/chat/completions"] = (503, {"error": "down"})
        attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertEqual((attempt.results, attempt.error, attempt.cost), (None, "HTTP 503", 0.0))
        self.assertEqual(len(self.server.requests), 3)


if __name__ == "__main__":
    unittest.main()
