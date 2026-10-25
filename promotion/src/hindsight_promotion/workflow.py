"""Harvest, stage, review and publish: the promotion workflow from the architecture doc.

1. harvest   reflect over each customer bank (per domain) and collect nominated facts
2. anonymize rewrite each candidate generically, drop what can't be generalized or still leaks
   triage    merge duplicates, score, keep a short list per bank (see triage.py)
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

from . import triage
from .anonymize import Anonymizer, leaks
from .config import PromotionConfig
from .llm import LLM
from .store import HindsightStore

DECISIONS = ("pending", "filtered", "approve", "reject")

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
    nominated: bool = False
    score: int = 0
    triage_note: str = ""


@dataclass
class HarvestResult:
    harvest_id: str
    candidates: list[Candidate] = field(default_factory=list)
    filtered: list[tuple[Candidate, str]] = field(default_factory=list)  # reviewable, with the reason
    dropped: list[tuple[Candidate, str]] = field(default_factory=list)   # never reach review: leaks, not generic


def harvest(store: HindsightStore, anonymizer: Anonymizer, cfg: PromotionConfig, *, llm: LLM | None = None,
            banks: list[str] | None = None, progress: Callable[[str], None] = lambda _: None) -> HarvestResult:
    """Collect and anonymize candidates; with an llm and triage enabled, also merge, score and cap them."""
    result = HarvestResult(harvest_id=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    customer_banks = [b for b in store.bank_ids() if b.startswith(cfg.customer_prefix)]
    customers = [b[len(cfg.customer_prefix):] for b in customer_banks]
    use_triage = cfg.triage and llm is not None
    global_ready = use_triage and cfg.global_bank in store.bank_ids()
    for bank in customer_banks:
        if banks and bank not in banks:
            continue
        raw, projects = _collect(store, cfg, bank, progress)
        if use_triage:
            raw = triage.merge(llm, raw, cfg.subject, lambda msg, bank=bank: progress(f"{bank}  {msg}"))
        forbidden = customers + projects + list(cfg.blocklist)
        kept = []
        for i, cand in enumerate(raw, 1):
            progress(f"{bank}  anonymizing {i}/{len(raw)}")
            rw = anonymizer.rewrite(cand.original)
            cand.text, cand.kind, cand.topic_id, cand.reason = rw.text, rw.kind, rw.topic_id, rw.reason
            if not rw.generic:
                result.dropped.append((cand, f"not generic: {rw.reason}"))
            elif found := leaks(rw.text, forbidden):
                result.dropped.append((cand, f"still names {', '.join(found)}"))
            else:
                kept.append(cand)
        if not use_triage:
            result.candidates += kept
            continue
        judged = []
        for i, cand in enumerate(kept, 1):
            progress(f"{bank}  scoring {i}/{len(kept)}")
            verdict = triage.judge(llm, store, cfg, cand, global_ready=global_ready)
            cand.score = triage.score(verdict, len(cand.source_documents))
            cand.triage_note = verdict.reason
            judged.append((cand, verdict))
        shortlist, filtered = triage.shortlist(cfg, judged)
        result.candidates += shortlist
        result.filtered += filtered
        progress(f"{bank}  {len(shortlist)} for review, {len(filtered)} filtered")
    return result


def _collect(store: HindsightStore, cfg: PromotionConfig, bank: str,
             progress: Callable[[str], None]) -> tuple[list[Candidate], list[str]]:
    """Reflect per domain, add nominated facts, skip facts already promoted or rejected.

    Also returns the project slugs seen on the source facts: project names are
    customer-specific too (acme-portal), so they must not reach the global bank.
    """
    life, scope = cfg.lifecycle, cfg.scope_prefix
    shared = f"{scope}{cfg.shared_scope}" if cfg.shared_scope is not None else None
    passes: list[tuple[str | None, list[str] | None]] = [(d, [f"{cfg.domain_prefix}{d}"]) for d in cfg.domains]
    if cfg.include_untagged:
        passes.append((None, None))
    seen: dict[str, Candidate] = {}
    projects: set[str] = set()

    def add(text: str, facts: list[dict], domain: str | None, nominated: bool = False) -> None:
        facts = [f for f in facts if not {life.done, life.rejected} & set(f["tags"])]
        projects.update(t[len(scope):] for f in facts for t in f["tags"]
                        if t.startswith(scope) and t != shared)
        key = _key(bank, text)
        if key in seen:
            seen[key].nominated |= nominated
        if not text.strip() or not facts or key in seen:
            return
        seen[key] = Candidate(
            id=key, source_bank=bank, domain=domain or _domain_of(facts, cfg.domain_prefix), original=" ".join(text.split()),
            source_facts=sorted({f["id"] for f in facts}),
            source_documents=sorted({f["document_id"] for f in facts if f["document_id"]}),
            nominated=nominated,
        )

    for domain, tags in passes:
        label = f"{cfg.domain_prefix}{domain}" if domain else "whole bank"
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
    for fact in store.memories(bank, [life.nominated]):
        add(fact["text"], [fact], None, nominated=True)
    return list(seen.values()), sorted(projects)


def stage(store: HindsightStore, cfg: PromotionConfig, result: HarvestResult) -> None:
    store.ensure_bank(cfg.staging_bank, cfg.staging_mission)
    life = cfg.lifecycle
    # Filtered candidates are staged too, so a reviewer who rescues one can close its loop.
    for c, status in [(c, life.staged) for c in result.candidates] + [(c, life.filtered) for c, _ in result.filtered]:
        store.retain(
            cfg.staging_bank, c.text, document_id=f"candidate:{c.id}",
            tags=[status, f"{cfg.kind_prefix}{c.kind}"] + ([f"{cfg.domain_prefix}{c.domain}"] if c.domain else []),
            metadata={"source_bank": c.source_bank, "source_documents": ",".join(c.source_documents),
                      "topic_id": c.topic_id, "harvest_id": result.harvest_id},
            context=cfg.staging_context,
        )


def write_review(result: HarvestResult, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result.harvest_id}.yaml"
    def entry(c: Candidate, decision: str, **extra) -> dict:
        return {
            "id": c.id, "decision": decision, "sap_release": "",
            "topic_id": c.topic_id, "kind": c.kind, "domain": c.domain, "text": c.text, **extra,
            "original": c.original, "why": c.reason,
            "source": {"bank": c.source_bank, "documents": c.source_documents, "facts": c.source_facts},
        }

    items = [entry(c, "pending", score=c.score, nominated=c.nominated, assessment=c.triage_note)
             for c in result.candidates]
    filtered = [entry(c, "filtered", score=c.score, filtered_because=why) for c, why in result.filtered]
    header = (
        "# Promotion review. For each item set decision to approve or reject.\n"
        "# 'items' is the short list. 'filtered' holds what triage set aside, with the reason; leave those\n"
        "# alone, or set decision approve on one to publish it anyway.\n"
        "# Approve needs sap_release (the release you verified it on) and a topic_id like pattern:cap-xsuaa-prod-auth.\n"
        "# Edit text freely; only text, topic_id, kind, domain and sap_release reach the global bank.\n"
        "# 'original' and 'source' hold customer data: this file stays local and is never committed.\n"
    )
    path.write_text(header + yaml.safe_dump({"harvest_id": result.harvest_id, "items": items, "filtered": filtered},
                                            sort_keys=False, allow_unicode=True, width=100))
    return path


@dataclass
class PublishReport:
    published: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def publish(store: HindsightStore, cfg: PromotionConfig, review_file: Path, *, dry_run: bool = False) -> PublishReport:
    data = yaml.safe_load(review_file.read_text()) or {}
    customers = [b[len(cfg.customer_prefix):] for b in store.bank_ids() if b.startswith(cfg.customer_prefix)]
    forbidden = customers + list(cfg.blocklist)
    report = PublishReport()
    for item in [*(data.get("items") or ()), *(data.get("filtered") or ())]:
        decision = str(item.get("decision", "pending")).strip().lower()
        if decision not in DECISIONS:
            report.errors.append(f"{item.get('id')}: unknown decision {decision!r}")
        elif decision == "pending":
            report.pending.append(item["id"])
        elif decision == "filtered":
            report.skipped.append(item["id"])
        elif decision == "reject":
            if not dry_run:
                _close(store, cfg, item, cfg.lifecycle.rejected)
            report.rejected.append(item["id"])
        elif problem := _check(item, cfg, forbidden):
            report.errors.append(f"{item.get('id')}: {problem}")
        else:
            if not dry_run:
                _publish_one(store, cfg, item)
                _close(store, cfg, item, cfg.lifecycle.done)
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
    tags = [*cfg.published_tags, f"{cfg.kind_prefix}{item['kind']}"]
    tags += [f"{cfg.domain_prefix}{item['domain']}"] if item.get("domain") else []
    # The topic id is the document id, so publishing the same topic again updates it.
    store.retain(
        cfg.global_bank, " ".join(str(item["text"]).split()), document_id=item["topic_id"], tags=tags,
        metadata={"sap_release": release, "candidate_id": item["id"],
                  "promoted_at": datetime.now(timezone.utc).date().isoformat()},
        context=cfg.published_context.format(release=release),
    )


def _close(store: HindsightStore, cfg: PromotionConfig, item: dict, outcome: str) -> None:
    life, source = cfg.lifecycle, item.get("source") or {}
    for doc in source.get("documents") or ():
        store.retag_document(source["bank"], doc, add=[outcome], remove=[life.nominated])
    staged = life.published if outcome == life.done else life.rejected
    store.retag_document(cfg.staging_bank, f"candidate:{item['id']}", add=[staged],
                         remove=[life.staged, life.filtered])


def nominate(store: HindsightStore, cfg: PromotionConfig, bank: str, document_id: str) -> None:
    store.retag_document(bank, document_id, add=[cfg.lifecycle.nominated])


def _key(bank: str, text: str) -> str:
    return hashlib.sha256(f"{bank}\x00{' '.join(text.lower().split())}".encode()).hexdigest()[:12]


def _domain_of(facts: list[dict], prefix: str) -> str | None:
    for f in facts:
        for t in f["tags"]:
            if t.startswith(prefix):
                return t[len(prefix):]
    return None


def summary(result: HarvestResult) -> str:
    lines = [(f"harvest {result.harvest_id}: {len(result.candidates)} for review, {len(result.filtered)} filtered, "
              f"{len(result.dropped)} dropped")]
    lines += [f"  + [{c.kind}] {c.topic_id or '(no topic)'}{f' score {c.score}' if c.score else ''}: {c.text[:90]}"
              for c in result.candidates]
    lines += [f"  ~ {c.topic_id or c.text[:40]}  ({why[:90]})" for c, why in result.filtered]
    lines += [f"  - {c.original[:70]}  ({why})" for c, why in result.dropped]
    return "\n".join(lines)

