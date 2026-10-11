"""The `discover` pipeline: the newest Common Crawl crawls → every board an ATS adapter recognises in them → the
install's discovered board list. The list is replaced only when every crawl read completely."""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jobscan import crawl, fetch, store
from jobscan.records import Board

CRAWLS = 3  # one crawl finds 91% of a hand-built board list, three 97%, a fourth nothing more
log = logging.getLogger("jobscan")


@dataclass(frozen=True)
class DiscoverOutcome:
    boards: int
    crawls: tuple[str, ...]


def discover(home: Path) -> DiscoverOutcome:
    """Raises crawl.CrawlError, leaving the previous list in place, when any crawl can't be read."""
    base = crawl.BASE
    crawls = crawl.latest_crawls(CRAWLS, base)
    boards: dict[str, Board] = {}
    for adapter in fetch.ADAPTERS.values():
        for crawl_id in crawls:
            urls = crawl.captures(crawl_id, adapter.CRAWL_HOST, base)
            found = [b for b in (adapter.board("", url) for url in urls) if b is not None]
            log.info("discover crawl=%s host=%s captures=%d boards=%d", crawl_id, adapter.CRAWL_HOST, len(urls),
                     len({b.id for b in found}))
            for board in found:
                boards.setdefault(board.id, board)
    conn = store.open_db(home)
    try:
        store.replace_discovered(conn, list(boards.values()), datetime.now(UTC).isoformat(timespec="microseconds"))
    finally:
        conn.close()
    log.info("discover_done boards=%d crawls=%s", len(boards), ",".join(crawls))
    return DiscoverOutcome(boards=len(boards), crawls=tuple(crawls))
