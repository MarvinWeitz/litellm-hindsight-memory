"""A small client for the OpenAI-compatible endpoint the promotion job uses.

Anonymization and triage both ask the model for JSON; the novelty check also
asks for plain text. Customer data only goes to this endpoint, never anywhere else.
"""

from __future__ import annotations

import json

import httpx


class LLM:
    def __init__(self, base_url: str, model: str | None, api_key: str | None, *, timeout: float):
        if not model:
            raise ValueError("no model for the promotion job: set llm.model, or the variable llm.model_env names")
        self.model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)

    def json(self, prompt: str) -> dict | None:
        """The model's JSON answer, or None if it wasn't JSON."""
        return parse_json(self.text(prompt, json_mode=True))

    def text(self, prompt: str, *, json_mode: bool = False) -> str:
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = self.http.post("/chat/completions", json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""

    def close(self) -> None:
        self.http.close()


def parse_json(raw: str) -> dict | None:
    try:
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
