import asyncio

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.agent.demo import DemoTools, example_state
from app.agent.schemas import AgentContext, Evidence, RetrievedRecord
from app.agent.settings import Settings
from app.agent.tools import (
    EvidenceValidationError,
    ToolGateway,
    ToolResultError,
    decode_result,
    validate_evidence,
)


def gateway(tools, settings=None):
    state = example_state()
    context = AgentContext(request_id=state["request_id"])
    return ToolGateway(tools, state, context, settings or Settings()), context


def test_structured_mcp_artifact_is_not_lost_when_text_is_not_json():
    result = decode_result(
        ToolMessage(
            content="display text",
            tool_call_id="test",
            artifact={"structured_content": {"items": [{"id": "TEST"}]}},
        )
    )
    assert result["items"][0]["id"] == "TEST"


def test_non_json_text_is_an_error_not_empty_results():
    with pytest.raises(ToolResultError):
        decode_result("upstream unavailable")


@pytest.mark.asyncio
async def test_confirmed_model_is_injected_and_cannot_be_changed():
    source = DemoTools()
    runner, context = gateway(source.tools)
    result = await runner.call("search_sdr", {"query": "vibration"}, "비교 대상 확인")
    assert not result.get("isError")
    assert source.calls[-1][1]["aircraft_model"] == "A320-214"
    bad = await runner.call("search_sdr", {"query": "x", "aircraft_model": "OTHER"}, "범위 확인")
    assert bad["error"]["code"] == "INVALID_ARGUMENT"
    assert context.traces[-1].outcome == "ERROR"


@pytest.mark.asyncio
async def test_mismatched_result_is_not_available_as_evidence():
    source = DemoTools()
    source.rows[0]["metadata"]["aircraft_model"] = "OTHER"
    runner, context = gateway(source.tools)
    result = await runner.call("search_sdr", {"query": "vibration"}, "기종 범위 확인")
    assert result["items"] == []
    assert context.records == []


@pytest.mark.asyncio
async def test_unretrieved_document_id_is_rejected_before_call():
    source = DemoTools()
    runner, _ = gateway(source.tools)
    result = await runner.call("get_document", {"id": "UNKNOWN"}, "문서 확인")
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert source.calls == []


@pytest.mark.asyncio
async def test_empty_search_and_error_have_different_trace_outcomes():
    class SearchInput(BaseModel):
        query: str

    async def error_search(query: str):
        return {"isError": True, "error": {"code": "INDEX_NOT_READY", "retryable": False}}

    error_tool = StructuredTool.from_function(
        coroutine=error_search,
        name="search_sdr",
        description="test",
        args_schema=SearchInput,
    )
    runner, context = gateway([error_tool])
    result = await runner.call("search_sdr", {"query": "test"}, "오류 구분")
    assert result["error"]["code"] == "INDEX_NOT_READY"
    assert context.traces[-1].outcome == "ERROR"
    assert context.traces[-1].attempts == 1
    empty, context2 = gateway(DemoTools(empty=True).tools)
    assert (await empty.call("search_sdr", {"query": "test"}, "빈 결과 구분"))["items"] == []
    assert context2.traces[-1].outcome == "EMPTY"


@pytest.mark.asyncio
async def test_timeout_retries_exactly_once_and_preserves_error():
    class EmptyInput(BaseModel):
        pass

    attempts = []

    async def slow():
        attempts.append(1)
        await asyncio.sleep(0.1)
        return {"items": []}

    tool = StructuredTool.from_function(
        coroutine=slow,
        name="list_departments",
        description="test",
        args_schema=EmptyInput,
    )
    runner, context = gateway([tool], Settings(tool_timeout=0.005, tool_retries=1))
    result = await runner.call("list_departments", {}, "현재 부서 확인")
    assert len(attempts) == 2
    assert context.traces[-1].attempts == 2
    assert result["isError"]


@pytest.mark.asyncio
async def test_failed_department_refresh_clears_stale_directory():
    class EmptyInput(BaseModel):
        pass

    async def failed():
        return {"isError": True, "error": {"code": "OFFLINE"}}

    tool = StructuredTool.from_function(
        coroutine=failed,
        name="list_departments",
        description="test",
        args_schema=EmptyInput,
    )
    runner, context = gateway([tool])
    context.departments = [{"code": "HYD"}]
    await runner.call("list_departments", {}, "현재 부서 확인")
    assert context.departments == []


def test_id_existence_alone_does_not_validate_invented_quote():
    context = AgentContext(
        request_id="test",
        records=[
            RetrievedRecord(
                source="SDR",
                ref_id="SDR-TEST",
                content="The original symptom only.",
            )
        ],
    )
    with pytest.raises(EvidenceValidationError):
        validate_evidence(
            [
                Evidence(
                    source="SDR",
                    ref_id="SDR-TEST",
                    excerpt="Successful repair confirmed.",
                    why="test",
                )
            ],
            context,
        )


def test_source_substitution_is_not_valid_evidence():
    context = AgentContext(
        request_id="test",
        records=[
            RetrievedRecord(
                source="SDR",
                ref_id="SDR-TEST",
                content="original",
            )
        ],
    )
    with pytest.raises(EvidenceValidationError):
        validate_evidence(
            [Evidence(source="AMM", ref_id="SDR-TEST", excerpt="original", why="test")], context
        )


def test_raw_error_cannot_be_hidden_by_structured_content():
    raw = {"isError": True, "error": {"code": "OFFLINE"}, "structuredContent": {"items": []}}
    assert decode_result(raw)["isError"] is True


def test_history_fields_and_derived_document_provenance_are_preserved():
    runner, context = gateway(DemoTools().tools)
    runner.record(
        "get_maintenance_history",
        [
            {
                "id": "MH-TEST",
                "tail_number": "DEMO-TAIL",
                "symptom_text": "synthetic symptom",
                "action_taken": "synthetic action",
                "closed_at": "2026-01-01T00:00:00Z",
            }
        ],
    )
    assert "synthetic symptom" in context.records[0].content
    assert context.records[0].metadata["tail_number"] == "DEMO-TAIL"
    assert "resolved" not in context.records[0].content
    runner.record(
        "search_amm",
        [
            {
                "id": "AMM-TEST",
                "content": "Synthetic AMM citation context only.",
                "source_case_id": "SDR-TEST",
                "material_type": "derived_amm_reference",
                "applicability_status": "not_verified",
            }
        ],
    )
    assert context.records[1].metadata["source_case_id"] == "SDR-TEST"
    assert context.records[1].metadata["material_type"] == "derived_amm_reference"
