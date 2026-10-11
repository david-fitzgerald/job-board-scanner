import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import fetch
from jobscan.config import ConfigError
from tests.fakes import FakeServer, ashby_job


class AshbyUrlTest(unittest.TestCase):
    def test_human_board_url_maps_to_api(self):
        self.assertEqual(fetch.ashby_api_url("https://jobs.ashbyhq.com/acme"),
                         "https://api.ashbyhq.com/posting-api/job-board/acme")

    def test_api_url_used_as_is_on_any_host(self):
        self.assertEqual(fetch.ashby_api_url("http://127.0.0.1:9/posting-api/job-board/acme?x=1"),
                         "http://127.0.0.1:9/posting-api/job-board/acme")

    def test_other_ats_is_none(self):
        self.assertIsNone(fetch.ashby_api_url("https://boards.greenhouse.io/acme"))

    def test_load_boards_dedupes_and_counts_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "boards.json"
            path.write_text(json.dumps({"companies": [
                {"company": "Acme", "board_url": "https://jobs.ashbyhq.com/acme"},
                {"company": "Acme again", "board_url": "https://api.ashbyhq.com/posting-api/job-board/acme"},
                {"company": "Other", "board_url": "https://boards.greenhouse.io/other"},
            ]}))
            boards, skipped = fetch.load_boards(path)
        self.assertEqual([(b.company, b.slug) for b in boards], [("Acme", "acme")])
        self.assertEqual(skipped, 1)

    def test_load_boards_rejects_bad_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "boards.json"
            path.write_text("{}")
            with self.assertRaises(ConfigError):
                fetch.load_boards(path)


class FetchBoardTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        patcher = mock.patch.object(fetch, "RETRY_PAUSES", (0, 0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def board(self, slug="acme"):
        return fetch.Board(company="Acme", slug=slug, api_url=f"{self.server.url}/posting-api/job-board/{slug}")

    def serve(self, slug, status, body):
        self.server.routes[f"/posting-api/job-board/{slug}"] = (status, body)

    def test_parses_listed_jobs_with_all_locations(self):
        self.serve("acme", 200, {"jobs": [
            ashby_job("1", "Account Executive", "London", secondaryLocations=[{"location": "Remote - UK"}]),
            ashby_job("2", "Hidden", isListed=False),
        ]})
        (posting,) = fetch.fetch_board(self.board())
        self.assertEqual(posting.key, "ashby:acme:1")
        self.assertEqual(posting.location, "London / Remote - UK")

    def test_html_only_description_keeps_block_breaks_and_entities(self):
        job = ashby_job("1", "AE", description="")
        job["descriptionHtml"] = "<p>Sell&nbsp;to <b>CISOs</b> &amp; CTOs</p><ul><li>Close</li><li>Grow</li></ul>"
        self.serve("acme", 200, {"jobs": [job]})
        (posting,) = fetch.fetch_board(self.board())
        self.assertEqual(posting.description, "Sell to CISOs & CTOs\nClose\nGrow")

    def test_success_rules(self):
        cases = {
            "malformed": (200, "{not json"),
            "nolist": (200, {"jobs": {"1": "x"}}),
            "missing": (200, {"apiVersion": "1"}),
            "notobject": (200, [1, 2]),
            "error": (500, {"error": "boom"}),
        }
        for slug, (status, body) in cases.items():
            self.serve(slug, status, body)
        self.serve("empty", 200, {"jobs": []})
        fetched, failed = fetch.fetch_all([self.board(s) for s in [*cases, "empty"]])
        self.assertEqual([r.board.slug for r in fetched], ["empty"])
        self.assertEqual(fetched[0].postings, ())
        self.assertEqual({f.board.slug: f.reason for f in failed}, {
            "malformed": "malformed JSON", "nolist": "response has no `jobs` list",
            "missing": "response has no `jobs` list", "notobject": "response has no `jobs` list", "error": "HTTP 500",
        })

    def test_retries_server_errors_then_gives_up(self):
        self.serve("acme", 503, {"error": "busy"})
        _, (failure,) = fetch.fetch_all([self.board()])
        self.assertEqual(failure.reason, "HTTP 503")
        self.assertEqual(len(self.server.requests), 3)

    def test_unreachable_board_is_a_failure(self):
        board = fetch.Board(company="Gone", slug="gone", api_url="http://127.0.0.1:9/posting-api/job-board/gone")
        _, (failure,) = fetch.fetch_all([board])
        self.assertTrue(failure.reason.startswith("network"))


if __name__ == "__main__":
    unittest.main()
