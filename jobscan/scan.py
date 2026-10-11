"""One scan for the install's profile: fetch → store → prefilter → tag → digest."""
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from jobscan import digest, fetch, prefilter, store, tag
from jobscan.config import Config, Profile

log = logging.getLogger("jobscan")


@dataclass(frozen=True)
class ScanOutcome:
    profile: str
    new: int
    carried_over: int  # matched but untagged: not delivered, retried next run
    digest_path: Path | None
    digest_failed: bool

    @property
    def complete(self) -> bool:
        return not self.carried_over and not self.digest_failed


def scan(config: Config, profile: Profile, defs: list[tag.TagDef]) -> ScanOutcome:
    started = _now()
    conn = store.open_db(config.home)
    try:
        boards_read, boards_failed = _fetch(conn, config, started)
        matched = [p for p in store.pending(conn, profile.name) if prefilter.matches(p.posting, profile)]
        entries, tagged, cost = _tag(conn, matched, defs, config)
        outcome = _digest(conn, config, profile, entries, carried_over=len(matched) - len(entries))
        store.record_run(conn, store.RunRecord(
            started=started, finished=_now(), boards_read=boards_read, boards_failed=boards_failed,
            new=outcome.new, tagged=tagged, carried_over=outcome.carried_over, cost=cost))
        return outcome
    finally:
        conn.close()


def _fetch(conn: sqlite3.Connection, config: Config, now: str) -> tuple[int, int]:
    boards, skipped = fetch.load_boards(config.boards_file)
    log.info("fetch_start boards=%d skipped_not_ashby=%d", len(boards), skipped)
    fetched, failed = fetch.fetch_all(boards)
    for result in fetched:
        store.apply_fetch(conn, result.board.id, result.postings, now)
    for failure in failed:
        log.warning("board_failed company=%r board=%s reason=%r", failure.board.company, failure.board.id,
                    failure.reason)
    log.info("fetch_done boards_read=%d boards_failed=%d postings=%d", len(fetched), len(failed),
             sum(len(r.postings) for r in fetched))
    return len(fetched), len(failed)


def _tag(conn: sqlite3.Connection, matched: list[store.StoredPosting], defs: list[tag.TagDef], config: Config,
         ) -> tuple[list[digest.DigestEntry], int, float]:
    """Digest entries for every matched posting with a full set of tag results (cached or fresh),
    plus the count tagged this run and its cost. Postings left untagged are simply not in the entries."""
    model_id = config.model.id
    results = {s.posting.key: store.cached_results(conn, s, defs, model_id) for s in matched}
    needed = [s for s in matched if len(results[s.posting.key]) < len(defs)]
    log.info("tag_start matched=%d cached=%d to_tag=%d", len(matched), len(matched) - len(needed), len(needed))
    attempts = tag.tag_all([s.posting for s in needed], defs, config.model, config.api_key)
    for s in needed:
        attempt = attempts.get(s.posting.key)
        if attempt is None:
            continue
        if attempt.results is None:
            # Auth failures need the operator; anything else may clear by the next run.
            level = logging.ERROR if attempt.error in ("HTTP 401", "HTTP 402", "HTTP 403") else logging.WARNING
            log.log(level, "tag_failed posting=%s error=%r", s.posting.key, attempt.error)
            continue
        store.save_results(conn, s, defs, model_id, attempt.results)
        results[s.posting.key] = attempt.results
    tagged = sum(1 for a in attempts.values() if a.results is not None)
    usage = sum((a.usage for a in attempts.values()), tag.Usage())
    cost = usage.cost
    if len(attempts) < len(needed):
        log.error("tag_stopped reason=consecutive_failures not_started=%d", len(needed) - len(attempts))
    log.info("tag_done tagged=%d failed=%d cost=%.4f cost_reported=%s reasoning_tokens=%d", tagged,
             len(attempts) - tagged, cost, usage.cost_reported, usage.reasoning_tokens)
    entries = [digest.DigestEntry(posting=s, tags={d.name: results[s.posting.key][d.name] for d in defs})
               for s in matched if len(results[s.posting.key]) == len(defs)]
    return entries, tagged, cost


def _digest(conn: sqlite3.Connection, config: Config, profile: Profile, entries: list[digest.DigestEntry],
            carried_over: int) -> ScanOutcome:
    if not entries and not carried_over:
        return ScanOutcome(profile.name, new=0, carried_over=0, digest_path=None, digest_failed=False)
    today = date.today().isoformat()
    text = digest.render(profile.name, today, entries, carried_over)
    try:
        path = digest.publish(config.home / "digests" / profile.name, today, text)
    except OSError as e:
        log.error("digest_write_failed profile=%s error=%r", profile.name, str(e))
        return ScanOutcome(profile.name, new=0, carried_over=carried_over, digest_path=None, digest_failed=True)
    store.deliver(conn, profile.name, [e.posting.posting.key for e in entries], path)
    log.info("digest profile=%s new=%d carried_over=%d path=%s", profile.name, len(entries), carried_over, path)
    return ScanOutcome(profile.name, new=len(entries), carried_over=carried_over, digest_path=path,
                       digest_failed=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
