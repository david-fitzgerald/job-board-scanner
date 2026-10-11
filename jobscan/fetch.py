"""Ashby board fetching: board list → postings per board, with one board's failure isolated from the rest."""
import html
import http.client
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from jobscan.config import ConfigError

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/"
TIMEOUT = 20
RETRY_PAUSES = (2, 10)  # seconds before the 2nd and 3rd tries
RETRYABLE = {429, 500, 502, 503, 504}
WORKERS = 8
HEADERS = {"User-Agent": "jobscan (+https://github.com/david-fitzgerald/jobscan)"}


@dataclass(frozen=True)
class Board:
    company: str
    slug: str
    api_url: str


@dataclass(frozen=True)
class Posting:
    key: str
    board: str
    company: str
    title: str
    location: str
    url: str
    description: str


@dataclass(frozen=True)
class BoardFetch:
    board: Board
    postings: tuple[Posting, ...]


@dataclass(frozen=True)
class BoardFailure:
    board: Board
    reason: str


class BoardError(Exception):
    """The board answered, but not with a usable job list."""


def load_boards(path: Path) -> tuple[list[Board], int]:
    """Ashby boards from a {"companies": [{"company", "board_url"}]} file, deduplicated by slug,
    plus the count of entries skipped as not Ashby."""
    try:
        entries = json.loads(path.read_text())["companies"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ConfigError(f"{path}: not a readable boards file ({type(e).__name__}: {e})") from None
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ConfigError(f"{path}: `companies` must be a list of objects")
    boards, skipped = {}, 0
    for entry in entries:
        api_url = ashby_api_url(entry.get("board_url") or "")
        if api_url is None:
            skipped += 1
            continue
        slug = api_url.rstrip("/").rsplit("/", 1)[-1]
        boards.setdefault(slug, Board(company=entry.get("company") or slug, slug=slug, api_url=api_url))
    return list(boards.values()), skipped


def ashby_api_url(board_url: str) -> str | None:
    """The posting-API URL for an Ashby board in either form the map stores: the human board URL
    (jobs.ashbyhq.com/<slug>) or the API URL itself, which is used as-is on any host."""
    parsed = urlparse(board_url)
    path = [p for p in parsed.path.split("/") if p]
    if path[:2] == ["posting-api", "job-board"] and len(path) > 2:
        return f"{parsed.scheme}://{parsed.netloc}/posting-api/job-board/{path[2]}"
    if parsed.netloc == "jobs.ashbyhq.com" and path:
        return ASHBY_API + path[0]
    return None


def fetch_all(boards: list[Board]) -> tuple[list[BoardFetch], list[BoardFailure]]:
    with ThreadPoolExecutor(WORKERS) as pool:
        outcomes = list(pool.map(_fetch_isolated, boards))
    return ([o for o in outcomes if isinstance(o, BoardFetch)],
            [o for o in outcomes if isinstance(o, BoardFailure)])


def _fetch_isolated(board: Board) -> BoardFetch | BoardFailure:
    try:
        return BoardFetch(board, fetch_board(board))
    # OSError covers URLError, timeouts and resets; ValueError covers bad JSON and bad UTF-8.
    except (OSError, http.client.HTTPException, ValueError, BoardError) as e:
        return BoardFailure(board, _reason(e))


def fetch_board(board: Board) -> tuple[Posting, ...]:
    """Succeeds only on a JSON object whose `jobs` is a list; anything else raises."""
    data = _get_json(board.api_url)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise BoardError("response has no `jobs` list")
    return tuple(p for p in (_posting(board, job) for job in jobs) if p is not None)


def _get_json(url: str):
    request = urllib.request.Request(url, headers=HEADERS)
    for pause in (*RETRY_PAUSES, None):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code not in RETRYABLE or pause is None:
                raise
        except (OSError, http.client.HTTPException):
            if pause is None:
                raise
        time.sleep(pause)


def _posting(board: Board, job) -> Posting | None:
    """A listed job with a string id and title; anything else is dropped."""
    if not isinstance(job, dict) or job.get("isListed") is False:
        return None
    job_id, title = job.get("id"), job.get("title")
    if not isinstance(job_id, str) or not isinstance(title, str):
        return None
    locations = [job.get("location")] + [s.get("location") for s in job.get("secondaryLocations") or []
                                         if isinstance(s, dict)]
    return Posting(
        key=f"ashby:{board.slug}:{job_id}",
        board=board.slug,
        company=board.company,
        title=title.strip(),
        location=" / ".join(loc for loc in locations if isinstance(loc, str) and loc),
        url=job.get("jobUrl") or "",
        description=_description(job),
    )


BLOCK_TAG = re.compile(r"<\s*(?:br|/?p|/?li|/?div|/?h[1-6])\b[^>]*>", re.I)
ANY_TAG = re.compile(r"<[^>]+>")


def _description(job: dict) -> str:
    plain = job.get("descriptionPlain")
    if isinstance(plain, str) and plain.strip():
        return plain.strip()
    text = html.unescape(ANY_TAG.sub("", BLOCK_TAG.sub("\n", job.get("descriptionHtml") or "")))
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _reason(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"network: {e.reason}"
    if isinstance(e, (OSError, http.client.HTTPException)):
        return f"network: {type(e).__name__}"
    if isinstance(e, ValueError):
        return "malformed JSON"
    return str(e) or type(e).__name__
