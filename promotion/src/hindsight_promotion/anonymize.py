"""Rewrite a candidate in generic terms, then check the result mechanically.

The LLM does the rewriting; the blocklist check runs afterwards on its output
and drops anything that still names a customer, so a model mistake can't
carry a customer detail into the global bank on its own. Human review is the
last line of defense.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

PROMPT = """You prepare SAP CAP/BTP knowledge for a knowledge base shared across all customers.

Rewrite the candidate below so it is generic: remove customer and company names, system IDs,
Cloud Connector location IDs, hostnames, subaccount and space names, people, and business data.
Keep the technical substance: SAP products, versions, APIs, error messages, configuration keys.
If the candidate is only true for one customer's landscape, or cannot be made generic without
losing its point, set "generic" to false.

Answer with JSON only:
{{"generic": true|false, "text": "<rewritten candidate, 1-4 sentences>",
  "kind": one of {kinds}, "topic_id": "<kind>:<short-kebab-case-topic>", "reason": "<one sentence>"}}

Candidate:
{text}"""

_TOPIC = re.compile(r"^[a-z]+:[a-z0-9][a-z0-9-]{2,80}$")


@dataclass(frozen=True)
class Rewrite:
    generic: bool
    text: str
    kind: str
    topic_id: str
    reason: str


class Anonymizer:
    def __init__(self, base_url: str, model: str, api_key: str | None, kinds: tuple[str, ...]):
        if not model:
            raise ValueError("no model for anonymization: set HINDSIGHT_LLM_MODEL or anonymize.model")
        self.model, self.kinds = model, kinds
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.Client(base_url=base_url, headers=headers, timeout=120)

    def rewrite(self, text: str) -> Rewrite:
        r = self.http.post("/chat/completions", json={
            "model": self.model,
            "messages": [{"role": "user", "content": PROMPT.format(text=text, kinds=list(self.kinds))}],
            "response_format": {"type": "json_object"},
        })
        r.raise_for_status()
        return parse(r.json()["choices"][0]["message"]["content"], self.kinds)


def parse(raw: str, kinds: tuple[str, ...]) -> Rewrite:
    """Read the model's JSON; anything malformed counts as not generic."""
    try:
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except ValueError:
        return Rewrite(False, "", "", "", "model answer was not JSON")
    kind = str(data.get("kind", "")).strip()
    topic = str(data.get("topic_id", "")).strip().lower()
    if kind not in kinds:
        kind = kinds[0]
    if not _TOPIC.match(topic):
        topic = ""
    return Rewrite(
        generic=bool(data.get("generic")) and bool(str(data.get("text", "")).strip()),
        text=" ".join(str(data.get("text", "")).split()),
        kind=kind,
        topic_id=topic,
        reason=str(data.get("reason", "")),
    )


def leaks(text: str, forbidden: list[str]) -> list[str]:
    """Return the forbidden words found in text, matched as whole words, ignoring case."""
    found = []
    for word in forbidden:
        if word and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.IGNORECASE):
            found.append(word)
    return found
