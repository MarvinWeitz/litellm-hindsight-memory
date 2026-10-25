"""Find the developer's turn in a request and add recalled memory to it.

Works with both request shapes the proxy sees:
- OpenAI chat: tool results arrive as ``role: tool`` messages.
- Anthropic messages (``/v1/messages``, used by Claude Code): tool results
  arrive as ``role: user`` messages made only of ``tool_result`` blocks.

Memory goes at the start of the developer's last real message, never into the
system prompt. Everything before that message stays byte-identical, so
provider prompt caching keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MEMORY_OPEN = "<hindsight_memory>"
MEMORY_CLOSE = "</hindsight_memory>"


@dataclass(frozen=True)
class Turn:
    index: int  # position of the developer's message in ``messages``
    text: str  # that message's text, without any memory block we added


def find_turn(messages: list[dict[str, Any]] | None) -> Turn | None:
    """Return the last user message that carries text a developer typed."""
    for index in range(len(messages or ()) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        text = _text_of(message.get("content"))
        if text:
            return Turn(index=index, text=text)
    return None


def inject(messages: list[dict[str, Any]], turn: Turn, memory: str) -> None:
    """Prepend the memory block to the turn's message, in place."""
    message = dict(messages[turn.index])
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = f"{memory}\n\n{content}"
    elif isinstance(content, list):
        message["content"] = [{"type": "text", "text": memory}, *content]
    else:
        return
    messages[turn.index] = message


def format_memory(sections: list[tuple[str, list[str]]], customer: str, project: str | None) -> str:
    """Render recalled facts per bank into one tagged block for the model."""
    scope = f"customer {customer}" + (f", project {project}" if project else "")
    lines = [
        MEMORY_OPEN,
        f"Memory recalled from earlier work ({scope}). Use it when it is relevant. "
        "If it conflicts with the conversation or the code, trust the conversation and the code.",
    ]
    for bank, facts in sections:
        if facts:
            lines.append(f"\n## {bank}")
            lines.extend(f"- {fact}" for fact in facts)
    lines.append(MEMORY_CLOSE)
    return "\n".join(lines)


def response_text(response: Any) -> tuple[str, bool]:
    """Return (final assistant text, is_final) from an OpenAI or Anthropic response.

    ``is_final`` is False when the model stopped to call a tool, because the
    turn is not finished yet.
    """
    data = _as_dict(response)
    if "choices" in data:  # OpenAI shape
        choices = data.get("choices") or [{}]
        choice = _as_dict(choices[0])
        message = _as_dict(choice.get("message") or {})
        has_tools = bool(message.get("tool_calls")) or choice.get("finish_reason") == "tool_calls"
        return _text_of(message.get("content")), not has_tools
    if "content" in data:  # Anthropic shape
        blocks = data.get("content") or []
        has_tools = data.get("stop_reason") == "tool_use" or any(
            _as_dict(b).get("type") == "tool_use" for b in blocks
        )
        return _text_of(blocks), not has_tools
    return "", False


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return _strip_memory(content).strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            block = _as_dict(block)
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = _strip_memory(block["text"]).strip()
                if text:
                    parts.append(text)
        return "\n\n".join(parts)
    return ""


def _strip_memory(text: str) -> str:
    start = text.find(MEMORY_OPEN)
    if start == -1:
        return text
    end = text.find(MEMORY_CLOSE, start)
    if end == -1:
        return text
    return text[:start] + text[end + len(MEMORY_CLOSE):]


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return method()
            except Exception:  # noqa: BLE001 - best effort on foreign response objects
                pass
    return {}
