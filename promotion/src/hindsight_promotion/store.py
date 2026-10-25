"""The few Hindsight operations the promotion job needs.

Reflect, retain and memory listing go through hindsight-client. Document tags
and the bank list use the HTTP API directly, because the client has no helper
for them.
"""

from __future__ import annotations

import httpx
from hindsight_client import Hindsight


class HindsightStore:
    def __init__(self, base_url: str, api_key: str | None = None):
        self.client = Hindsight(base_url=base_url, api_key=api_key)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.Client(base_url=base_url.rstrip("/") + "/v1/default", headers=headers, timeout=60)

    def close(self) -> None:
        self.client.close()
        self.http.close()

    def bank_ids(self) -> list[str]:
        r = self.http.get("/banks", params={"limit": 1000})
        r.raise_for_status()
        return [b["bank_id"] for b in r.json()["banks"]]

    def ensure_bank(self, bank_id: str, mission: str) -> None:
        if bank_id not in self.bank_ids():
            self.client.create_bank(bank_id, name=bank_id, retain_mission=mission)

    def reflect(self, bank_id: str, query: str, *, budget: str, schema: dict,
                tags: list[str] | None, tags_match: str) -> tuple[dict, list[dict]]:
        """Return the structured answer and the facts it was based on."""
        r = self.client.reflect(
            bank_id, query, budget=budget, response_schema=schema, include_facts=True,
            tags=tags, tags_match=tags_match,
        )
        facts = [
            {"id": f.id, "text": f.text, "tags": list(f.tags or ()), "document_id": f.document_id}
            for f in ((r.based_on.memories if r.based_on else None) or [])
        ]
        return r.structured_output or {}, facts

    def has_memories(self, bank_id: str, tags: list[str]) -> bool:
        return self.client.list_memories(bank_id, tags=tags, tags_match="any_strict", limit=1).total > 0

    def memories(self, bank_id: str, tags: list[str]) -> list[dict]:
        out, offset = [], 0
        while True:
            page = self.client.list_memories(bank_id, tags=tags, tags_match="any_strict", limit=100, offset=offset)
            out += [{"id": m.id, "text": m.text or "", "tags": list(m.tags or ()), "document_id": m.document_id}
                    for m in page.items]
            offset += len(page.items)
            if not page.items or offset >= page.total:
                return out

    def retain(self, bank_id: str, text: str, *, document_id: str, tags: list[str],
               metadata: dict[str, str], context: str) -> None:
        self.client.retain(bank_id, text, document_id=document_id, tags=tags, metadata=metadata, context=context,
                           retain_async=True)  # extraction runs in Hindsight; do not wait per item

    def document_tags(self, bank_id: str, document_id: str) -> list[str]:
        r = self.http.get(f"/banks/{bank_id}/documents/{document_id}")
        r.raise_for_status()
        return list(r.json().get("tags") or ())

    def set_document_tags(self, bank_id: str, document_id: str, tags: list[str]) -> None:
        r = self.http.patch(f"/banks/{bank_id}/documents/{document_id}", json={"tags": tags})
        r.raise_for_status()

    def retag_document(self, bank_id: str, document_id: str, add: list[str], remove: list[str] = ()) -> None:
        tags = [t for t in self.document_tags(bank_id, document_id) if t not in remove]
        self.set_document_tags(bank_id, document_id, tags + [t for t in add if t not in tags])
