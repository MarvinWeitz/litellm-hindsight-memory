from __future__ import annotations

from pathlib import Path

import yaml

from hindsight_promotion.anonymize import Rewrite, leaks, parse
from hindsight_promotion.config import load_config
from hindsight_promotion.workflow import harvest, nominate, publish, stage, write_review

CFG = load_config(Path(__file__).parents[1] / "promotion.yaml")

PORTAL_FACT = {"id": "f1", "text": "Acme portal: CAP must use cds.requires.auth.kind xsuaa in production.",
               "tags": ["project:acme-portal", "domain:cap"], "document_id": "turn:a"}
LANDSCAPE_FACT = {"id": "f2", "text": "Acme runs S/4HANA 2023 via Cloud Connector location ACME-DC1.",
                  "tags": ["project:_shared", "domain:s4-integration"], "document_id": "seed:b"}
DONE_FACT = {"id": "f3", "text": "Old fact", "tags": ["domain:cap", "promote:done"], "document_id": "seed:c"}


class FakeStore:
    def __init__(self):
        self.banks = ["customer::acme", "global::sap"]
        self.retained: list[dict] = []
        self.doc_tags: dict[tuple[str, str], list[str]] = {("customer::acme", "turn:a"): ["project:acme-portal"]}
        self.nominated = [{"id": "f4", "text": "Destinations need ProxyType OnPremise for Cloud Connector.",
                           "tags": ["promote:candidate", "domain:destinations"], "document_id": "turn:n"}]

    def bank_ids(self):
        return list(self.banks)

    def ensure_bank(self, bank_id, mission):
        if bank_id not in self.banks:
            self.banks.append(bank_id)

    def reflect(self, bank_id, query, *, budget, schema, tags, tags_match):
        if tags == ["domain:cap"]:
            return {"candidates": [{"text": PORTAL_FACT["text"], "source_fact_ids": ["f1"]},
                                   {"text": DONE_FACT["text"], "source_fact_ids": ["f3"]}]}, [PORTAL_FACT, DONE_FACT]
        if tags is None:
            return {"candidates": [{"text": LANDSCAPE_FACT["text"]}, {"text": PORTAL_FACT["text"]}]}, [LANDSCAPE_FACT, PORTAL_FACT]
        return {"candidates": []}, []

    def memories(self, bank_id, tags):
        return self.nominated if tags == ["promote:candidate"] else []

    def retain(self, bank_id, text, *, document_id, tags, metadata, context):
        self.retained.append({"bank": bank_id, "text": text, "document_id": document_id, "tags": tags,
                              "metadata": metadata})

    def retag_document(self, bank_id, document_id, add, remove=()):
        tags = [t for t in self.doc_tags.get((bank_id, document_id), []) if t not in remove]
        self.doc_tags[(bank_id, document_id)] = tags + [t for t in add if t not in tags]


class FakeAnonymizer:
    """Generalizes known texts; leaves the landscape fact customer-specific."""

    def rewrite(self, text):
        if "ACME-DC1" in text:
            return Rewrite(False, "", "decision", "", "only true for this customer's landscape")
        if "ProxyType" in text:
            return Rewrite(True, "On-premise destinations need ProxyType OnPremise via Cloud Connector.",
                           "pattern", "pattern:onprem-destination-proxytype", "generic")
        if "Acme portal" in text:
            # A sloppy rewrite that keeps the project name: the leak check must catch it.
            return Rewrite(True, "In acme-portal and any CAP app, use xsuaa auth in production.",
                           "pattern", "pattern:cap-xsuaa-production", "generic")
        return Rewrite(True, text, "pattern", "pattern:x-y", "")


def test_harvest_anonymizes_dedupes_and_drops_leaks():
    result = harvest(FakeStore(), FakeAnonymizer(), CFG)
    kept = {c.topic_id for c in result.candidates}
    dropped = {c.original: why for c, why in result.dropped}
    assert kept == {"pattern:onprem-destination-proxytype"}
    assert "acme-portal" in dropped[PORTAL_FACT["text"]]  # project slug caught after rewriting
    assert dropped[LANDSCAPE_FACT["text"]].startswith("not generic")
    assert all("Old fact" not in c.original for c, _ in result.dropped)  # promote:done is skipped
    assert len(result.dropped) == 2  # the portal fact came from two passes but counts once


def test_stage_keeps_customer_in_metadata_only(tmp_path):
    store = FakeStore()
    result = harvest(store, FakeAnonymizer(), CFG)
    stage(store, CFG, result)
    staged = store.retained[0]
    assert staged["bank"] == "staging::promotion"
    assert staged["metadata"]["source_bank"] == "customer::acme"
    assert not any("acme" in t for t in staged["tags"])
    assert "promote:staged" in staged["tags"]


def _review(tmp_path, store, **changes):
    result = harvest(store, FakeAnonymizer(), CFG)
    path = write_review(result, tmp_path)
    data = yaml.safe_load(path.read_text())
    data["items"][0].update(changes)
    path.write_text(yaml.safe_dump(data))
    return path


def test_publish_requires_release_and_writes_global_by_topic(tmp_path):
    store = FakeStore()
    path = _review(tmp_path, store, decision="approve")
    report = publish(store, CFG, path)
    assert report.errors and "sap_release" in report.errors[0]
    assert not store.retained

    path = _review(tmp_path, store, decision="approve", sap_release="CAP 9, BTP Connectivity 2026-10")
    report = publish(store, CFG, path)
    assert report.published == ["pattern:onprem-destination-proxytype"]
    out = store.retained[-1]
    assert out["bank"] == "global::sap" and out["document_id"] == "pattern:onprem-destination-proxytype"
    assert "customer" not in str(out["metadata"]).lower() and "acme" not in str(out).lower()
    # the loop is closed on the source and on the staged candidate
    assert store.doc_tags[("customer::acme", "turn:n")] == ["promote:done"]
    staged = [v for (b, _), v in store.doc_tags.items() if b == "staging::promotion"]
    assert staged == [["promote:published"]]


def test_publish_refuses_edited_text_that_names_a_customer(tmp_path):
    store = FakeStore()
    path = _review(tmp_path, store, decision="approve", sap_release="2026-10", text="As at Acme, set ProxyType.")
    report = publish(store, CFG, path)
    assert "acme" in report.errors[0].lower()
    assert not store.retained


def test_reject_marks_source_and_dry_run_writes_nothing(tmp_path):
    store = FakeStore()
    path = _review(tmp_path, store, decision="reject")
    assert publish(store, CFG, path, dry_run=True).rejected
    assert ("customer::acme", "turn:n") not in store.doc_tags
    publish(store, CFG, path)
    assert store.doc_tags[("customer::acme", "turn:n")] == ["promote:rejected"]


def test_nominate_adds_tag():
    store = FakeStore()
    nominate(store, "customer::acme", "turn:a")
    assert store.doc_tags[("customer::acme", "turn:a")] == ["project:acme-portal", "promote:candidate"]


def test_parse_and_leaks():
    assert parse('noise {"generic": true, "text": "x y", "kind": "bogus", "topic_id": "Bad Id"}', CFG.kinds) \
        == Rewrite(True, "x y", "decision", "", "")
    assert not parse("not json", CFG.kinds).generic
    assert leaks("Acme's portal", ["acme"]) == ["acme"]
    assert leaks("acme-portal", ["acme"]) == ["acme"]
    assert leaks("macmexx", ["acme"]) == []
