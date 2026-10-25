"""Routing configuration for the Hindsight memory plugin.

Everything that decides which banks a request reads and writes lives in one
YAML file, so the tag scheme can change without touching code. The path comes
from ``HINDSIGHT_ROUTING_CONFIG``.

There are no built-in defaults: every key must be set in the file, and keys
that may be switched off take an explicit ``null``. Unknown keys are refused,
so a typo fails at load time instead of silently changing routing.

Two kinds of request tags drive routing:
- the bank tag (``tags.bank``) picks the memory bank, e.g. ``customer:acme``
- the scope tag (``tags.scope``) narrows recall and retain inside that bank,
  e.g. ``project:acme-portal``
Each kind has a name, which is also its placeholder in bank and tag templates
(``{customer}``, ``{project}``).
"""

from __future__ import annotations

import os
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

TAGS_MATCH = ("any", "all", "any_strict", "all_strict", "exact")
BUDGETS = ("low", "mid", "high")
UNTAGGED = ("passthrough", "reject")


class ConfigError(ValueError):
    """The routing file is missing a key, has an unknown key, or a bad value."""


@dataclass(frozen=True)
class TagKind:
    name: str  # placeholder name in templates, also used in messages and logs
    prefix: str


@dataclass(frozen=True)
class ReadTarget:
    bank: str
    tags: tuple[str, ...]
    tags_match: str
    types: tuple[str, ...]  # memory types to recall; empty = all
    budget: str
    max_tokens: int


@dataclass(frozen=True)
class RoutingConfig:
    hindsight_url: str
    hindsight_api_key: str | None
    client_timeout: float
    user_agent: str
    bank_tag: TagKind
    scope_tag: TagKind | None
    scope_when_missing: str | None
    opt_out_tag: str | None
    passthrough_prefixes: tuple[str, ...]
    slug_pattern: re.Pattern[str]
    lowercase: bool
    write_bank: str
    reads: tuple[ReadTarget, ...]
    retain_enabled: bool
    retain_tags: tuple[str, ...]
    retain_max_chars: int
    retain_template: str
    retain_context: str
    retain_document_prefix: str
    retain_metadata: dict[str, str]
    recall_timeout: float
    cache_ttl: float
    cache_size: int
    memory_marker: str
    memory_header: str
    untagged_request: str
    allowed: dict[str, list[str] | None] | None

    @property
    def placeholders(self) -> tuple[str, ...]:
        return (self.bank_tag.name,) + ((self.scope_tag.name,) if self.scope_tag else ())

    @classmethod
    def from_dict(cls, raw: dict[str, Any], env: dict[str, str] | None = None) -> RoutingConfig:
        env = os.environ if env is None else env
        r = _Reader(raw, "hindsight_routing")
        hs = r.section("hindsight", {"url_env", "api_key_env", "client_timeout_seconds", "user_agent"})
        tags = r.section("tags", {"bank", "scope", "opt_out", "passthrough", "slug_pattern", "lowercase"})
        banks = r.section("banks", {"write", "read"})
        retain = r.section("retain", {"enabled", "tags", "max_chars", "template", "context",
                                      "document_prefix", "metadata"})
        recall = r.section("recall", {"timeout_seconds", "cache_ttl_seconds", "cache_size"})
        inject = r.section("inject", {"marker", "header"})
        r.done({"hindsight", "tags", "banks", "retain", "recall", "inject", "untagged_request", "allowed"})

        bank_tag = _tag_kind(tags.section("bank", {"name", "prefix"}))
        scope_raw = tags.get("scope", (dict, type(None)))
        scope_tag, when_missing = None, None
        if scope_raw is not None:
            scope = _Reader(scope_raw, f"{tags.path}.scope")
            scope_tag = _tag_kind(scope, extra=frozenset({"when_missing"}))
            when_missing = scope.get("when_missing", (str, type(None)))
            if scope_tag.name == bank_tag.name:
                raise ConfigError(f"{tags.path}.scope.name must differ from {tags.path}.bank.name")

        api_key_env = hs.get("api_key_env", (str, type(None)))
        cfg = cls(
            hindsight_url=_env(env, hs.get("url_env", str), f"{hs.path}.url_env"),
            hindsight_api_key=env.get(api_key_env) or None if api_key_env else None,
            client_timeout=hs.number("client_timeout_seconds"),
            user_agent=hs.get("user_agent", str),
            bank_tag=bank_tag,
            scope_tag=scope_tag,
            scope_when_missing=when_missing,
            opt_out_tag=tags.get("opt_out", (str, type(None))),
            passthrough_prefixes=tuple(tags.str_list("passthrough")),
            slug_pattern=_regex(tags.get("slug_pattern", str), f"{tags.path}.slug_pattern"),
            lowercase=tags.get("lowercase", bool),
            write_bank=banks.get("write", str),
            reads=tuple(_read_target(t, f"{banks.path}.read[{i}]")
                        for i, t in enumerate(banks.get("read", list))),
            retain_enabled=retain.get("enabled", bool),
            retain_tags=tuple(retain.str_list("tags")),
            retain_max_chars=int(retain.number("max_chars")),
            retain_template=retain.get("template", str),
            retain_context=retain.get("context", str),
            retain_document_prefix=retain.get("document_prefix", str),
            retain_metadata={str(k): str(v) for k, v in (retain.get("metadata", (dict, type(None))) or {}).items()},
            recall_timeout=recall.number("timeout_seconds"),
            cache_ttl=recall.number("cache_ttl_seconds"),
            cache_size=int(recall.number("cache_size")),
            memory_marker=inject.get("marker", str),
            memory_header=inject.get("header", str),
            untagged_request=r.choice("untagged_request", UNTAGGED),
            allowed=r.get("allowed", (dict, type(None))),
        )
        cfg._check_templates()
        return cfg

    def _check_templates(self) -> None:
        """Refuse templates whose placeholders the tag scheme does not define."""
        known = set(self.placeholders)
        templates = [("banks.write", self.write_bank), ("retain.tags", *self.retain_tags)]
        templates += [(f"banks.read[{i}]", t.bank, *t.tags) for i, t in enumerate(self.reads)]
        for where, *values in templates:
            for value in values:
                for field_name in _fields(value):
                    if field_name not in known:
                        raise ConfigError(
                            f"{where}: unknown placeholder {{{field_name}}}; "
                            f"the tag scheme defines {', '.join('{' + k + '}' for k in sorted(known))}"
                        )
        if self.bank_tag.name not in _fields(self.write_bank):
            raise ConfigError(f"banks.write must contain {{{self.bank_tag.name}}}, or every request "
                              "would write into the same bank")
        for field_name in _fields(self.retain_template):
            if field_name not in ("user", "assistant"):
                raise ConfigError(f"retain.template: unknown placeholder {{{field_name}}}; use {{user}} and {{assistant}}")
        for field_name in _fields(self.memory_header):
            if field_name != "scope":
                raise ConfigError(f"inject.header: unknown placeholder {{{field_name}}}; only {{scope}} is available")


