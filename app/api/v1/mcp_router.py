"""MCP (Model Context Protocol) proxy endpoints.

Two access modes:

  Per-server global endpoint  — /api/v1/mcp/global/{server_name}
    Requires X-API-Key header (shared key for all global servers).  Proxies
    exactly ONE backing MCP server from the top-level `mcp_servers` Firestore
    collection.  Use one endpoint per server in Claude Code / owner tools.

  Agent endpoint              — /api/v1/mcp/{agent_id}
    No additional auth (agent_id scopes access).  Aggregates all MCP servers
    listed in that agent's Firestore record, prefixing tool names with
    '{server_name}__' to prevent collisions.  Used by Vertex AI ADK agents
    via MCPToolset.

Transport notes:
  Primary:  Streamable HTTP (MCP spec 2025-03-26) via StreamableHTTPSessionManager.
  Legacy:   SSE transport at /{agent_id}/sse and /global/{server_name}/sse
            for ADK MCPToolset compatibility.  SSE sessions are stored in
            process memory; in multi-instance Cloud Run deployments,
            GET /sse and POST /messages must reach the same instance.
            Prefer Streamable HTTP for new integrations.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool
from starlette.responses import Response

from app.models.agent import Agent, MCPServerConfig
from app.services.mcp_service import MCPService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_firestore(request: Request):
    return request.app.state.firestore


def _get_mcp_service(request: Request) -> MCPService:
    if not hasattr(request.app.state, "mcp_service"):
        request.app.state.mcp_service = MCPService()
    return request.app.state.mcp_service


def _get_sse_transport(request: Request, key: str, messages_path: str) -> SseServerTransport:
    """Return (and lazily create) the SSE transport stored per key.

    The same transport instance must handle both GET /sse and POST /messages
    for a given session.  In multi-instance Cloud Run, sticky routing is not
    guaranteed, so prefer the Streamable HTTP endpoints for production use.
    """
    if not hasattr(request.app.state, "mcp_sse_transports"):
        request.app.state.mcp_sse_transports = {}
    transports: dict[str, SseServerTransport] = request.app.state.mcp_sse_transports
    if key not in transports:
        transports[key] = SseServerTransport(messages_path)
    return transports[key]


def _build_mcp_server(
    server_name: str,
    mcp_svc: MCPService,
    mcp_servers: list[MCPServerConfig],
    prefix_tools: bool = True,
) -> Server:
    """Create a low-level MCP Server whose tool handlers proxy to backing servers.

    When prefix_tools is True, aggregates multiple servers with '{name}__' tool
    prefixes.  When False (per-server global endpoints), exposes the single
    backing server's tools unprefixed.
    """
    server = Server(server_name)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return await mcp_svc.get_aggregated_tools(mcp_servers, prefix_tools=prefix_tools)

    @server.call_tool()
    async def call_tool(name: str, arguments: Optional[dict]) -> list:
        return await mcp_svc.call_backing_tool(
            mcp_servers, name, arguments or {}, prefix_tools=prefix_tools
        )

    return server


async def _get_agent_or_404(agent_id: str, request: Request) -> Agent:
    agent = await _get_firestore(request).get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


async def _get_global_server_or_404(server_name: str, request: Request) -> MCPServerConfig:
    server = await _get_firestore(request).get_global_mcp_server_by_name(server_name)
    if not server:
        raise HTTPException(
            status_code=404,
            detail=f"Global MCP server '{server_name}' not found or disabled",
        )
    return server


def _validate_global_api_key(x_api_key: str, mcp_svc: MCPService) -> None:
    """Raise 401 if the provided API key doesn't match the configured global key."""
    from app.config import get_settings
    settings = get_settings()
    if not settings.mcp_global_api_key_secret:
        raise HTTPException(
            status_code=503,
            detail="Global MCP endpoints are not configured (MCP_GLOBAL_API_KEY_SECRET not set)",
        )
    expected = mcp_svc.get_global_api_key()
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Global per-server endpoints
# Registration order matters: literal 'global' segment must precede /{agent_id}.
# ---------------------------------------------------------------------------

