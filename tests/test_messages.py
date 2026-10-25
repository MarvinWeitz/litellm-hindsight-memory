from litellm_hindsight.messages import find_turn, format_memory, inject, response_text


def test_openai_turn_skips_tool_messages():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Fix the XSUAA config"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "file contents"},
    ]
    turn = find_turn(messages)
    assert turn.index == 1 and turn.text == "Fix the XSUAA config"


def test_anthropic_turn_skips_tool_result_blocks():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Add a CAP handler"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "..."}]},
    ]
    turn = find_turn(messages)
    assert turn.index == 0 and turn.text == "Add a CAP handler"


def test_inject_keeps_earlier_messages_identical():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [{"type": "text", "text": "second"}]},
    ]
    before = [dict(m) for m in messages[:2]]
    turn = find_turn(messages)
    inject(messages, turn, "<hindsight_memory>x</hindsight_memory>")
    assert messages[:2] == before
    assert messages[2]["content"][0]["text"].startswith("<hindsight_memory>")
    # The memory block is ignored when the turn is read again (no feedback loop).
    assert find_turn(messages).text == "second"


def test_format_memory_skips_empty_banks():
    text = format_memory([("customer::acme", ["fact a"]), ("global::sap", [])], "acme", "acme-portal")
    assert "## customer::acme\n- fact a" in text
    assert "global::sap" not in text


def test_response_text_detects_tool_use():
    openai_final = {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}
    openai_tool = {"choices": [{"message": {"content": None, "tool_calls": [{}]}, "finish_reason": "tool_calls"}]}
    anthropic_final = {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}
    anthropic_tool = {"content": [{"type": "tool_use"}], "stop_reason": "tool_use"}
    assert response_text(openai_final) == ("done", True)
    assert response_text(openai_tool)[1] is False
    assert response_text(anthropic_final) == ("done", True)
    assert response_text(anthropic_tool)[1] is False
