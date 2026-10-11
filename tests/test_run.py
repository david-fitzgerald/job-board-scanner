"""End to end: `jobscan run` against a fake Ashby and a fake model, through the real CLI."""
import contextlib
import fcntl
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import cli, fetch, store, tag
from jobscan.records import Board
from tests.fakes import FakeServer, ashby_job, model_reply, tags_quoting_the_posting

PROFILE = """titles = ["Account Executive"]
locations = ["London"]
eligible_where = "the UK"
tags = ["founding", "location_eligible", "segment", "language_required", "tech_company"]
"""


class RunFixture(unittest.TestCase):
    def setUp(self):
        self.ats, self.model = FakeServer(), FakeServer()
        self.addCleanup(self.ats.close)
        self.addCleanup(self.model.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "profiles").mkdir()
        (self.home / "profiles" / "ae.toml").write_text(PROFILE)
        (self.home / "boards.json").write_text(json.dumps({"companies": [
            {"company": "Acme", "board_url": f"{self.ats.url}/posting-api/job-board/acme"},
            {"company": "Broken", "board_url": f"{self.ats.url}/posting-api/job-board/broken"},
        ]}))
        self.write_config(boards_file=True)
        self.ats.routes["/posting-api/job-board/broken"] = (500, {"error": "boom"})
        self.jobs = [ashby_job("1", "Account Executive"), ashby_job("2", "Enterprise Account Executive"),
                     ashby_job("3", "Account Executive", location="Berlin"), ashby_job("4", "Engineer")]
        self.ats.routes["/posting-api/job-board/acme"] = (200, lambda _: (200, {"jobs": self.jobs}))
        self.model_ok()
        for target, name, value in [(fetch, "RETRY_PAUSES", (0, 0)), (tag, "PAUSE", 0),
                                    (tag, "RATE_LIMIT_PAUSES", (0, 0, 0, 0))]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"JOBSCAN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, boards_file):
        (self.home / "config.toml").write_text(('boards_file = "boards.json"\n' if boards_file else "")
                                               + f'[model]\nbase_url = "{self.model.url}"\nslug = "test/model"\n')

    def model_ok(self):
        self.model.routes["/chat/completions"] = (200, lambda payload: model_reply(tags_quoting_the_posting(payload)))

    def run_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["run"])
        return code, out.getvalue().strip().split("\t")

    def digests(self):
        return sorted((self.home / "digests" / "ae").glob("*.md"))


class RunTest(RunFixture):
    def test_new_then_nothing_then_one_added(self):
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        text = Path(path).read_text()
        self.assertIn("## Acme — Enterprise Account Executive · London", text)
        self.assertIn('- founding: **yes** — "Account Executive"', text)
        self.assertNotIn("Berlin", text)
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.assertEqual(len(self.digests()), 1)
        self.jobs.append(ashby_job("5", "Founding Account Executive"))
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "1"))
        self.assertIn("Founding Account Executive", Path(path).read_text())
        self.assertEqual(len(self.digests()), 2)

    def test_board_failure_is_logged_and_run_completes(self):
        code, (_, new, _) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        log = (self.home / "runs.log").read_text()
        self.assertIn("board_failed company='Broken' board=ashby:broken reason='HTTP 500'", log)

    def test_model_down_carries_postings_over_until_it_returns(self):
        self.model.routes["/chat/completions"] = (503, {"error": "down"})
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (1, "0"))
        self.assertEqual(Path(path).read_text(), "# ae — " + Path(path).stem + " · 0 new · 2 untagged, carried over\n")
        self.model_ok()
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("Enterprise Account Executive", Path(path).read_text())
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))

    def test_fabricated_quote_is_flagged_not_dropped(self):
        def fabricating(payload):
            tags = tags_quoting_the_posting(payload)
            tags["founding"]["quote"] = "you will be our very first seller"
            return model_reply(tags)

        self.model.routes["/chat/completions"] = (200, fabricating)
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn('- founding: **yes** — "you will be our very first seller" ⚠ quote not in posting',
                      Path(path).read_text())

    def test_cached_tags_are_not_requested_again(self):
        self.run_cli()
        calls = len(self.model.requests)
        self.jobs.append(ashby_job("5", "Account Executive"))
        self.run_cli()
        self.assertEqual(len(self.model.requests), calls + 1)

    def test_digest_write_failure_leaves_postings_pending(self):
        (self.home / "digests").mkdir()
        (self.home / "digests" / "ae").write_text("a file where the directory should be")
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new, path), (1, "0", "-"))
        (self.home / "digests" / "ae").unlink()
        code, (_, new, _) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))

    def test_second_run_while_locked_is_refused(self):
        with (self.home / ".lock").open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, _ = self.run_cli()
        self.assertEqual(code, 2)

    def test_missing_openrouter_key_is_a_config_error(self):
        (self.home / "config.toml").write_text(
            'boards_file = "boards.json"\n[model]\nbase_url = "https://openrouter.ai/api/v1"\nslug = "x/y"\n')
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            code, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(self.ats.requests, [])


