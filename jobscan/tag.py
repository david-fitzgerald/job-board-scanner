"""LLM tagging: tag definitions + posting → one model call → validated values with checked quotes.

Shared by `run` and `eval`, so eval measures exactly the production path."""
import hashlib
import http.client
import json
import re
import threading
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from jobscan.config import ConfigError, ModelConfig
from jobscan.records import Posting

TRIES = 3
PAUSE, RATE_LIMIT_PAUSE = 5, 30  # seconds before a retry
TIMEOUT = 300
WORKERS = 4
STOP_AFTER_FAILURES = 3  # consecutive failed postings → treat the model as unavailable
UNKNOWN = "unknown"


@dataclass(frozen=True)
class TagDef:
    name: str
    definition: str
    values: tuple[str, ...] | None  # None: free text
    multi: bool
    default: str | None

    @property
    def hash(self) -> str:
        """Identity of the question asked; an edited definition invalidates cached answers."""
        return hashlib.sha256(json.dumps([self.definition, self.values, self.multi, self.default]).encode()).hexdigest()


@dataclass(frozen=True)
class TagResult:
    value: str | tuple[str, ...]
    quote: str | None
    verified: bool  # quote found in the posting text
    warn: bool  # a quote was needed and is missing or not found: shown with ⚠


@dataclass(frozen=True)
class Usage:
    """Billing and reasoning evidence summed over every reply received for one posting, rejected ones included."""
    cost: float = 0.0
    cost_reported: bool = True  # False if any reply came without a cost (local endpoints never report one)
    reasoning_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.cost + other.cost, self.cost_reported and other.cost_reported,
                     self.reasoning_tokens + other.reasoning_tokens)


@dataclass(frozen=True)
class TagAttempt:
    """One posting's tagging outcome: `results` on success, `error` otherwise; usage either way."""
    results: dict[str, TagResult] | None
    usage: Usage
    error: str | None


@dataclass(frozen=True)
class _Reply:
    content: str | None  # None when the response envelope carries no message text
    usage: Usage


class ReplyError(Exception):
    """The model answered, but not with a valid tag object."""


def builtin_names() -> tuple[str, ...]:
    return tuple(_builtins())


def _builtins() -> dict:
    return tomllib.loads((Path(__file__).parent / "tags.toml").read_text())


def definitions(tag_names: tuple[str, ...], eligible_where: str) -> list[TagDef]:
    builtins = _builtins()
    unknown = [n for n in tag_names if n not in builtins]
    if unknown:
        raise ConfigError(f"unknown tags {unknown}; built-ins are {sorted(builtins)}")
    return [TagDef(
        name=name,
        definition=builtins[name]["definition"].strip().replace("{eligible_where}", eligible_where),
        values=tuple(builtins[name]["values"]) if "values" in builtins[name] else None,
        multi=builtins[name].get("multi", False),
        default=builtins[name].get("default"),
    ) for name in tag_names]


PROMPT = """You tag job postings. Apply the rules below exactly, using only the posting text.

## Ground rules
- **Posting text only:** title, location field and job description. Do not use what you know about the company from elsewhere; outside knowledge turns an `unknown` into a false `yes`.
- `unknown` means the posting doesn't say. A `no` needs text that rules the tag out; the absence of a `yes` is `unknown`, except where a tag below sets a different default.

## Tags
{tags}

## Output
Return JSON with every tag. `value` is one of the tag's allowed values.
`quote` is a short span (≤200 characters) copied verbatim from the title, location or description that supports the value, or null when the value is `unknown`, an empty list, or a default with nothing to cite.

## Posting
Quote text from inside the tags, never the tags themselves.
<title>{title}</title>
<location>{location}</location>
<description>
{description}
</description>"""


def prompt(posting: Posting, defs: list[TagDef]) -> str:
    tags = "\n\n".join(f"### `{d.name}`\n{_allowed(d)}\n{d.definition}" for d in defs)
    return PROMPT.format(tags=tags, title=posting.title, location=posting.location, description=posting.description)


def _allowed(d: TagDef) -> str:
    if d.values is None:
        return f"Value: `{d.default}` or free text."
    listed = " · ".join(f"`{v}`" for v in d.values)
    return f"Value: a list of zero or more of {listed}." if d.multi else f"Value: one of {listed}."


def schema(defs: list[TagDef]) -> dict:
    def value_schema(d: TagDef) -> dict:
        if d.values is None:
            return {"type": "string"}
        if d.multi:
            return {"type": "array", "items": {"type": "string", "enum": list(d.values)}}
        return {"type": "string", "enum": list(d.values)}

    return {"type": "object", "additionalProperties": False, "required": [d.name for d in defs],
            "properties": {d.name: {"type": "object", "additionalProperties": False, "required": ["value", "quote"],
                                    "properties": {"value": value_schema(d), "quote": {"type": ["string", "null"]}}}
                           for d in defs}}


