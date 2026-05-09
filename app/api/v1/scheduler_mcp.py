# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Scheduler MCP server (Streamable HTTP).

Exposes four tools that wrap the existing ScheduledJobService:
  - create_scheduled_reminder
  - list_scheduled_reminders
  - update_scheduled_reminder
  - delete_scheduled_reminder

Authentication: each agent has a scheduler API key whose SHA-256 hash is
stored on its Firestore agent document. The agent presents the plaintext
key in the X-API-Key header on every request. The middleware looks up
the matching agent and injects agent_id into tool calls so the LLM never
has to pass it itself.

Mounted as an ASGI app at /api/v1/mcp/scheduler. The MCP session manager
runs in stateless mode (one request = one tool call), and the manager's
lifecycle is managed in the FastAPI app's lifespan in app/main.py.
"""
import contextvars
import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Optional

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool

from app.models.agent import Agent
from app.schemas.scheduled_job import ScheduledJobCreate, ScheduledJobUpdate
from app.services.firestore_service import FirestoreService
from app.services.scheduled_job_service import ScheduledJobService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-request context
# ---------------------------------------------------------------------------
# The MCP Server's call_tool handler is registered once at module load, so it
# can't see request state directly. We thread the authenticated agent and the
# Firestore client through a ContextVar that the ASGI handler sets before
# delegating to the session manager. ContextVar propagates across asyncio
# task boundaries within the same call chain.
_request_ctx: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "scheduler_mcp_request_ctx", default=None
)


def hash_api_key(plaintext: str) -> str:
    """SHA-256 hex digest of an API key. Same hashing used for storage + lookup."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _ctx() -> dict:
    ctx = _request_ctx.get()
    if ctx is None:
        raise RuntimeError("scheduler MCP tool called outside of an authenticated request")
    return ctx


def _agent() -> Agent:
    return _ctx()["agent"]


def _firestore() -> FirestoreService:
    return _ctx()["firestore"]


def _service() -> ScheduledJobService:
    return ScheduledJobService(_firestore())


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
# The user_name field description is shared between create and list. It is
# load-bearing: the LLM has no other way to know which user the reminder is
# for, and prior to this design it was guessing the user's display name and
# putting it in user_id, which produced silently-failing duplicate jobs.
_USER_NAME_DESC = (
    "The user's name EXACTLY as it appears in the '[From: <name>] ...' prefix "
    "of the user's most recent message. Copy that value verbatim. Do NOT pass "
    "platform-specific display names (e.g. Slack handles), email addresses, "
    "Slack/Google Chat/Telegram IDs, or paraphrased versions of the name. "
    "If you cannot see a '[From: ...]' prefix, do not call this tool — ask "
    "the user who they are first."
)

