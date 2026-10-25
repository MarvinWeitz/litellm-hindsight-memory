import pytest
from conftest import make_config

from litellm_hindsight.routing import RoutingError, collect_tags, resolve


def test_default_scheme_routes_customer_and_project(config):
    route = resolve(["customer:acme", "project:acme-portal", "domain:cap"], config)
    assert route.write_bank == "customer::acme"
    assert [r.bank for r in route.reads] == ["customer::acme", "global::sap"]
    assert route.reads[0].tags == ("project:acme-portal", "project:_shared")
    assert route.reads[0].tags_match == "any_strict"
    assert route.reads[1].tags == ()
    assert route.retain_tags == ("project:acme-portal", "source:litellm-proxy", "domain:cap")


def test_missing_project_falls_back_to_shared(config):
    route = resolve(["customer:acme"], config)
    assert route.scope_value == "_shared"
    assert route.reads[0].tags == ("project:_shared", "project:_shared")


def test_untagged_request_passes_through_by_default(config):
    assert resolve(["some-other-tag"], config) is None


def test_untagged_request_can_be_rejected():
    cfg = make_config(untagged_request="reject")
    with pytest.raises(RoutingError):
        resolve([], cfg)


def test_opt_out_tag_disables_memory(config):
    assert resolve(["customer:acme", "memory:off"], config) is None


@pytest.mark.parametrize(
    "tags",
    [
        ["customer:acme", "customer:globex"],
        ["customer:acme", "project:a", "project:b"],
        ["customer:initech"],
        ["customer:acme", "project:acme-portl"],
        ["customer:Acme Corp"],
    ],
)
def test_unsafe_tags_are_refused(config, tags):
    with pytest.raises(RoutingError):
        resolve(tags, config)


def test_slugs_are_case_insensitive(config):
    assert resolve(["Customer:ACME", "project:Acme-Portal"], config).scope_value == "acme-portal"


def test_custom_scheme_and_extra_shared_bank():
    def read(bank, tags=()):
        return {"bank": bank, "tags": list(tags), "tags_match": "any_strict", "types": [],
                "budget": "low", "max_tokens": 500}

    cfg = make_config(
        tags={"bank": {"name": "kunde", "prefix": "kunde="},
              "scope": {"name": "repo", "prefix": "repo=", "when_missing": None}},
        banks={
            "write": "cust-{kunde}",
            "read": [read("cust-{kunde}", ["repo:{repo}"]), read("industry::utilities")],
        },
        retain={"tags": ["repo:{repo}"]},
        allowed=None,
    )
    route = resolve(["kunde=acme", "repo=portal"], cfg)
    assert route.write_bank == "cust-acme"
    assert [r.bank for r in route.reads] == ["cust-acme", "industry::utilities", "global::sap"]
    assert route.reads[0].tags == ("repo:portal",)
    assert route.retain_tags == ("repo:portal",)


def test_collect_tags_reads_every_location():
    data = {
        "tags": ["customer:acme"],
        "metadata": {"tags": ["project:acme-portal"]},
        "litellm_metadata": {"tags": ["domain:cap", "customer:acme"]},
    }
    assert collect_tags(data) == ["customer:acme", "project:acme-portal", "domain:cap"]
