from __future__ import annotations

from dataclasses import replace

import yaml
from hindsight_promotion.anonymize import Rewrite
from hindsight_promotion.triage import Verdict, merge, shortlist
from hindsight_promotion.workflow import (
    Candidate,
    harvest,
    publish,
    stage,
    write_review,
)
from test_workflow import CFG

FACTS = [
    {"id": f"f{i}", "text": t, "tags": ["domain:cap"], "document_id": f"turn:{i}"}
    for i, t in enumerate([
        "Grant role collection SAP_BR_BUPA_MASTER_SPECIALIST or the BP API returns 403.",
        "The business partner API answers 403 without SAP_BR_BUPA_MASTER_SPECIALIST.",
        "Always write tests for your CAP services.",
        "Use cds watch for local development.",
        "Set cds.requires.auth.kind to xsuaa in production.",
    ])
]
TOPICS = {0: "pitfall:bp-api-403-role", 2: "pattern:cap-write-tests", 3: "pattern:cds-watch",
          4: "pattern:cap-xsuaa-production"}


class Store:
    def __init__(self, banks=("customer::acme", "global::sap")):
        self.banks, self.retained, self.doc_tags = list(banks), [], {}

    def bank_ids(self):
        return list(self.banks)

    def ensure_bank(self, bank_id, mission):
        pass

    def has_memories(self, bank_id, tags):
        return True

    def reflect(self, bank_id, query, *, budget, schema, tags, tags_match):
        if tags != ["domain:cap"]:
            return {"candidates": []}, []
        return {"candidates": [{"text": f["text"], "source_fact_ids": [f["id"]]} for f in FACTS]}, FACTS

    def memories(self, bank_id, tags):
        return []

    def recall(self, bank_id, query, *, limit=3):
        assert bank_id == "global::sap"
        if "xsuaa" in query:
            return [{"id": "g1", "text": "CAP apps use xsuaa auth in production.", "document_id": "pattern:cap-auth"}]
        return []

    def retain(self, bank_id, text, *, document_id, tags, metadata, context):
        self.retained.append({"bank": bank_id, "document_id": document_id, "tags": tags})

    def retag_document(self, bank_id, document_id, add, remove=()):
        tags = [t for t in self.doc_tags.get((bank_id, document_id), []) if t not in remove]
        self.doc_tags[(bank_id, document_id)] = tags + [t for t in add if t not in tags]


class Anon:
    def rewrite(self, text):
        i = next(i for i, f in enumerate(FACTS) if f["text"] == text)
        return Rewrite(True, text, TOPICS[i].split(":")[0], TOPICS[i], "generic")


class LLM:
    """Groups the two 403 notes, knows the generic advice already, scores the role fix highest."""

    def __init__(self):
        self.prompts = []

    def text(self, prompt, *, json_mode=False):
        self.prompts.append(prompt)
        if "cds-watch" in prompt or "write-tests" in prompt:
            return "Use cds watch locally and write tests for your services."
        return "I don't know the details."

    def json(self, prompt):
        self.prompts.append(prompt)
        if "Group the notes" in prompt:
            return {"groups": [[1, 2], [3], [4]]}  # note 5 left out on purpose
        note = prompt.split("Note:\n")[1].split("\n\n")[0]
        if "cds watch" in note or "write tests" in note:
            return {"novel": False, "duplicate_of": "", "specificity": 2, "reusability": 5, "reason": "common knowledge"}
        if "xsuaa" in note:
            return {"novel": True, "duplicate_of": "[pattern:cap-auth]", "specificity": 4, "reusability": 5,
                    "reason": "same as the existing auth item"}
        return {"novel": True, "duplicate_of": "made-up-id", "specificity": 5, "reusability": 4, "reason": "concrete role"}


def test_harvest_shortlists_and_explains_the_rest():
    llm = LLM()
    result = harvest(Store(), Anon(), CFG, llm=llm)
    assert [c.topic_id for c in result.candidates] == ["pitfall:bp-api-403-role"]
    top = result.candidates[0]
    assert top.source_documents == ["turn:0", "turn:1"]  # the duplicate was merged in
    assert top.score == 5 + 4 + 2
    why = {c.topic_id: w for c, w in result.filtered}
    assert why["pattern:cds-watch"].startswith("the model already knows")
    assert why["pattern:cap-write-tests"].startswith("the model already knows")
    assert why["pattern:cap-xsuaa-production"].startswith("already in global::sap as pattern:cap-auth")
    # the baseline is asked about the topic id, not the anonymized text
    assert not any("SAP_BR_BUPA" in p for p in llm.prompts if p.startswith("You are an SAP"))


def test_merge_keeps_notes_the_model_left_out_and_ignores_bad_indexes():
    cands = [Candidate(str(i), "customer::acme", None, f"note {i}", [f"f{i}"], [f"d{i}"]) for i in range(3)]

    class L:
        def json(self, prompt):
            return {"groups": [[1, 3, "x", 99], [3]]}

    out = merge(L(), cands)
    assert [c.id for c in out] == ["0", "1"]
    assert out[0].source_documents == ["d0", "d2"]


def test_shortlist_caps_per_bank_but_keeps_nominated():
    cfg = replace(CFG, max_per_bank=2, min_score=7)
    cands = [Candidate(str(i), "customer::acme", None, "", [], [], score=s) for i, s in enumerate([8, 12, 10, 6])]
    nominated = Candidate("n", "customer::acme", None, "", [], [], score=3, nominated=True)
    keep, filtered = shortlist(cfg, [(c, Verdict()) for c in [*cands, nominated]])
    assert [c.id for c in keep] == ["n", "1"]
    assert {c.id: w.split(" ")[0] for c, w in filtered} == {"3": "score", "2": "below", "0": "below"}


def test_filtered_items_are_staged_and_can_be_rescued(tmp_path):
    store = Store()
    result = harvest(store, Anon(), CFG, llm=LLM())
    stage(store, CFG, result)
    assert sorted(r["tags"][0] for r in store.retained) == ["promote:filtered"] * 3 + ["promote:staged"]

    path = write_review(result, tmp_path)
    data = yaml.safe_load(path.read_text())
    assert data["filtered"][0]["decision"] == "filtered" and data["filtered"][0]["filtered_because"]
    rescued = next(i for i in data["filtered"] if i["topic_id"] == "pattern:cds-watch")
    rescued.update(decision="approve", sap_release="CAP 9")
    path.write_text(yaml.safe_dump(data))

    report = publish(store, CFG, path)
    assert report.published == ["pattern:cds-watch"]
    assert len(report.pending) == 1 and len(report.skipped) == 2
    assert store.doc_tags[("staging::promotion", f"candidate:{rescued['id']}")] == ["promote:published"]


def test_without_a_global_bank_nothing_is_recalled():
    result = harvest(Store(banks=["customer::acme"]), Anon(), CFG, llm=LLM())
    assert "pattern:cap-xsuaa-production" in {c.topic_id for c in result.candidates}
