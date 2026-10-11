"""All persistent state: one SQLite file under $JOBSCAN_HOME. Only the main thread writes."""
import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jobscan.fetch import Posting

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    key TEXT PRIMARY KEY, board TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL,
    location TEXT NOT NULL, url TEXT NOT NULL, description TEXT NOT NULL, input_hash TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS postings_board ON postings (board);
CREATE TABLE IF NOT EXISTS deliveries (
    profile TEXT NOT NULL, posting_key TEXT NOT NULL, digest_path TEXT NOT NULL, PRIMARY KEY (profile, posting_key)
);
CREATE TABLE IF NOT EXISTS runs (
    started TEXT NOT NULL, finished TEXT NOT NULL, boards_read INTEGER NOT NULL, boards_failed INTEGER NOT NULL,
    new INTEGER NOT NULL, tagged INTEGER NOT NULL, carried_over INTEGER NOT NULL, cost REAL NOT NULL
);
"""


@dataclass(frozen=True)
class StoredPosting:
    posting: Posting
    input_hash: str
    first_seen: str


@dataclass(frozen=True)
class RunRecord:
    started: str
    finished: str
    boards_read: int
    boards_failed: int
    new: int
    tagged: int
    carried_over: int
    cost: float


def open_db(home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(home / "jobscan.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def input_hash(posting: Posting) -> str:
    """Hash of everything the model sees, so an edit to title, location or description re-tags."""
    return hashlib.sha256("\x1f".join([posting.title, posting.location, posting.description]).encode()).hexdigest()


def apply_fetch(conn: sqlite3.Connection, board: str, postings: tuple[Posting, ...], now: str) -> None:
    """Record one successfully read board: upsert what it lists (reopening re-seen postings) and close
    the board's postings it no longer lists. Never call this for a board whose fetch failed."""
    with conn:
        conn.executemany(
            """INSERT INTO postings (key, board, company, title, location, url, description, input_hash,
                                     first_seen, last_seen, closed)
               VALUES (:key, :board, :company, :title, :location, :url, :description, :input_hash, :now, :now, 0)
               ON CONFLICT (key) DO UPDATE SET
                   company = excluded.company, title = excluded.title, location = excluded.location,
                   url = excluded.url, description = excluded.description, input_hash = excluded.input_hash,
                   last_seen = excluded.last_seen, closed = 0""",
            [{**vars(p), "input_hash": input_hash(p), "now": now} for p in postings],
        )
        # Every posting the board still lists now carries last_seen = now.
        conn.execute("UPDATE postings SET closed = 1 WHERE board = ? AND last_seen < ?", (board, now))


def pending(conn: sqlite3.Connection, profile: str) -> list[StoredPosting]:
    """Open postings never delivered to this profile, oldest first."""
    rows = conn.execute(
        """SELECT * FROM postings
           WHERE closed = 0 AND key NOT IN (SELECT posting_key FROM deliveries WHERE profile = ?)
           ORDER BY first_seen, key""",
        (profile,),
    )
    return [StoredPosting(
        posting=Posting(key=r["key"], board=r["board"], company=r["company"], title=r["title"],
                        location=r["location"], url=r["url"], description=r["description"]),
        input_hash=r["input_hash"],
        first_seen=r["first_seen"],
    ) for r in rows]


def deliver(conn: sqlite3.Connection, profile: str, posting_keys: list[str], digest_path: Path) -> None:
    """Record that these postings are in a published digest. Call only after the digest file exists."""
    with conn:
        conn.executemany("INSERT INTO deliveries (profile, posting_key, digest_path) VALUES (?, ?, ?)",
                         [(profile, key, str(digest_path)) for key in posting_keys])


def record_run(conn: sqlite3.Connection, run: RunRecord) -> None:
    with conn:
        conn.execute(
            """INSERT INTO runs (started, finished, boards_read, boards_failed, new, tagged, carried_over, cost)
               VALUES (:started, :finished, :boards_read, :boards_failed, :new, :tagged, :carried_over, :cost)""",
            vars(run),
        )
