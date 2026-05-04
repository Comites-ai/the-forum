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
TOOLS: list[Tool] = [
    Tool(
        name="create_scheduled_reminder",
        description=(
            "Create a recurring scheduled reminder for a user. The reminder runs on a "
            "cron schedule and sends the prompt to this agent at each tick; the agent's "
            "response is delivered to the user on their preferred platform. "
            "agent_id is determined automatically from authentication and does not need "
            "to be passed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable name (e.g. 'Daily goal review')",
                },
                "prompt": {
                    "type": "string",
                    "description": "Message sent to the agent at each tick",
                },
                "schedule": {
                    "type": "string",
                    "description": "Cron expression — minute hour dom month dow (e.g. '0 9 * * 1-5' for 9 AM weekdays)",
                },
                "user_id": {
                    "type": "string",
                    "description": "Unified user ID from the users collection",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone (e.g. 'America/New_York'). Defaults to UTC.",
                },
                "output_platform": {
                    "type": "string",
                    "enum": ["slack", "google_chat", "telegram"],
                    "description": (
                        "Platform to deliver the reminder on. If omitted, defaults to "
                        "the user's most recently used platform with this agent (or "
                        "'slack' if the user has no session yet)."
                    ),
                },
            },
            "required": ["name", "prompt", "schedule", "user_id"],
        },
    ),
    Tool(
        name="list_scheduled_reminders",
        description="List all scheduled reminders this agent has created for a user.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Unified user ID from the users collection",
                },
            },
            "required": ["user_id"],
        },
    ),
    Tool(
        name="update_scheduled_reminder",
        description=(
            "Update an existing scheduled reminder. Only the fields you pass are changed. "
            "The job must belong to the calling agent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job ID returned by list_scheduled_reminders",
                },
                "name": {"type": "string"},
                "prompt": {"type": "string"},
                "schedule": {"type": "string", "description": "New cron expression"},
                "timezone": {"type": "string", "description": "New IANA timezone"},
                "enabled": {
                    "type": "boolean",
                    "description": "Set false to pause the reminder, true to resume",
                },
            },
            "required": ["job_id"],
        },
    ),
    Tool(
        name="delete_scheduled_reminder",
        description="Delete a scheduled reminder. The job must belong to the calling agent.",
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job ID returned by list_scheduled_reminders",
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


async def _handle_create(args: dict[str, Any]) -> str:
    agent = _agent()
    user_id = args["user_id"]
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
    jobs = await _service().list_jobs(agent_id=agent.id, user_id=args["user_id"])
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
