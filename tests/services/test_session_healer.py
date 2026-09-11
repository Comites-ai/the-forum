# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Tests for the orphaned-tool-call healer.

These build real `SessionEvent` protos rather than stand-ins: the shape of
the event the Forum writes back into an agent's memory is the risky part of
this feature, so the tests should break if that shape stops being buildable.
"""
from datetime import UTC, datetime, timedelta

import pytest
from google.cloud.aiplatform_v1beta1.types import (
    Content,
    EventMetadata,
    FunctionCall,
    FunctionResponse,
    Part,
    SessionEvent,
)

from app.services.session_healer import (
    HealOutcome,
    OrphanedCall,
    SessionHealer,
    build_heal_event,
    find_orphaned_tool_calls,
    interrupted_result,
    session_resource_name,
)

ENGINE = "projects/p/locations/us-central1/reasoningEngines/123"
SESSION = f"{ENGINE}/sessions/abc"


def _at(seconds_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds_ago)


def _text_event(text: str, *, name: str, seconds_ago: float = 300) -> SessionEvent:
    return SessionEvent(
        name=name,
        author="maggie",
        invocation_id="inv-1",
        content=Content(role="model", parts=[Part(text=text)]),
        timestamp=_at(seconds_ago),
    )


def _call_event(
    *calls: tuple[str, str],
    name: str,
    seconds_ago: float = 300,
    long_running: tuple[str, ...] = (),
    author: str = "maggie",
    invocation_id: str = "inv-1",
    branch: str = "",
) -> SessionEvent:
    event = SessionEvent(
        name=name,
        author=author,
        invocation_id=invocation_id,
        content=Content(
            role="model",
            parts=[
                Part(function_call=FunctionCall(id=call_id, name=tool))
                for call_id, tool in calls
            ],
        ),
        timestamp=_at(seconds_ago),
    )
    if long_running or branch:
        event.event_metadata = EventMetadata(
            long_running_tool_ids=list(long_running), branch=branch
        )
    return event


def _response_event(
    *responses: tuple[str, str], name: str, seconds_ago: float = 290
) -> SessionEvent:
    return SessionEvent(
        name=name,
        author="maggie",
        invocation_id="inv-1",
        content=Content(
            role="user",
            parts=[
                Part(
                    function_response=FunctionResponse(
                        id=call_id, name=tool, response={"ok": True}
                    )
                )
                for call_id, tool in responses
            ],
        ),
        timestamp=_at(seconds_ago),
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_healthy_history_has_no_orphans():
    events = [
        _text_event("hello", name="e1", seconds_ago=400),
        _call_event(("toolu_1", "write_sheet"), name="e2", seconds_ago=350),
        _response_event(("toolu_1", "write_sheet"), name="e3", seconds_ago=340),
        _text_event("done", name="e4", seconds_ago=330),
    ]
    state = find_orphaned_tool_calls(events)
    healable, long_running = state.healable, state.long_running
    assert healable == []
    assert long_running == []


def test_unanswered_call_at_the_tail_is_an_orphan():
    events = [
        _text_event("hello", name="e1", seconds_ago=400),
        _call_event(("toolu_1", "write_sheet"), name="e2", seconds_ago=350),
    ]
    healable = find_orphaned_tool_calls(events).healable
    assert healable == [OrphanedCall(call_id="toolu_1", tool_name="write_sheet")]


def test_orphan_that_is_not_the_last_event_is_left_alone():
    """The engine kept going after the call, so the run was not cut off here."""
    events = [
        _call_event(("toolu_1", "write_sheet"), name="e1", seconds_ago=350),
        _text_event("carrying on", name="e2", seconds_ago=340),
    ]
    state = find_orphaned_tool_calls(events)
    healable, long_running = state.healable, state.long_running
    assert healable == []
    assert long_running == []


def test_every_sibling_of_a_parallel_call_is_reported():
    events = [
        _call_event(
            ("toolu_1", "write_sheet"),
            ("toolu_2", "read_sheet"),
            ("toolu_3", "notify"),
            name="e1",
        ),
    ]
    healable = find_orphaned_tool_calls(events).healable
    assert [orphan.call_id for orphan in healable] == ["toolu_1", "toolu_2", "toolu_3"]


def test_partially_answered_parallel_call_reports_only_the_gap():
    """
    A run cut off after one sibling replied is still poison.

    Anthropic wants every `tool_use` in a message answered, so the surviving
    id has to be healed — and the one that did reply must not be answered
    twice, which would be its own provider error.
    """
    events = [
        _call_event(
            ("toolu_1", "write_sheet"), ("toolu_2", "notify"), name="e1",
            seconds_ago=350,
        ),
        _response_event(("toolu_1", "write_sheet"), name="e2", seconds_ago=345),
    ]
    state = find_orphaned_tool_calls(events)
    assert [orphan.call_id for orphan in state.healable] == ["toolu_2"]
    # The result has to inherit from the call event, not from the tail.
    assert state.source.name == "e1"


def test_long_running_calls_are_never_healed():
    """A human-in-the-loop call is unanswered on purpose."""
    events = [
        _call_event(
            ("toolu_1", "ask_human"), name="e1", long_running=("toolu_1",)
        ),
    ]
    state = find_orphaned_tool_calls(events)
    healable, long_running = state.healable, state.long_running
    assert healable == []
    assert [orphan.tool_name for orphan in long_running] == ["ask_human"]


def test_events_are_ordered_by_timestamp_not_arrival():
    """The tail check is the whole safety argument; don't trust list order."""
    events = [
        _call_event(("toolu_1", "write_sheet"), name="e2", seconds_ago=300),
        _text_event("earlier", name="e1", seconds_ago=400),
    ]
    healable = find_orphaned_tool_calls(events).healable
    assert [orphan.call_id for orphan in healable] == ["toolu_1"]


