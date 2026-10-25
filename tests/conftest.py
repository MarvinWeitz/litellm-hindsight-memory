from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from litellm_hindsight.config import RoutingConfig


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
    return RoutingConfig.from_dict({"allowed": {"acme": ["acme-portal", "acme-s4-extension"], "globex": None}})
