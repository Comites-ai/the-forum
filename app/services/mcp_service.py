"""MCP aggregation proxy service.

Connects to backing MCP servers (as a client) and aggregates their tools.
Used by the MCP router to present a per-agent or per-server tool surface.

Supports three transports:
  - 'sse'              legacy MCP SSE transport
  - 'streamable_http'  MCP spec 2025-03-26 (preferred for HTTP servers)
  - 'stdio'            subprocess over stdin/stdout (npx/uvx allowlist)
"""
import logging
from contextlib import asynccontextmanager
from typing import Optional

from google.cloud import secretmanager
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent, Tool

from app.config import get_settings
from app.models.agent import ALLOWED_STDIO_COMMANDS, MCPServerConfig

logger = logging.getLogger(__name__)


class MCPService:
    """Aggregates tools from configured backing MCP servers."""

    def __init__(self):
        self.settings = get_settings()
        self._global_api_key: Optional[str] = None

    def fetch_secret(self, secret_name: str, project_id: str) -> str:
        """Fetch a secret value from Secret Manager."""
        client = secretmanager.SecretManagerServiceClient()
        secret_path = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": secret_path})
        return response.payload.data.decode("utf-8")

    def get_api_key(self, config: MCPServerConfig) -> Optional[str]:
        """Resolve the API key for an HTTP-transport backing MCP server config."""
        if not config.api_key_secret:
            return None
        project = config.api_key_project_id or self.settings.gcp_project_id
        try:
            return self.fetch_secret(config.api_key_secret, project)
        except Exception as e:
            logger.error(f"Failed to fetch API key for MCP server '{config.name}': {e}")
            return None

    def get_global_api_key(self) -> Optional[str]:
        """Return the global MCP endpoint API key (cached per process)."""
        if self._global_api_key is None and self.settings.mcp_global_api_key_secret:
            try:
                self._global_api_key = self.fetch_secret(
                    self.settings.mcp_global_api_key_secret,
                    self.settings.gcp_project_id,
                )
            except Exception as e:
                logger.error(f"Failed to fetch global MCP API key: {e}")
        return self._global_api_key

    def _resolve_stdio_env(self, config: MCPServerConfig) -> dict[str, str]:
        """Build the env dict for a stdio subprocess, resolving Secret Manager refs."""
        env: dict[str, str] = dict(config.env or {})
        if config.env_secrets:
            project = config.api_key_project_id or self.settings.gcp_project_id
            for env_name, secret_name in config.env_secrets.items():
                try:
                    env[env_name] = self.fetch_secret(secret_name, project)
                except Exception as e:
                    logger.error(
                        f"Failed to fetch secret '{secret_name}' for env '{env_name}' "
                        f"on MCP server '{config.name}': {e}"
                    )
        return env

    @asynccontextmanager
    async def _connect(self, config: MCPServerConfig):
        """Open an MCP ClientSession for a backing server.

        Yields an initialized ClientSession; handles the transport selection
        and initialization handshake.
        """
        if config.transport == "sse":
            api_key = self.get_api_key(config)
            headers = {config.api_key_header: api_key} if api_key else {}
            async with sse_client(config.url, headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        elif config.transport == "streamable_http":
            api_key = self.get_api_key(config)
            headers = {config.api_key_header: api_key} if api_key else {}
            # streamablehttp_client yields (read, write, session_id_callback)
            async with streamablehttp_client(config.url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        elif config.transport == "stdio":
            if config.command not in ALLOWED_STDIO_COMMANDS:
                raise ValueError(
                    f"stdio command {config.command!r} is not in the allowlist"
                )
            params = StdioServerParameters(
                command=config.command,
                args=list(config.args or []),
                env=self._resolve_stdio_env(config),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            raise ValueError(f"Unknown MCP transport: {config.transport!r}")

    async def get_aggregated_tools(
        self,
        mcp_servers: list[MCPServerConfig],
        prefix_tools: bool = True,
    ) -> list[Tool]:
        """Connect to each enabled backing server and return their combined tools.

        When prefix_tools is True (agent-scoped endpoints), tool names are
        prefixed with '{server.name}__' to prevent collisions across servers.
        When False (per-server global endpoints), names are returned unchanged.
        Servers that are unreachable are skipped (logged but not fatal).
        """
        all_tools: list[Tool] = []
        for server_config in mcp_servers:
            if not server_config.enabled:
                continue
            try:
                async with self._connect(server_config) as session:
                    result = await session.list_tools()
                for tool in result.tools:
                    tool_name = (
                        f"{server_config.name}__{tool.name}" if prefix_tools else tool.name
                    )
                    all_tools.append(
                        Tool(
                            name=tool_name,
                            description=tool.description,
                            inputSchema=tool.inputSchema,
                        )
                    )
                logger.info(
                    f"Loaded {len(result.tools)} tools from MCP server '{server_config.name}' "
                    f"(transport={server_config.transport})"
                )
            except Exception as e:
                logger.error(
                    f"Failed to load tools from MCP server '{server_config.name}' "
                    f"(transport={server_config.transport}): {e}"
                )
        return all_tools

    async def call_backing_tool(
        self,
        mcp_servers: list[MCPServerConfig],
        tool_name: str,
        arguments: dict,
        prefix_tools: bool = True,
    ) -> list:
        """Route a tool call to the correct backing server.

        When prefix_tools is True, tool_name must be '{server_name}__{tool_name}'.
        When False, tool_name is passed through to the single configured server.
        Returns a list of MCP Content objects (TextContent, ImageContent, etc.).
        """
        if prefix_tools:
            parts = tool_name.split("__", 1)
            if len(parts) != 2:
                msg = f"Invalid tool name format '{tool_name}' — expected '{{server}}__{{tool}}'"
                logger.error(msg)
                return [TextContent(type="text", text=f"Error: {msg}")]
            server_name, backing_tool_name = parts
            server_config = next(
                (s for s in mcp_servers if s.name == server_name and s.enabled),
                None,
            )
        else:
            enabled = [s for s in mcp_servers if s.enabled]
            if len(enabled) != 1:
                msg = (
                    f"Per-server endpoint expected exactly one enabled backing server, "
                    f"got {len(enabled)}"
                )
                logger.error(msg)
                return [TextContent(type="text", text=f"Error: {msg}")]
            server_config = enabled[0]
            backing_tool_name = tool_name

        if not server_config:
            msg = f"No enabled MCP server for tool '{tool_name}'"
            logger.error(msg)
            return [TextContent(type="text", text=f"Error: {msg}")]

        try:
            async with self._connect(server_config) as session:
                result = await session.call_tool(backing_tool_name, arguments)
            logger.info(
                f"Called tool '{backing_tool_name}' on MCP server '{server_config.name}' "
                f"(isError={result.isError})"
            )
            return result.content
        except Exception as e:
            msg = f"Error calling tool '{backing_tool_name}' on server '{server_config.name}': {e}"
            logger.error(msg)
            return [TextContent(type="text", text=f"Error: {msg}")]
