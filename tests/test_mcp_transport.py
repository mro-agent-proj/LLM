"""Real localhost MCP transport with synthetic v0.3 data and request-bound authorization."""

import asyncio
import json
import socket

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from starlette.responses import JSONResponse

from app.agent.demo import DemoTools, ScriptedDemoModel, example_state
from app.agent.mcp_client import MCPContractError, MCPToolProvider
from app.agent.runtime import AgentRuntime
from app.agent.settings import Settings
from app.agent.tools import ToolGateway, get_context
from app.agent.workflow import build_graph, completion_payload


@pytest.mark.asyncio
async def test_actual_mcp_transport_v03_auth_and_full_graph(monkeypatch):
    source = DemoTools()
    mcp = FastMCP("synthetic-v03-contract", stateless_http=True, json_response=True)

    @mcp.tool()
    async def get_request_context(request_id: str) -> dict:
        return source.response("get_request_context", locals())

    @mcp.tool()
    async def get_aircraft_history(
        request_id: str, context_version: int, query: str | None = None, limit: int = 20
    ) -> dict:
        return source.response("get_aircraft_history", locals())

    @mcp.tool()
    async def search_departments(
        request_id: str, context_version: int, query: str | None = None, limit: int = 5
    ) -> dict:
        return source.response("search_departments", locals())

    @mcp.tool()
    async def search_jasc_codes(
        request_id: str,
        context_version: int,
        query: str | None = None,
        codes: list[str] | None = None,
        limit: int = 5,
    ) -> dict:
        return source.response(
            "search_jasc_codes", {k: v for k, v in locals().items() if v is not None}
        )

    @mcp.tool()
    async def search_sdr_cases(
        request_id: str, context_version: int, query: str, limit: int = 5
    ) -> dict:
        if query == "synthetic-rate-limited":
            response = {
                "status": "ERROR",
                "error": {"code": "RATE_LIMITED", "retryable": True, "retry_after_ms": 0},
            }
            return CallToolResult(
                isError=True,
                structuredContent=response,
                content=[TextContent(type="text", text=json.dumps(response))],
            )
        return source.response("search_sdr_cases", locals())

    @mcp.tool()
    async def search_amm_references(
        request_id: str, context_version: int, query: str, limit: int = 5
    ) -> dict:
        return source.response("search_amm_references", locals())

    @mcp.tool()
    async def get_evidence(request_id: str, context_version: int, evidence_id: str) -> dict:
        return source.response("get_evidence", locals())

    @mcp.tool()
    async def search_technical_docs(
        request_id: str,
        context_version: int,
        query: str,
        include_unknown_applicability: bool = False,
        limit: int = 5,
    ) -> dict:
        return source.response("search_technical_docs", locals())

    @mcp.tool()
    async def assign_request() -> dict:
        raise AssertionError("A write tool must never be exposed")

    app = mcp.streamable_http_app()
    seen_auth = []

    class Delegation:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                auth = dict(scope["headers"]).get(b"authorization")
                seen_auth.append(auth)
                if auth != b"Bearer synthetic-delegation":
                    return await JSONResponse({"error": "unauthorized"}, status_code=401)(
                        scope, receive, send
                    )
            await app(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(Delegation(), log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.02)
        assert server.started
        monkeypatch.setenv("BUSINESS_MCP_URL", f"http://127.0.0.1:{port}/mcp")
        monkeypatch.setenv("RAG_MCP_URL", f"http://127.0.0.1:{port}/mcp")
        provider = MCPToolProvider(
            rag_token="synthetic-delegation", business_token="synthetic-delegation"
        )
        tools = await provider("business", "rag")
        assert len(tools) == 8
        assert "assign_request" not in {tool.name for tool in tools}
        runtime = AgentRuntime(model=ScriptedDemoModel(), tools=provider)
        state = await build_graph(runtime).ainvoke(example_state())
        payload = await completion_payload(state, runtime)
        assert payload["outcome"] == "ASSIGN"
        assert payload["evidence"][0]["ref_id"] == "EV-SDR-DEMO-001"
        assert state["agent_context"]["traces"][-1]["tool"] == "get_evidence"
        assert seen_auth and all(v == b"Bearer synthetic-delegation" for v in seen_auth)
        assert "synthetic-delegation" not in str(state)
        context = get_context(state)
        gateway = ToolGateway(tools, state, context, Settings())
        error = await gateway.call(
            "search_sdr_cases", {"query": "synthetic-rate-limited"}, "실제 MCP 오류 보존"
        )
        assert error["error"]["code"] == "RATE_LIMITED"
        assert context.traces[-1].attempts == 2
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        sock.close()


@pytest.mark.asyncio
async def test_missing_required_tool_is_configuration_error_not_no_evidence():
    provider = MCPToolProvider()

    class Incomplete:
        async def get_tools(self, server_name):
            return []

    provider.client = Incomplete()
    with pytest.raises(MCPContractError, match="missing required tools"):
        await provider("rag")
