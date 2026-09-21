# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Agent-to-agent (A2A) MCP server (Streamable HTTP).

Lets any agent attached to The Forum communicate with any other attached
agent, with The Forum mediating the call. Four tools:

  - list_agents:          who else is attached, and what they can be asked
  - get_agent_inquiries:  the full inquiry records one agent publishes
  - query_agent:          send a message to another agent and get its reply
  - get_query_status:     what became of an earlier query_agent call

Agents publish their "inquiries" — the requests they know how to field —
on their Firestore agent document (registered at deploy time by each agent
repo's register_agent.py from an inquiries.json). See docs/FOR_AGENT_DEVELOPERS.md.

Because agents serve multiple human users, every query_agent call must say
who it is on behalf of. The target agent receives the message prefixed:

    [From Agent: <caller display name> | On Behalf Of: <user primary name>] <message>

and each (caller, target, user) triple gets its own persistent Vertex AI
session, so different users' exchanges never share conversation history.

Every query_agent call also leaves a delivery receipt (PLAT-51): a record
of the call, written when the message is handed to the target's engine,
marked delivered when the engine starts answering, and closed with the
reply or with what went wrong. Its id comes back in the result and in
every error, and get_query_status reads it. The point is the failure the
caller cannot see through: an MCP client that drops the connection mid-call
(a 504 from Cloud Run, or the ADK session pool tearing a session down under
a concurrent turn) learns nothing about whether the message landed, and a
blind resend of a non-idempotent request double-writes.

Authentication mirrors the scheduler MCP: the agent presents its MCP API
key (the same key provisioned by scripts/provision_scheduler_api_key.py)
in the X-API-Key header; the SHA-256 hash identifies the calling agent.
The caller's identity always comes from the key — an agent cannot
impersonate another agent or omit attribution.

Mounted as an ASGI app at /api/v1/mcp/agents. Stateless Streamable HTTP
(one request = one tool call); the session manager's lifecycle is managed
in the FastAPI app's lifespan in app/main.py. This server is also useful
interactively: point an MCP client (e.g. Claude Code) at it with a valid
agent key to explore and test attached agents during development.
"""
import asyncio
import contextvars
import json
import logging
import time
from datetime import datetime, UTC
from typing import Any, Awaitable, Optional

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool

from app.api.v1.history_mcp import _format_timestamp, _user_timezone
from app.api.v1.scheduler_mcp import hash_api_key
from app.config import get_settings
from app.models.a2a_query import (
    QUERY_EMPTY_REPLY,
    QUERY_FAILED,
    QUERY_REPLIED,
    QUERY_SENT,
    QUERY_TIMED_OUT,
    RETENTION,
    A2AQuery,
    a2a_session_key,
    decode_query_id,
)
from app.models.agent import Agent
from app.models.user import User
from app.services.firestore_service import FirestoreService
from app.services.run_tracker import (
    CUT_OFF_TIMEOUT,
    active_run_marker,
    describe_cut_off,
    log_run_cut_off,
)
from app.services.session_healer import get_session_healer
from app.services.vertex_ai_service import VertexAIResponse, VertexAIService

logger = logging.getLogger(__name__)

# How long query_agent waits for the target agent's reply. Reasoning-engine
# turns with several tool calls can take a while; a timeout says the target
# is slow, never that it lacks the capability.
#
# 240s, not more: query_agent runs inside an inbound Cloud Run request, and
# Cloud Run's own request timeout is 300s. Past that the request is killed
# outright and the `except asyncio.TimeoutError` branch below never runs, so
# the marker would never get its reason and the caller would get nothing it
# could act on. 240 leaves margin for the Firestore writes either side.
#
# It was 120s, which was cutting off ordinary A2A calls several times a week
# (#26). Raising it is half the answer; the other half is `_log_a2a_query`,
# which records how long these calls actually take so the next move — an
# asynchronous query_agent, or nothing — is decided from data rather than
# from another guess at a number.
QUERY_TIMEOUT_SECONDS = 240

# The whole query_agent call, every attempt included, has to finish inside
# this. Cloud Run kills the request at 300s; the margin is for the Firestore
# writes and the response. The dead-session retry below used to be able to
# push a call to 480s, which is how two of Maggie's relays on 2026-09-18
# ended as 504s with no Forum error text at all (#28).
REQUEST_BUDGET_SECONDS = 270

# A second attempt with less than this left is not worth making: it would
# time out before an engine turn of any substance could finish.
MIN_RETRY_SECONDS = 30

# Collection holding the A2A session docs the run marker rides on. Matches
# FirestoreService.a2a_sessions_collection.
A2A_SESSIONS = "a2a_sessions"

# How many of the caller's recent queries get_query_status lists.
STATUS_DEFAULT_LIMIT = 5
STATUS_MAX_LIMIT = 20
# The message is echoed back so the caller can recognise its own query;
# the caller wrote it, so a preview is enough.
MESSAGE_PREVIEW_CHARS = 300

# Background work that must outlive the request that started it: storing a
# reply that arrived after the caller-facing timeout. Held here so the tasks
# are not garbage-collected mid-flight.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro: Awaitable[Any]) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _log_a2a_query(
    *,
    caller: Agent,
    target: Agent,
    session_id: str,
    duration_seconds: float,
    outcome: str,
    chunk_count: Optional[int] = None,
) -> None:
    """
    Record how long one query_agent call to the engine actually took.

    This is the measurement the timeout argument turns on, and nothing else
    in the Forum supplies it. `run_cut_off` cannot: its `elapsed_seconds` is
    detection lag — how long a marker sat before the *next* turn noticed it
    — not the duration of a call.

    Every outcome is logged, not just the successful ones, so the share of
    calls that time out is computable rather than inferred. `timeout_seconds`
    rides along on every row because a sample of completed calls is censored
    at whatever ceiling was in force when it was collected: the distribution
    is only readable against its limit, and the limit changes.

    The `json_fields` names are a log-query contract the same way
    `run_cut_off`'s are — keep them stable.
    """
    logger.info(
        f"A2A query {caller.display_name} -> {target.display_name}: "
        f"{outcome} after {duration_seconds:.1f}s",
        extra={
            "json_fields": {
                "event": "a2a_query",
                "agent_id": target.vertex_ai_agent_id,
                "caller_agent_id": caller.vertex_ai_agent_id,
                "caller": caller.display_name,
                "target": target.display_name,
                "session_id": session_id,
                "platform": "a2a",
                "outcome": outcome,
                "duration_seconds": round(duration_seconds, 1),
                "timeout_seconds": QUERY_TIMEOUT_SECONDS,
                "chunk_count": chunk_count,
            }
        },
    )


async def _recover_from_cut_off_run(entry: dict, target: Agent, session_id: str) -> None:
    """
    Deal with a previous query on this session that never came back.

    The `wait_for` in `_query` is a real cut-off: the Forum stops listening
    while the engine may still be mid-tool, which is exactly how a
    `function_call` ends up with no result. Logging it is unconditional and
    is the signal that says whether the timeout wants tuning; repairing the
    session happens only if an operator has opted in.
    """
    cut_off = describe_cut_off(
        entry.get("active_run"),
        stale_after_seconds=get_settings().cut_off_after_seconds,
    )
    if not cut_off:
        return

    log_run_cut_off(
        cut_off,
        agent_id=target.vertex_ai_agent_id,
        session_id=session_id,
        platform="a2a",
    )
    outcome = await get_session_healer().heal(
        agent_id=target.vertex_ai_agent_id,
        session_id=session_id,
        reason=cut_off.reason,
        elapsed_seconds=cut_off.elapsed_seconds,
    )
    logger.info(
        f"Cut-off recovery for A2A session with {target.display_name}: "
        f"{outcome.reason}"
    )


# ---------------------------------------------------------------------------
# Delivery receipts (PLAT-51)
# ---------------------------------------------------------------------------
class _QueryRecord:
    """
    The Forum's record of one query_agent call, kept up to date as it runs.

    Every write is best-effort. The receipt exists to make failures
    survivable; it must never be the cause of one, so a Firestore hiccup
    here is logged and the relay carries on without a receipt (the result
    then says so). The Firestore methods raise; this is where that is
    decided.
    """

    def __init__(
        self,
        firestore: FirestoreService,
        *,
        session_key: str,
        caller: Agent,
        target: Agent,
        user: User,
        message: str,
    ) -> None:
        self._firestore = firestore
        self._session_key = session_key
        self._target = target
        self._record = A2AQuery(
            caller_agent_id=caller.id,
            target_agent_id=target.id,
            user_id=user.id,
            caller=caller.display_name,
            target=target.display_name,
            on_behalf_of=user.primary_name,
            message=message,
        )
        self.doc_id: Optional[str] = None

    @property
    def query_id(self) -> Optional[str]:
        return self._record.query_id if self.doc_id else None

    def mention(self) -> str:
        """The sentence every error carries, so the caller knows where to look."""
        if not self.query_id:
            return (
                "No delivery receipt could be written for this call; use "
                "get_query_status with agent_name and on_behalf_of to see your "
                "recent queries."
            )
        return (
            f"Delivery receipt query_id: {self.query_id}. Call get_query_status "
            f"with it to see whether the message was delivered and whether a "
            f"reply arrived later."
        )

    async def opened(self, *, session_id: str, attempt: int, timeout: float) -> None:
        """The message is about to be handed to the engine."""
        self._record.session_id = session_id
        self._record.attempts = attempt
        self._record.timeout_seconds = timeout
        try:
            if self.doc_id is None:
                self.doc_id = await self._firestore.create_a2a_query(
                    self._session_key, self._record
                )
                self._record.id = self.doc_id
            else:
                await self._update(
                    status=QUERY_SENT,
                    session_id=session_id,
                    attempts=attempt,
                    timeout_seconds=timeout,
                    delivered_at=None,
                    finished_at=None,
                )
        except Exception as e:  # noqa: BLE001 - a lost receipt must not fail the relay
            logger.warning(
                f"Could not write the delivery receipt for a query to "
                f"{self._target.display_name}: {e}"
            )
            self.doc_id = None

    def delivered(self) -> None:
        """
        The engine has sent its first chunk back: it has the message.

        Called from the loop thread by VertexAIService.send_message. Only
        `delivered_at` is written, never the status, so this can never race
        the outcome write and leave a replied call looking merely delivered.
        """
        if self.doc_id is None:
            return
        _spawn(self._update(delivered_at=datetime.now(UTC)))

    async def replied(self, reply: str) -> None:
        await self._close(QUERY_REPLIED, reply=reply)

    async def empty(self, chunk_count: int) -> None:
        await self._close(
            QUERY_EMPTY_REPLY,
            error=f"the engine ended its turn without any text ({chunk_count} chunks)",
        )

    async def timed_out(self, timeout: float) -> None:
        await self._close(
            QUERY_TIMED_OUT,
            error=f"the Forum stopped waiting after {timeout:.0f}s",
        )

    async def failed(self, exc: BaseException) -> None:
        await self._close(QUERY_FAILED, error=str(exc)[:500] or type(exc).__name__)

    def collect_late_reply(self, send: "asyncio.Future[VertexAIResponse]") -> None:
        """
        Keep listening after the caller-facing timeout.

        The engine call is still running; when it finishes, store what it
        produced so get_query_status can hand the caller the reply it
        missed. Best effort: Cloud Run throttles CPU once no request is in
        flight, and an idle instance is eventually reclaimed, so a late
        reply can also simply never be recorded.
        """
        def _done(task: "asyncio.Future[VertexAIResponse]") -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                if isinstance(exc, asyncio.TimeoutError):
                    return
                _spawn(self._update(error=f"after the timeout, the engine stream failed: {exc}"[:500]))
                return
            response = task.result()
            text = (response.text or "").strip()
            if text:
                _spawn(self._late_reply(text))
            else:
                _spawn(self._update(
                    error=(
                        f"after the timeout, the engine ended its turn without any "
                        f"text ({response.chunk_count} chunks)"
                    ),
                    finished_at=datetime.now(UTC),
                ))

        send.add_done_callback(_done)

    async def _late_reply(self, text: str) -> None:
        logger.info(
            f"Late reply from {self._target.display_name} stored on receipt "
            f"{self.doc_id} after the caller-facing timeout"
        )
        await self._update(
            status=QUERY_REPLIED, reply=text, error=None, finished_at=datetime.now(UTC)
        )
        # The run did finish; the marker the timeout left would otherwise
        # send the next query into cut-off recovery on a healthy session.
        await self._firestore.clear_active_run(self._session_key, collection=A2A_SESSIONS)

    async def _close(self, status: str, **fields: Any) -> None:
        await self._update(status=status, finished_at=datetime.now(UTC), **fields)

    async def _update(self, **fields: Any) -> None:
        if self.doc_id is None:
            return
        try:
            await self._firestore.update_a2a_query(self._session_key, self.doc_id, fields)
        except Exception as e:  # noqa: BLE001 - see class docstring
            logger.warning(
                f"Could not update delivery receipt {self.doc_id} for a query to "
                f"{self._target.display_name}: {e}"
            )


def _explain(record: A2AQuery) -> str:
    """What the status means for the caller, in the words it should act on."""
    target = record.target
    if record.status == QUERY_REPLIED:
        return "Delivered and answered. The reply is included."
    if record.status == QUERY_TIMED_OUT:
        head = (
            f"{target} acknowledged the message, then the Forum stopped waiting"
            if record.delivered
            else "The Forum handed the message to the engine, then stopped waiting before hearing anything back"
        )
        return (
            f"{head} ({record.error}). Anything you asked for may already have "
            f"been done. Ask {target} for the current state rather than "
            f"resending; if its reply arrives late it will appear here."
        )
    if record.status == QUERY_EMPTY_REPLY:
        return (
            f"Delivered. {target} ran its turn but ended it without any text, "
            f"which usually means it used tools and stopped. It may have acted "
            f"on the message; ask for the current state rather than resending."
        )
    if record.status == QUERY_FAILED:
        where = "after delivery" if record.delivered else "before anything came back from the engine"
        return (
            f"The engine call failed {where}: {record.error}. "
            + (
                f"{target} had the message, so it may have acted on it."
                if record.delivered
                else "Delivery is unknown; the message may not have been processed."
            )
        )
    # QUERY_SENT: no outcome was ever recorded.
    age = (datetime.now(UTC) - record.sent_at).total_seconds()
    ceiling = record.timeout_seconds or QUERY_TIMEOUT_SECONDS
    if age <= ceiling:
        return (
            f"In flight. {target} has the message and is working on it."
            if record.delivered
            else f"In flight. The message has been handed to {target}'s engine; nothing heard back yet."
        )
    if record.delivered:
        return (
            f"{target} had the message and started answering, but the Forum "
            f"never recorded an outcome (its request was probably cut off). "
            f"Treat it as delivered: ask for the current state rather than resending."
        )
    return (
        f"The message was handed to {target}'s engine but nothing was ever "
        f"heard back and no outcome was recorded. Delivery is unknown."
    )


def _query_dict(record: A2AQuery, tz_name: str) -> dict:
    status = record.status
    if status == QUERY_SENT and record.delivered:
        status = "delivered"
    message = record.message
    if len(message) > MESSAGE_PREVIEW_CHARS:
        message = message[:MESSAGE_PREVIEW_CHARS] + "…"
    out = {
        "query_id": record.query_id,
        "agent": record.target,
        "on_behalf_of": record.on_behalf_of,
        "message": message,
        "status": status,
        "delivered": record.delivered,
        "attempts": record.attempts,
        "sent_at": _format_timestamp(record.sent_at, tz_name),
        "delivered_at": _format_timestamp(record.delivered_at, tz_name) if record.delivered_at else None,
        "finished_at": _format_timestamp(record.finished_at, tz_name) if record.finished_at else None,
        "meaning": _explain(record),
    }
    if record.reply is not None:
        out["reply"] = record.reply
    if record.error:
        out["error"] = record.error
    return out


# ---------------------------------------------------------------------------
# Per-request context (same pattern as scheduler_mcp)
# ---------------------------------------------------------------------------
_request_ctx: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "agents_mcp_request_ctx", default=None
)


def _ctx() -> dict:
    ctx = _request_ctx.get()
    if ctx is None:
        raise RuntimeError("agents MCP tool called outside of an authenticated request")
    return ctx


def _caller() -> Agent:
    return _ctx()["agent"]


def _firestore() -> FirestoreService:
    return _ctx()["firestore"]


def _vertex_ai() -> VertexAIService:
    return _ctx()["vertex_ai"]


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
_ON_BEHALF_OF_DESC = (
    "The human user this query concerns, EXACTLY as their name appears in the "
    "'[From: <name>] ...' prefix of the conversation you are working in (or the "
    "'On Behalf Of' name if you were yourself queried by another agent). "
    "Required: agents serve multiple users, and the target agent needs to know "
    "whose data is being asked about. Do not guess or omit."
)

TOOLS: list[Tool] = [
    Tool(
        name="list_agents",
        description=(
            "List the other agents attached to this Forum: their display name, "
            "what they do, and the names of the inquiries they can field. Use "
            "get_agent_inquiries for full request/response formats."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_agent_inquiries",
        description=(
            "Get the full inquiry records one agent publishes: what you can "
            "ping it about, how to phrase the request, and what response "
            "format to expect."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "The agent's display name exactly as returned by list_agents.",
                },
            },
            "required": ["agent_name"],
        },
    ),
    Tool(
        name="query_agent",
        description=(
            "Send a message to another agent attached to this Forum and get its "
            "reply. The Forum delivers your message to the target agent with an "
            "attribution prefix identifying you and who the query is on behalf "
            "of. Conversation history is kept per (you, target agent, user), so "
            "follow-up queries about the same user continue the same exchange. "
            "Prefer the request_format from the target's published inquiries; "
            "free-form messages are allowed but structured inquiries get "
            "structured answers. The result includes a query_id, the delivery "
            "receipt: if this call ends in an error, a timeout, or a lost "
            "connection, pass it to get_query_status to learn whether the "
            "message was delivered before you consider sending it again."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "The target agent's display name exactly as returned by list_agents.",
                },
                "message": {
                    "type": "string",
                    "description": (
                        "The message to send. For published inquiries, use the "
                        "inquiry's request_format (e.g. 'AGENT_QUERY: planned_workouts_today')."
                    ),
                },
                "on_behalf_of": {
                    "type": "string",
                    "description": _ON_BEHALF_OF_DESC,
                },
            },
            "required": ["agent_name", "message", "on_behalf_of"],
        },
    ),
    Tool(
        name="get_query_status",
        description=(
            "Find out what became of an earlier query_agent call. Use it whenever "
            "query_agent returned an error, a timeout, or a lost connection: it "
            "tells you whether your message reached the target and whether a "
            "reply arrived, including one that arrived after the Forum stopped "
            "waiting, so you never have to resend blind. Look up one call by the "
            "query_id from query_agent's result or error text. If you have no "
            "id because the connection dropped, pass agent_name and on_behalf_of "
            "instead to list your most recent queries to that agent for that "
            f"user, newest first. Records are kept for {RETENTION.days} days."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query_id": {
                    "type": "string",
                    "description": "The delivery receipt returned by query_agent.",
                },
                "agent_name": {
                    "type": "string",
                    "description": (
                        "With on_behalf_of, instead of query_id: the target agent's "
                        "display name, to list your recent queries to it."
                    ),
                },
                "on_behalf_of": {
                    "type": "string",
                    "description": (
                        "With agent_name: the user the queries were on behalf of, "
                        "exactly as you passed it to query_agent."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"When listing: how many recent queries to return "
                        f"(default {STATUS_DEFAULT_LIMIT}, max {STATUS_MAX_LIMIT})."
                    ),
                },
            },
        },
    ),
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------
def _inquiry_to_dict(inquiry) -> dict:
    return {
        "name": inquiry.name,
        "description": inquiry.description,
        "request_format": inquiry.request_format,
        "response_format": inquiry.response_format,
    }


async def _resolve_target_agent(agent_name: Any) -> Agent:
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise ValueError("agent_name is required. Use a display name from list_agents.")
    target = await _firestore().get_agent_by_display_name(agent_name.strip())
    if not target:
        raise ValueError(
            f"No agent found with name {agent_name!r}. Use the exact display "
            f"name from list_agents."
        )
    return target


async def _resolve_on_behalf_of_user(on_behalf_of: Any):
    if not isinstance(on_behalf_of, str) or not on_behalf_of.strip():
        raise ValueError(
            "on_behalf_of is required: the name of the human user this query "
            "concerns, from the '[From: <name>]' prefix of your conversation."
        )
    user = await _firestore().get_user_by_any_name(on_behalf_of.strip())
    if not user:
        raise ValueError(
            f"No user found with name {on_behalf_of!r}. Pass the exact name "
            f"from the '[From: <name>]' prefix — do not paraphrase."
        )
    return user


def _a2a_session_key(caller_id: str, target_id: str, user_id: str) -> str:
    return a2a_session_key(caller_id, target_id, user_id)


async def _handle_list_agents(args: dict[str, Any]) -> str:
    caller = _caller()
    agents = await _firestore().list_agents()
    result = [
        {
            "display_name": a.display_name,
            "description": a.description,
            "inquiries": [i.name for i in (a.inquiries or [])],
        }
        for a in agents
        if a.id != caller.id
    ]
    return json.dumps(result)


async def _handle_get_inquiries(args: dict[str, Any]) -> str:
    target = await _resolve_target_agent(args.get("agent_name"))
    return json.dumps({
        "agent": target.display_name,
        "description": target.description,
        "inquiries": [_inquiry_to_dict(i) for i in (target.inquiries or [])],
    })


async def _handle_query_agent(args: dict[str, Any]) -> str:
    deadline = time.monotonic() + REQUEST_BUDGET_SECONDS

    caller = _caller()
    target = await _resolve_target_agent(args.get("agent_name"))
    if target.id == caller.id:
        raise ValueError("You cannot query yourself. Use list_agents to find other agents.")

    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message is required.")

    user = await _resolve_on_behalf_of_user(args.get("on_behalf_of"))

    firestore = _firestore()
    vertex_ai = _vertex_ai()

    # One persistent conversation per (caller, target, user) — different
    # users' exchanges must never share history.
    session_key = _a2a_session_key(caller.id, target.id, user.id)
    receipt = _QueryRecord(
        firestore,
        session_key=session_key,
        caller=caller,
        target=target,
        user=user,
        message=message,
    )

    async def _fresh_session() -> str:
        # The Vertex-session user id deliberately avoids ':' —
        # VertexAIService.send_message splits the combined id on the first colon.
        new_id = await vertex_ai.create_session(
            target.vertex_ai_agent_id,
            user_name=f"agent-{caller.id}-for-{user.id}",
        )
        await firestore.save_a2a_session(
            session_key, new_id, engine_id=target.vertex_ai_agent_id
        )
        return new_id

    async def _query(session_id: str, attempt: int) -> VertexAIResponse:
        # Marked before and cleared after, so the next query on this session
        # can tell that a run was started and never came back — the moment a
        # tool call gets stranded without a result (PLAT-42).
        await firestore.mark_run_started(
            session_key, active_run_marker(), collection=A2A_SESSIONS
        )
        started = time.monotonic()
        # Never past the ceiling, and never past what is left of the request.
        timeout = max(0.0, min(QUERY_TIMEOUT_SECONDS, deadline - started))
        await receipt.opened(session_id=session_id, attempt=attempt, timeout=timeout)

        def _log(outcome: str, chunk_count: Optional[int] = None) -> None:
            _log_a2a_query(
                caller=caller,
                target=target,
                session_id=session_id,
                duration_seconds=time.monotonic() - started,
                outcome=outcome,
                chunk_count=chunk_count,
            )

        # The engine call runs as its own task, shielded from the timeout, so
        # a reply that arrives after the Forum stopped waiting still gets
        # stored on the receipt instead of being thrown away.
        send = asyncio.ensure_future(
            vertex_ai.send_message(
                agent_id=target.vertex_ai_agent_id,
                session_id=session_id,
                message=prefixed,
                on_first_chunk=receipt.delivered,
            )
        )
        try:
            response = await asyncio.wait_for(asyncio.shield(send), timeout=timeout)
        except asyncio.TimeoutError:
            # The engine may well still be running; we have simply stopped
            # listening. Leave the marker, say why, and let the next query
            # decide (after the grace period) whether anything needs repair.
            await firestore.mark_run_started(
                session_key,
                active_run_marker(CUT_OFF_TIMEOUT),
                collection=A2A_SESSIONS,
            )
            _log(QUERY_TIMED_OUT)
            await receipt.timed_out(timeout)
            receipt.collect_late_reply(send)
            # Deliberately not "try again later": the target is most likely
            # still working, so whatever was asked for may already have been
            # done. A blind retry of a non-idempotent request double-writes.
            raise ValueError(
                f"{target.display_name} did not reply within {timeout:.0f}s, "
                f"so the Forum stopped waiting. Its reply is lost, but the work is "
                f"probably still running and anything you asked it to change may "
                f"already have happened. Ask it for the current state before sending "
                f"the same request again. {receipt.mention()}"
            )
        except Exception as e:
            # Logged so the timed-out share is a fraction of every call, not
            # only of the ones that came back one way or another.
            _log(QUERY_FAILED)
            await receipt.failed(e)
            raise

        replied = bool(response.text and response.text.strip())
        _log(
            QUERY_REPLIED if replied else QUERY_EMPTY_REPLY,
            chunk_count=response.chunk_count,
        )
        if replied:
            await firestore.clear_active_run(session_key, collection=A2A_SESSIONS)
            await receipt.replied(response.text.strip())
        else:
            await receipt.empty(response.chunk_count)
        return response

    prefixed = (
        f"[From Agent: {caller.display_name} | On Behalf Of: {user.primary_name}] {message}"
    )

    # Sessions live on one specific engine. If the target was redeployed
    # since this entry was written, its engine changed and the stored
    # session is dead — recreate instead of querying into a guaranteed
    # failure. Entries without engine_id predate engine tracking and are
    # treated the same way (#18).
    entry = await firestore.get_a2a_session(session_key)
    session_id = None
    if entry:
        if entry.get("engine_id") == target.vertex_ai_agent_id:
            session_id = entry.get("vertex_ai_session_id")
        else:
            await firestore.delete_a2a_session(session_key)
    used_cached_session = session_id is not None
    if session_id is None:
        session_id = await _fresh_session()
    elif entry:
        await _recover_from_cut_off_run(entry, target, session_id)

    response = await _query(session_id, attempt=1)
    reply = (response.text or "").strip()

    if not reply and used_cached_session and response.chunk_count == 0:
        # A dead session is indistinguishable from a genuinely empty reply:
        # the engine's SessionNotFoundError dies mid-stream and reaches us
        # as a cleanly-terminated stream with 0 chunks (#18). Since the
        # cached session is the prime suspect, drop it and retry ONCE on a
        # fresh one before declaring failure.
        #
        # Zero chunks is the whole signature. An empty reply that came with
        # chunks means the engine ran the turn — used tools, thought, and
        # stopped without text — so the message was delivered and acted on,
        # and resending it is exactly the duplicate this receipt exists to
        # prevent (#28).
        remaining = deadline - time.monotonic()
        if remaining >= MIN_RETRY_SECONDS:
            logger.warning(
                f"Empty reply from {target.display_name} on cached A2A session "
                f"{session_key}; recreating the session and retrying once"
            )
            await firestore.delete_a2a_session(session_key)
            session_id = await _fresh_session()
            response = await _query(session_id, attempt=2)
            reply = (response.text or "").strip()
        else:
            logger.warning(
                f"Empty reply from {target.display_name} on cached A2A session "
                f"{session_key} with {remaining:.0f}s of the request left; "
                f"not retrying"
            )

    if not reply:
        if response.chunk_count > 0:
            raise ValueError(
                f"{target.display_name} received the message and ran its turn "
                f"({response.chunk_count} chunks) but ended it without any text — "
                f"it most likely used tools and stopped. It may have acted on "
                f"what you sent. Ask it for the current state rather than sending "
                f"the same request again. {receipt.mention()}"
            )
        raise ValueError(
            f"{target.display_name} returned an empty reply "
            f"({response.chunk_count} chunks). It may be misconfigured — "
            f"try again or contact its operator. {receipt.mention()}"
        )

    return json.dumps({
        "agent": target.display_name,
        "on_behalf_of": user.primary_name,
        "query_id": receipt.query_id,
        "reply": reply,
    })


async def _handle_get_query_status(args: dict[str, Any]) -> str:
    caller = _caller()
    firestore = _firestore()
    query_id = args.get("query_id")

    if isinstance(query_id, str) and query_id.strip():
        caller_id, target_id, user_id, doc_id = decode_query_id(query_id)
        if caller_id != caller.id:
            # Structural scoping: the token names its caller, and a token
            # minted for another agent is nobody's business here.
            raise ValueError(f"query_id {query_id!r} is not one of your queries.")
        session_key = _a2a_session_key(caller_id, target_id, user_id)
        record = await firestore.get_a2a_query(session_key, doc_id)
        if record is None:
            raise ValueError(
                f"No query with id {query_id!r}. Receipts are kept for "
                f"{RETENTION.days} days; if the call was recent, the Forum never "
                f"handed the message to the target, so it was not delivered."
            )
        user = await firestore.get_user_by_any_name(record.on_behalf_of)
        return json.dumps({"query": _query_dict(record, _user_timezone(user))})

    agent_name = args.get("agent_name")
    on_behalf_of = args.get("on_behalf_of")
    if not (isinstance(agent_name, str) and agent_name.strip()) or not (
        isinstance(on_behalf_of, str) and on_behalf_of.strip()
    ):
        raise ValueError(
            "Pass either query_id, or both agent_name and on_behalf_of to list "
            "your recent queries to that agent for that user."
        )
    target = await _resolve_target_agent(agent_name)
    user = await _resolve_on_behalf_of_user(on_behalf_of)
    limit = args.get("limit")
    try:
        limit = STATUS_DEFAULT_LIMIT if limit is None else int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer.")
    limit = max(1, min(STATUS_MAX_LIMIT, limit))

    session_key = _a2a_session_key(caller.id, target.id, user.id)
    records = await firestore.list_a2a_queries(session_key, limit)
    tz_name = _user_timezone(user)
    return json.dumps({
        "agent": target.display_name,
        "on_behalf_of": user.primary_name,
        "timezone": tz_name,
        "queries": [_query_dict(r, tz_name) for r in records],
        "note": (
            "Newest first. A query_agent call that left no record here never "
            "reached the target's engine."
            if records
            else "No queries from you to this agent for this user in the last "
            f"{RETENTION.days} days: nothing was delivered."
        ),
    })


# ---------------------------------------------------------------------------
# MCP Server registration
# ---------------------------------------------------------------------------
def _build_server() -> Server:
    server: Server = Server("agents")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def _call_tool(name: str, arguments: Optional[dict[str, Any]]) -> list[TextContent]:
        args = arguments or {}
        try:
            if name == "list_agents":
                result = await _handle_list_agents(args)
            elif name == "get_agent_inquiries":
                result = await _handle_get_inquiries(args)
            elif name == "query_agent":
                result = await _handle_query_agent(args)
            elif name == "get_query_status":
                result = await _handle_get_query_status(args)
            else:
                raise ValueError(f"Unknown tool: {name}")
            return [TextContent(type="text", text=result)]
        except ValueError:
            # Surface as a clean error response — MCP wraps raised exceptions as isError=True
            raise
        except Exception as e:
            logger.exception(f"Error in agents MCP tool {name!r}: {e}")
            raise

    return server


mcp_server = _build_server()
session_manager = StreamableHTTPSessionManager(
    mcp_server,
    stateless=True,
    json_response=True,
)


# ---------------------------------------------------------------------------
# ASGI entry point
# ---------------------------------------------------------------------------
async def _send_json(send, status: int, body: dict) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


async def asgi_app(scope, receive, send) -> None:
    """ASGI entry point: authenticate via X-API-Key, then delegate to MCP."""
    if scope["type"] != "http":
        await _send_json(send, 405, {"error": "Method not allowed"})
        return

    headers = {k: v for k, v in scope.get("headers", [])}
    api_key_bytes = headers.get(b"x-api-key")
    if not api_key_bytes:
        await _send_json(send, 401, {"error": "Missing X-API-Key header"})
        return

    try:
        api_key = api_key_bytes.decode("utf-8")
    except UnicodeDecodeError:
        await _send_json(send, 400, {"error": "Invalid X-API-Key encoding"})
        return

    state = scope.get("state", {})
    firestore: Optional[FirestoreService] = state.get("firestore")
    vertex_ai: Optional[VertexAIService] = state.get("vertex_ai")
    if firestore is None or vertex_ai is None:
        app = scope.get("app")
        app_state = getattr(app, "state", None)
        firestore = firestore or getattr(app_state, "firestore", None)
        vertex_ai = vertex_ai or getattr(app_state, "vertex_ai", None)
    if firestore is None or vertex_ai is None:
        logger.error("agents MCP: FirestoreService/VertexAIService not available in ASGI scope")
        await _send_json(send, 500, {"error": "Server misconfigured"})
        return

    agent = await firestore.get_agent_by_scheduler_api_key_hash(hash_api_key(api_key))
    if not agent:
        await _send_json(send, 401, {"error": "Invalid API key"})
        return

    token = _request_ctx.set({
        "agent": agent,
        "firestore": firestore,
        "vertex_ai": vertex_ai,
    })
    try:
        await session_manager.handle_request(scope, receive, send)
    finally:
        _request_ctx.reset(token)
