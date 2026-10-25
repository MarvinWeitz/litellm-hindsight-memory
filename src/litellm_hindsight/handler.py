"""LiteLLM proxy callback that routes every tagged request through Hindsight.

Before the call: resolve the request's tags to banks, recall from all read
banks in parallel (once per developer turn, cached for the tool-loop requests
that follow), and add the memory to the developer's message.

After the call: save the developer's message and the final answer to the
customer bank. This runs in LiteLLM's success logger, after the response has
gone back, so the developer never waits for it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import OrderedDict
from typing import Any

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from .config import ReadTarget, RoutingConfig, load_config
from .messages import find_turn, format_memory, inject, response_text
from .routing import Route, RoutingError, collect_tags, resolve

log = logging.getLogger("litellm_hindsight")
if os.environ.get("HINDSIGHT_LOG_LEVEL"):
    # LiteLLM doesn't configure third-party loggers, so info lines would be dropped.
    log.setLevel(os.environ["HINDSIGHT_LOG_LEVEL"].upper())
    if not log.handlers:
        _handler = logging.StreamHandler()
        _handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        log.addHandler(_handler)
        log.propagate = False

METADATA_KEY = "hindsight_memory"
_CHAT_CALL_TYPES = {"completion", "acompletion", "anthropic_messages", "responses", "aresponses"}


class HindsightMemoryHandler(CustomLogger):
    def __init__(self, config: RoutingConfig | None = None, client: Any = None) -> None:
        super().__init__()
        self._config = config
        self._client = client
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()

    @property
    def config(self) -> RoutingConfig:
        if self._config is None:
            self._config = load_config()
        return self._config

    @property
    def client(self) -> Any:
        if self._client is None:
            from hindsight_client import Hindsight

            cfg = self.config
            self._client = Hindsight(
                base_url=cfg.hindsight_url,
                api_key=cfg.hindsight_api_key,
                timeout=cfg.client_timeout,
                user_agent=cfg.user_agent,
            )
        return self._client

    # ---- before the call ---------------------------------------------------

    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type) -> dict:
        if call_type not in _CHAT_CALL_TYPES:
            return data
        try:
            route = resolve(collect_tags(data), self.config)
        except RoutingError as exc:
            raise HTTPException(status_code=400, detail=f"hindsight memory routing: {exc}") from exc
        if route is None:
            return data

        messages = data.get("messages")
        turn = find_turn(messages, self.config.memory_marker)
        if turn is None:
            return data

        key = _turn_key(route, turn.text)
        memory = self._cache_get(key)
        recalled = False
        if memory is None:
            memory = await self._recall(route, turn.text)
            self._cache_put(key, memory)
            recalled = True
        if memory:
            inject(messages, turn, memory)

        _metadata(data)[METADATA_KEY] = {
            "bank_value": route.bank_value,
            "scope_value": route.scope_value,
            "write_bank": route.write_bank,
            "retain_tags": list(route.retain_tags),
            "turn_key": key,
            "user_text": turn.text,
            "recalled": recalled,
        }
        return data

    async def _recall(self, route: Route, query: str) -> str:
        started = time.perf_counter()
        results = await asyncio.gather(
            *(self._recall_one(target, query) for target in route.reads), return_exceptions=True
        )
        sections: list[tuple[str, list[str]]] = []
        for target, result in zip(route.reads, results):
            if isinstance(result, BaseException):
                log.warning("hindsight recall from %s skipped: %r", target.bank, result)
                continue
            sections.append((target.bank, result))
        log.info(
            "hindsight recall %s banks=%d facts=%d took=%.0fms",
            route.label, len(route.reads),
            sum(len(facts) for _, facts in sections), (time.perf_counter() - started) * 1000,
        )
        if not any(facts for _, facts in sections):
            return ""
        cfg = self.config
        return format_memory(sections, cfg.memory_header.format(scope=route.label), cfg.memory_marker)

    async def _recall_one(self, target: ReadTarget, query: str) -> list[str]:
        kwargs: dict[str, Any] = {"budget": target.budget, "max_tokens": target.max_tokens}
        if target.tags:
            kwargs.update(tags=list(target.tags), tags_match=target.tags_match)
        if target.types:
            kwargs["types"] = list(target.types)
        response = await asyncio.wait_for(
            self.client.arecall(bank_id=target.bank, query=query, **kwargs),
            timeout=self.config.recall_timeout,
        )
        return [
            r.text
            for r in (response.results or [])
            if getattr(r, "text", None) and _tags_allowed(getattr(r, "tags", None), target)
        ]

    # ---- after the call ----------------------------------------------------

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        if not self.config.retain_enabled:
            return
        info = _find_route_info(kwargs)
        if not info:
            return
        answer, is_final = response_text(response_obj)
        if not is_final or not answer:
            return
        cfg = self.config
        content = cfg.retain_template.format(user=info["user_text"], assistant=answer)
        limit = cfg.retain_max_chars
        if len(content) > limit:
            content = content[:limit]
        try:
            await self.client.aretain(
                bank_id=info["write_bank"],
                content=content,
                context=cfg.retain_context,
                document_id=f"{cfg.retain_document_prefix}{info['turn_key']}",
                tags=info["retain_tags"],
                metadata={**cfg.retain_metadata, "model": str(kwargs.get("model", ""))},
                retain_async=True,
            )
            log.info("hindsight retain into %s tags=%s chars=%d", info["write_bank"], info["retain_tags"], len(content))
        except Exception as exc:  # noqa: BLE001 - memory must never fail a request
            log.warning("hindsight retain into %s failed: %r", info["write_bank"], exc)

    # ---- turn cache ----------------------------------------------------------

    def _cache_get(self, key: str) -> str | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, memory = entry
        if time.monotonic() - stored_at > self.config.cache_ttl:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return memory

    def _cache_put(self, key: str, memory: str) -> None:
        self._cache[key] = (time.monotonic(), memory)
        self._cache.move_to_end(key)
        while len(self._cache) > self.config.cache_size:
            self._cache.popitem(last=False)


def _tags_allowed(result_tags: list[str] | None, target: ReadTarget) -> bool:
    """Re-check each recalled memory against the target's tag filter.

    Hindsight can return memories outside the filter (seen with facts from
    another project of the same customer), so the plugin enforces the
    project boundary itself instead of trusting the server-side filter.
    """
    if not target.tags:
        return True
    tags, wanted = set(result_tags or ()), set(target.tags)
    match = target.tags_match
    if not tags:
        return match in ("any", "all")
    if match in ("any", "any_strict"):
        return bool(tags & wanted)
    if match in ("all", "all_strict"):
        return wanted <= tags
    if match == "exact":
        return tags == wanted
    return True


def _turn_key(route: Route, text: str) -> str:
    raw = "\x00".join((route.write_bank, route.scope_value or "", text))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _metadata(data: dict) -> dict:
    key = "litellm_metadata" if isinstance(data.get("litellm_metadata"), dict) else "metadata"
    if not isinstance(data.get(key), dict):
        data[key] = {}
    return data[key]


def _find_route_info(kwargs: dict) -> dict | None:
    params = kwargs.get("litellm_params") or {}
    for container in (params.get("metadata"), params.get("litellm_metadata"),
                      kwargs.get("metadata"), kwargs.get("litellm_metadata")):
        if isinstance(container, dict) and isinstance(container.get(METADATA_KEY), dict):
            return container[METADATA_KEY]
    return None
