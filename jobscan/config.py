"""Config and profile loading: TOML under $JOBSCAN_HOME parsed into typed records, or ConfigError."""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(Exception):
    """A config or profile problem the user must fix before any work starts (exit 2)."""


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    slug: str
    provider: str | None
    quantization: str | None

    @property
    def id(self) -> str:
        """Everything that decides which weights answer: a cached tag result is valid only for the same id."""
        return "|".join([self.base_url, self.slug, self.provider or "", self.quantization or ""])


@dataclass(frozen=True)
class Config:
    home: Path
    boards_file: Path
    model: ModelConfig
    api_key: str | None = field(repr=False)


@dataclass(frozen=True)
class Profile:
    name: str
    titles: tuple[str, ...]
    exclude_titles: tuple[str, ...]
    locations: tuple[str, ...]
    eligible_where: str
    tags: tuple[str, ...]


def jobscan_home() -> Path:
    return Path(os.environ.get("JOBSCAN_HOME") or "~/.jobscan").expanduser()


def load_config(home: Path) -> Config:
    path = home / "config.toml"
    data = _read_toml(path)
    model = data.get("model")
    if not isinstance(model, dict):
        raise ConfigError(f"{path}: missing [model] table")
    boards_file = Path(_string(data, "boards_file", path)).expanduser()
    if not boards_file.is_absolute():
        boards_file = home / boards_file
    model_config = ModelConfig(
        base_url=_string(model, "base_url", path).rstrip("/"),
        slug=_string(model, "slug", path),
        provider=_string(model, "provider", path, required=False),
        quantization=_string(model, "quantization", path, required=False),
    )
    try:
        url = urlparse(model_config.base_url)
        url.port  # raises on a malformed port
    except ValueError:
        url = None
    if url is None or url.scheme not in ("http", "https") or not url.hostname or url.query or url.fragment \
            or not model_config.base_url.isprintable() or " " in model_config.base_url:
        raise ConfigError(f"{path}: `model.base_url` must be an http(s) URL with no spaces, query or fragment")
    # The OpenRouter key goes to OpenRouter only, never to another configured endpoint.
    api_key = None
    if url.hostname == "openrouter.ai":
        if url.scheme != "https":
            raise ConfigError(f"{path}: OpenRouter must be reached over https")
        if not (model_config.provider and model_config.quantization):
            raise ConfigError(f"{path}: OpenRouter needs `model.provider` and `model.quantization` pinned")
        api_key = os.environ.get("OPENROUTER_API_KEY") or None
        if api_key is None:
            raise ConfigError("OPENROUTER_API_KEY is not set")
        if not api_key.isascii() or not api_key.isprintable() or " " in api_key:
            # Checked here because a malformed header value would otherwise surface inside exception text.
            raise ConfigError("OPENROUTER_API_KEY contains whitespace or control characters")
    return Config(home=home, boards_file=boards_file, model=model_config, api_key=api_key)


def load_profile(home: Path) -> Profile:
    """The skeleton serves exactly one profile per install."""
    files = sorted((home / "profiles").glob("*.toml"))
    if len(files) != 1:
        raise ConfigError(f"{home / 'profiles'}: expected exactly one profile, found {len(files)}")
    path = files[0]
    data = _read_toml(path)
    return Profile(
        name=path.stem,
        titles=_strings(data, "titles", path),
        exclude_titles=_strings(data, "exclude_titles", path, required=False),
        locations=_strings(data, "locations", path),
        eligible_where=_string(data, "eligible_where", path),
        tags=_unique(_strings(data, "tags", path), "tags", path),
    )


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: not readable ({type(e).__name__})") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None


def _string(data: dict, key: str, path: Path, required: bool = True) -> str | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: `{key}` must be a non-empty string")
    return value


def _strings(data: dict, key: str, path: Path, required: bool = True) -> tuple[str, ...]:
    value = data.get(key)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or (required and not value) \
            or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{path}: `{key}` must be a {'non-empty ' if required else ''}list of strings")
    return tuple(value)


def _unique(values: tuple[str, ...], key: str, path: Path) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ConfigError(f"{path}: `{key}` lists a name twice")
    return values
