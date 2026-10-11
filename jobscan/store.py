"""All persistent state: one SQLite file under $JOBSCAN_HOME. Only the main thread writes."""
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jobscan.config import ConfigError
from jobscan.records import Posting
from jobscan.tag import TagDef, TagResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    key TEXT PRIMARY KEY, ats TEXT NOT NULL, board TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL,
    location TEXT NOT NULL, url TEXT NOT NULL, description TEXT NOT NULL, input_hash TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS postings_board ON postings (board);
CREATE TABLE IF NOT EXISTS tag_results (
    posting_key TEXT NOT NULL, input_hash TEXT NOT NULL, def_hash TEXT NOT NULL, model_id TEXT NOT NULL,
    value TEXT NOT NULL, quote TEXT, verified INTEGER NOT NULL, warn INTEGER NOT NULL,
    PRIMARY KEY (posting_key, input_hash, def_hash, model_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
    profile TEXT NOT NULL, posting_key TEXT NOT NULL, digest_path TEXT NOT NULL, PRIMARY KEY (profile, posting_key)
);
CREATE TABLE IF NOT EXISTS runs (
    started TEXT NOT NULL, finished TEXT NOT NULL, boards_read INTEGER NOT NULL, boards_failed INTEGER NOT NULL,
    new INTEGER NOT NULL, tagged INTEGER NOT NULL, carried_over INTEGER NOT NULL, cost REAL NOT NULL,
    cost_complete INTEGER NOT NULL  -- 0 when some model reply reported no cost: `cost` is then a lower bound
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
    cost_complete: bool


def open_db(home: Path) -> sqlite3.Connection:
    """Opens (creating if needed) the install's database; a file that can't be opened is a setup error."""
    path = home / "jobscan.sqlite"
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error as e:
        raise ConfigError(f"{path}: cannot open database ({type(e).__name__})") from None
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
    except sqlite3.Error as e:
        conn.close()
        raise ConfigError(f"{path}: cannot open database ({type(e).__name__}: {e})") from None
    return conn


def input_hash(posting: Posting) -> str:
    """Hash of everything the model sees, so an edit to title, location or description re-tags."""
    return hashlib.sha256("\x1f".join([posting.title, posting.location, posting.description]).encode()).hexdigest()


def apply_fetch(conn: sqlite3.Connection, board: str, postings: tuple[Posting, ...], now: str) -> None:
    """Record one successfully read board as the full truth for that board: what it lists is open (re-seen
    postings reopen), everything else of the board's is closed. Never call this for a board whose fetch failed."""
    with conn:
        conn.execute("UPDATE postings SET closed = 1 WHERE board = ?", (board,))
        conn.executemany(
            """INSERT INTO postings (key, ats, board, company, title, location, url, description, input_hash,
                                     first_seen, last_seen, closed)
               VALUES (:key, :ats, :board, :company, :title, :location, :url, :description, :input_hash,
                       :now, :now, 0)
               ON CONFLICT (key) DO UPDATE SET
                   company = excluded.company, title = excluded.title, location = excluded.location,
                   url = excluded.url, description = excluded.description, input_hash = excluded.input_hash,
                   last_seen = excluded.last_seen, closed = 0""",
            [{**vars(p), "input_hash": input_hash(p), "now": now} for p in postings],
        )


def pending(conn: sqlite3.Connection, profile: str) -> list[StoredPosting]:
    """Open postings never delivered to this profile, oldest first."""
    rows = conn.execute(
        """SELECT * FROM postings
           WHERE closed = 0 AND key NOT IN (SELECT posting_key FROM deliveries WHERE profile = ?)
           ORDER BY first_seen, key""",
        (profile,),
    )
    return [StoredPosting(
        posting=Posting(key=r["key"], ats=r["ats"], board=r["board"], company=r["company"], title=r["title"],
                        location=r["location"], url=r["url"], description=r["description"]),
        input_hash=r["input_hash"],
        first_seen=r["first_seen"],
    ) for r in rows]


def cached_results(conn: sqlite3.Connection, stored: StoredPosting, defs: list[TagDef], model_id: str,
                   ) -> dict[str, TagResult]:
    """Tag results still valid for this posting text, definition and model, by tag name."""
    by_hash = {d.hash: d for d in defs}
    rows = conn.execute(
        f"""SELECT def_hash, value, quote, verified, warn FROM tag_results
            WHERE posting_key = ? AND input_hash = ? AND model_id = ? AND def_hash IN ({",".join("?" * len(defs))})""",
        (stored.posting.key, stored.input_hash, model_id, *by_hash),
    )
    results = {}
    for r in rows:
        d = by_hash[r["def_hash"]]
        value = json.loads(r["value"])
        results[d.name] = TagResult(value=tuple(value) if d.multi else value, quote=r["quote"],
                                    verified=bool(r["verified"]), warn=bool(r["warn"]))
    return results


def save_results(conn: sqlite3.Connection, stored: StoredPosting, defs: list[TagDef], model_id: str,
                 results: dict[str, TagResult]) -> None:
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO tag_results
               (posting_key, input_hash, def_hash, model_id, value, quote, verified, warn)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(stored.posting.key, stored.input_hash, d.hash, model_id, json.dumps(results[d.name].value),
              results[d.name].quote, results[d.name].verified, results[d.name].warn) for d in defs],
        )


def deliver(conn: sqlite3.Connection, profile: str, posting_keys: list[str], digest_path: Path) -> None:
    """Record that these postings are in a published digest. Call only after the digest file exists."""
    with conn:
        conn.executemany("INSERT INTO deliveries (profile, posting_key, digest_path) VALUES (?, ?, ?)",
                         [(profile, key, str(digest_path)) for key in posting_keys])


def record_run(conn: sqlite3.Connection, run: RunRecord) -> None:
    with conn:
        conn.execute(
            """INSERT INTO runs (started, finished, boards_read, boards_failed, new, tagged, carried_over, cost,
                                cost_complete)
               VALUES (:started, :finished, :boards_read, :boards_failed, :new, :tagged, :carried_over, :cost,
                       :cost_complete)""",
            vars(run),
        )
