"""PDF get_tools() contract; only readonly tools are exposed to these nodes."""

import asyncio
import os
from typing import Literal

from langchain_core.tools import BaseTool

from app.agent.transport import transient_error

BUSINESS_TOOLS = frozenset(
    {
        "get_request_context",
        "get_aircraft_history",
        "search_departments",
    }
)
RAG_TOOLS = frozenset(
    {
        "search_jasc_codes",
        "search_sdr_cases",
        "search_amm_references",
        "search_technical_docs",
        "get_evidence",
    }
)
OPTIONAL_TOOLS = frozenset({"search_technical_docs"})


class MCPContractError(RuntimeError):
    pass


class MCPToolProvider:
    def __init__(self, *, rag_token: str | None = None, business_token: str | None = None) -> None:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        self.client = MultiServerMCPClient(
            {
                "business": {
                    "transport": "streamable_http",
                    "url": os.getenv("BUSINESS_MCP_URL", "http://localhost:8101/mcp"),
                    **(
                        {"headers": {"Authorization": f"Bearer {business_token}"}}
                        if business_token
                        else {}
                    ),
                },
                "rag": {
                    "transport": "streamable_http",
                    "url": os.getenv("RAG_MCP_URL", "http://localhost:8102/mcp"),
                    **({"headers": {"Authorization": f"Bearer {rag_token}"}} if rag_token else {}),
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
                    for attempt in range(2):
                        try:
                            discovered = await asyncio.wait_for(
                                self.client.get_tools(server_name=server), timeout=10
                            )
                            break
                        except Exception as exc:
                            if not transient_error(exc) or attempt:
                                raise
                    selected = [tool for tool in discovered if tool.name in allowed]
                    missing = allowed - OPTIONAL_TOOLS - {tool.name for tool in selected}
                    if missing:
                        raise MCPContractError(
                            f"{server} MCP missing required tools: {sorted(missing)}"
                        )
                    self.cache[server] = selected
        return [tool for server in servers for tool in self.cache[server]]


_provider: MCPToolProvider | None = None


async def get_tools(*servers: Literal["business", "rag"]) -> list[BaseTool]:
    global _provider
    if _provider is None:
        _provider = MCPToolProvider()
    return await _provider(*servers)
