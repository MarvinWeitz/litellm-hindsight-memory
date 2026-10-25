from types import SimpleNamespace

import pytest

from conftest import ROUTING_FILE, TEST_ENV, make_config
from litellm_hindsight.config import ConfigError, RoutingConfig
from litellm_hindsight.provision import provision
from litellm_hindsight.routing import RoutingError, resolve

RECALL = {"tags": [], "tags_match": "any", "types": [], "budget": "low", "max_tokens": 1000}


def bank(bank_id, applies_to="all", **profile):
    return {"id": bank_id, "name": None, "applies_to": applies_to, "recall": dict(RECALL), "profile": profile}


def _cfg(*banks, **overrides):
    return make_config(global_banks=list(banks), allowed=None, **overrides)


def _reads(cfg, tags):
    return [r.bank for r in resolve(tags, cfg).reads]


def test_no_global_banks_when_the_list_is_empty():
    assert _reads(_cfg(), ["customer:acme"]) == ["customer::acme"]


def test_global_bank_for_every_request_comes_after_the_customer_bank():
    route = resolve(["customer:acme", "project:portal"], _cfg(bank("global::sap")))
    assert [r.bank for r in route.reads] == ["customer::acme", "global::sap"]
    assert route.reads[1].max_tokens == 1000 and route.reads[1].tags == ()
    assert route.write_bank == "customer::acme"


def test_applies_to_customers_and_exclusions():
    cfg = _cfg(
        bank("global::sap", {"exclude_customer": ["initech"]}),
        bank("industry::utilities", {"customer": ["Globex"]}),
    )
    assert _reads(cfg, ["customer:acme"]) == ["customer::acme", "global::sap"]
    assert _reads(cfg, ["customer:globex"]) == ["customer::globex", "global::sap", "industry::utilities"]
    assert _reads(cfg, ["customer:initech"]) == ["customer::initech"]


def test_applies_to_projects_uses_the_fallback_project():
    cfg = _cfg(bank("global::portal", {"project": ["portal", "_shared"]}),
               bank("global::not-legacy", {"exclude_project": ["legacy"]}))
    assert _reads(cfg, ["customer:acme", "project:portal"])[1:] == ["global::portal", "global::not-legacy"]
    assert _reads(cfg, ["customer:acme"])[1:] == ["global::portal", "global::not-legacy"]  # when_missing = _shared
    assert _reads(cfg, ["customer:acme", "project:legacy"]) == ["customer::acme"]


def test_applies_to_request_tags_is_an_opt_in():
    cfg = _cfg(bank("global::abap", {"request_tags": ["stack:abap"]}))
    assert _reads(cfg, ["customer:acme"]) == ["customer::acme"]
    assert _reads(cfg, ["customer:acme", "stack:abap"]) == ["customer::acme", "global::abap"]


def test_conditions_combine():
    cfg = _cfg(bank("g", {"customer": ["acme"], "request_tags": ["stack:abap"]}))
    assert _reads(cfg, ["customer:acme"]) == ["customer::acme"]
    assert _reads(cfg, ["customer:globex", "stack:abap"]) == ["customer::globex"]
    assert _reads(cfg, ["customer:acme", "stack:abap"]) == ["customer::acme", "g"]


def test_applies_to_keys_follow_the_tag_names():
    cfg = _cfg(
        bank("g", {"kunde": ["acme"], "exclude_repo": ["old"]}),
        tags={"bank": {"name": "kunde", "prefix": "kunde="},
              "scope": {"name": "repo", "prefix": "repo=", "when_missing": None}},
        banks={"write": "cust-{kunde}", "read": []},
        retain={"tags": []},
    )
    assert _reads(cfg, ["kunde=acme", "repo=new"]) == ["g"]
    assert _reads(cfg, ["kunde=acme", "repo=old"]) == []
    with pytest.raises(ConfigError, match="unknown key"):
        _cfg(bank("g", {"customer": ["acme"]}), tags=cfg_tags_kunde())


def cfg_tags_kunde():
    return {"bank": {"name": "kunde", "prefix": "kunde="}, "scope": None}


def test_proxy_never_writes_a_global_bank():
    cfg = _cfg(bank("shared-acme"), banks={"write": "shared-{customer}", "read": []})
    with pytest.raises(RoutingError, match="read-only"):
        resolve(["customer:acme"], cfg)


