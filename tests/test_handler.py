import pytest
from fastapi import HTTPException

from litellm_hindsight.config import RoutingConfig
from litellm_hindsight.handler import METADATA_KEY, HindsightMemoryHandler


def _request(text, tags, **extra):
    return {"model": "m", "messages": [{"role": "user", "content": text}], "tags": tags, **extra}


async def test_recall_reads_customer_project_shared_and_global(config, fake_hindsight):
    handler = HindsightMemoryHandler(config, fake_hindsight)
    data = await handler.async_pre_call_hook(None, None, _request("auth?", ["customer:acme", "project:acme-portal"]), "acompletion")
    content = data["messages"][0]["content"]
    assert "XSUAA role collections" in content  # project fact
    assert "2023 FPS02" in content  # customer-wide fact
    assert "cds.requires.auth.kind" in content  # global bank
    assert "API_BUSINESS_PARTNER" not in content  # other project of the same customer
    assert "Globex" not in content  # other customer
    assert content.endswith("auth?")
    assert {r["bank_id"] for r in fake_hindsight.recalls} == {"customer::acme", "global::sap"}


async def test_project_scope_is_enforced_even_if_server_leaks(config, fake_hindsight):
    fake_hindsight.ignore_tag_filter = True
    handler = HindsightMemoryHandler(config, fake_hindsight)
    data = await handler.async_pre_call_hook(None, None, _request("auth?", ["customer:acme", "project:acme-portal"]), "acompletion")
    content = data["messages"][0]["content"]
    assert "XSUAA role collections" in content
    assert "2023 FPS02" in content
    assert "API_BUSINESS_PARTNER" not in content  # other project's fact dropped by the plugin
    assert "cds.requires.auth.kind" in content  # untargeted global bank unaffected


async def test_customer_bank_skips_observations(config, fake_hindsight):
    handler = HindsightMemoryHandler(config, fake_hindsight)
    await handler.async_pre_call_hook(None, None, _request("auth?", ["customer:acme", "project:acme-portal"]), "acompletion")
    types = {r["bank_id"]: r.get("types") for r in fake_hindsight.recalls}
    assert types == {"customer::acme": ["world", "experience"], "global::sap": None}


async def test_customer_isolation(config, fake_hindsight):
    handler = HindsightMemoryHandler(config, fake_hindsight)
    data = await handler.async_pre_call_hook(None, None, _request("idp?", ["customer:globex"]), "acompletion")
    content = data["messages"][0]["content"]
    assert "Azure AD" in content
    assert "Acme" not in content
    assert all(r["bank_id"] != "customer::acme" for r in fake_hindsight.recalls)


async def test_tool_loop_requests_reuse_the_turn_recall(config, fake_hindsight):
    handler = HindsightMemoryHandler(config, fake_hindsight)
    tags = ["customer:acme", "project:acme-portal"]
    first = await handler.async_pre_call_hook(None, None, _request("auth?", tags), "acompletion")
    follow_up = {
        "model": "m",
        "tags": tags,
        "messages": [
            {"role": "user", "content": "auth?"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "..."},
        ],
    }
    second = await handler.async_pre_call_hook(None, None, follow_up, "acompletion")
    assert len(fake_hindsight.recalls) == 2  # one turn, two banks, no second recall
    assert second["messages"][0]["content"] == first["messages"][0]["content"]  # cache-friendly


async def test_slow_bank_times_out_without_failing(fake_hindsight):
    cfg = RoutingConfig.from_dict({"recall": {"timeout_seconds": 0.05}})
    fake_hindsight.delay["global::sap"] = 1
    handler = HindsightMemoryHandler(cfg, fake_hindsight)
    data = await handler.async_pre_call_hook(None, None, _request("auth?", ["customer:acme", "project:acme-portal"]), "acompletion")
    content = data["messages"][0]["content"]
    assert "XSUAA role collections" in content
    assert "global::sap" not in content


async def test_untagged_and_non_chat_requests_are_untouched(config, fake_hindsight):
    handler = HindsightMemoryHandler(config, fake_hindsight)
    data = await handler.async_pre_call_hook(None, None, _request("hi", []), "acompletion")
    assert data["messages"][0]["content"] == "hi"
    data = await handler.async_pre_call_hook(None, None, {"input": "x", "tags": ["customer:acme"]}, "aembedding")
    assert fake_hindsight.recalls == []


async def test_bad_tags_are_rejected_with_400(config, fake_hindsight):
    handler = HindsightMemoryHandler(config, fake_hindsight)
    with pytest.raises(HTTPException) as err:
        await handler.async_pre_call_hook(None, None, _request("hi", ["customer:initech"]), "acompletion")
    assert err.value.status_code == 400


async def test_retain_saves_turn_to_customer_bank_only(config, fake_hindsight):
    handler = HindsightMemoryHandler(config, fake_hindsight)
    data = await handler.async_pre_call_hook(None, None, _request("auth?", ["customer:acme", "project:acme-portal", "kind:decision"]), "acompletion")
    kwargs = {"model": "m", "litellm_params": {"metadata": data["metadata"]}}
    final = {"choices": [{"message": {"content": "Use role collections."}, "finish_reason": "stop"}]}
    await handler.async_log_success_event(kwargs, final, None, None)
    assert len(fake_hindsight.retains) == 1
    saved = fake_hindsight.retains[0]
    assert saved["bank_id"] == "customer::acme"
    assert saved["content"] == "Developer: auth?\n\nAssistant: Use role collections."
    assert "<hindsight_memory>" not in saved["content"]
    assert saved["tags"] == ["project:acme-portal", "source:litellm-proxy", "kind:decision"]
    assert saved["retain_async"] is True
    assert data["metadata"][METADATA_KEY]["write_bank"] == "customer::acme"


async def test_tool_call_responses_are_not_retained(config, fake_hindsight):
    handler = HindsightMemoryHandler(config, fake_hindsight)
    data = await handler.async_pre_call_hook(None, None, _request("auth?", ["customer:acme"]), "acompletion")
    kwargs = {"litellm_params": {"metadata": data["metadata"]}}
    tool = {"choices": [{"message": {"content": None, "tool_calls": [{}]}, "finish_reason": "tool_calls"}]}
    await handler.async_log_success_event(kwargs, tool, None, None)
    assert fake_hindsight.retains == []
