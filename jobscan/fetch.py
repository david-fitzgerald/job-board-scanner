"""Board fetching: board list → postings per board through the ATS adapter, one board's failure isolated
from the rest. A board counts as read only on HTTP 200 with a payload its adapter parses completely."""
import http.client
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from jobscan import ashby
from jobscan.config import ConfigError
from jobscan.records import Board, BoardError, Posting

ADAPTERS = {ashby.ATS: ashby}
TIMEOUT = 20
RETRY_PAUSES = (2, 10)  # seconds before the 2nd and 3rd tries
RETRYABLE = {429, 500, 502, 503, 504}
WORKERS = 8
HEADERS = {"User-Agent": "jobscan (+https://github.com/david-fitzgerald/jobscan)"}


@dataclass(frozen=True)
class BoardFetch:
    board: Board
    postings: tuple[Posting, ...]


@dataclass(frozen=True)
class BoardFailure:
    board: Board
    reason: str
    http_status: int | None = None  # 404 means the board is gone; anything else may clear by the next run


def load_boards(path: Path) -> tuple[list[Board], int]:
    """Boards some adapter recognises, from a {"companies": [{"company", "board_url"}]} file, deduplicated by
    board id, plus the count of entries no adapter recognises."""
    try:
        entries = json.loads(path.read_text())["companies"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as e:
        raise ConfigError(f"{path}: not a readable boards file ({type(e).__name__}: {e})") from None
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ConfigError(f"{path}: `companies` must be a list of objects")
    boards, skipped = {}, 0
    for entry in entries:
        company, url = entry.get("company"), entry.get("board_url")
        found = next((b for adapter in ADAPTERS.values()
                      if (b := adapter.board(company if isinstance(company, str) else "",
                                             url if isinstance(url, str) else "")) is not None), None)
        if found is None:
            skipped += 1
            continue
        boards.setdefault(found.id, found)
    return list(boards.values()), skipped


def fetch_all(boards: list[Board]) -> tuple[list[BoardFetch], list[BoardFailure]]:
    with ThreadPoolExecutor(WORKERS) as pool:
        outcomes = list(pool.map(_fetch_isolated, boards))
    return ([o for o in outcomes if isinstance(o, BoardFetch)],
            [o for o in outcomes if isinstance(o, BoardFailure)])


def _fetch_isolated(board: Board) -> BoardFetch | BoardFailure:
    try:
        return BoardFetch(board, ADAPTERS[board.ats].parse(board, _get_json(board.api_url)))
    # OSError covers URLError, timeouts and resets; ValueError covers bad JSON and bad UTF-8;
    # RecursionError covers deeply nested JSON on Python < 3.14.
    except (OSError, http.client.HTTPException, ValueError, RecursionError, BoardError) as e:
        return BoardFailure(board, _reason(e), e.code if isinstance(e, urllib.error.HTTPError) else None)


def _get_json(url: str):
    request = urllib.request.Request(url, headers=HEADERS)
    for pause in (*RETRY_PAUSES, None):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                if response.status != 200:
                    raise BoardError(f"HTTP {response.status}")
                return json.load(response)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code not in RETRYABLE or pause is None:
                raise
        except (OSError, http.client.HTTPException):
            if pause is None:
                raise
        time.sleep(pause)


def _reason(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"network: {e.reason}"
    if isinstance(e, (OSError, http.client.HTTPException)):
        return f"network: {type(e).__name__}"
    if isinstance(e, (ValueError, RecursionError)):
        return "malformed JSON"
    return str(e) or type(e).__name__
