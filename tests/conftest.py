from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from litellm_hindsight.config import RoutingConfig

ROUTING_FILE = Path(__file__).parents[1] / "config" / "hindsight_routing.yaml"
TEST_ENV = {"HINDSIGHT_API_URL": "http://hindsight.test"}


def routing_dict(**overrides: Any) -> dict[str, Any]:
    """The repo's routing file as a dict, with top-level sections merged from overrides."""
    raw = copy.deepcopy(yaml.safe_load(ROUTING_FILE.read_text())["hindsight_routing"])
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict) and key != "allowed":
            raw[key] = {**raw[key], **value}
        else:
            raw[key] = value
    return raw


def make_config(**overrides: Any) -> RoutingConfig:
    return RoutingConfig.from_dict(routing_dict(**overrides), env=TEST_ENV)


@dataclass
class FakeHindsight:
    """Stands in for hindsight_client.Hindsight: canned facts per bank, records calls."""

    facts: dict[str, list[tuple[str, list[str]]]] = field(default_factory=dict)
    recalls: list[dict[str, Any]] = field(default_factory=list)
    retains: list[dict[str, Any]] = field(default_factory=list)
    delay: dict[str, float] = field(default_factory=dict)
    ignore_tag_filter: bool = False  # simulates a server returning out-of-scope memories

    async def arecall(self, bank_id: str, query: str, **kwargs: Any):
        import asyncio

        self.recalls.append({"bank_id": bank_id, "query": query, **kwargs})
        if bank_id in self.delay:
            await asyncio.sleep(self.delay[bank_id])
        wanted = set(kwargs.get("tags") or ())
        results = []
        for text, tags in self.facts.get(bank_id, []):
            if wanted and not wanted.intersection(tags) and not self.ignore_tag_filter:
                continue  # mimics any_strict: untagged facts never match
            results.append(SimpleNamespace(text=text, tags=tags))
        return SimpleNamespace(results=results)

    async def aretain(self, bank_id: str, content: str, **kwargs: Any):
        self.retains.append({"bank_id": bank_id, "content": content, **kwargs})
        return SimpleNamespace(success=True)


@pytest.fixture
def fake_hindsight() -> FakeHindsight:
    return FakeHindsight(
        facts={
            "customer::acme": [
                ("Acme portal uses XSUAA role collections per tenant", ["project:acme-portal"]),
                ("Acme S/4 is on 2023 FPS02", ["project:_shared"]),
                ("Acme S/4 extension calls API_BUSINESS_PARTNER", ["project:acme-s4-extension"]),
            ],
            "customer::globex": [("Globex uses Azure AD via IAS", ["project:_shared"])],
            "global::sap": [("CAP: set cds.requires.auth.kind=xsuaa for BTP", ["domain:cap"])],
        }
    )


@pytest.fixture
def config() -> RoutingConfig:
    return make_config(allowed={"acme": ["acme-portal", "acme-s4-extension"], "globex": None})
