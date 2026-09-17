# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Agent-to-agent (A2A) MCP server (Streamable HTTP).

Lets any agent attached to The Forum communicate with any other attached
agent, with The Forum mediating the call. Three tools:

  - list_agents:          who else is attached, and what they can be asked
  - get_agent_inquiries:  the full inquiry records one agent publishes
  - query_agent:          send a message to another agent and get its reply

Agents publish their "inquiries" — the requests they know how to field —
on their Firestore agent document (registered at deploy time by each agent
repo's register_agent.py from an inquiries.json). See docs/FOR_AGENT_DEVELOPERS.md.

Because agents serve multiple human users, every query_agent call must say
who it is on behalf of. The target agent receives the message prefixed:

    [From Agent: <caller display name> | On Behalf Of: <user primary name>] <message>

and each (caller, target, user) triple gets its own persistent Vertex AI
session, so different users' exchanges never share conversation history.

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
from typing import Any, Optional

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool

from app.api.v1.scheduler_mcp import hash_api_key
from app.config import get_settings
from app.models.agent import Agent
from app.services.firestore_service import FirestoreService
from app.services.run_tracker import (
    CUT_OFF_TIMEOUT,
    active_run_marker,
    describe_cut_off,
    log_run_cut_off,
)
from app.services.session_healer import get_session_healer
from app.services.vertex_ai_service import VertexAIService

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

# Collection holding the A2A session docs the run marker rides on. Matches
# FirestoreService.a2a_sessions_collection.
A2A_SESSIONS = "a2a_sessions"

# How one query_agent call to the engine ended. Values are part of the
# `a2a_query` log contract — keep them stable.
QUERY_REPLIED = "replied"
QUERY_EMPTY_REPLY = "empty_reply"
QUERY_TIMED_OUT = "timed_out"
QUERY_FAILED = "failed"


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
            "structured answers."
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
    return f"{caller_id}__{target_id}__{user_id}"


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

    async def _query(session_id: str):
        # Marked before and cleared after, so the next query on this session
        # can tell that a run was started and never came back — the moment a
        # tool call gets stranded without a result (PLAT-42).
        await firestore.mark_run_started(
            session_key, active_run_marker(), collection=A2A_SESSIONS
        )
        started = time.monotonic()

        def _log(outcome: str, chunk_count: Optional[int] = None) -> None:
            _log_a2a_query(
                caller=caller,
                target=target,
                session_id=session_id,
                duration_seconds=time.monotonic() - started,
                outcome=outcome,
                chunk_count=chunk_count,
            )

        try:
            response = await asyncio.wait_for(
                vertex_ai.send_message(
                    agent_id=target.vertex_ai_agent_id,
                    session_id=session_id,
                    message=prefixed,
                ),
                timeout=QUERY_TIMEOUT_SECONDS,
            )
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
            # Deliberately not "try again later": the target is most likely
            # still working, so whatever was asked for may already have been
            # done. A blind retry of a non-idempotent request double-writes.
            raise ValueError(
                f"{target.display_name} did not reply within {QUERY_TIMEOUT_SECONDS}s, "
                f"so the Forum stopped waiting. Its reply is lost, but the work is "
                f"probably still running and anything you asked it to change may "
                f"already have happened. Ask it for the current state before sending "
                f"the same request again."
            )
        except Exception:
            # Logged so the timed-out share is a fraction of every call, not
            # only of the ones that came back one way or another.
            _log(QUERY_FAILED)
            raise

        replied = bool(response.text and response.text.strip())
        _log(
            QUERY_REPLIED if replied else QUERY_EMPTY_REPLY,
            chunk_count=response.chunk_count,
        )
        if replied:
            await firestore.clear_active_run(session_key, collection=A2A_SESSIONS)
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

    response = await _query(session_id)
    reply = (response.text or "").strip()

    if not reply and used_cached_session:
        # A dead session is indistinguishable from a genuinely empty reply:
        # the engine's SessionNotFoundError dies mid-stream and reaches us
        # as a cleanly-terminated stream with 0 chunks (#18). Since the
        # cached session is the prime suspect, drop it and retry ONCE on a
        # fresh one before declaring failure.
        logger.warning(
            f"Empty reply from {target.display_name} on cached A2A session "
            f"{session_key}; recreating the session and retrying once"
        )
        await firestore.delete_a2a_session(session_key)
        session_id = await _fresh_session()
        response = await _query(session_id)
        reply = (response.text or "").strip()

    if not reply:
        raise ValueError(
            f"{target.display_name} returned an empty reply "
            f"({response.chunk_count} chunks). It may be misconfigured — "
            f"try again or contact its operator."
        )

    return json.dumps({
        "agent": target.display_name,
        "on_behalf_of": user.primary_name,
        "reply": reply,
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
