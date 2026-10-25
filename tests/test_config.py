import pytest
from conftest import TEST_ENV, make_config, routing_dict

from litellm_hindsight.config import ConfigError, RoutingConfig, load_config


def test_repo_routing_file_loads(monkeypatch):
    monkeypatch.setenv("HINDSIGHT_API_URL", "http://h")
    cfg = load_config("config/hindsight_routing.yaml")
    assert cfg.bank_tag.name == "customer" and cfg.scope_tag.prefix == "project:"
    assert cfg.hindsight_url == "http://h"


def test_no_config_path_is_an_error(monkeypatch):
    monkeypatch.delenv("HINDSIGHT_ROUTING_CONFIG", raising=False)
    with pytest.raises(ConfigError, match="HINDSIGHT_ROUTING_CONFIG"):
        load_config()


@pytest.mark.parametrize("section,key", [("recall", "timeout_seconds"), ("tags", "slug_pattern"),
                                         ("retain", "template"), (None, "untagged_request")])
def test_every_key_is_required(section, key):
    raw = routing_dict()
    (raw[section] if section else raw).pop(key)
    with pytest.raises(ConfigError, match=f"{key} is required"):
        RoutingConfig.from_dict(raw, env=TEST_ENV)


def test_read_target_keys_are_required():
    raw = routing_dict()
    raw["banks"]["read"][0].pop("budget")
    with pytest.raises(ConfigError, match=r"banks.read\[0\].budget is required"):
        RoutingConfig.from_dict(raw, env=TEST_ENV)


def test_unknown_keys_are_refused():
    with pytest.raises(ConfigError, match="unknown key"):
        make_config(recall={"timeout_secs": 1})


def test_unknown_placeholder_is_refused():
    with pytest.raises(ConfigError, match="unknown placeholder {team}"):
        make_config(retain={"tags": ["team:{team}"]})


def test_missing_hindsight_url_env_is_an_error():
    with pytest.raises(ConfigError, match="HINDSIGHT_API_URL"):
        RoutingConfig.from_dict(routing_dict(), env={})


def test_scope_can_be_switched_off():
    cfg = make_config(
        tags={"scope": None},
        banks={"write": "customer::{customer}", "read": []},
        retain={"tags": ["source:litellm-proxy"]},
    )
    from litellm_hindsight.routing import resolve

    route = resolve(["customer:acme", "project:ignored"], cfg)
    assert route.scope_value is None and route.label == "customer acme"
    assert [r.bank for r in route.reads] == ["global::sap"]
