import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import digest
from jobscan.fetch import Posting
from jobscan.store import StoredPosting


def entry(job_id="1", title="Founding Account Executive"):
    p = Posting(key=f"ashby:acme:{job_id}", board="acme", company="Acme AI", title=title, location="London",
                url=f"https://jobs.ashbyhq.com/acme/{job_id}", description="")
    return digest.DigestEntry(posting=StoredPosting(posting=p, input_hash="h",
                                                    first_seen="2026-10-12T08:00:00.000000+00:00"))


class RenderTest(unittest.TestCase):
    def test_header_and_entry(self):
        text = digest.render("david-ae", "2026-10-12", [entry()], carried_over=0)
        self.assertEqual(text, "# david-ae — 2026-10-12 · 1 new\n\n"
                               "## Acme AI — Founding Account Executive · London\n"
                               "https://jobs.ashbyhq.com/acme/1 · Ashby · first seen 2026-10-12\n")

    def test_header_counts_carry_over(self):
        text = digest.render("david-ae", "2026-10-12", [], carried_over=3)
        self.assertEqual(text, "# david-ae — 2026-10-12 · 0 new · 3 untagged, carried over\n")


class PublishTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "digests" / "ae"

    def test_same_day_digests_get_suffixes_and_nothing_is_overwritten(self):
        paths = [digest.publish(self.dir, "2026-10-12", f"run {n}\n") for n in range(3)]
        self.assertEqual([p.name for p in paths], ["2026-10-12.md", "2026-10-12-2.md", "2026-10-12-3.md"])
        self.assertEqual([p.read_text() for p in paths], ["run 0\n", "run 1\n", "run 2\n"])
        self.assertEqual(sorted(f.name for f in self.dir.iterdir()), sorted(p.name for p in paths))

    def test_file_appearing_after_name_choice_survives(self):
        real_link = os.link

        def racing_link(src, dst):
            if Path(dst).name == "2026-10-12.md" and not Path(dst).exists():
                Path(dst).write_text("someone else's digest\n")
            return real_link(src, dst)

        with mock.patch.object(digest.os, "link", racing_link):
            path = digest.publish(self.dir, "2026-10-12", "mine\n")
        self.assertEqual(path.name, "2026-10-12-2.md")
        self.assertEqual((self.dir / "2026-10-12.md").read_text(), "someone else's digest\n")
        self.assertEqual(path.read_text(), "mine\n")

    def test_unwritable_directory_raises(self):
        self.dir.parent.mkdir(parents=True)
        self.dir.write_text("a file where the directory should be")
        with self.assertRaises(OSError):
            digest.publish(self.dir, "2026-10-12", "x\n")


if __name__ == "__main__":
    unittest.main()
