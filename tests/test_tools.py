import asyncio
import copy

import httpx
import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from app.agent.demo import INPUTS, DemoTools, example_state
from app.agent.schemas import AgentContext, Evidence, RetrievedRecord
from app.agent.settings import Settings
from app.agent.tools import (
    EvidenceValidationError,
    ToolGateway,
    ToolResultError,
    decode_result,
    evidence_groups,
    validate_evidence,
)


def gateway(source=None, settings=None):
    source = source or DemoTools()
    state = example_state()
    ctx = source.response("get_request_context", {"request_id": state["request_id"]})["result"]
    context = AgentContext(
        request_id=state["request_id"],
        context_version=1,
        request_context=ctx,
        applied_scope=copy.deepcopy(source.scope),
    )
    return ToolGateway(source.tools, state, context, settings or Settings()), context


def test_structured_mcp_artifact_and_structured_error_are_preserved():
    message = ToolMessage(
        content="display",
        tool_call_id="test",
        artifact={"structured_content": {"status": "OK", "items": [{"evidence_id": "EV-SDR-X"}]}},
    )
    assert decode_result(message)["items"][0]["evidence_id"] == "EV-SDR-X"
    failed = ToolMessage(
        content="display",
        tool_call_id="test",
        status="error",
        artifact={"structured_content": {"status": "ERROR", "error": {"code": "RATE_LIMITED"}}},
    )
    assert decode_result(failed)["error"]["code"] == "RATE_LIMITED"


def test_non_json_text_is_an_error_not_empty_results():
    with pytest.raises(ToolResultError):
        decode_result("upstream unavailable")


@pytest.mark.asyncio
async def test_confirmed_request_version_is_injected_and_scope_cannot_be_changed():
    source = DemoTools()
    runner, context = gateway(source)
    assert not (await runner.call("search_sdr_cases", {"query": "vibration"}, "범위")).get(
        "isError"
    )
    assert source.calls[-1][1]["request_id"] == example_state()["request_id"]
    assert source.calls[-1][1]["context_version"] == 1
    for bad in (
        {"context_version": 2},
        {"request_id": "OTHER"},
        {"model_key": "A330343"},
        {"operator_code": "OTHER"},
        {"tenant_id": "OTHER"},
        {"query": "x", "limit": 21},
    ):
        result = await runner.call("search_sdr_cases", {"query": "x", **bad}, "범위 위반")
        assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert context.records


@pytest.mark.asyncio
async def test_mismatched_result_invalidates_evidence_not_silent_empty():
    source = DemoTools()
    source.rows[0]["model_key"] = "A330343"
    runner, context = gateway(source)
    result = await runner.call("search_sdr_cases", {"query": "vibration"}, "기종 범위")
    assert result["error"]["code"] == "SCOPE_VIOLATION"
    assert not context.records
    assert context.hold.reason == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_unretrieved_evidence_id_is_rejected_before_call():
    source = DemoTools()
    runner, _ = gateway(source)
    assert (await runner.call("get_evidence", {"evidence_id": "UNKNOWN"}, "원문"))["error"][
        "code"
    ] == "INVALID_ARGUMENT"
    assert source.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, outcome",
    [
        ("NO_DATA", "EMPTY"),
        ("NO_MATCH", "EMPTY"),
        ("NEEDS_INPUT", "NEEDS_INPUT"),
        ("ERROR", "ERROR"),
    ],
)
async def test_business_statuses_remain_distinct(status, outcome):
    source = DemoTools(empty=True)

    def mutate(name, args, result):
        if name == "search_sdr_cases":
            result.update(
                status=status,
                missing_fields=["operator_code"] if status == "NEEDS_INPUT" else [],
                warnings=["synthetic warning"],
                error={"code": "INDEX_NOT_READY", "retryable": False}
                if status == "ERROR"
                else None,
            )
        return result

    source.on_call = mutate
    runner, context = gateway(source)
    result = await runner.call("search_sdr_cases", {"query": "x"}, "상태 구분")
    assert result["status"] == status
    assert context.traces[-1].outcome == outcome
    assert context.traces[-1].response["warnings"] == ["synthetic warning"]
    if status == "NEEDS_INPUT":
        assert context.hold.unresolved_questions == ["operator_code"]


