"""End to end: `jobscan discover` against fake Common Crawl index files, through the real CLI."""
import contextlib
import fcntl
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import cli, crawl, store
from tests.fakes import FakeFiles, cdx_line, crawl_files, crawl_list

CRAWLS = ("CC-MAIN-2026-39", "CC-MAIN-2026-34", "CC-MAIN-2026-30")


def captures(*slugs):
    return [cdx_line(f"https://jobs.ashbyhq.com/{slug}/job-1") for slug in slugs] + \
        [cdx_line("https://app.ashbyhq.com/login"), cdx_line("https://www.example.com/")]


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.cc = FakeFiles()
        self.addCleanup(self.cc.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)  # no config, profile, boards file or key: discover needs none of them
        for target, name, value in [(crawl, "BASE", self.cc.url), (crawl, "RETRY_PAUSES", (0, 0, 0))]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"JOBSCAN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.serve({CRAWLS[0]: captures("acme", "Beta"), CRAWLS[1]: captures("beta", "gamma"),
                    CRAWLS[2]: captures("acme")})

    def serve(self, by_crawl):
        self.cc.files.clear()
        self.cc.files.update(crawl_list("CC-MAIN-2025-51", *by_crawl))
        for crawl_id, lines in by_crawl.items():
            self.cc.files.update(crawl_files(crawl_id, lines))

    def run_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["discover"])
        return code, out.getvalue().strip()

    def discovered(self):
        conn = store.open_db(self.home)
        try:
            return sorted((b.id, b.api_url) for b in store.live_discovered(conn))
        finally:
            conn.close()

    def test_union_of_three_crawls_case_folded(self):
        self.assertEqual(self.run_cli(), (0, "discovered\t3\t" + ",".join(CRAWLS)))
        self.assertEqual(self.discovered(), [
            ("ashby:acme", "https://api.ashbyhq.com/posting-api/job-board/acme"),
            ("ashby:beta", "https://api.ashbyhq.com/posting-api/job-board/beta"),
            ("ashby:gamma", "https://api.ashbyhq.com/posting-api/job-board/gamma"),
        ])

    def test_unreadable_crawl_keeps_the_previous_list(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.serve({CRAWLS[0]: captures("delta"), CRAWLS[1]: captures("delta"), CRAWLS[2]: captures("delta")})
        self.cc.fail[f"/cc-index/collections/{CRAWLS[2]}/indexes/cluster.idx"] = [503] * 4
        self.assertEqual(self.run_cli(), (1, ""))
        self.assertEqual([b for b, _ in self.discovered()], ["ashby:acme", "ashby:beta", "ashby:gamma"])
        self.assertIn("discover_failed", (self.home / "runs.log").read_text())

    def test_unwritable_database_is_a_usage_error_and_keeps_the_list(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.serve({CRAWLS[0]: captures("delta"), CRAWLS[1]: captures("delta"), CRAWLS[2]: captures("delta")})
        database = self.home / "jobscan.sqlite"
        database.chmod(0o444)
        self.addCleanup(database.chmod, 0o644)
        self.assertEqual(self.run_cli(), (2, ""))
        database.chmod(0o644)
        self.assertEqual([b for b, _ in self.discovered()], ["ashby:acme", "ashby:beta", "ashby:gamma"])

    def test_refused_while_a_run_holds_the_lock(self):
        with (self.home / ".lock").open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_cli(), (2, ""))
        self.assertEqual(self.discovered(), [])

    def test_missing_home_is_a_usage_error(self):
        with mock.patch.dict(os.environ, {"JOBSCAN_HOME": str(self.home / "nowhere")}):
            self.assertEqual(self.run_cli(), (2, ""))


if __name__ == "__main__":
    unittest.main()