TOOLS: list[Tool] = [
    Tool(
        name="create_scheduled_reminder",
        description=(
            "Create a recurring scheduled reminder for the user you are currently "
            "talking to. The reminder runs on a cron schedule; at each tick, the "
            "prompt is sent to this agent and the agent's response is delivered "
            "to the user. Use this when the user asks for things like 'remind me "
            "every weekday at 9 AM to review my goals'.\n\n"
            "agent_id is resolved automatically from authentication — do not pass "
            "it. user_name resolves to the unified user record server-side."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short human-readable name for the reminder (e.g. 'Daily goal review'). Shown back to the user when they list reminders.",
                },
                "prompt": {
                    "type": "string",
                    "description": "The exact message that will be sent to this agent at each scheduled tick. Write it as if the user is asking it (e.g. 'What should I focus on today?'), since the agent will respond as if to a fresh user message.",
                },
                "schedule": {
                    "type": "string",
                    "description": "5-field cron expression: 'minute hour day-of-month month day-of-week' (e.g. '0 9 * * 1-5' for 9 AM Mon-Fri).",
                },
                "user_name": {
                    "type": "string",
                    "description": _USER_NAME_DESC,
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone the cron is evaluated in (e.g. 'America/New_York'). Defaults to UTC.",
                },
                "output_platform": {
                    "type": "string",
                    "enum": ["slack", "google_chat", "telegram"],
                    "description": (
                        "Platform to deliver the reminder on. If omitted, defaults to "
                        "whichever platform the user most recently chatted with this "
                        "agent on (or 'slack' if they have no session yet). Only set "
                        "this if the user explicitly asks for a different platform."
                    ),
                },
            },
            "required": ["name", "prompt", "schedule", "user_name"],
        },
    ),
    Tool(
        name="list_scheduled_reminders",
        description=(
            "List all scheduled reminders this agent has created for the user you "
            "are currently talking to. Returns each reminder's job_id (needed to "
            "update or delete it), name, schedule, and current state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user_name": {
                    "type": "string",
                    "description": _USER_NAME_DESC,
                },
            },
            "required": ["user_name"],
        },
    ),
    Tool(
        name="update_scheduled_reminder",
        description=(
            "Update an existing scheduled reminder. Only the fields you pass are "
            "changed; everything else is preserved. To find the job_id, call "
            "list_scheduled_reminders first. The job must have been created by "
            "this agent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The reminder's id, as returned by list_scheduled_reminders. Not the reminder's name.",
                },
                "name": {"type": "string", "description": "New display name."},
                "prompt": {"type": "string", "description": "New scheduled prompt text."},
                "schedule": {"type": "string", "description": "New cron expression."},
                "timezone": {"type": "string", "description": "New IANA timezone."},
                "enabled": {
                    "type": "boolean",
                    "description": "Set false to pause the reminder (it stays in the system but stops firing); true to resume.",
                },
            },
            "required": ["job_id"],
        },
    ),
    Tool(
        name="delete_scheduled_reminder",
        description=(
            "Permanently delete a scheduled reminder. To find the job_id, call "
            "list_scheduled_reminders first. The job must have been created by "
            "this agent. There is no undo — if the user might want it back, "
            "consider update_scheduled_reminder with enabled=false instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The reminder's id, as returned by list_scheduled_reminders.",
                },
            },
            "required": ["job_id"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------
def _job_to_dict(job) -> dict:
    """Serialize a ScheduledJob to a JSON-safe dict for tool responses."""
    return {
        "id": job.id,
        "name": job.name,
        "prompt": job.prompt,
        "schedule": job.schedule,
        "timezone": job.timezone,
        "user_id": job.user_id,
        "output_platform": job.output_platform,
        "enabled": job.enabled,
        "last_execution_at": (
            job.last_execution_at.isoformat()
            if isinstance(job.last_execution_at, datetime)
            else None
        ),
        "consecutive_failures": job.consecutive_failures,
    }


async def _resolve_default_platform(user_id: str, agent_id: str) -> str:
    """Default output_platform to the user's most-recent session platform, or 'slack'."""
    session = await _firestore().get_session_by_user(user_id=user_id, agent_id=agent_id)
    if session and getattr(session, "last_active_platform", None):
        return session.last_active_platform
    return "slack"


async def _resolve_user_id_from_name(user_name: Any) -> str:
    """Resolve the LLM-supplied user_name to a unified user_id.

    The MCP tool schema asks the LLM to send the name from the
    `[From: <name>] ...` message prefix. The middleware does the lookup so
    the LLM never has to know (or guess) the Firestore document ID. If the
    name doesn't resolve, we raise with a message that nudges the LLM to
    fix what it sent — this is the validation that makes the bug class
    "LLM put the display name in user_id" impossible from the new path.
    """
    if not isinstance(user_name, str) or not user_name.strip():
        raise ValueError(
            "user_name is required. Pass the user's name from the "
            "'[From: <name>] ...' prefix of their most recent message."
        )
    name = user_name.strip()
    user = await _firestore().get_user_by_primary_name(name)
    if not user:
        raise ValueError(
            f"No user found with name {name!r}. Pass the exact name from the "
            f"'[From: <name>] ...' prefix of the user's most recent message — "
            f"do not paraphrase, translate, or use a platform handle."
        )
    return user.id


async def _handle_create(args: dict[str, Any]) -> str:
    agent = _agent()
    user_id = await _resolve_user_id_from_name(args.get("user_name"))
    output_platform = args.get("output_platform")
    if not output_platform:
        output_platform = await _resolve_default_platform(user_id, agent.id)

    create_data = ScheduledJobCreate(
        name=args["name"],
        prompt=args["prompt"],
        agent_id=agent.id,
        user_id=user_id,
        output_platform=output_platform,
        schedule=args["schedule"],
        timezone=args.get("timezone", "UTC"),
        enabled=True,
    )
    job = await _service().create_job(create_data)
    return json.dumps(_job_to_dict(job))


async def _handle_list(args: dict[str, Any]) -> str:
    agent = _agent()
    user_id = await _resolve_user_id_from_name(args.get("user_name"))
    jobs = await _service().list_jobs(agent_id=agent.id, user_id=user_id)
    return json.dumps([_job_to_dict(j) for j in jobs])


async def _load_owned_job(job_id: str):
    """Load a job and verify it belongs to the authenticated agent."""
    job = await _service().get_job(job_id)
    if not job or job.agent_id != _agent().id:
        # Don't leak whether the job exists for a different agent
        raise ValueError(f"Scheduled reminder not found: {job_id}")
    return job


async def _handle_update(args: dict[str, Any]) -> str:
    job_id = args["job_id"]
    await _load_owned_job(job_id)

    update_data = ScheduledJobUpdate(
        name=args.get("name"),
        prompt=args.get("prompt"),
        schedule=args.get("schedule"),
        timezone=args.get("timezone"),
        enabled=args.get("enabled"),
    )
    job = await _service().update_job(job_id, update_data)
    return json.dumps(_job_to_dict(job))


async def _handle_delete(args: dict[str, Any]) -> str:
    job_id = args["job_id"]
    await _load_owned_job(job_id)
    success = await _service().delete_job(job_id)
    return json.dumps({"success": success, "job_id": job_id})


# ---------------------------------------------------------------------------
# MCP Server registration
# ---------------------------------------------------------------------------
def _build_server() -> Server:
    server: Server = Server("scheduler")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def _call_tool(name: str, arguments: Optional[dict[str, Any]]) -> list[TextContent]:
        args = arguments or {}
        try:
            if name == "create_scheduled_reminder":
                result = await _handle_create(args)
            elif name == "list_scheduled_reminders":
                result = await _handle_list(args)
            elif name == "update_scheduled_reminder":
                result = await _handle_update(args)
            elif name == "delete_scheduled_reminder":
                result = await _handle_delete(args)
            else:
                raise ValueError(f"Unknown tool: {name}")
            return [TextContent(type="text", text=result)]
        except ValueError as e:
            # Surface as a clean error response — MCP wraps raised exceptions as isError=True
            raise
        except Exception as e:
            logger.exception(f"Error in scheduler MCP tool {name!r}: {e}")
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
        # Streamable HTTP doesn't use websockets; reject anything else.
        await _send_json(send, 405, {"error": "Method not allowed"})
        return

    # Pull headers (lowercase ASCII bytes per ASGI spec)
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

    # Resolve agent via Firestore. The middleware FastAPI app injects firestore
    # into scope["state"]["firestore"] via lifespan (see app/main.py).
    firestore: Optional[FirestoreService] = scope.get("state", {}).get("firestore")
    if firestore is None:
        # Fallback: resolve via the FastAPI app reference if present
        app = scope.get("app")
        firestore = getattr(getattr(app, "state", None), "firestore", None)
    if firestore is None:
        logger.error("scheduler MCP: FirestoreService not available in ASGI scope")
        await _send_json(send, 500, {"error": "Server misconfigured"})
        return

    agent = await firestore.get_agent_by_scheduler_api_key_hash(hash_api_key(api_key))
    if not agent:
        await _send_json(send, 401, {"error": "Invalid API key"})
        return

    token = _request_ctx.set({"agent": agent, "firestore": firestore})
    try:
        await session_manager.handle_request(scope, receive, send)
    finally:
        _request_ctx.reset(token)
