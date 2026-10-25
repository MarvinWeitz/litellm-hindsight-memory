"""Run the plugin inside a real LiteLLM proxy (in-process) with mock models."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from litellm.integrations.custom_logger import CustomLogger

import litellm_hindsight

AUTH = {"Authorization": "Bearer sk-test"}
TAGS = {"x-litellm-tags": "customer:acme,project:acme-portal"}


class SentMessages(CustomLogger):
    """Records the messages LiteLLM actually sent to the model."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list = []

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self.sent.append(kwargs.get("messages"))


@pytest.fixture(scope="module")
def proxy():
    import litellm

    os.environ["CONFIG_FILE_PATH"] = str(Path(__file__).with_name("proxy_config.yaml"))
    from fastapi.testclient import TestClient
    from litellm.proxy.proxy_server import app

    spy = SentMessages()
    with TestClient(app) as client:
        litellm.callbacks.append(spy)
        yield client, spy


@pytest.fixture
def hindsight(proxy, fake_hindsight, config):
    handler = litellm_hindsight.proxy_handler_instance
    handler._client, handler._config = fake_hindsight, config
    handler._cache.clear()
    proxy[1].sent.clear()
    return fake_hindsight


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_chat_completion_gets_memory_and_is_retained(proxy, hindsight):
    client, spy = proxy
    r = client.post(
        "/v1/chat/completions",
        headers={**AUTH, **TAGS},
        json={"model": "mock-gpt", "messages": [{"role": "user", "content": "how do we do auth?"}]},
    )
    assert r.status_code == 200, r.text
    assert _wait_for(lambda: hindsight.retains and spy.sent)
    sent_user = spy.sent[0][-1]["content"]
    assert "XSUAA role collections per tenant" in sent_user
    assert "API_BUSINESS_PARTNER" not in sent_user
    assert hindsight.retains[0]["bank_id"] == "customer::acme"
    assert hindsight.retains[0]["content"].startswith("Developer: how do we do auth?")


def test_anthropic_messages_route(proxy, hindsight):
    client, spy = proxy
    r = client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "mock-claude",
            "max_tokens": 100,
            "tags": ["customer:acme", "project:acme-portal"],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "auth on the portal?"}]}],
        },
    )
    assert r.status_code == 200, r.text
    assert _wait_for(lambda: hindsight.retains)
    assert {c["bank_id"] for c in hindsight.recalls} == {"customer::acme", "global::sap"}
    assert hindsight.retains[0]["tags"] == ["project:acme-portal", "source:litellm-proxy"]


def test_streaming_is_retained_after_the_stream(proxy, hindsight):
    client, _ = proxy
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={**AUTH, **TAGS},
        json={"model": "mock-gpt", "stream": True, "messages": [{"role": "user", "content": "stream it"}]},
    ) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    assert "data:" in body
    assert _wait_for(lambda: hindsight.retains)
    assert hindsight.retains[0]["content"] == "Developer: stream it\n\nAssistant: Use XSUAA role collections."


def test_untagged_request_passes_through(proxy, hindsight):
    client, _ = proxy
    r = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "mock-gpt", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 200
    time.sleep(0.3)
    assert hindsight.recalls == [] and hindsight.retains == []


def test_unknown_customer_is_rejected(proxy, hindsight):
    client, _ = proxy
    r = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "x-litellm-tags": "customer:initech"},
        json={"model": "mock-gpt", "messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 400
    assert "unknown customer" in r.text