@pytest.mark.parametrize(
    "banks, message",
    [
        ("global::sap", "wrong type"),
        ([{"id": "g", "name": None, "applies_to": "all", "profile": {}}], "recall is required"),
        ([{**bank("g"), "aplies_to": "all"}], "unknown key"),
        ([bank("g"), bank("g")], "listed twice"),
        ([bank("global::{customer}")], "without placeholders"),
        ([bank("g", {"customers": ["acme"]})], "unknown key"),
        ([bank("g", {})], "'all' or a mapping"),
        ([bank("g", "some")], "'all' or a mapping"),
        ([bank("g", {"customer": "acme"})], "wrong type"),
        ([bank("g", disposition={"kindness": 3})], "unknown key"),
        ([bank("g", retain_misson="x")], "unknown key"),
        ([{**bank("g"), "recall": {**RECALL, "budget": "huge"}}], "budget must be one of"),
    ],
)
def test_invalid_global_banks_are_refused(banks, message):
    with pytest.raises(ConfigError, match=message):
        _cfg(*banks) if isinstance(banks, list) else make_config(global_banks=banks)


def test_global_banks_key_is_required():
    from conftest import routing_dict

    raw = routing_dict()
    del raw["global_banks"]
    with pytest.raises(ConfigError, match="global_banks is required"):
        RoutingConfig.from_dict(raw, env=TEST_ENV)


def test_bank_cannot_be_both_a_read_target_and_global():
    read = {"bank": "global::sap", **RECALL}
    with pytest.raises(ConfigError, match="also in global_banks"):
        _cfg(bank("global::sap"), banks={"read": [read]})


def test_repo_config_defines_global_sap():
    (sap,) = make_config().global_banks
    assert sap.id == "global::sap" and sap.name == "SAP CAP/BTP knowledge"
    create = sap.create_kwargs()
    assert "reusable SAP CAP/BTP knowledge" in create["retain_mission"] and "\n" not in create["retain_mission"]
    assert create["disposition_skepticism"] == 4
    assert [label["key"] for label in sap.bank_config["entity_labels"]] == ["domain", "kind"]
    assert ROUTING_FILE.exists()


class RecordingClient:
    def __init__(self):
        self.calls = []

    def create_bank(self, bank_id, **kwargs):
        self.calls.append(("create_bank", bank_id, kwargs))
        return SimpleNamespace(bank_id=bank_id)

    def update_bank_config(self, bank_id, **kwargs):
        self.calls.append(("update_bank_config", bank_id, kwargs))


def test_provision_sends_profile_and_config():
    cfg = _cfg(
        bank("global::sap", retain_mission="Extract  reusable\n knowledge.",
             disposition={"skepticism": 4, "empathy": 1}, config={"enable_reranking": True}),
        {**bank("industry::utilities"), "name": "Utilities"},
    )
    client = RecordingClient()
    done = provision(client, cfg.global_banks, config_overrides={"enable_reranking": False}, progress=lambda _: None)
    assert done == ["global::sap", "industry::utilities"]
    assert client.calls == [
        ("create_bank", "global::sap", {"name": "global::sap", "retain_mission": "Extract reusable knowledge.",
                                        "disposition_skepticism": 4, "disposition_empathy": 1}),
        ("update_bank_config", "global::sap", {"enable_reranking": False}),
        ("create_bank", "industry::utilities", {"name": "Utilities"}),
        ("update_bank_config", "industry::utilities", {"enable_reranking": False}),
    ]


def test_provision_without_config_skips_update():
    client = RecordingClient()
    provision(client, _cfg(bank("g")).global_banks, progress=lambda _: None)
    assert client.calls == [("create_bank", "g", {"name": "g"})]


def test_provision_one_bank_and_dry_run():
    cfg = _cfg(bank("a"), bank("b", config={"enable_reranking": False}))
    client, lines = RecordingClient(), []
    assert provision(client, cfg.global_banks, only=["b"], dry_run=True, progress=lines.append) == ["b"]
    assert client.calls == [] and "update_bank_config" in lines[0]
    with pytest.raises(ValueError, match="not in global_banks"):
        provision(client, cfg.global_banks, only=["c"])
