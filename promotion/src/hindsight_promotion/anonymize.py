"""Rewrite a candidate in generic terms, then check the result mechanically.

The LLM does the rewriting; the blocklist check runs afterwards on its output
and drops anything that still names a customer, so a model mistake can't
carry a customer detail into the global bank on its own. Human review is the
last line of defense.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .llm import LLM, parse_json

PROMPT = """You prepare {subject} knowledge for a knowledge base shared across all customers.

Rewrite the candidate below so it is generic: remove customer and company names, system IDs,
Cloud Connector location IDs, hostnames, subaccount and space names, people, and business data.
Keep the technical substance: products, versions, APIs, error messages, configuration keys.
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
    def __init__(self, llm: LLM, kinds: tuple[str, ...], subject: str):
        self.llm, self.kinds, self.subject = llm, kinds, subject

    def rewrite(self, text: str) -> Rewrite:
        prompt = PROMPT.format(text=text, kinds=list(self.kinds), subject=self.subject)
        return parse(self.llm.text(prompt, json_mode=True), self.kinds)


def parse(raw: str, kinds: tuple[str, ...]) -> Rewrite:
    """Read the model's JSON; anything malformed counts as not generic."""
    data = parse_json(raw)
    if data is None:
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
