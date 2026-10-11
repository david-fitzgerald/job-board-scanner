"""`jobscan eval`: score the production tagging path against a labelled posting set, as G2 scored the spike."""
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from jobscan import tag
from jobscan.config import Config, ConfigError
from jobscan.fetch import Posting

ELIGIBLE_WHERE = "the UK"  # the G2 set's labels answer UK eligibility
MINORITY = {"founding": lambda v: v == "yes", "location_eligible": lambda v: v == "no",
            "language_required": lambda v: v != "no"}  # the gate tags and their minority answer
BAR_ACCURACY, BAR_RECALL, BAR_QUOTES = 0.90, 0.80, 0.95


@dataclass(frozen=True)
class LabelledPosting:
    id: str
    posting: Posting
    labels: dict[str, str]


@dataclass(frozen=True)
class TagScore:
    tag: str
    labelled: int
    predicted: int
    accuracy: float
    minority: int
    recall: float | None  # None: no minority answers in the set, which fails the gate
    precision: float | None
    clears: bool


@dataclass(frozen=True)
class EvalReport:
    postings: int
    tagged: int
    scores: list[TagScore]
    quotes: int
    quote_rate: float | None  # None: no quotes at all, which fails the gate
    cost: float
    passed: bool


def load_set(path: Path) -> list[LabelledPosting]:
    try:
        rows = json.loads(path.read_text())
        items = [LabelledPosting(
            id=str(r["id"]),
            posting=Posting(key=str(r["id"]), board="eval", company="", title=r["title"], location=r["location"],
                            url="", description=r["description"]),
            labels={k: str(v) for k, v in r["labels"].items()},
        ) for r in rows]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise ConfigError(f"{path}: not a labelled set ({type(e).__name__}: {e})") from None
    if len({i.id for i in items}) != len(items):
        raise ConfigError(f"{path}: duplicate ids")
    return items


def run_eval(items: list[LabelledPosting], config: Config) -> EvalReport:
    """Tags every posting with all built-in tags in one call each (the production prompt), scores the gate tags,
    and keeps the raw predictions under $JOBSCAN_HOME/eval/ for inspection."""
    defs = tag.definitions(tag.builtin_names(), ELIGIBLE_WHERE)
    attempts = tag.tag_all([i.posting for i in items], defs, config.model, config.api_key)
    predictions = {key: a.results for key, a in attempts.items() if a.results is not None}
    _save(config.home, items, attempts)
    return score(items, predictions, cost=sum(a.cost for a in attempts.values()))


def score(items: list[LabelledPosting], predictions: dict[str, dict[str, tag.TagResult]], cost: float = 0.0,
          ) -> EvalReport:
    scores = [_score_tag(name, is_minority, items, predictions) for name, is_minority in MINORITY.items()]
    quotes = [r for results in predictions.values() for r in results.values() if r.quote is not None]
    quote_rate = sum(r.verified for r in quotes) / len(quotes) if quotes else None
    passed = all(s.clears for s in scores) and quote_rate is not None and quote_rate >= BAR_QUOTES
    return EvalReport(postings=len(items), tagged=len(predictions), scores=scores, quotes=len(quotes),
                      quote_rate=quote_rate, cost=cost, passed=passed)


def _score_tag(name, is_minority, items, predictions) -> TagScore:
    pairs = [(i.labels[name].lower(), _predicted(predictions.get(i.id), name)) for i in items if name in i.labels]
    right = [_same(name, truth, pred) for truth, pred in pairs]
    minority = [ok for (truth, _), ok in zip(pairs, right) if is_minority(truth)]
    flagged = [ok for (_, pred), ok in zip(pairs, right) if pred is not None and is_minority(pred)]
    accuracy = sum(right) / len(pairs) if pairs else 0.0
    recall = sum(minority) / len(minority) if minority else None
    return TagScore(tag=name, labelled=len(pairs), predicted=sum(p is not None for _, p in pairs), accuracy=accuracy,
                    minority=len(minority), recall=recall, precision=sum(flagged) / len(flagged) if flagged else None,
                    clears=accuracy >= BAR_ACCURACY and recall is not None and recall >= BAR_RECALL)


def _predicted(results: dict[str, tag.TagResult] | None, name: str) -> str | None:
    """A missing prediction is None, which never matches a label."""
    if results is None or name not in results:
        return None
    return str(results[name].value).lower()


def _same(name: str, truth: str, pred: str | None) -> bool:
    if pred is None:
        return False
    if name == "language_required" and truth != "no" and pred != "no":
        return truth in pred or pred in truth  # "mandarin" vs "mandarin chinese", as G2 scored it
    return truth == pred


def _save(home: Path, items: list[LabelledPosting], attempts: dict[str, tag.TagAttempt]) -> None:
    directory = home / "eval"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with (directory / f"{stamp}.jsonl").open("w") as f:
        for item in items:
            attempt = attempts.get(item.id)
            row = {"id": item.id, "labels": item.labels, "error": attempt.error if attempt else "not started",
                   "cost": attempt.cost if attempt else 0.0,
                   "tags": {k: asdict(v) for k, v in attempt.results.items()} if attempt and attempt.results else None}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def format_report(report: EvalReport) -> str:
    def pct(x):
        return "n/a" if x is None else f"{x:.1%}"

    lines = [f"postings {report.postings} · tagged {report.tagged} · cost ${report.cost:.4f}"]
    for s in report.scores:
        lines.append(f"{s.tag:18} acc {pct(s.accuracy)} (n={s.labelled}, predicted {s.predicted}) · minority recall "
                     f"{pct(s.recall)} of {s.minority} · precision {pct(s.precision)}  {'✓' if s.clears else '✗'}")
    lines.append(f"quotes verbatim {pct(report.quote_rate)} of {report.quotes}  "
                 f"{'✓' if report.quote_rate is not None and report.quote_rate >= BAR_QUOTES else '✗'}")
    lines.append(f"G2 bar: {'PASS' if report.passed else 'FAIL'}")
    return "\n".join(lines)