class BoardSourcesTest(RunFixture):
    """Boards come from the boards file and from discovery; a board is one board whichever source names it."""

    def discover(self, *slugs):
        conn = store.open_db(self.home)
        try:
            store.replace_discovered(conn, [Board("ashby", s, s, f"{self.ats.url}/posting-api/job-board/{s}")
                                            for s in slugs], "2026-10-11T00:00:00.000000+00:00")
        finally:
            conn.close()

    def list_boards(self, *slugs):
        (self.home / "boards.json").write_text(json.dumps({"companies": [
            {"company": s.title(), "board_url": f"{self.ats.url}/posting-api/job-board/{s}"} for s in slugs]}))

    def requests_to(self, slug):
        return sum(1 for _, path, _ in self.ats.requests if path == f"/posting-api/job-board/{slug}")

    def test_discovered_only_install(self):
        self.write_config(boards_file=False)
        self.discover("acme")
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("## acme — Enterprise Account Executive · London", Path(path).read_text())

    def test_listed_name_wins_over_the_discovered_slug(self):
        self.list_boards("acme")
        self.discover("acme")
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("## Acme — Enterprise Account Executive · London", Path(path).read_text())
        self.assertEqual(self.requests_to("acme"), 1)

    def test_no_boards_is_a_usage_error(self):
        self.write_config(boards_file=False)
        self.assertEqual(self.run_cli()[0], 2)

    def test_board_moving_between_sources_is_read_once_and_never_redelivered(self):
        self.write_config(boards_file=False)
        self.discover("acme")
        self.assertEqual(self.run_cli()[1][1], "2")
        self.write_config(boards_file=True)
        self.list_boards("ACME")
        before = self.requests_to("acme")
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.assertEqual(self.requests_to("acme"), before + 1)
        self.discover()
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.write_config(boards_file=False)
        self.discover("acme")
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))

    def test_discovered_board_is_skipped_after_three_404s_but_a_listed_one_never(self):
        self.list_boards("acme", "listedgone")
        self.discover("gone")
        for _ in range(store.DEAD_AFTER + 2):
            self.run_cli()
        self.assertEqual(self.requests_to("gone"), store.DEAD_AFTER)
        self.assertEqual(self.requests_to("listedgone"), store.DEAD_AFTER + 2)

    def test_carried_over_postings_of_a_404_board_wait_and_of_a_failing_board_go_out(self):
        for status, delivered in [(404, "0"), (500, "2")]:
            with self.subTest(status=status):
                self.setUp()
                self.list_boards("acme")
                self.model.routes["/chat/completions"] = (503, {"error": "down"})
                self.assertEqual(self.run_cli()[0], 1)
                self.model_ok()
                self.ats.routes["/posting-api/job-board/acme"] = (status, {"error": "x"})
                self.assertEqual(self.run_cli()[1][1], delivered)

    def test_board_back_after_a_404_delivers_its_carried_postings(self):
        self.list_boards("acme")
        self.model.routes["/chat/completions"] = (503, {"error": "down"})
        self.run_cli()
        self.model_ok()
        self.ats.routes["/posting-api/job-board/acme"] = (404, {"error": "x"})
        self.assertEqual(self.run_cli()[1][1], "0")
        self.ats.routes["/posting-api/job-board/acme"] = (200, lambda _: (200, {"jobs": self.jobs}))
        self.assertEqual(self.run_cli()[1][1], "2")

    def test_board_dropped_from_discovery_contributes_nothing_until_listed(self):
        self.write_config(boards_file=False)
        self.discover("acme")
        self.model.routes["/chat/completions"] = (503, {"error": "down"})
        self.run_cli()
        self.model_ok()
        self.ats.routes["/posting-api/job-board/other"] = (200, {"jobs": []})
        self.discover("other")
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.write_config(boards_file=True)
        self.list_boards("acme")
        self.assertEqual(self.run_cli()[1][1], "2")


if __name__ == "__main__":
    unittest.main()
