"""Real localhost Streamable HTTP transport; synthetic data and no model API."""

import asyncio
import socket

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from app.agent.demo import example_state
from app.agent.mcp_client import MCPToolProvider
from app.agent.schemas import AgentContext
from app.agent.settings import Settings
from app.agent.tools import ToolGateway


@pytest.mark.asyncio
async def test_actual_mcp_transport_artifact_and_readonly_tool_filter(monkeypatch):
    mcp = FastMCP("synthetic-contract-test", stateless_http=True, json_response=True)

    @mcp.tool()
    async def search_sdr(query: str, aircraft_model: str | None = None) -> dict:
        return {
            "items": [
                {
                    "id": "SDR-TRANSPORT-TEST",
                    "source": "SDR",
                    "content": "SYNTHETIC: wheel vibration.",
                    "metadata": {"aircraft_model": aircraft_model, "mock": True},
                }
            ]
        }

    @mcp.tool()
    async def assign_request() -> dict:
        raise AssertionError("A write tool must never be exposed to LLM2/3")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.02)
        assert server.started
        monkeypatch.setenv("RAG_MCP_URL", f"http://127.0.0.1:{port}/mcp")
        provider = MCPToolProvider()
        tools = await provider("rag")
        assert [tool.name for tool in tools] == ["search_sdr"]
        state = example_state()
        context = AgentContext(request_id=state["request_id"])
        runner = ToolGateway(tools, state, context, Settings())
        result = await runner.call("search_sdr", {"query": "vibration"}, "전송·원문 확인")
        assert not result.get("isError")
        assert context.records[0].ref_id == "SDR-TRANSPORT-TEST"
        assert context.records[0].content == "SYNTHETIC: wheel vibration."
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        sock.close()
