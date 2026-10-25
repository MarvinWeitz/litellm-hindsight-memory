"""Settings for the promotion job, read from promotion.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class PromotionConfig:
    customer_prefix: str
    staging_bank: str
    global_bank: str
    domains: tuple[str, ...]
    include_untagged: bool
    query: str
    budget: str
    max_candidates: int
    llm_base_url: str
    llm_api_key: str | None
    llm_model: str | None
    blocklist: tuple[str, ...]
    kinds: tuple[str, ...]
    triage: bool = True
    max_per_bank: int = 5
    min_score: int = 7
    novelty_check: bool = True

    @classmethod
    def from_dict(cls, raw: dict) -> PromotionConfig:
        banks, harvest, anon = raw["banks"], raw["harvest"], raw["anonymize"]
        triage = raw.get("triage") or {}
        return cls(
            customer_prefix=banks["customers"],
            staging_bank=banks["staging"],
            global_bank=banks["global"],
            domains=tuple(harvest.get("domains") or ()),
            include_untagged=bool(harvest.get("include_untagged", True)),
            query=" ".join(harvest["query"].split()),
            budget=harvest.get("budget", "mid"),
            max_candidates=int(harvest.get("max_candidates_per_pass", 10)),
            llm_base_url=anon["base_url"].rstrip("/"),
            llm_api_key=os.environ.get(anon.get("api_key_env", "")) or None,
            llm_model=os.environ.get(anon.get("model_env", "")) or anon.get("model"),
            blocklist=tuple(anon.get("blocklist") or ()),
            kinds=tuple(raw.get("kinds") or ("decision", "pitfall", "pattern", "runbook")),
            triage=bool(triage.get("enabled", True)),
            max_per_bank=int(triage.get("max_per_bank", 5)),
            min_score=int(triage.get("min_score", 7)),
            novelty_check=bool(triage.get("novelty_check", True)),
        )


def load_config(path: str | os.PathLike[str] | None = None) -> PromotionConfig:
    path = path or os.environ.get("PROMOTION_CONFIG") or Path(__file__).parents[2] / "promotion.yaml"
    return PromotionConfig.from_dict(yaml.safe_load(Path(path).read_text()))