def test_empty_session_has_no_orphans():
    state = find_orphaned_tool_calls([])
    assert (state.source, state.healable, state.long_running) == (None, [], [])


# ---------------------------------------------------------------------------
# The event we write back
# ---------------------------------------------------------------------------


def test_heal_event_answers_every_orphan_in_one_event():
    source = _call_event(
        ("toolu_1", "write_sheet"),
        ("toolu_2", "notify"),
        name="e1",
        branch="maggie.tracking_agent",
    )
    orphans = find_orphaned_tool_calls([source]).healable
    event = build_heal_event(source, orphans, interrupted_result("cut off", 122.0))

    assert event.content.role == "user"
    assert len(event.content.parts) == 2
    assert [p.function_response.id for p in event.content.parts] == [
        "toolu_1",
        "toolu_2",
    ]
    assert [p.function_response.name for p in event.content.parts] == [
        "write_sheet",
        "notify",
    ]
    # Same invocation, same branch, same author as the call it answers.
    assert event.author == "maggie"
    assert event.invocation_id == "inv-1"
    assert event.event_metadata.branch == "maggie.tracking_agent"


def test_heal_event_carries_the_truth_about_the_cutoff():
    source = _call_event(("toolu_1", "write_sheet"), name="e1")
    orphans = find_orphaned_tool_calls([source]).healable
    event = build_heal_event(
        source, orphans, interrupted_result("the Forum timed out waiting", 120.0)
    )
    text = str(dict(event.content.parts[0].function_response.response))
    assert "the Forum timed out waiting" in text
    assert "120s" in text
    assert "unconfirmed" in text


def test_interrupted_result_omits_elapsed_when_unknown():
    text = interrupted_result("the run was cut off", None)["error"]
    assert "after" not in text
    assert "the run was cut off" in text


def test_heal_event_survives_a_source_with_no_branch():
    source = _call_event(("toolu_1", "write_sheet"), name="e1")
    orphans = find_orphaned_tool_calls([source]).healable
    event = build_heal_event(source, orphans, interrupted_result("cut off", None))
    assert event.event_metadata.branch == ""


# ---------------------------------------------------------------------------
# Resource names
# ---------------------------------------------------------------------------


def test_resource_name_from_full_engine_name_and_bare_session():
    assert session_resource_name(ENGINE, "abc") == SESSION


def test_resource_name_strips_the_user_id_the_forum_prepends():
    """VertexAIService hands out `user_id:session_id`; only the tail is real."""
    assert session_resource_name(ENGINE, "Jonathan:abc") == SESSION


def test_resource_name_expands_a_bare_engine_id():
    name = session_resource_name("123", "abc")
    assert name.endswith("/reasoningEngines/123/sessions/abc")
    assert name.startswith("projects/test-project/locations/us-central1/")


# ---------------------------------------------------------------------------
# The healer end to end, against a fake Sessions API
# ---------------------------------------------------------------------------


class _FakePager:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        async def gen():
            for event in self._events:
                yield event

        return gen()


class FakeSessionClient:
    """Stands in for SessionServiceAsyncClient.

    `tails` is a list of event-lists returned by successive list_events calls,
    which is how a test makes the tail move between the check and the append.
    """

    def __init__(self, *tails, list_error=None, append_error=None):
        self._tails = list(tails) or [[]]
        self.list_calls = 0
        self.appended = []
        self.list_error = list_error
        self.append_error = append_error

    async def list_events(self, request=None):
        if self.list_error:
            raise self.list_error
        index = min(self.list_calls, len(self._tails) - 1)
        self.list_calls += 1
        return _FakePager(self._tails[index])

    async def append_event(self, request=None):
        if self.append_error:
            raise self.append_error
        self.appended.append(request)
        return object()


class _Settings:
    heal_orphaned_tool_calls = True
    heal_grace_seconds = 120
    gcp_project_id = "test-project"
    gcp_location = "us-central1"