def load_config(path: str | os.PathLike[str] | None = None) -> RoutingConfig:
    path = path or os.environ.get("HINDSIGHT_ROUTING_CONFIG")
    if not path:
        raise ConfigError("no routing config: set HINDSIGHT_ROUTING_CONFIG to the hindsight_routing.yaml path")
    data = yaml.safe_load(Path(path).read_text()) or {}
    # Allow the routing block to sit under a top-level key, as in the docs.
    data = data.get("hindsight_routing", data)
    return RoutingConfig.from_dict(data)


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
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ConfigError(f"{self.path}.{key} has the wrong type: {value!r}")
        return value

    def number(self, key: str) -> float:
        value = self.get(key, (int, float))
        if isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{self.path}.{key} must be a positive number, got {value!r}")
        return float(value)

    def choice(self, key: str, options: tuple[str, ...]) -> str:
        value = self.get(key, str)
        if value not in options:
            raise ConfigError(f"{self.path}.{key} must be one of {', '.join(options)}, got {value!r}")
        return value

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
        """Refuse keys this section does not know; the caller reads the known ones."""
        unknown = sorted(set(self.data) - keys)
        if unknown:
            raise ConfigError(f"{self.path}: unknown key(s) {', '.join(unknown)}")


def _tag_kind(r: _Reader, extra: frozenset[str] = frozenset()) -> TagKind:
    r.done({"name", "prefix"} | extra)
    name, prefix = r.get("name", str), r.get("prefix", str)
    if not name.isidentifier():
        raise ConfigError(f"{r.path}.name must be a plain word like customer, got {name!r}")
    if not prefix:
        raise ConfigError(f"{r.path}.prefix must not be empty")
    return TagKind(name=name, prefix=prefix)


def _read_target(raw: Any, path: str) -> ReadTarget:
    r = _Reader(raw, path)
    r.done({"bank", "tags", "tags_match", "types", "budget", "max_tokens"})
    return ReadTarget(
        bank=r.get("bank", str),
        tags=tuple(r.str_list("tags")),
        tags_match=r.choice("tags_match", TAGS_MATCH),
        types=tuple(r.str_list("types")),
        budget=r.choice("budget", BUDGETS),
        max_tokens=int(r.number("max_tokens")),
    )


def _env(env: dict[str, str], name: str, where: str) -> str:
    value = env.get(name)
    if not value:
        raise ConfigError(f"{where} names {name}, but that environment variable is not set")
    return value


def _regex(pattern: str, where: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"{where} is not a valid regular expression: {exc}") from exc


def _fields(template: str) -> list[str]:
    return [f for _, f, _, _ in string.Formatter().parse(template) if f is not None]
