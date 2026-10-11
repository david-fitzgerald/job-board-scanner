import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import ashby, fetch
from jobscan.config import ConfigError
from jobscan.records import Board
from tests.fakes import FakeServer, ashby_job


class AshbyBoardTest(unittest.TestCase):
    def test_human_board_url_maps_to_api(self):
        board = ashby.board("Acme", "https://jobs.ashbyhq.com/acme")
        self.assertEqual((board.id, board.api_url), ("ashby:acme", "https://api.ashbyhq.com/posting-api/job-board/acme"))

    def test_api_url_used_as_is_on_any_host(self):
        self.assertEqual(ashby.board("Acme", "http://127.0.0.1:9/posting-api/job-board/acme?x=1").api_url,
                         "http://127.0.0.1:9/posting-api/job-board/acme")

    def test_other_ats_and_malformed_urls_are_none(self):
        self.assertIsNone(ashby.board("Acme", "https://boards.greenhouse.io/acme"))
        self.assertIsNone(ashby.board("Acme", "https://[::1/posting-api/job-board/acme"))
        self.assertIsNone(ashby.board("Acme", "https://jobs.ashbyhq.com:99999/acme"))
        for url in ["/posting-api/job-board/acme", "http:///posting-api/job-board/acme",
                    "file://host/posting-api/job-board/acme"]:
            self.assertIsNone(ashby.board("Acme", url), url)

    def test_load_boards_dedupes_and_counts_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "boards.json"
            path.write_text(json.dumps({"companies": [
                {"company": "Acme", "board_url": "https://jobs.ashbyhq.com/acme"},
                {"company": "Acme again", "board_url": "https://api.ashbyhq.com/posting-api/job-board/acme"},
                {"company": "Other", "board_url": "https://boards.greenhouse.io/other"},
                {"company": None, "board_url": 7},
            ]}))
            boards, skipped = fetch.load_boards(path)
        self.assertEqual([(b.company, b.id) for b in boards], [("Acme", "ashby:acme")])
        self.assertEqual(skipped, 2)

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
        return Board(ats="ashby", slug=slug, company="Acme", api_url=f"{self.server.url}/posting-api/job-board/{slug}")

    def serve(self, slug, status, body):
        self.server.routes[f"/posting-api/job-board/{slug}"] = (status, body)

    def fetch_one(self, slug="acme"):
        fetched, failed = fetch.fetch_all([self.board(slug)])
        return fetched[0].postings if fetched else failed[0].reason

    def test_parses_listed_jobs_with_all_locations(self):
        self.serve("acme", 200, {"jobs": [
            ashby_job("1", "Account Executive", "London", secondaryLocations=[{"location": "Remote - UK"}]),
            {**ashby_job("2", "Hidden", isListed=False), "title": None},
        ]})
        (posting,) = self.fetch_one()
        self.assertEqual((posting.key, posting.ats, posting.board), ("ashby:acme:1", "ashby", "ashby:acme"))
        self.assertEqual(posting.location, "London / Remote - UK")

    def test_html_only_description_keeps_block_breaks_and_entities(self):
        job = ashby_job("1", "AE", description="")
        job["descriptionHtml"] = "<p>Sell&nbsp;to <b>CISOs</b> &amp; CTOs</p><ul><li>Close</li><li>Grow</li></ul>"
        self.serve("acme", 200, {"jobs": [job]})
        (posting,) = self.fetch_one()
        self.assertEqual(posting.description, "Sell to CISOs & CTOs\nClose\nGrow")

    def test_success_rules(self):
        cases = {
            "malformed": (200, "{not json"),
            "nolist": (200, {"jobs": {"1": "x"}}),
            "missing": (200, {"apiVersion": "1"}),
            "notobject": (200, [1, 2]),
            "error": (500, {"error": "boom"}),
            "accepted": (203, {"jobs": []}),
        }
        for slug, (status, body) in cases.items():
            self.serve(slug, status, body)
        self.serve("empty", 200, {"jobs": []})
        fetched, failed = fetch.fetch_all([self.board(s) for s in [*cases, "empty"]])
        self.assertEqual([(r.board.slug, r.postings) for r in fetched], [("empty", ())])
        self.assertEqual({f.board.slug: f.reason for f in failed}, {
            "malformed": "malformed JSON", "nolist": "response has no `jobs` list",
            "missing": "response has no `jobs` list", "notobject": "response has no `jobs` list", "error": "HTTP 500",
            "accepted": "HTTP 203",
        })

    def test_one_malformed_listed_job_fails_the_whole_board(self):
        for bad in [{"title": None}, {"secondaryLocations": 1}, {"secondaryLocations": [1]},
                    {"secondaryLocations": {}}, {"secondaryLocations": ""}, {"secondaryLocations": False},
                    {"isListed": "false"}, {"isListed": 0},
                    {"descriptionHtml": {"x": 1}, "descriptionPlain": None}, {"location": 7}, {"id": 12}]:
            self.serve("acme", 200, {"jobs": [ashby_job("1", "AE"), {**ashby_job("2", "AE"), **bad}]})
            reason = self.fetch_one()
            self.assertIsInstance(reason, str, bad)
            self.assertTrue(reason.startswith("job 1:"), reason)

    def test_duplicate_listed_ids_fail_the_board(self):
        self.serve("acme", 200, {"jobs": [ashby_job("1", "AE"), ashby_job("1", "Other AE")]})
        self.assertEqual(self.fetch_one(), "duplicate listed job ids")

    def test_retries_server_errors_then_gives_up(self):
        self.serve("acme", 503, {"error": "busy"})
        self.assertEqual(self.fetch_one(), "HTTP 503")
        self.assertEqual(len(self.server.requests), 3)

    def test_unreachable_board_is_a_failure(self):
        board = Board(ats="ashby", slug="gone", company="Gone", api_url="http://127.0.0.1:9/posting-api/job-board/gone")
        _, (failure,) = fetch.fetch_all([board])
        self.assertTrue(failure.reason.startswith("network"))


if __name__ == "__main__":
    unittest.main()
