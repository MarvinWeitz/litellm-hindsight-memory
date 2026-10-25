"""Harvest, stage, review and publish: the promotion workflow from the architecture doc.

1. harvest   reflect over each customer bank (per domain) and collect nominated facts
2. anonymize rewrite each candidate generically, drop what can't be generalized or still leaks
3. stage     retain candidates into the staging bank, source customer in metadata only
4. review    a person edits the review file: approve, reject, fix the text, set the SAP release
5. publish   retain approved items into the global bank under a stable topic id, then mark
             the source documents promote:done (or promote:rejected) so they aren't harvested again
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .anonymize import Anonymizer, leaks
from .config import PromotionConfig
from .store import HindsightStore

NOMINATED, DONE, REJECTED = "promote:candidate", "promote:done", "promote:rejected"
STAGED, PUBLISHED = "promote:staged", "promote:published"
DECISIONS = ("pending", "approve", "reject")

REFLECT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "One reusable pattern, pitfall, fix or recipe"},
                    "source_fact_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text"],
            },
        }
    },
    "required": ["candidates"],
}


@dataclass
class Candidate:
    id: str
    source_bank: str
    domain: str | None
    original: str
    source_facts: list[str]
    source_documents: list[str]
    text: str = ""
    kind: str = ""
    topic_id: str = ""
    reason: str = ""


@dataclass
class HarvestResult:
    harvest_id: str
    candidates: list[Candidate] = field(default_factory=list)
    dropped: list[tuple[Candidate, str]] = field(default_factory=list)


def harvest(store: HindsightStore, anonymizer: Anonymizer, cfg: PromotionConfig, *,
            banks: list[str] | None = None, progress: Callable[[str], None] = lambda _: None) -> HarvestResult:
    result = HarvestResult(harvest_id=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    customer_banks = [b for b in store.bank_ids() if b.startswith(cfg.customer_prefix)]
    customers = [b[len(cfg.customer_prefix):] for b in customer_banks]
    for bank in customer_banks:
        if banks and bank not in banks:
            continue
        raw, projects = _collect(store, cfg, bank, progress)
        forbidden = customers + projects + list(cfg.blocklist)
        for i, cand in enumerate(raw, 1):
            progress(f"{bank}  anonymizing {i}/{len(raw)}")
            rw = anonymizer.rewrite(cand.original)
            cand.text, cand.kind, cand.topic_id, cand.reason = rw.text, rw.kind, rw.topic_id, rw.reason
            if not rw.generic:
                result.dropped.append((cand, f"not generic: {rw.reason}"))
            elif found := leaks(rw.text, forbidden):
                result.dropped.append((cand, f"still names {', '.join(found)}"))
            else:
                result.candidates.append(cand)
    return result


def _collect(store: HindsightStore, cfg: PromotionConfig, bank: str,
             progress: Callable[[str], None]) -> tuple[list[Candidate], list[str]]:
    """Reflect per domain, add nominated facts, skip facts already promoted or rejected.

    Also returns the project slugs seen on the source facts: project names are
    customer-specific too (acme-portal), so they must not reach the global bank.
    """
    passes: list[tuple[str | None, list[str] | None]] = [(d, [f"domain:{d}"]) for d in cfg.domains]
    if cfg.include_untagged:
        passes.append((None, None))
    seen: dict[str, Candidate] = {}
    projects: set[str] = set()

    def add(text: str, facts: list[dict], domain: str | None) -> None:
        facts = [f for f in facts if not {DONE, REJECTED} & set(f["tags"])]
        projects.update(t[len("project:"):] for f in facts for t in f["tags"]
                        if t.startswith("project:") and t != "project:_shared")
        key = _key(bank, text)
        if not text.strip() or not facts or key in seen:
            return
        seen[key] = Candidate(
            id=key, source_bank=bank, domain=domain or _domain_of(facts), original=" ".join(text.split()),
            source_facts=sorted({f["id"] for f in facts}),
            source_documents=sorted({f["document_id"] for f in facts if f["document_id"]}),
        )

    for domain, tags in passes:
        label = f"domain:{domain}" if domain else "whole bank"
        # Reflect is an LLM loop and takes seconds to minutes; skip domains with no facts.
        if tags and not store.has_memories(bank, tags):
            progress(f"{bank}  {label}: no facts, skipped")
            continue
        started, before = time.monotonic(), len(seen)
        progress(f"{bank}  {label}: reflecting...")
        out, facts = store.reflect(bank, cfg.query, budget=cfg.budget, schema=REFLECT_SCHEMA,
                                   tags=tags, tags_match="any_strict" if tags else "any")
        by_id = {f["id"]: f for f in facts}
        for item in (out.get("candidates") or [])[: cfg.max_candidates]:
            cited = [by_id[i] for i in item.get("source_fact_ids") or () if i in by_id]
            add(str(item.get("text", "")), cited or facts, domain)
        progress(f"{bank}  {label}: {len(seen) - before} new candidates ({time.monotonic() - started:.0f} s)")
    for fact in store.memories(bank, [NOMINATED]):
        add(fact["text"], [fact], None)
    return list(seen.values()), sorted(projects)


def stage(store: HindsightStore, cfg: PromotionConfig, result: HarvestResult) -> None:
    store.ensure_bank(cfg.staging_bank, "Keep each promotion candidate as one fact, worded as given.")
    for c in result.candidates:
        store.retain(
            cfg.staging_bank, c.text, document_id=f"candidate:{c.id}",
            tags=[STAGED, f"kind:{c.kind}"] + ([f"domain:{c.domain}"] if c.domain else []),
            metadata={"source_bank": c.source_bank, "source_documents": ",".join(c.source_documents),
                      "topic_id": c.topic_id, "harvest_id": result.harvest_id},
            context="Promotion candidate waiting for review",
        )


def write_review(result: HarvestResult, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result.harvest_id}.yaml"
    items = [{
        "id": c.id, "decision": "pending", "sap_release": "",
        "topic_id": c.topic_id, "kind": c.kind, "domain": c.domain, "text": c.text,
        "original": c.original, "why": c.reason,
        "source": {"bank": c.source_bank, "documents": c.source_documents, "facts": c.source_facts},
    } for c in result.candidates]
    header = (
        "# Promotion review. For each item set decision to approve or reject.\n"
        "# Approve needs sap_release (the release you verified it on) and a topic_id like pattern:cap-xsuaa-prod-auth.\n"
        "# Edit text freely; only text, topic_id, kind, domain and sap_release reach the global bank.\n"
        "# 'original' and 'source' hold customer data: this file stays local and is never committed.\n"
    )
    path.write_text(header + yaml.safe_dump({"harvest_id": result.harvest_id, "items": items},
                                            sort_keys=False, allow_unicode=True, width=100))
    return path


@dataclass
class PublishReport:
    published: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def publish(store: HindsightStore, cfg: PromotionConfig, review_file: Path, *, dry_run: bool = False) -> PublishReport:
    data = yaml.safe_load(review_file.read_text()) or {}
    customers = [b[len(cfg.customer_prefix):] for b in store.bank_ids() if b.startswith(cfg.customer_prefix)]
    forbidden = customers + list(cfg.blocklist)
    report = PublishReport()
    for item in data.get("items") or ():
        decision = str(item.get("decision", "pending")).strip().lower()
        if decision not in DECISIONS:
            report.errors.append(f"{item.get('id')}: unknown decision {decision!r}")
        elif decision == "pending":
            report.pending.append(item["id"])
        elif decision == "reject":
            if not dry_run:
                _close(store, cfg, item, REJECTED)
            report.rejected.append(item["id"])
        elif problem := _check(item, cfg, forbidden):
            report.errors.append(f"{item.get('id')}: {problem}")
        else:
            if not dry_run:
                _publish_one(store, cfg, item)
                _close(store, cfg, item, DONE)
            report.published.append(item["topic_id"])
    return report


def _check(item: dict, cfg: PromotionConfig, forbidden: list[str]) -> str | None:
    if not str(item.get("text", "")).strip():
        return "text is empty"
    if not str(item.get("sap_release", "")).strip():
        return "sap_release is missing"
    if not re.match(r"^[a-z]+:[a-z0-9][a-z0-9-]{2,80}$", str(item.get("topic_id", ""))):
        return "topic_id must look like pattern:cap-xsuaa-prod-auth"
    if item.get("kind") not in cfg.kinds:
        return f"kind must be one of {', '.join(cfg.kinds)}"
    if found := leaks(item["text"], forbidden):
        return f"text still names {', '.join(found)}"
    return None


def _publish_one(store: HindsightStore, cfg: PromotionConfig, item: dict) -> None:
    release = str(item["sap_release"]).strip()
    tags = ["source:promotion", f"kind:{item['kind']}"] + ([f"domain:{item['domain']}"] if item.get("domain") else [])
    # The topic id is the document id, so publishing the same topic again updates it.
    store.retain(
        cfg.global_bank, " ".join(str(item["text"]).split()), document_id=item["topic_id"], tags=tags,
        metadata={"sap_release": release, "candidate_id": item["id"],
                  "promoted_at": datetime.now(timezone.utc).date().isoformat()},
        context=f"Reviewed SAP knowledge, verified on {release}",
    )


def _close(store: HindsightStore, cfg: PromotionConfig, item: dict, outcome: str) -> None:
    source = item.get("source") or {}
    for doc in source.get("documents") or ():
        store.retag_document(source["bank"], doc, add=[outcome], remove=[NOMINATED])
    staged = PUBLISHED if outcome == DONE else REJECTED
    store.retag_document(cfg.staging_bank, f"candidate:{item['id']}", add=[staged], remove=[STAGED])


def nominate(store: HindsightStore, bank: str, document_id: str) -> None:
    store.retag_document(bank, document_id, add=[NOMINATED])


def _key(bank: str, text: str) -> str:
    return hashlib.sha256(f"{bank}\x00{' '.join(text.lower().split())}".encode()).hexdigest()[:12]


def _domain_of(facts: list[dict]) -> str | None:
    for f in facts:
        for t in f["tags"]:
            if t.startswith("domain:"):
                return t[len("domain:"):]
    return None


def summary(result: HarvestResult) -> str:
    lines = [f"harvest {result.harvest_id}: {len(result.candidates)} candidates, {len(result.dropped)} dropped"]
    lines += [f"  + [{c.kind}] {c.topic_id or '(no topic)'}: {c.text[:90]}" for c in result.candidates]
    lines += [f"  - {c.original[:70]}  ({why})" for c, why in result.dropped]
    return "\n".join(lines)