def _healer(client, **overrides) -> SessionHealer:
    settings = _Settings()
    for key, value in overrides.items():
        setattr(settings, key, value)
    return SessionHealer(client=client, settings=settings)


async def _heal(healer) -> HealOutcome:
    return await healer.heal(
        agent_id=ENGINE,
        session_id="Jonathan:abc",
        reason="the run was cut off",
        elapsed_seconds=123.0,
    )


@pytest.mark.asyncio
async def test_disabled_healer_reads_nothing():
    client = FakeSessionClient([_call_event(("toolu_1", "t"), name="e1")])
    outcome = await _heal(_healer(client, heal_orphaned_tool_calls=False))
    assert outcome == HealOutcome(healed=False, reason="disabled")
    assert client.list_calls == 0
    assert client.appended == []


@pytest.mark.asyncio
async def test_healthy_session_is_not_written_to():
    events = [
        _call_event(("toolu_1", "write_sheet"), name="e1", seconds_ago=350),
        _response_event(("toolu_1", "write_sheet"), name="e2", seconds_ago=340),
    ]
    client = FakeSessionClient(events)
    outcome = await _heal(_healer(client))
    assert outcome.healed is False
    assert outcome.reason == "no_orphan"
    assert client.appended == []


@pytest.mark.asyncio
async def test_orphan_past_the_grace_period_is_healed():
    events = [_call_event(("toolu_1", "write_sheet"), name="e1", seconds_ago=300)]
    client = FakeSessionClient(events)
    outcome = await _heal(_healer(client))

    assert outcome.healed is True
    assert outcome.reason == "healed"
    assert [orphan.tool_name for orphan in outcome.calls] == ["write_sheet"]

    assert len(client.appended) == 1
    request = client.appended[0]
    assert request.name == SESSION
    response = request.event.content.parts[0].function_response
    assert response.id == "toolu_1"
    assert "unconfirmed" in str(dict(response.response))


@pytest.mark.asyncio
async def test_orphan_inside_the_grace_period_is_left_alone():
    """The tool may simply still be working."""
    events = [_call_event(("toolu_1", "write_sheet"), name="e1", seconds_ago=10)]
    client = FakeSessionClient(events)
    outcome = await _heal(_healer(client))
    assert outcome.healed is False
    assert outcome.reason == "within_grace"
    assert client.appended == []


@pytest.mark.asyncio
async def test_a_tail_that_moves_before_the_append_aborts_the_write():
    """The engine finished the tool while we were deciding — do nothing."""
    orphaned = [_call_event(("toolu_1", "write_sheet"), name="e1", seconds_ago=300)]
    settled = orphaned + [
        _response_event(("toolu_1", "write_sheet"), name="e2", seconds_ago=5)
    ]
    client = FakeSessionClient(orphaned, settled)
    outcome = await _heal(_healer(client))

    assert outcome.healed is False
    assert outcome.reason == "tail_moved"
    assert client.appended == []


@pytest.mark.asyncio
async def test_long_running_call_reports_itself_rather_than_healing():
    events = [
        _call_event(
            ("toolu_1", "ask_human"),
            name="e1",
            seconds_ago=900,
            long_running=("toolu_1",),
        )
    ]
    client = FakeSessionClient(events)
    outcome = await _heal(_healer(client))
    assert outcome.healed is False
    assert outcome.reason == "long_running"
    assert client.appended == []


@pytest.mark.asyncio
async def test_empty_session_is_reported_not_healed():
    client = FakeSessionClient([])
    outcome = await _heal(_healer(client))
    assert outcome.healed is False
    assert outcome.reason == "no_events"


@pytest.mark.asyncio
async def test_a_refused_read_leaves_the_session_untouched():
    client = FakeSessionClient(list_error=PermissionError("no sessions.list"))
    outcome = await _heal(_healer(client))
    assert outcome.healed is False
    assert outcome.reason == "error"
    assert client.appended == []


@pytest.mark.asyncio
async def test_a_refused_append_is_swallowed():
    """A session we cannot repair must not take the caller's turn down."""
    events = [_call_event(("toolu_1", "write_sheet"), name="e1", seconds_ago=300)]
    client = FakeSessionClient(events, append_error=PermissionError("no appendEvent"))
    outcome = await _heal(_healer(client))
    assert outcome.healed is False
    assert outcome.reason == "error"


@pytest.mark.asyncio
async def test_healing_is_idempotent_across_two_runs():
    """The second pass sees its own result and finds nothing to do."""
    events = [_call_event(("toolu_1", "write_sheet"), name="e1", seconds_ago=300)]
    client = FakeSessionClient(events)
    first = await _heal(_healer(client))
    assert first.healed is True

    healed_event = client.appended[0].event
    healed_event.name = "e2"
    client._tails = [events + [healed_event]]
    client.list_calls = 0

    second = await _heal(_healer(client))
    assert second.healed is False
    assert second.reason == "no_orphan"
    assert len(client.appended) == 1