@pytest.mark.asyncio
async def test_singular_result_and_issued_evidence_id_are_supported():
    runner, context = gateway()
    await runner.call("search_sdr_cases", {"query": "vibration"}, "사례")
    assert context.records[0].ref_id == "EV-SDR-DEMO-001"
    result = await runner.call("get_evidence", {"evidence_id": "EV-SDR-DEMO-001"}, "원문")
    assert result["status"] == "OK"
    assert context.records[0].metadata["quote_spans"]
    assert context.records[0].metadata["case_id"] == "SDR-DEMO-001"


@pytest.mark.asyncio
async def test_timeout_retries_exactly_once_and_preserves_error():
    attempts = []

    async def slow(**kwargs):
        attempts.append(1)
        await asyncio.sleep(0.1)

    source = DemoTools()
    tool = StructuredTool.from_function(
        coroutine=slow,
        name="search_departments",
        description="test",
        args_schema=INPUTS["search_departments"],
    )
    source.tools = [tool]
    runner, context = gateway(source, Settings(tool_timeout=0.005, tool_retries=1))
    result = await runner.call("search_departments", {}, "일시 오류")
    assert len(attempts) == 2
    assert context.traces[-1].attempts == 2
    assert result["error"]["code"] == "UPSTREAM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_httpx_and_nested_transport_failures_retry_once():
    for grouped in (False, True):
        attempts = []

        async def fail(**kwargs):
            attempts.append(1)
            error = httpx.ConnectError("synthetic")
            raise ExceptionGroup("transport", [error]) if grouped else error

        source = DemoTools()
        source.tools = [
            StructuredTool.from_function(
                coroutine=fail,
                name="search_sdr_cases",
                description="test",
                args_schema=INPUTS["search_sdr_cases"],
            )
        ]
        runner, context = gateway(source)
        assert (await runner.call("search_sdr_cases", {"query": "x"}, "연결"))["isError"]
        assert len(attempts) == 2
        assert context.traces[-1].attempts == 2


@pytest.mark.asyncio
async def test_explicit_retryable_only_and_rate_limit_delay(monkeypatch):
    waits = []

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr("app.agent.tools.asyncio.sleep", sleep)
    source = DemoTools()
    source.on_call = lambda name, args, result: {
        "status": "ERROR",
        "error": {"code": "RATE_LIMITED", "retryable": True, "retry_after_ms": 20},
    }
    runner, context = gateway(source)
    await runner.call("search_sdr_cases", {"query": "x"}, "재시도")
    assert len(source.calls) == 2
    assert waits == [0.02]
    assert context.traces[-1].attempts == 2


@pytest.mark.asyncio
async def test_failed_department_refresh_clears_stale_directory():
    source = DemoTools()
    source.on_call = lambda *args: {
        "status": "ERROR",
        "error": {"code": "OFFLINE", "retryable": False},
    }
    runner, context = gateway(source)
    context.departments = [{"code": "HYD"}]
    await runner.call("search_departments", {}, "현재 부서")
    assert context.departments == []
    assert len(source.calls) == 1


def test_id_source_and_original_quote_are_all_required():
    context = AgentContext(
        request_id="test",
        records=[
            RetrievedRecord(
                source="SDR", ref_id="EV-SDR-TEST", content="The original symptom only."
            )
        ],
    )
    for source, quote in (("SDR", "Successful repair confirmed."), ("AMM", "original")):
        with pytest.raises(EvidenceValidationError):
            validate_evidence(
                [Evidence(source=source, ref_id="EV-SDR-TEST", excerpt=quote, why="test")], context
            )


def test_raw_error_cannot_be_hidden_by_structured_content():
    raw = {"isError": True, "error": {"code": "OFFLINE"}, "structuredContent": {"items": []}}
    result = decode_result(raw)
    assert result["isError"] is True
    assert result["error"]["code"] == "OFFLINE"


@pytest.mark.asyncio
async def test_history_fields_and_derived_document_provenance_are_preserved():
    runner, context = gateway()
    await runner.call("get_aircraft_history", {}, "현재 개체의 이력")
    record = context.records[0]
    assert record.ref_id == "MH-000123"
    for key in (
        "reported_at",
        "occurred_at",
        "closed_at",
        "ata_chapter",
        "jasc_code",
        "department_id",
        "source_sdr_ids",
        "mock",
        "outcome",
    ):
        assert key in record.metadata
    assert record.metadata["outcome"] is None
    assert "resolved" not in record.content
    await runner.call("search_sdr_cases", {"query": "vibration"}, "유사 사례")
    await runner.call("search_amm_references", {"query": "vibration"}, "인용 맥락")
    assert context.records[-1].metadata["relationship_status"] == "ambiguous"
    assert context.records[-1].metadata["applicability_status"] == "not_verified"
    assert len(evidence_groups(context)["DEMO-GROUP-001"]) == 2


