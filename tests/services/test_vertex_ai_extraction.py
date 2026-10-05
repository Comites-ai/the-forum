# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""VertexAIService._extract_text_from_chunks: which text gets delivered.

Only the turn's final reply — the text after the last tool call — reaches
the user. Text a model writes beside a tool call is it thinking out loud.
When the final response has no text, everything is joined as before.
"""
import json
import logging

import pytest

from app.services.vertex_ai_service import VertexAIService


@pytest.fixture
def service() -> VertexAIService:
    # Skip __init__: it calls vertexai.init, and extraction needs no client.
    return object.__new__(VertexAIService)


def _model(*parts) -> str:
    return json.dumps({"author": "dare", "content": {"role": "model", "parts": list(parts)}})


def _tool_result(name: str, response: dict) -> str:
    return json.dumps({
        "author": "dare",
        "content": {
            "role": "user",
            "parts": [{"function_response": {"name": name, "response": response}}],
        },
    })


def _text(text: str) -> dict:
    return {"text": text}


def _call(name: str) -> dict:
    return {"function_call": {"name": name, "args": {}}}


def test_text_beside_a_tool_call_is_left_out(service):
    chunks = [
        _model(_text("That's Branch B. Let me notify Maggie, then nudge Jonathan."), _call("query_agent")),
        _tool_result("query_agent", {"result": "Maggie noted it."}),
        _model(_text("Hey Jon. It's noon.")),
    ]

    text, _, _, _ = service._extract_text_from_chunks(chunks)

    assert text == "Hey Jon. It's noon."


def test_text_after_the_call_in_the_same_response_is_left_out(service):
    chunks = [
        _model(_call("read_diary"), _text("Checking the diary now.")),
        _tool_result("read_diary", {"rows": []}),
        _model(_text("No entry today.")),
    ]

    text, _, _, _ = service._extract_text_from_chunks(chunks)

    assert text == "No entry today."


def test_only_the_text_after_the_last_of_several_tool_rounds_is_delivered(service):
    chunks = [
        _model(_text("First I'll read the diary."), _call("read_diary")),
        _tool_result("read_diary", {"rows": []}),
        _model(_text("Now the date."), _call("get_date")),
        _tool_result("get_date", {"date": "2026-10-05"}),
        _model(_text("Hey Jon. ")),
        _model(_text("It's noon.")),
    ]

    text, _, _, _ = service._extract_text_from_chunks(chunks)

    assert text == "Hey Jon. It's noon."


def test_final_response_without_text_falls_back_to_all_text(service):
    chunks = [
        _model(_text("Reminder set for 9am. "), _call("create_scheduled_reminder")),
        _tool_result("create_scheduled_reminder", {"status": "ok"}),
        _model(_text("   ")),
    ]

    text, _, _, _ = service._extract_text_from_chunks(chunks)

    assert text == "Reminder set for 9am.    "


def test_turn_without_tool_calls_is_unchanged(service):
    chunks = [_model(_text("Hello ")), _model(_text("there."))]

    text, _, _, _ = service._extract_text_from_chunks(chunks)

    assert text == "Hello there."


def test_diagnostics_still_count_every_part(service):
    chunks = [
        _model(_text("Let me check."), _call("read_diary")),
        _tool_result("read_diary", {"rows": []}),
        _model(_text("Nothing today.")),
    ]

    _, breakdown, function_names, function_errors = service._extract_text_from_chunks(chunks)

    assert breakdown == {
        "text": 2,
        "function_call": 1,
        "function_response": 1,
        "other": 0,
        "unparseable": 0,
    }
    assert function_names == ["read_diary"]
    assert function_errors == []


def test_left_out_parts_are_logged(service, caplog):
    chunks = [
        _model(_text("Let me check."), _call("read_diary")),
        _tool_result("read_diary", {"rows": []}),
        _model(_text("Nothing today.")),
    ]

    with caplog.at_level(logging.INFO, logger="app.services.vertex_ai_service"):
        service._extract_text_from_chunks(chunks)

    assert "Left out 1 of 2 text parts" in caplog.text
