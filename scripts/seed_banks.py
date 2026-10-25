"""Create the demo banks with the configuration from the architecture doc, then add sample memories.

Usage: python scripts/seed_banks.py [--no-memories]
Needs HINDSIGHT_API_URL (default http://localhost:8888). Adding memories needs
the proxy running with a working provider key, because Hindsight extracts
facts through the hindsight-extraction model.
"""

from __future__ import annotations

import argparse
import os

from hindsight_client import Hindsight

DOMAIN_VALUES = [
    ("cap", "SAP Cloud Application Programming Model: CDS, services, handlers"),
    ("xsuaa", "Authentication, roles, XSUAA / IAS"),
    ("destinations", "BTP destinations and Connectivity / Cloud Connector"),
    ("hana-cloud", "HANA Cloud, HDI containers, persistence"),
    ("event-mesh", "Eventing, Event Mesh, messaging"),
    ("s4-integration", "S/4HANA APIs, OData, side-by-side extensions"),
]
KIND_VALUES = [
    ("decision", "A choice made and why"),
    ("pitfall", "An error, gotcha or failed approach and its fix"),
    ("pattern", "A reusable implementation approach"),
    ("runbook", "Step-by-step operational procedure"),
]
ENTITY_LABELS = [
    {"key": "domain", "type": "multi-values", "tag": True,
     "values": [{"value": v, "description": d} for v, d in DOMAIN_VALUES]},
    {"key": "kind", "type": "value", "tag": True,
     "values": [{"value": v, "description": d} for v, d in KIND_VALUES]},
]

CUSTOMER = {
    "retain_mission": (
        "Extract technical decisions and their reasons, system landscape (S/4 version, BTP subaccounts, "
        "destinations), integration points, customer conventions, known errors and fixes, open risks. "
        "Ignore greetings, scheduling and status chatter."
    ),
    "observations_mission": (
        "Consolidate durable conventions and recurring problems for this customer. "
        "Flag contradictions with earlier decisions."
    ),
    "reflect_mission": (
        "You are a senior SAP consultant on this customer's projects. "
        "Ground every answer in past decisions and the customer's landscape."
    ),
}
GLOBAL = {
    "retain_mission": (
        "Extract reusable SAP CAP/BTP knowledge: patterns, configuration recipes, version-specific pitfalls, "
        "error messages with root causes. Ignore anything customer-specific."
    ),
    "observations_mission": (
        "Consolidate patterns confirmed across several sources. Flag guidance that contradicts newer SAP releases."
    ),
    "reflect_mission": (
        "You are an SAP CAP/BTP architect. Give generic, version-aware guidance "
        "and say when it depends on the landscape."
    ),
}

BANKS = {
    "customer::acme": (CUSTOMER, [["project:acme-portal"], ["project:acme-s4-extension"], ["project:_shared"]]),
    "customer::globex": (CUSTOMER, [["project:_shared"]]),
    "global::sap": (GLOBAL, None),
}

SAMPLE_MEMORIES = [
    ("customer::acme", ["project:_shared", "domain:s4-integration"],
     "Acme runs S/4HANA 2023 FPS02 on premise, reached from BTP through the Cloud Connector "
     "with the location ID ACME-DC1."),
    ("customer::acme", ["project:acme-portal", "domain:xsuaa", "kind:decision"],
     "For the Acme portal we decided to use one XSUAA role collection per tenant, because Acme's "
     "IAS groups map one-to-one to tenants."),
    ("customer::acme", ["project:acme-s4-extension", "domain:s4-integration", "kind:pitfall"],
     "The Acme S/4 extension got 403 errors from API_BUSINESS_PARTNER until the technical user got "
     "the SAP_BR_BUPA_MASTER_SPECIALIST role."),
    ("customer::globex", ["project:_shared", "domain:xsuaa"],
     "Globex authenticates through Azure AD federated into SAP IAS."),
    ("global::sap", ["domain:cap", "kind:pattern"],
     "In CAP on BTP, set cds.requires.auth.kind to xsuaa in production and keep mocked auth only "
     "in the development profile."),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-memories", action="store_true", help="only create and configure banks")
    args = parser.parse_args()

    client = Hindsight(base_url=os.environ.get("HINDSIGHT_API_URL", "http://localhost:8888"))
    for bank_id, (missions, scopes) in BANKS.items():
        client.create_bank(
            bank_id, name=bank_id, disposition_skepticism=4, disposition_literalism=4, disposition_empathy=1,
            **missions,
        )
        client.update_bank_config(
            bank_id, entity_labels=ENTITY_LABELS,
            **({"default_observation_scopes": scopes} if scopes else {}),
        )
        print(f"configured {bank_id}")

    if args.no_memories:
        return
    for bank_id, tags, text in SAMPLE_MEMORIES:
        client.retain(bank_id, text, tags=tags, context="seed data", metadata={"source": "seed"})
        print(f"retained into {bank_id}: {text[:60]}...")


if __name__ == "__main__":
    main()
