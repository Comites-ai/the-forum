"""MCP (Model Context Protocol) proxy endpoints.

Exposes two access modes:

  Global endpoint  — /api/v1/mcp
    Requires X-API-Key header.  Aggregates ALL configured MCP servers:
    the `mcp_servers` Firestore collection (owner-managed) plus every
    agent's mcp_servers list.  For use by Claude Code and other owner tools.

  Agent endpoint   — /api/v1/mcp/{agent_id}
    No additional auth (agent_id scopes access).  Exposes only the MCP
    servers listed in that specific agent's Firestore record.  Used by
    Vertex AI ADK agents via MCPToolset.

Transport notes:
  Primary:  Streamable HTTP (MCP spec 2025-03-26) via StreamableHTTPSessionManager.
  Legacy:   SSE transport at /{agent_id}/sse for ADK MCPToolset compatibility.
            SSE transport stores sessions in process memory; in multi-instance
            Cloud Run deployments, GET /sse and POST /messages must reach the
            same instance.  Prefer Streamable HTTP for new integrations.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, TextContent
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


def _get_sse_transport(request: Request, key: str) -> SseServerTransport:
    """Return (and lazily create) the SSE transport stored per agent/global key.

    The same transport instance must handle both GET /sse and POST /messages
    for a given session.  In multi-instance Cloud Run, sticky routing is not
    guaranteed, so prefer the Streamable HTTP endpoints for production use.
    """
    if not hasattr(request.app.state, "mcp_sse_transports"):
        request.app.state.mcp_sse_transports = {}
    transports: dict[str, SseServerTransport] = request.app.state.mcp_sse_transports
    if key not in transports:
        if key == "global":
            messages_path = "/api/v1/mcp/sse/messages"
        else:
            messages_path = f"/api/v1/mcp/{key}/messages"
        transports[key] = SseServerTransport(messages_path)
    return transports[key]


def _build_mcp_server(server_name: str, mcp_svc: MCPService, mcp_servers: list[MCPServerConfig]) -> Server:
    """Create a low-level MCP Server whose tool handlers proxy to backing servers."""
    server = Server(server_name)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return await mcp_svc.get_aggregated_tools(mcp_servers)

    @server.call_tool()
    async def call_tool(name: str, arguments: Optional[dict]) -> list:
        return await mcp_svc.call_backing_tool(mcp_servers, name, arguments or {})

    return server


async def _get_agent_or_404(agent_id: str, request: Request) -> Agent:
    agent = await _get_firestore(request).get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


def _validate_global_api_key(x_api_key: str, mcp_svc: MCPService) -> None:
    """Raise 401 if the provided API key doesn't match the configured global key."""
    from app.config import get_settings
    settings = get_settings()
    if not settings.mcp_global_api_key_secret:
        raise HTTPException(
            status_code=503,
            detail="Global MCP endpoint is not configured (MCP_GLOBAL_API_KEY_SECRET not set)",
        )
    expected = mcp_svc.get_global_api_key()
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Global endpoint (Streamable HTTP, authenticated)
# Route registration order matters: exact paths before parameterised ones.
# ---------------------------------------------------------------------------

@router.api_route("", methods=["GET", "POST", "DELETE"])
async def mcp_global_streamable(
    request: Request,
    x_api_key: str = Header(..., description="Global MCP endpoint API key"),
):
    """Streamable HTTP MCP endpoint exposing all configured MCP servers.

    Requires the X-API-Key header.  Suitable for Claude Code and other
    owner-level integrations.
    """
    mcp_svc = _get_mcp_service(request)
    _validate_global_api_key(x_api_key, mcp_svc)

    mcp_servers = await _get_firestore(request).get_all_mcp_servers()
    server = _build_mcp_server("middleware-global-proxy", mcp_svc, mcp_servers)

    session_manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=False)
    async with session_manager.run():
        await session_manager.handle_request(request.scope, request.receive, request._send)
    return Response()


# ---------------------------------------------------------------------------
# Agent-scoped endpoints
# ---------------------------------------------------------------------------

@router.api_route("/{agent_id}", methods=["GET", "POST", "DELETE"])
async def mcp_agent_streamable(agent_id: str, request: Request):
    """Streamable HTTP MCP endpoint for a specific agent.

    Exposes only the MCP servers configured on that agent's Firestore record.
    Used by Vertex AI ADK agents or any MCP client that supports Streamable HTTP.
    """
    agent = await _get_agent_or_404(agent_id, request)
    mcp_servers = [s for s in (agent.mcp_servers or []) if s.enabled]

    mcp_svc = _get_mcp_service(request)
    server = _build_mcp_server(f"middleware-agent-proxy-{agent_id}", mcp_svc, mcp_servers)

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

    Note: SSE sessions are stored in process memory.  In multi-instance
    Cloud Run deployments, GET /sse and POST /messages must reach the same
    instance.  Prefer the Streamable HTTP endpoint for new integrations.
    """
    agent = await _get_agent_or_404(agent_id, request)
    mcp_servers = [s for s in (agent.mcp_servers or []) if s.enabled]

    mcp_svc = _get_mcp_service(request)
    server = _build_mcp_server(f"middleware-sse-proxy-{agent_id}", mcp_svc, mcp_servers)
    transport = _get_sse_transport(request, agent_id)

    async with transport.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())
    return Response()


@router.post("/{agent_id}/messages")
async def mcp_agent_sse_messages(agent_id: str, request: Request):
    """Client-to-server message handler for the legacy SSE transport."""
    transport = _get_sse_transport(request, agent_id)
    await transport.handle_post_message(request.scope, request.receive, request._send)
    return Response()
