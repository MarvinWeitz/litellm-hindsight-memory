"""Routing configuration for the Hindsight memory plugin.

Everything that decides which banks a request reads and writes lives in one
YAML file, so the tag scheme can change without touching code. The path comes
from ``HINDSIGHT_ROUTING_CONFIG``; without it the built-in defaults apply.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "tags": {
        "customer": "customer:",
        "project": "project:",
        "opt_out": "memory:off",
        "passthrough": ["domain:", "kind:"],
    },
    "banks": {
        "write": "customer::{customer}",
        "read": [
            {
                "bank": "customer::{customer}",
                "tags": ["project:{project}", "project:_shared"],
                "tags_match": "any_strict",
                # Observations are consolidated across a bank and can carry another
                # project's facts under this project's tag, so read raw facts only.
                "types": ["world", "experience"],
                "budget": "low",
                "max_tokens": 1500,
            },
            {"bank": "global::sap", "budget": "low", "max_tokens": 1000},
        ],
    },
    "retain": {
        "enabled": True,
        "tags": ["project:{project}", "source:litellm-proxy"],
        "max_chars": 12000,
    },
    "recall": {"timeout_seconds": 0.8, "cache_ttl_seconds": 3600, "cache_size": 2048},
    "defaults": {"project_when_missing": "_shared"},
    "untagged_request": "passthrough",
    "allowed": None,
}


@dataclass(frozen=True)
class ReadTarget:
    bank: str
    tags: tuple[str, ...] = ()
    tags_match: str = "any_strict"
    types: tuple[str, ...] = ()  # memory types to recall; empty = all
    budget: str = "low"
    max_tokens: int = 1500


@dataclass(frozen=True)
class RoutingConfig:
    customer_prefix: str
    project_prefix: str
    opt_out_tag: str | None
    passthrough_prefixes: tuple[str, ...]
    write_bank: str
    reads: tuple[ReadTarget, ...]
    retain_enabled: bool
    retain_tags: tuple[str, ...]
    retain_max_chars: int
    recall_timeout: float
    cache_ttl: float
    cache_size: int
    project_when_missing: str | None
    untagged_request: str
    allowed: dict[str, list[str] | None] | None = field(default=None)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RoutingConfig:
        cfg = _merge(DEFAULT_CONFIG, raw)
        tags, banks, retain, recall = cfg["tags"], cfg["banks"], cfg["retain"], cfg["recall"]
        untagged = cfg["untagged_request"]
        if untagged not in ("passthrough", "reject"):
            raise ValueError(f"untagged_request must be 'passthrough' or 'reject', got {untagged!r}")
        return cls(
            customer_prefix=tags["customer"],
            project_prefix=tags["project"],
            opt_out_tag=tags.get("opt_out"),
            passthrough_prefixes=tuple(tags.get("passthrough") or ()),
            write_bank=banks["write"],
            reads=tuple(
                ReadTarget(
                    bank=r["bank"],
                    tags=tuple(r.get("tags") or ()),
                    tags_match=r.get("tags_match", "any_strict"),
                    types=tuple(r.get("types") or ()),
                    budget=r.get("budget", "low"),
                    max_tokens=int(r.get("max_tokens", 1500)),
                )
                for r in banks["read"]
            ),
            retain_enabled=bool(retain["enabled"]),
            retain_tags=tuple(retain.get("tags") or ()),
            retain_max_chars=int(retain["max_chars"]),
            recall_timeout=float(recall["timeout_seconds"]),
            cache_ttl=float(recall["cache_ttl_seconds"]),
            cache_size=int(recall["cache_size"]),
            project_when_missing=cfg["defaults"].get("project_when_missing"),
            untagged_request=untagged,
            allowed=cfg.get("allowed"),
        )


def load_config(path: str | os.PathLike[str] | None = None) -> RoutingConfig:
    path = path or os.environ.get("HINDSIGHT_ROUTING_CONFIG")
    if not path:
        return RoutingConfig.from_dict({})
    data = yaml.safe_load(Path(path).read_text()) or {}
    # Allow the routing block to sit under a top-level key, as in the docs.
    data = data.get("hindsight_routing", data)
    return RoutingConfig.from_dict(data)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge dicts recursively; lists and scalars in ``override`` replace ``base``."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out
