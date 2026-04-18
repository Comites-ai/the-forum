"""MCP aggregation proxy service.

Connects to backing MCP servers (as a client) and aggregates their tools.
Used by the MCP router to present a per-agent or global tool surface.
"""
import logging
from typing import Optional

from google.cloud import secretmanager
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession
from mcp.types import Tool, TextContent

from app.config import get_settings
from app.models.agent import MCPServerConfig

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
        """Resolve the API key for a backing MCP server config."""
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

    async def get_aggregated_tools(
        self, mcp_servers: list[MCPServerConfig]
    ) -> list[Tool]:
        """Connect to each enabled backing server and return their combined tools.

        Tools are prefixed with '{server.name}__' to prevent name collisions.
        Servers that are unreachable are skipped (logged but not fatal).
        """
        all_tools: list[Tool] = []
        for server_config in mcp_servers:
            if not server_config.enabled:
                continue
            try:
                api_key = self.get_api_key(server_config)
                headers = {server_config.api_key_header: api_key} if api_key else {}
                async with sse_client(server_config.url, headers=headers) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        for tool in result.tools:
                            all_tools.append(
                                Tool(
                                    name=f"{server_config.name}__{tool.name}",
                                    description=tool.description,
                                    inputSchema=tool.inputSchema,
                                )
                            )
                logger.info(
                    f"Loaded {len(result.tools)} tools from MCP server '{server_config.name}'"
                )
            except Exception as e:
                logger.error(
                    f"Failed to load tools from MCP server '{server_config.name}' "
                    f"({server_config.url}): {e}"
                )
        return all_tools

    async def call_backing_tool(
        self,
        mcp_servers: list[MCPServerConfig],
        prefixed_name: str,
        arguments: dict,
    ) -> list:
        """Route a prefixed tool call to the correct backing server.

        Expects tool names in the format '{server_name}__{tool_name}'.
        Returns a list of MCP Content objects (TextContent, ImageContent, etc.).
        """
        parts = prefixed_name.split("__", 1)
        if len(parts) != 2:
            msg = f"Invalid tool name format '{prefixed_name}' — expected '{{server}}_{{tool}}'"
            logger.error(msg)
            return [TextContent(type="text", text=f"Error: {msg}")]

        server_name, tool_name = parts
        server_config = next(
            (s for s in mcp_servers if s.name == server_name and s.enabled),
            None,
        )
        if not server_config:
            msg = f"No enabled MCP server named '{server_name}'"
            logger.error(msg)
            return [TextContent(type="text", text=f"Error: {msg}")]

        try:
            api_key = self.get_api_key(server_config)
            headers = {server_config.api_key_header: api_key} if api_key else {}
            async with sse_client(server_config.url, headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    logger.info(
                        f"Called tool '{tool_name}' on MCP server '{server_name}' "
                        f"(isError={result.isError})"
                    )
                    return result.content
        except Exception as e:
            msg = f"Error calling tool '{tool_name}' on server '{server_name}': {e}"
            logger.error(msg)
            return [TextContent(type="text", text=f"Error: {msg}")]
