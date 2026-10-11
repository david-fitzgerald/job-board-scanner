"""Markdown digest of a profile's new postings: pure rendering plus a never-overwrite publish."""
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from jobscan.fetch import ADAPTERS
from jobscan.store import StoredPosting
from jobscan.tag import TagResult


@dataclass(frozen=True)
class DigestEntry:
    posting: StoredPosting
    tags: dict[str, TagResult]  # in the profile's tag order


def render(profile: str, date: str, entries: list[DigestEntry], carried_over: int) -> str:
    header = f"# {profile} — {date} · {len(entries)} new"
    if carried_over:
        header += f" · {carried_over} untagged, carried over"
    blocks = [header]
    for entry in entries:
        p = entry.posting.posting
        blocks.append("\n".join([
            f"## {p.company} — {p.title} · {p.location or 'location not given'}",
            f"{p.url} · {ADAPTERS[p.ats].DISPLAY_NAME} · first seen {entry.posting.first_seen[:10]}",
            *(_tag_line(name, result) for name, result in entry.tags.items()),
        ]))
    return "\n\n".join(blocks) + "\n"


def _tag_line(name: str, result: TagResult) -> str:
    shown = (", ".join(result.value) or "unknown") if isinstance(result.value, tuple) else result.value
    if result.quote is None and not result.warn:  # unknown, empty or the tag's default: nothing to cite
        return f"- {name}: {shown}"
    if result.quote is None:
        return f"- {name}: **{shown}** ⚠ no quote"
    return f'- {name}: **{shown}** — "{result.quote}"' + ("" if result.verified else " ⚠ quote not in posting")


def publish(directory: Path, date: str, text: str) -> Path:
    """Write `text` as <date>.md, or <date>-2.md, -3, … if taken. An existing digest is never overwritten,
    even one created after the name was chosen: the finished file is hard-linked into place, which fails
    rather than replaces."""
    directory.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".digest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        n = 1
        while True:
            path = directory / (f"{date}.md" if n == 1 else f"{date}-{n}.md")
            try:
                os.link(temp, path)
                return path
            except FileExistsError:
                n += 1
    finally:
        os.unlink(temp)
