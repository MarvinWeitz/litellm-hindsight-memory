"""Cut a harvest down to a short list worth a person's time.

1. merge    one LLM call per bank groups candidates that say the same thing; a group
            becomes one candidate with the sources of all its members
2. judge    for each anonymized candidate: what the model says about the topic without
            any memory (novelty), and which global items are closest (duplicates); a judge
            call then scores specificity and reusability
3. cap      keep the best max_per_bank per bank; nominated facts are always kept

Nothing is thrown away: filtered candidates go into the review file with the reason,
so a reviewer can still approve one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .config import PromotionConfig
from .llm import LLM

MERGE_PROMPT = """Below are numbered notes harvested from one team's SAP CAP/BTP memory.
Group the notes that state the same fact, fix or advice, even if worded differently.
Notes on related but different points stay in separate groups.

Answer with JSON only: {{"groups": [[<note numbers>], ...]}}

{notes}"""

BASELINE_PROMPT = """You are an SAP CAP/BTP developer. Without looking anything up, say in 3-5 sentences
what you know about this topic: {topic} ({kind}, area: {domain})."""

JUDGE_PROMPT = """You decide whether a note belongs in a knowledge base shared by all SAP CAP/BTP teams.

Note:
{text}

What a general model already says about the topic, with no knowledge base:
{baseline}

Closest items already in the knowledge base:
{existing}

novel: false if the general answer already contains the note's key point.
duplicate_of: the id of an existing item that says the same, or empty.
specificity 1-5: 5 = concrete error message, config key, role name or version; 1 = generic advice.
reusability 1-5: 5 = any team on this stack will hit it; 1 = rare edge case.

Answer with JSON only:
{{"novel": true|false, "duplicate_of": "", "specificity": 1, "reusability": 1, "reason": "<one sentence>"}}"""


@dataclass
class Verdict:
    novel: bool = True
    duplicate_of: str = ""
    specificity: int = 3
    reusability: int = 3
    reason: str = ""


def merge(llm: LLM, candidates: list, progress: Callable[[str], None] = lambda _: None) -> list:
    """Group duplicates; each group keeps its first member, with every member's sources."""
    if len(candidates) < 2:
        return list(candidates)
    notes = "\n".join(f"{i}. {c.original}" for i, c in enumerate(candidates, 1))
    data = llm.json(MERGE_PROMPT.format(notes=notes)) or {}
    used: set[int] = set()
    merged = []
    for group in data.get("groups") or ():
        members = [candidates[i - 1] for i in _ints(group) if 1 <= i <= len(candidates) and i not in used]
        used.update(i for i in _ints(group) if 1 <= i <= len(candidates))
        if members:
            merged.append(_combine(members))
    # anything the model left out stays as it was
    merged += [c for i, c in enumerate(candidates, 1) if i not in used]
    if len(merged) < len(candidates):
        progress(f"merged {len(candidates)} candidates into {len(merged)}")
    return merged


def judge(llm: LLM, store, cfg: PromotionConfig, cand, *, global_ready: bool) -> Verdict:
    baseline = ""
    if cfg.novelty_check:
        baseline = llm.text(BASELINE_PROMPT.format(topic=cand.topic_id or cand.text[:80], kind=cand.kind,
                                                   domain=cand.domain or "general"))
    existing = store.recall(cfg.global_bank, cand.text) if global_ready else []
    data = llm.json(JUDGE_PROMPT.format(
        text=cand.text, baseline=baseline.strip() or "(not checked)",
        existing="\n".join(f"- [{e['document_id']}] {e['text']}" for e in existing) or "(none)",
    ))
    if data is None:
        return Verdict(reason="judge answer was not JSON")
    known = {e["document_id"] for e in existing if e["document_id"]}
    dup = str(data.get("duplicate_of") or "").strip().strip("[]")
    return Verdict(
        novel=bool(data.get("novel", True)) if cfg.novelty_check else True,
        duplicate_of=dup if dup in known else "",  # only trust ids the model was actually shown
        specificity=_score(data.get("specificity")),
        reusability=_score(data.get("reusability")),
        reason=str(data.get("reason", "")),
    )


def score(verdict: Verdict, evidence: int) -> int:
    """Specificity and reusability (1-5 each) plus up to 3 for independent sources."""
    return verdict.specificity + verdict.reusability + min(evidence, 3)


def shortlist(cfg: PromotionConfig, judged: list[tuple[object, Verdict]]) -> tuple[list, list[tuple[object, str]]]:
    """Split one bank's judged candidates into kept and filtered (with the reason)."""
    keep, filtered, ranked = [], [], []
    for cand, v in judged:
        if cand.nominated:
            keep.append(cand)
        elif v.duplicate_of:
            filtered.append((cand, f"already in {cfg.global_bank} as {v.duplicate_of}: {v.reason}"))
        elif not v.novel:
            filtered.append((cand, f"the model already knows this: {v.reason}"))
        elif cand.score < cfg.min_score:
            filtered.append((cand, f"score {cand.score} below {cfg.min_score}: {v.reason}"))
        else:
            ranked.append(cand)
    ranked.sort(key=lambda c: -c.score)
    room = max(cfg.max_per_bank - len(keep), 0)
    keep += ranked[:room]
    filtered += [(c, f"below the top {cfg.max_per_bank} for this bank (score {c.score})") for c in ranked[room:]]
    return keep, filtered


def _combine(members: list):
    first = members[0]
    for other in members[1:]:
        first.source_facts = sorted(set(first.source_facts) | set(other.source_facts))
        first.source_documents = sorted(set(first.source_documents) | set(other.source_documents))
        first.nominated = first.nominated or other.nominated
        first.domain = first.domain or other.domain
    return first


def _ints(group) -> list[int]:
    out = []
    for x in group if isinstance(group, list) else ():
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


def _score(value) -> int:
    try:
        return min(max(int(value), 1), 5)
    except (TypeError, ValueError):
        return 3
