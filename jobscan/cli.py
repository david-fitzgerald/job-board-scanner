"""Command-line entry point: parses arguments, owns logging, the run lock and exit codes; dispatches to the modules."""
import argparse
import fcntl
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from jobscan import digest, fetch, prefilter, store
from jobscan.config import ConfigError, jobscan_home, load_config, load_profile

EXIT_OK, EXIT_INCOMPLETE, EXIT_USAGE = 0, 1, 2
log = logging.getLogger("jobscan")


class RunLocked(Exception):
    """Another jobscan process holds this $JOBSCAN_HOME."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobscan")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="fetch → filter → tag → write the profile's digest")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code == 0 else EXIT_USAGE
    home = jobscan_home()
    try:
        config = load_config(home)
        profile = load_profile(home)
        _setup_logging(home)
        with _run_lock(home):
            return _run(config, profile)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    except RunLocked:
        print(f"jobscan: another run is using {home}", file=sys.stderr)
        return EXIT_USAGE


def _run(config, profile) -> int:
    started = _now()
    boards, skipped = fetch.load_boards(config.boards_file)
    log.info("fetch_start boards=%d skipped_not_ashby=%d", len(boards), skipped)
    fetched, failed = fetch.fetch_all(boards)
    conn = store.open_db(config.home)
    for result in fetched:
        store.apply_fetch(conn, result.board.slug, result.postings, started)
    for failure in failed:
        log.warning("board_failed company=%r slug=%s reason=%r", failure.board.company, failure.board.slug,
                    failure.reason)
    log.info("fetch_done boards_read=%d boards_failed=%d postings=%d", len(fetched), len(failed),
             sum(len(r.postings) for r in fetched))
    matched = [p for p in store.pending(conn, profile.name) if prefilter.matches(p.posting, profile)]
    entries = [digest.DigestEntry(posting=p) for p in matched]
    exit_code, path = EXIT_OK, None
    if entries:
        today = date.today().isoformat()
        text = digest.render(profile.name, today, entries, carried_over=0)
        try:
            path = digest.publish(config.home / "digests" / profile.name, today, text)
        except OSError as e:
            log.error("digest_write_failed profile=%s error=%r", profile.name, str(e))
            exit_code = EXIT_INCOMPLETE
        else:
            store.deliver(conn, profile.name, [e.posting.posting.key for e in entries], path)
    delivered = len(entries) if path else 0
    log.info("digest profile=%s new=%d path=%s", profile.name, delivered, path or "-")
    print(f"{profile.name}\t{delivered}\t{path or '-'}")
    store.record_run(conn, store.RunRecord(started=started, finished=_now(), boards_read=len(fetched),
                                           boards_failed=len(failed), new=delivered, tagged=0, carried_over=0,
                                           cost=0.0))
    return exit_code


def _setup_logging(home: Path) -> None:
    for old in list(log.handlers):  # main() can run more than once per process (tests)
        log.removeHandler(old)
        old.close()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(sys.stderr), logging.FileHandler(home / "runs.log")):
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


class _run_lock:
    """One run per $JOBSCAN_HOME: overlapping runs would double-tag and race delivery."""

    def __init__(self, home: Path):
        self.path = home / ".lock"

    def __enter__(self):
        self.file = self.path.open("w")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RunLocked from None
        return self

    def __exit__(self, *exc):
        self.file.close()


def _now() -> str:
    # Fixed-width microsecond timestamps compare correctly as strings; store.apply_fetch relies on that.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
