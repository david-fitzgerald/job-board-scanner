import tempfile
import unittest
from pathlib import Path

from jobscan import store
from jobscan.records import Posting


def posting(job_id, board="acme", title="Account Executive"):
    return Posting(key=f"ashby:{board}:{job_id}", ats="ashby", board=f"ashby:{board}", company=board.title(), title=title,
                   location="London", url=f"https://jobs.ashbyhq.com/{board}/{job_id}", description="Sell software.")


class ApplyFetchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open_db(Path(tmp.name))
        self.addCleanup(self.conn.close)

    def closed(self):
        return {r["key"]: r["closed"] for r in self.conn.execute("SELECT key, closed FROM postings")}

    def test_absent_postings_close_and_reseen_reopen(self):
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"), posting("2")), "2026-10-11T10:00:00.000000+00:00")
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"),), "2026-10-11T11:00:00.000000+00:00")
        self.assertEqual(self.closed(), {"ashby:acme:1": 0, "ashby:acme:2": 1})
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"), posting("2")), "2026-10-11T12:00:00.000000+00:00")
        self.assertEqual(self.closed(), {"ashby:acme:1": 0, "ashby:acme:2": 0})

    def test_valid_empty_board_closes_all_and_other_boards_untouched(self):
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"),), "2026-10-11T10:00:00.000000+00:00")
        store.apply_fetch(self.conn, "ashby:beta", (posting("1", "beta"),), "2026-10-11T10:00:00.000000+00:00")
        store.apply_fetch(self.conn, "ashby:acme", (), "2026-10-11T11:00:00.000000+00:00")
        self.assertEqual(self.closed(), {"ashby:acme:1": 1, "ashby:beta:1": 0})

    def test_closure_does_not_depend_on_clock_order(self):
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"), posting("2")), "2026-10-11T12:00:00.000000+00:00")
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"),), "2026-10-11T09:00:00.000000+00:00")
        self.assertEqual(self.closed(), {"ashby:acme:1": 0, "ashby:acme:2": 1})

    def test_unopenable_database_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "jobscan.sqlite").mkdir()
            with self.assertRaises(store.ConfigError):
                store.open_db(Path(d))

    def test_first_seen_survives_updates_and_input_hash_tracks_location(self):
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"),), "2026-10-11T10:00:00.000000+00:00")
        moved = Posting(**{**vars(posting("1")), "location": "Remote - UK"})
        store.apply_fetch(self.conn, "ashby:acme", (moved,), "2026-10-11T11:00:00.000000+00:00")
        row = self.conn.execute("SELECT first_seen, input_hash FROM postings").fetchone()
        self.assertEqual(row["first_seen"], "2026-10-11T10:00:00.000000+00:00")
        self.assertEqual(row["input_hash"], store.input_hash(moved))
        self.assertNotEqual(store.input_hash(moved), store.input_hash(posting("1")))


if __name__ == "__main__":
    unittest.main()