def tag_posting(posting: Posting, defs: list[TagDef], model: ModelConfig, api_key: str | None) -> TagAttempt:
    usage, error = Usage(), None
    for attempt in range(TRIES):
        if attempt:
            time.sleep(RATE_LIMIT_PAUSE if error == "HTTP 429" else PAUSE)
        try:
            reply = _call(model, api_key, prompt(posting, defs), schema(defs))
        except urllib.error.HTTPError as e:
            e.close()
            error = f"HTTP {e.code}"
            continue
        # Exception classes only, never their text: request errors can echo headers, and the key is one.
        except (OSError, http.client.HTTPException, ValueError) as e:
            error = type(e).__name__
            continue
        usage += reply.usage
        try:
            if reply.content is None:
                raise ReplyError("no message content")
            return TagAttempt(results=parse_reply(reply.content, defs, posting), usage=usage, error=None)
        except ReplyError as e:
            error = f"invalid reply: {e}"
    return TagAttempt(results=None, usage=usage, error=error)


def _call(model: ModelConfig, api_key: str | None, text: str, response_schema: dict) -> _Reply:
    """One chat-completions request. Usage is read before anything is validated, so a rejected reply still counts."""
    body = {"model": model.slug, "temperature": 0, "usage": {"include": True},
            "messages": [{"role": "user", "content": text}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "tags", "strict": True, "schema": response_schema}}}
    if model.provider:
        body["provider"] = {"only": [model.provider], "allow_fallbacks": False, "require_parameters": True,
                            **({"quantizations": [model.quantization]} if model.quantization else {})}
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})}
    request = urllib.request.Request(f"{model.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        reply = json.load(response)
    if not isinstance(reply, dict):
        return _Reply(content=None, usage=Usage(cost_reported=False))
    usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
    cost = usage.get("cost")
    details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    reasoning = details.get("reasoning_tokens")
    choices = reply.get("choices")
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) \
        else None
    content = message.get("content") if isinstance(message, dict) else None
    return _Reply(content=content if isinstance(content, str) else None,
                  usage=Usage(cost=float(cost) if isinstance(cost, (int, float)) else 0.0,
                              cost_reported=isinstance(cost, (int, float)),
                              reasoning_tokens=reasoning if isinstance(reasoning, int) else 0))


def parse_reply(content: str, defs: list[TagDef], posting: Posting) -> dict[str, TagResult]:
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        raise ReplyError("not JSON") from None
    if not isinstance(data, dict) or set(data) != {d.name for d in defs}:
        raise ReplyError("not an object with exactly the requested tags")
    return {d.name: _result(d, data[d.name], posting) for d in defs}


def _result(d: TagDef, cell, posting: Posting) -> TagResult:
    if not isinstance(cell, dict) or set(cell) != {"value", "quote"}:
        raise ReplyError(f"`{d.name}` is not a {{value, quote}} object")
    value, quote = _value(d, cell["value"]), cell["quote"]
    if quote is not None and not isinstance(quote, str):
        raise ReplyError(f"`{d.name}` quote is not a string")
    quote = quote.strip() if quote and quote.strip() else None
    verified = quote is not None and verify_quote(quote, posting)
    return TagResult(value=value, quote=quote, verified=verified, warn=_needs_quote(d, value) and not verified)


def _value(d: TagDef, raw):
    if d.multi:
        if not isinstance(raw, list) or any(v not in d.values for v in raw):
            raise ReplyError(f"`{d.name}` must be a list of {d.values}")
        return tuple(v for v in d.values if v in raw)  # canonical order, duplicates dropped
    if not isinstance(raw, str) or not raw.strip():
        raise ReplyError(f"`{d.name}` must be a non-empty string")
    value = raw.strip()
    if d.values is None:
        return d.default if d.default and value.lower() == d.default else value
    if value not in d.values:
        raise ReplyError(f"`{d.name}` value {value!r} not in {d.values}")
    return value


def _needs_quote(d: TagDef, value) -> bool:
    return value not in (UNKNOWN, (), d.default)


PUNCT = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "‐": "-", "‑": "-",
                       "–": "-", "—": "-", " ": " "})


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).translate(PUNCT)).strip()


def verify_quote(quote: str, posting: Posting) -> bool:
    """True when the quote appears within one of the posting's title, location or description (never spanning
    two), ignoring Unicode compatibility forms, curly quotes, dash variants and whitespace runs.
    Blank quotes never verify."""
    needle = _normalise(quote)
    return bool(needle) and any(needle in _normalise(field)
                                for field in (posting.title, posting.location, posting.description))


def tag_all(postings: list[Posting], defs: list[TagDef], model: ModelConfig, api_key: str | None,
            ) -> dict[str, TagAttempt]:
    """Tag postings in parallel, keyed by posting key. After STOP_AFTER_FAILURES consecutive failures (in
    completion order) no further postings start; in-flight results are kept. Unstarted postings are absent."""
    stop = threading.Event()

    def work(posting: Posting) -> TagAttempt | None:
        return None if stop.is_set() else tag_posting(posting, defs, model, api_key)

    attempts, consecutive_failures = {}, 0
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(work, p): p.key for p in postings}
        for future in as_completed(futures):
            attempt = future.result()
            if attempt is None:
                continue
            attempts[futures[future]] = attempt
            consecutive_failures = 0 if attempt.results else consecutive_failures + 1
            if consecutive_failures >= STOP_AFTER_FAILURES:
                stop.set()
    return attempts