@router.api_route("/global/{server_name}", methods=["GET", "POST", "DELETE"])
async def mcp_global_server_streamable(
    server_name: str,
    request: Request,
    x_api_key: str = Header(..., description="Global MCP endpoint API key"),
):
    """Streamable HTTP MCP endpoint for a single globally-registered server.

    Each server in the `mcp_servers` Firestore collection gets its own URL
    here.  Claude Code and other MCP clients should add one entry per server.
    """
    mcp_svc = _get_mcp_service(request)
    _validate_global_api_key(x_api_key, mcp_svc)

    server_config = await _get_global_server_or_404(server_name, request)
    server = _build_mcp_server(
        f"middleware-global-{server_name}",
        mcp_svc,
        [server_config],
        prefix_tools=False,
    )

    session_manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=False)
    async with session_manager.run():
        await session_manager.handle_request(request.scope, request.receive, request._send)
    return Response()


@router.get("/global/{server_name}/sse")
async def mcp_global_server_sse(
    server_name: str,
    request: Request,
    x_api_key: str = Header(..., description="Global MCP endpoint API key"),
):
    """Legacy SSE MCP endpoint for a single globally-registered server."""
    mcp_svc = _get_mcp_service(request)
    _validate_global_api_key(x_api_key, mcp_svc)

    server_config = await _get_global_server_or_404(server_name, request)
    server = _build_mcp_server(
        f"middleware-global-sse-{server_name}",
        mcp_svc,
        [server_config],
        prefix_tools=False,
    )
    transport = _get_sse_transport(
        request,
        f"global:{server_name}",
        f"/api/v1/mcp/global/{server_name}/messages",
    )

    async with transport.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())
    return Response()


@router.post("/global/{server_name}/messages")
async def mcp_global_server_sse_messages(
    server_name: str,
    request: Request,
    x_api_key: str = Header(..., description="Global MCP endpoint API key"),
):
    """Client-to-server message handler for the global SSE transport."""
    mcp_svc = _get_mcp_service(request)
    _validate_global_api_key(x_api_key, mcp_svc)

    transport = _get_sse_transport(
        request,
        f"global:{server_name}",
        f"/api/v1/mcp/global/{server_name}/messages",
    )
    await transport.handle_post_message(request.scope, request.receive, request._send)
    return Response()


# ---------------------------------------------------------------------------
# Agent-scoped endpoints
# ---------------------------------------------------------------------------

@router.api_route("/{agent_id}", methods=["GET", "POST", "DELETE"])
async def mcp_agent_streamable(agent_id: str, request: Request):
    """Streamable HTTP MCP endpoint for a specific agent.

    Exposes all MCP servers configured on that agent's Firestore record,
    aggregated into a single tool surface with '{server_name}__' tool prefixes.
    """
    agent = await _get_agent_or_404(agent_id, request)
    mcp_servers = [s for s in (agent.mcp_servers or []) if s.enabled]

    mcp_svc = _get_mcp_service(request)
    server = _build_mcp_server(
        f"middleware-agent-{agent_id}",
        mcp_svc,
        mcp_servers,
        prefix_tools=True,
    )

    session_manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=False)
    async with session_manager.run():
        await session_manager.handle_request(request.scope, request.receive, request._send)
    return Response()


@router.get("/{agent_id}/sse")
async def mcp_agent_sse(agent_id: str, request: Request):
    """Legacy SSE MCP endpoint for ADK MCPToolset compatibility.

    Configure your ADK agent with:
        MCPToolset(connection_params=SseServerParams(
            url="{middleware_url}/api/v1/mcp/{agent_id}/sse"
        ))
    """
    agent = await _get_agent_or_404(agent_id, request)
    mcp_servers = [s for s in (agent.mcp_servers or []) if s.enabled]

    mcp_svc = _get_mcp_service(request)
    server = _build_mcp_server(
        f"middleware-agent-sse-{agent_id}",
        mcp_svc,
        mcp_servers,
        prefix_tools=True,
    )
    transport = _get_sse_transport(
        request,
        f"agent:{agent_id}",
        f"/api/v1/mcp/{agent_id}/messages",
    )

    async with transport.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())
    return Response()


@router.post("/{agent_id}/messages")
async def mcp_agent_sse_messages(agent_id: str, request: Request):
    """Client-to-server message handler for the agent SSE transport."""
    transport = _get_sse_transport(
        request,
        f"agent:{agent_id}",
        f"/api/v1/mcp/{agent_id}/messages",
    )
    await transport.handle_post_message(request.scope, request.receive, request._send)
    return Response()
