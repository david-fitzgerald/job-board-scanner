"""Records shared across the pipeline: boards and postings, independent of which ATS they came from."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Board:
    ats: str
    slug: str
    company: str
    api_url: str

    @property
    def id(self) -> str:
        return f"{self.ats}:{self.slug}"


@dataclass(frozen=True)
class Posting:
    key: str  # <ats>:<slug>:<ATS posting id>
    ats: str
    board: str  # Board.id
    company: str
    title: str
    location: str
    url: str
    description: str


class BoardError(Exception):
    """The board answered, but not with a usable, well-formed job list."""
