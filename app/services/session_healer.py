# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""
Repair sessions whose last event is a tool call that never got a result.

When a run is cut off between the agent emitting a `function_call` and the
tool replying, the Agent Engine session keeps the call event with no
matching `function_response`. Every later turn replays that history, and
Anthropic rejects it outright (`tool_use` ids were found without
`tool_result` blocks). The session is poisoned until it is replaced.

Agents built from the current Agent-Template heal this themselves in
`ResilientLlm` before each model call. This module is the Forum-side
safety net for the agents that don't: it appends an honest "interrupted,
side effects unconfirmed" result so the next turn replays cleanly.

It is deliberately timid. Writing into an agent's own memory is not
something the Forum should do casually, so every append is guarded by:

  * a tail check — the orphaned call must be in the session's *last*
    event; anything appended after it means the engine kept running;
  * a grace period — that last event must be older than
    `heal_grace_seconds`, so a tool that is merely slow gets to finish;
  * a re-check — the tail is listed again immediately before the append
    and the write is abandoned if it moved;
  * long-running-tool awareness — ADK marks human-in-the-loop calls in
    `event_metadata.long_running_tool_ids`; those are *supposed* to sit
    unanswered and are never healed;
  * a config flag, `heal_orphaned_tool_calls`, off by default.

Retrying the cut-off call is out of scope. The Forum cannot know whether
the tool's side effect happened, so "unconfirmed, re-check" is the only
honest result it can write.
"""
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Optional, Sequence

from google.cloud.aiplatform_v1beta1.services.session_service import (
    SessionServiceAsyncClient,
)
from google.cloud.aiplatform_v1beta1.types import (
    AppendEventRequest,
    Content,
    EventMetadata,
    FunctionResponse,
    ListEventsRequest,
    Part,
    SessionEvent,
)

from app.config import get_settings

logger = logging.getLogger(__name__)

# How much of the session tail we pull to decide. Answered calls are found
# by scanning every event we fetch, so this only needs to be deep enough to
# contain the responses to the calls in the last event — one turn's worth of
# events, generously.
TAIL_PAGE_SIZE = 50


@dataclass(frozen=True)
class OrphanedCall:
    """A `function_call` part with no `function_response` anywhere after it."""

    call_id: str
    tool_name: str


@dataclass(frozen=True)
class HealOutcome:
    """
    What `SessionHealer.heal` did, and why.

    `reason` is a stable machine-readable token so callers can log and count
    outcomes without parsing prose:

      disabled      — the feature flag is off
      no_events     — the session is empty or unreadable
      no_orphan     — the tail is healthy, nothing to do (the common case)
      long_running  — the only unanswered calls are long-running by design
      within_grace  — an orphan exists but the tool may still be working
      tail_moved    — the engine appended something while we were deciding
      healed        — a synthetic result was appended
      error         — the Sessions API refused us; the session is untouched
    """

    healed: bool
    reason: str
    calls: tuple[OrphanedCall, ...] = ()
    detail: str = ""


@dataclass
class TailState:
    """What the tail of a session is waiting on.

    `source` is the event that made the calls — the one whose author,
    invocation id and branch the synthetic result has to inherit.
    """

    source: Optional[SessionEvent] = None
    healable: list[OrphanedCall] = field(default_factory=list)
    long_running: list[OrphanedCall] = field(default_factory=list)


@dataclass
class _Tail:
    events: list[SessionEvent] = field(default_factory=list)

    @property
    def last(self) -> Optional[SessionEvent]:
        return self.events[-1] if self.events else None


def session_resource_name(agent_id: str, session_id: str) -> str:
    """
    Build the Sessions API resource name for a session.

    `agent_id` is whatever the agent's Firestore record carries in
    `vertex_ai_agent_id` — either a full reasoning-engine resource name or
    the bare numeric id, both of which are accepted elsewhere in the Forum.
    `session_id` may be the bare id or the `user_id:session_id` pair that
    `VertexAIService.create_session` hands out.
    """
    engine = agent_id
    if not engine.startswith("projects/"):
        settings = get_settings()
        engine = (
            f"projects/{settings.gcp_project_id}"
            f"/locations/{settings.gcp_location}"
            f"/reasoningEngines/{engine}"
        )
    if ":" in session_id:
        session_id = session_id.split(":", 1)[1]
    return f"{engine}/sessions/{session_id}"


def _sorted_by_time(events: Sequence[SessionEvent]) -> list[SessionEvent]:
    """
    Oldest first.

    `list_events` is documented to return events in order, but the tail check
    is the whole safety argument here — it is worth not depending on that.
    Events with no timestamp keep their relative position rather than sorting
    to the front.
    """
    indexed = list(enumerate(events))
    epoch = datetime.fromtimestamp(0, UTC)
    indexed.sort(key=lambda pair: (pair[1].timestamp or epoch, pair[0]))
    return [event for _, event in indexed]


def _parts(event: SessionEvent) -> list[Part]:
    content = event.content
    if not content or not content.parts:
        return []
    return list(content.parts)


def _long_running_ids(event: SessionEvent) -> set[str]:
    """Call ids ADK flagged as long-running (human-in-the-loop and friends)."""
    metadata = event.event_metadata
    if not metadata or not metadata.long_running_tool_ids:
        return set()
    return {call_id for call_id in metadata.long_running_tool_ids if call_id}


def _only_answers_follow(ordered: Sequence[SessionEvent], call_index: int) -> bool:
    """
    Whether the session has done nothing but answer tools since `call_index`.

    This is the tail check, and it is the whole safety argument. If any event
    after the call carries text or a fresh call, the model spoke again — which
    means the provider accepted that history and the session is not broken,
    so it is not ours to touch. If the only thing that followed was *partial*
    answers, the run is still stuck in the tool-answering phase and the
    remaining ids will poison the next turn exactly as a bare call event would.
    """
    for event in ordered[call_index + 1:]:
        parts = _parts(event)
        if not parts:
            continue
        if any(not part.function_response for part in parts):
            return False
    return True


def find_orphaned_tool_calls(events: Sequence[SessionEvent]) -> "TailState":
    """
    Find tool calls the session is still waiting on at its tail.

    `long_running` holds unanswered calls that ADK marked long-running, which
    are unanswered on purpose — they are reported separately so callers can
    say "nothing to heal" for the right reason.
    """
    ordered = _sorted_by_time(events)
    if not ordered:
        return TailState()

    answered: set[str] = set()
    last_call_index = -1
    for index, event in enumerate(ordered):
        for part in _parts(event):
            response = part.function_response
            if response and response.id:
                answered.add(response.id)
            if part.function_call and part.function_call.id:
                last_call_index = index

    if last_call_index < 0 or not _only_answers_follow(ordered, last_call_index):
        return TailState()

    call_event = ordered[last_call_index]
    long_running = _long_running_ids(call_event)

    healable: list[OrphanedCall] = []
    deferred: list[OrphanedCall] = []
    for part in _parts(call_event):
        call = part.function_call
        if not call or not call.id or call.id in answered:
            continue
        orphan = OrphanedCall(call_id=call.id, tool_name=call.name or "unknown_tool")
        if call.id in long_running:
            deferred.append(orphan)
        else:
            healable.append(orphan)

    return TailState(source=call_event, healable=healable, long_running=deferred)


def interrupted_result(reason: str, elapsed_seconds: Optional[float]) -> dict[str, Any]:
    """
    The synthetic tool result.

    It says what actually happened rather than guessing, because the agent
    reading it back has no other way to find out: the Forum stopped listening,
    and whether the tool's side effect landed is genuinely unknown.
    """
    if elapsed_seconds is not None:
        when = f" after {elapsed_seconds:.0f}s"
    else:
        when = ""
    return {
        "error": (
            f"interrupted: the Forum did not receive a result for this call "
            f"({reason}{when}). Treat its side effects as unconfirmed and "
            f"re-check before assuming they happened."
        )
    }


def build_heal_event(
    source: SessionEvent,
    orphans: Sequence[OrphanedCall],
    result: dict[str, Any],
) -> SessionEvent:
    """
    Build the ADK-shaped event that answers `orphans`.

    Parallel calls made in one event get one event back carrying all their
    responses — the shape ADK's own `merge_parallel_function_response_events`
    produces, and the shape Anthropic requires, since it wants every
    `tool_use` in a message answered in the message immediately after it.
    Healing one of three sibling calls would leave the session just as broken.

    Author, invocation id and branch are copied from the call event so the
    result lands in the same invocation and the same sub-agent branch the
    call came from.
    """
    parts = [
        Part(
            function_response=FunctionResponse(
                id=orphan.call_id,
                name=orphan.tool_name,
                response=result,
            )
        )
        for orphan in orphans
    ]
    event = SessionEvent(
        author=source.author or "user",
        content=Content(role="user", parts=parts),
        invocation_id=source.invocation_id,
        timestamp=datetime.now(UTC),
    )
    branch = source.event_metadata.branch if source.event_metadata else ""
    if branch:
        event.event_metadata = EventMetadata(branch=branch)
    return event


class SessionHealer:
    """Appends missing tool results to Agent Engine sessions."""

    def __init__(self, client: Optional[SessionServiceAsyncClient] = None, settings=None):
        self._client = client
        self._settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        """Whether an operator has opted in to Forum-side healing."""
        return bool(self._settings.heal_orphaned_tool_calls)

    def _get_client(self) -> SessionServiceAsyncClient:
        # Built on first use so importing this module (and running its tests)
        # needs no credentials.
        if self._client is None:
            self._client = SessionServiceAsyncClient(
                client_options={
                    "api_endpoint": (
                        f"{self._settings.gcp_location}-aiplatform.googleapis.com"
                    )
                }
            )
        return self._client

    async def _list_tail(self, session_name: str) -> _Tail:
        client = self._get_client()
        pager = await client.list_events(
            request=ListEventsRequest(parent=session_name, page_size=TAIL_PAGE_SIZE)
        )
        events = [event async for event in pager]
        return _Tail(events=_sorted_by_time(events))

    async def heal(
        self,
        *,
        agent_id: str,
        session_id: str,
        reason: str,
        elapsed_seconds: Optional[float] = None,
    ) -> HealOutcome:
        """
        Repair the session if — and only if — its tail is an orphaned call.

        `reason` describes the cut-off ("the run was cut off", "the Forum
        timed out waiting"); it is quoted back to the agent in the synthetic
        result. Never raises: a session we cannot repair is left exactly as
        it was, and the caller's turn proceeds.
        """
        if not self.enabled:
            return HealOutcome(healed=False, reason="disabled")

        session_name = session_resource_name(agent_id, session_id)
        try:
            tail = await self._list_tail(session_name)
        except Exception as exc:
            logger.warning(f"Could not read session tail for {session_name}: {exc}")
            return HealOutcome(healed=False, reason="error", detail=str(exc))

        last = tail.last
        if last is None:
            return HealOutcome(healed=False, reason="no_events")

        state = find_orphaned_tool_calls(tail.events)
        orphans = state.healable
        if not orphans:
            if state.long_running:
                return HealOutcome(
                    healed=False,
                    reason="long_running",
                    calls=tuple(state.long_running),
                )
            return HealOutcome(healed=False, reason="no_orphan")

        grace = self._settings.heal_grace_seconds
        age = self._age_seconds(last)
        if age is not None and age < grace:
            return HealOutcome(
                healed=False,
                reason="within_grace",
                calls=tuple(orphans),
                detail=f"tail is {age:.0f}s old, grace is {grace}s",
            )

        # The engine may have finished the tool while we were deciding. Look
        # once more, as late as possible: a duplicate result is its own
        # provider error, and doing nothing is always the safer miss.
        try:
            recheck = await self._list_tail(session_name)
        except Exception as exc:
            logger.warning(f"Could not re-read session tail for {session_name}: {exc}")
            return HealOutcome(healed=False, reason="error", detail=str(exc))

        if recheck.last is None or recheck.last.name != last.name:
            return HealOutcome(
                healed=False, reason="tail_moved", calls=tuple(orphans)
            )

        event = build_heal_event(
            state.source, orphans, interrupted_result(reason, elapsed_seconds)
        )
        try:
            await self._get_client().append_event(
                request=AppendEventRequest(name=session_name, event=event)
            )
        except Exception as exc:
            logger.warning(f"Could not append healing event to {session_name}: {exc}")
            return HealOutcome(healed=False, reason="error", detail=str(exc))

        tools = ",".join(orphan.tool_name for orphan in orphans)
        logger.warning(
            f"Healed {len(orphans)} orphaned tool call(s) in {session_name}: {tools}",
            extra={
                "json_fields": {
                    "event": "session_healed",
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "orphan_count": len(orphans),
                    "tools": tools,
                    "cutoff_reason": reason,
                }
            },
        )
        return HealOutcome(healed=True, reason="healed", calls=tuple(orphans))

    @staticmethod
    def _age_seconds(event: SessionEvent) -> Optional[float]:
        if not event.timestamp:
            return None
        stamp = event.timestamp
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return (datetime.now(UTC) - stamp).total_seconds()


@lru_cache()
def get_session_healer() -> SessionHealer:
    """
    The process-wide healer.

    Shared so the gRPC channel to the Sessions API is built once rather than
    per request — the request-scoped services that use it are themselves
    constructed per request.
    """
    return SessionHealer()
