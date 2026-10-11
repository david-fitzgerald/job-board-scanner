"""Ashby adapter: board URLs → API URL, and the public posting-API payload → postings.

Parsing is all-or-nothing per board: one malformed listed job fails the board, so a partial snapshot
never closes postings that are still live."""
import html
import re
from urllib.parse import urlparse

from jobscan.records import Board, BoardError, Posting

ATS = "ashby"
DISPLAY_NAME = "Ashby"
API = "https://api.ashbyhq.com/posting-api/job-board/"


def board(company: str, board_url: str) -> Board | None:
    """A Board for either URL form the map stores: the human board URL (jobs.ashbyhq.com/<slug>) or a posting-API
    URL on any host (how tests point at a fake server), normalised to scheme://host/posting-api/job-board/<slug>.
    None when the URL isn't recognisably Ashby's."""
    try:
        parsed = urlparse(board_url)
        parsed.port  # noqa: B018 -- raises on a malformed port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    path = [p for p in parsed.path.split("/") if p]
    if path[:2] == ["posting-api", "job-board"] and len(path) > 2:
        api_url = f"{parsed.scheme}://{parsed.netloc}/posting-api/job-board/{path[2]}"
        slug = path[2]
    elif parsed.netloc == "jobs.ashbyhq.com" and path:
        api_url, slug = API + path[0], path[0]
    else:
        return None
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=api_url)


def parse(board: Board, data) -> tuple[Posting, ...]:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise BoardError("response has no `jobs` list")
    postings = tuple(p for p in (_posting(board, i, job) for i, job in enumerate(jobs)) if p is not None)
    if len({p.key for p in postings}) != len(postings):
        raise BoardError("duplicate listed job ids")
    return postings


def _posting(board: Board, index: int, job) -> Posting | None:
    if not isinstance(job, dict):
        raise BoardError(f"job {index} is not an object")
    listed = job.get("isListed", True)
    if not isinstance(listed, bool):
        raise BoardError(f"job {index}: `isListed` is not a boolean")
    if not listed:
        return None
    job_id = _field(job, "id", index, required=True)
    title = _field(job, "title", index, required=True)
    secondary = job.get("secondaryLocations")
    secondary = [] if secondary is None else secondary
    if not isinstance(secondary, list) or not all(isinstance(s, dict) for s in secondary):
        raise BoardError(f"job {index}: `secondaryLocations` is not a list of objects")
    locations = [_field(job, "location", index)] + [_field(s, "location", index) for s in secondary]
    return Posting(
        key=f"{board.id}:{job_id}",
        ats=ATS,
        board=board.id,
        company=board.company,
        title=title.strip(),
        location=" / ".join(loc for loc in locations if loc),
        url=_field(job, "jobUrl", index) or "",
        description=_description(_field(job, "descriptionPlain", index), _field(job, "descriptionHtml", index)),
    )


def _field(record: dict, name: str, index: int, required: bool = False) -> str | None:
    value = record.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise BoardError(f"job {index}: `{name}` is not {'a non-empty ' if required else 'a '}string")
    return value


BLOCK_TAG = re.compile(r"<\s*(?:br|/?p|/?li|/?div|/?h[1-6])\b[^>]*>", re.I)
ANY_TAG = re.compile(r"<[^>]+>")


def _description(plain: str | None, rich: str | None) -> str:
    if plain and plain.strip():
        return plain.strip()
    text = html.unescape(ANY_TAG.sub("", BLOCK_TAG.sub("\n", rich or "")))
    lines = (re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)
