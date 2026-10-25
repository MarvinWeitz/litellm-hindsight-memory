"""Settings for the promotion job, read from promotion.yaml.

There are no built-in defaults: every key must be set in the file, and the file
itself must be named with --config or PROMOTION_CONFIG. Unknown keys are refused.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """promotion.yaml is missing a key, has an unknown key, or a bad value."""


@dataclass(frozen=True)
class Lifecycle:
    """Tags that track a fact through the workflow, on source documents and staged candidates."""

    nominated: str
    done: str
    rejected: str
    staged: str
    filtered: str
    published: str


@dataclass(frozen=True)
class PromotionConfig:
    hindsight_url: str
    hindsight_api_key: str | None
    hindsight_timeout: float
    customer_prefix: str
    staging_bank: str
    global_bank: str
    scope_prefix: str
    shared_scope: str | None
    domain_prefix: str
    kind_prefix: str
    published_tags: tuple[str, ...]
    lifecycle: Lifecycle
    domains: tuple[str, ...]
    include_untagged: bool
    query: str
    budget: str
    max_candidates: int
    llm_base_url: str
    llm_api_key: str | None
    llm_model: str | None
    llm_timeout: float
    subject: str
    blocklist: tuple[str, ...]
    kinds: tuple[str, ...]
    triage: bool
    max_per_bank: int
    min_score: int
    novelty_check: bool
    review_dir: Path
    staging_mission: str
    staging_context: str
    published_context: str

    @classmethod
    def from_dict(cls, raw: dict, env: dict[str, str] | None = None, base: Path | None = None) -> PromotionConfig:
        env = os.environ if env is None else env
        base = base or Path.cwd()
        r = _Reader(raw, "promotion")
        r.done({"hindsight", "banks", "tags", "harvest", "llm", "anonymize", "triage", "kinds", "review", "texts"})
        hs = r.section("hindsight", {"url_env", "api_key_env", "timeout_seconds"})
        banks = r.section("banks", {"customers", "staging", "global"})
        tags = r.section("tags", {"scope_prefix", "shared_scope", "domain_prefix", "kind_prefix", "published",
                                  "lifecycle"})
        life = tags.section("lifecycle", {"nominated", "done", "rejected", "staged", "filtered", "published"})
        harvest = r.section("harvest", {"domains", "include_untagged", "query", "budget", "max_candidates_per_pass"})
        llm = r.section("llm", {"base_url", "api_key_env", "model", "model_env", "timeout_seconds"})
        anon = r.section("anonymize", {"subject", "blocklist"})
        triage = r.section("triage", {"enabled", "max_per_bank", "min_score", "novelty_check"})
        review = r.section("review", {"dir"})
        texts = r.section("texts", {"staging_mission", "staging_context", "published_context"})

        url_env = hs.get("url_env", str)
        if not env.get(url_env):
            raise ConfigError(f"{hs.path}.url_env names {url_env}, but that environment variable is not set")
        model, model_env = llm.get("model", (str, type(None))), llm.get("model_env", (str, type(None)))
        if (model is None) == (model_env is None):
            raise ConfigError(f"{llm.path}: set exactly one of model and model_env, the other to null")
        kinds = tuple(r.str_list("kinds"))
        if not kinds:
            raise ConfigError("promotion.kinds must not be empty")
        published_context = texts.get("published_context", str)
        if "{release}" not in published_context:
            raise ConfigError(f"{texts.path}.published_context must contain {{release}}")
        review_dir = Path(review.get("dir", str))
        return cls(
            hindsight_url=env[url_env],
            hindsight_api_key=_optional_env(env, hs.get("api_key_env", (str, type(None)))),
            hindsight_timeout=hs.number("timeout_seconds"),
            customer_prefix=banks.get("customers", str),
            staging_bank=banks.get("staging", str),
            global_bank=banks.get("global", str),
            scope_prefix=tags.get("scope_prefix", str),
            shared_scope=tags.get("shared_scope", (str, type(None))),
            domain_prefix=tags.get("domain_prefix", str),
            kind_prefix=tags.get("kind_prefix", str),
            published_tags=tuple(tags.str_list("published")),
            lifecycle=Lifecycle(**{k: life.get(k, str) for k in Lifecycle.__dataclass_fields__}),
            domains=tuple(harvest.str_list("domains")),
            include_untagged=harvest.get("include_untagged", bool),
            query=" ".join(harvest.get("query", str).split()),
            budget=harvest.get("budget", str),
            max_candidates=int(harvest.number("max_candidates_per_pass")),
            llm_base_url=llm.get("base_url", str).rstrip("/"),
            llm_api_key=_optional_env(env, llm.get("api_key_env", (str, type(None)))),
            llm_model=model or env.get(model_env) or None,
            llm_timeout=llm.number("timeout_seconds"),
            subject=anon.get("subject", str),
            blocklist=tuple(anon.str_list("blocklist")),
            kinds=kinds,
            triage=triage.get("enabled", bool),
            max_per_bank=int(triage.number("max_per_bank")),
            min_score=int(triage.number("min_score")),
            novelty_check=triage.get("novelty_check", bool),
            review_dir=review_dir if review_dir.is_absolute() else base / review_dir,
            staging_mission=texts.get("staging_mission", str),
            staging_context=texts.get("staging_context", str),
            published_context=published_context,
        )


def load_config(path: str | os.PathLike[str] | None = None, env: dict[str, str] | None = None) -> PromotionConfig:
    path = path or os.environ.get("PROMOTION_CONFIG")
    if not path:
        raise ConfigError("no promotion config: pass --config or set PROMOTION_CONFIG to the promotion.yaml path")
    path = Path(path)
    # A relative review dir is relative to the config file, not to where the command runs.
    return PromotionConfig.from_dict(yaml.safe_load(path.read_text()) or {}, env=env, base=path.resolve().parent)


def _optional_env(env: dict[str, str], name: str | None) -> str | None:
    return (env.get(name) or None) if name else None


class _Reader:
    """Reads one mapping of the YAML file, naming the full key path in every error."""

    def __init__(self, data: Any, path: str):
        if not isinstance(data, dict):
            raise ConfigError(f"{path} must be a mapping")
        self.data, self.path = data, path

    def get(self, key: str, kind: type | tuple[type, ...]) -> Any:
        if key not in self.data:
            raise ConfigError(f"{self.path}.{key} is required")
        value = self.data[key]
        if not isinstance(value, kind):
            raise ConfigError(f"{self.path}.{key} has the wrong type: {value!r}")
        return value

    def number(self, key: str) -> float:
        value = self.get(key, (int, float))
        if isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{self.path}.{key} must be a positive number, got {value!r}")
        return float(value)

    def str_list(self, key: str) -> list[str]:
        value = self.get(key, list)
        if not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{self.path}.{key} must be a list of strings")
        return value

    def section(self, key: str, keys: set[str]) -> _Reader:
        sub = _Reader(self.get(key, dict), f"{self.path}.{key}")
        sub.done(keys)
        return sub

    def done(self, keys: set[str]) -> None:
        unknown = sorted(set(self.data) - keys)
        if unknown:
            raise ConfigError(f"{self.path}: unknown key(s) {', '.join(unknown)}")
