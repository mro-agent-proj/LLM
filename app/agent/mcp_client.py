"""PDF get_tools() contract; only readonly tools are exposed to these nodes."""

import asyncio
import os
from typing import Literal

from langchain_core.tools import BaseTool

BUSINESS_TOOLS = frozenset(
    {
        "get_aircraft",
        "get_maintenance_history",
        "search_similar_history",
        "list_departments",
    }
)
RAG_TOOLS = frozenset({"search_sdr", "search_amm", "get_document"})


class MCPToolProvider:
    def __init__(self) -> None:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        self.client = MultiServerMCPClient(
            {
                "business": {
                    "transport": "streamable_http",
                    "url": os.getenv("BUSINESS_MCP_URL", "http://localhost:8101/mcp"),
                },
                "rag": {
                    "transport": "streamable_http",
                    "url": os.getenv("RAG_MCP_URL", "http://localhost:8102/mcp"),
                },
            }
        )
        self.cache: dict[str, list[BaseTool]] = {}
        self.lock = asyncio.Lock()

    async def __call__(self, *servers: Literal["business", "rag"]) -> list[BaseTool]:
        async with self.lock:
            for server in servers:
                if server not in self.cache:
                    allowed = BUSINESS_TOOLS if server == "business" else RAG_TOOLS
                    discovered = await asyncio.wait_for(
                        self.client.get_tools(server_name=server),
                        timeout=10,
                    )
                    self.cache[server] = [tool for tool in discovered if tool.name in allowed]
        return [tool for server in servers for tool in self.cache[server]]


_provider: MCPToolProvider | None = None


async def get_tools(*servers: Literal["business", "rag"]) -> list[BaseTool]:
    global _provider
    if _provider is None:
        _provider = MCPToolProvider()
    return await _provider(*servers)