@pytest.mark.asyncio
async def test_jasc_candidates_must_be_verified_and_are_not_locked_filters():
    source = DemoTools()
    source.scope["locked_filters"]["jasc_codes"] = ["3230"]
    runner, context = gateway(source)
    assert (
        await runner.call("search_sdr_cases", {"query": "x", "candidate_jasc": ["3230"]}, "후보")
    )["isError"]
    assert (await runner.call("search_jasc_codes", {"codes": ["3230"]}, "코드 정의"))[
        "status"
    ] == "OK"
    assert (
        await runner.call("search_sdr_cases", {"query": "x", "candidate_jasc": ["3230"]}, "후보")
    )["status"] == "OK"
    assert context.applied_scope["locked_filters"]["jasc_codes"] == ["3230"]


@pytest.mark.asyncio
async def test_snapshot_or_context_change_invalidates_prior_basis():
    for changed in ("snapshot", "context"):
        source = DemoTools()
        runner, context = gateway(source)
        await runner.call("search_sdr_cases", {"query": "x"}, "사례")
        if changed == "snapshot":
            source.snapshots = ["DEMO-NEW-RELEASE"]
        else:
            source.version = 2
        result = await runner.call("search_amm_references", {"query": "x"}, "추가 근거")
        assert result["isError"]
        assert context.hold.reason == (
            "SNAPSHOT_CHANGED" if changed == "snapshot" else "CONTEXT_CHANGED"
        )
        assert context.records == []


@pytest.mark.asyncio
async def test_cursor_cannot_be_used_for_a_different_query():
    source = DemoTools()

    def cursor(name, args, result):
        result["next_cursor"] = "DEMO-CURSOR"

    source.on_call = cursor
    runner, _ = gateway(source)
    await runner.call("search_sdr_cases", {"query": "x"}, "첫 페이지")
    before = len(source.calls)
    assert (
        await runner.call(
            "search_sdr_cases", {"query": "changed", "cursor": "DEMO-CURSOR"}, "다른 질의"
        )
    )["isError"]
    assert len(source.calls) == before
    assert (
        await runner.call(
            "search_sdr_cases", {"query": "x", "cursor": "DEMO-CURSOR"}, "다음 페이지"
        )
    )["status"] == "OK"


@pytest.mark.asyncio
async def test_technical_docs_do_not_use_customer_airline_as_operator_filter():
    source = DemoTools()
    runner, context = gateway(source)
    assert (await runner.call("search_technical_docs", {"query": "vibration"}, "기술 배경"))[
        "status"
    ] == "OK"
    assert context.records[0].source == "TECHNICAL_DOC"
    assert (
        context.records[0].metadata["retrieval"]["applied_scope"]["operator_filter_applied"]
        is False
    )
    source.technical[0]["applicability_status"] = "unknown"
    runner, context = gateway(source)
    assert (await runner.call("search_technical_docs", {"query": "x"}, "기술 배경"))[
        "status"
    ] == "NO_MATCH"
    assert (
        await runner.call(
            "search_technical_docs",
            {"query": "x", "include_unknown_applicability": True},
            "미상 참고",
        )
    )["status"] == "OK"
    assert context.records[0].metadata["applicability_status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("reported_jasc", "2400"), ("difficulty_date", "2026-09-01")]
)
async def test_unchanged_envelope_does_not_hide_row_level_locked_filter_violation(field, value):
    source = DemoTools()
    source.scope["locked_filters"].update(jasc_codes=["3230"], difficulty_from="2026-10-01")
    source.rows[0][field] = value
    runner, context = gateway(source)
    result = await runner.call("search_sdr_cases", {"query": "x"}, "원문 조건 확인")
    assert result["error"]["code"] == "SCOPE_VIOLATION"
    assert context.records == []


@pytest.mark.asyncio
async def test_scope_changed_without_new_version_is_not_silently_replanned():
    source = DemoTools()
    runner, context = gateway(source)
    source.scope["operator_code"] = "DIFFERENT"
    result = await runner.call("get_request_context", {}, "현재 범위 확인")
    assert result["error"]["code"] == "SCOPE_CHANGED_WITHOUT_VERSION"
    assert context.hold.reason == "VALIDATION_FAILED"
