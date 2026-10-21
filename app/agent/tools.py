"""Execute maintenance enquiries while preserving source and aircraft applicability."""

import asyncio
import copy
import json
import re
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as SchemaError
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, ToolException
from pydantic import ValidationError

from app.agent.mcp_client import BUSINESS_TOOLS, RAG_TOOLS
from app.agent.schemas import AgentContext, GraphState, InternalHold, RetrievedRecord, ToolTrace
from app.agent.settings import Settings
from app.agent.transport import transient_error

SOURCE_TYPES = {
    "mock_aircraft_history": "HISTORY",
    "sdr": "SDR",
    "derived_amm_reference": "AMM",
    "airbus_technical_article": "TECHNICAL_DOC",
}
SEARCH_SOURCES = {
    "get_aircraft_history": {"HISTORY"},
    "search_sdr_cases": {"SDR", "AMM"},
    "search_amm_references": {"AMM"},
    "search_technical_docs": {"TECHNICAL_DOC"},
}
TERMINAL = {"ASSIGNED", "ACCEPTED", "HUMAN_REVIEW", "CLOSED"}


class EvidenceValidationError(ValueError):
    pass


class ToolResultError(ValueError):
    pass


class ScopeViolation(ToolResultError):
    def __init__(self, code: str, reason: str, message: str):
        super().__init__(message)
        self.code, self.reason = code, reason


def input_schema(tool: BaseTool) -> dict:
    return (
        tool.args_schema
        if isinstance(tool.args_schema, dict)
        else tool.get_input_schema().model_json_schema()
    )


def decode_result(raw: Any) -> dict:
    """Keep structured error details even when the MCP wrapper says isError."""
    failed = False
    if isinstance(raw, ToolMessage):
        failed = raw.status == "error"
        artifact = raw.artifact
        raw = (
            artifact.get("structured_content", raw.content)
            if isinstance(artifact, dict)
            else raw.content
        )
    if isinstance(raw, dict):
        failed = failed or bool(raw.get("isError"))
        result = raw.get("structuredContent", raw)
        if failed and isinstance(result, dict) and raw.get("error") and not result.get("error"):
            result = {**result, "error": raw["error"]}
    else:
        if isinstance(raw, list):
            raw = "\n".join(
                i.get("text", "") for i in raw if isinstance(i, dict) and i.get("type") == "text"
            )
        try:
            result = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError as exc:
            raise ToolResultError("Tool text must contain a JSON object") from exc
    if not isinstance(result, dict):
        raise ToolResultError("Tool returned no JSON object")
    return {**result, "isError": True} if failed else result


def get_context(state: GraphState) -> AgentContext:
    context = (
        AgentContext.model_validate(state["agent_context"])
        if state.get("agent_context")
        else AgentContext(request_id=state["request_id"])
    )
    if context.request_id != state["request_id"]:
        raise EvidenceValidationError("AI context belongs to another request")
    return context


def evidence_text(item: dict) -> str | None:
    # An issued quote is narrower than narrative_excerpt, which may be the full source.
    for key in ("text", "quote", "content", "narrative_excerpt", "excerpt"):
        if isinstance(item.get(key), str) and item[key].strip():
            return item[key]
    parts = []
    for key in ("symptom", "symptom_text", "observations", "actions", "action_taken", "outcome"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(v for v in value if isinstance(v, str) and v.strip())
    return "\n".join(parts) or None


def _failure(code: str, message: str = "") -> dict:
    return {
        "status": "ERROR",
        "isError": True,
        "error": {"code": code, "message": message, "retryable": False},
    }


def _signature(args: dict) -> dict:
    return {k: v for k, v in args.items() if k not in {"cursor", "limit"}}


class ToolGateway:
    def __init__(
        self, tools: list[BaseTool], state: GraphState, context: AgentContext, settings: Settings
    ):
        self.tools = {t.name: t for t in tools if t.name in BUSINESS_TOOLS | RAG_TOOLS}
        self.state, self.context, self.settings = state, context, settings
        self.calls = 0

    def catalogue(self) -> list[dict]:
        catalogue = []
        for tool in self.tools.values():
            schema = copy.deepcopy(input_schema(tool))
            for key in ("request_id", "context_version"):
                schema.get("properties", {}).pop(key, None)
                if key in schema.get("required", []):
                    schema["required"].remove(key)
            catalogue.append(
                {"name": tool.name, "description": tool.description, "input_schema": schema}
            )
        return catalogue

    def scoped_arguments(self, name: str, supplied: dict) -> dict:
        forbidden = {
            "operator_code",
            "model_key",
            "tenant_id",
            "role",
            "locked_filters",
            "airline_code",
            "tail_number",
            "aircraft_model",
            "snapshot_ids",
        }
        if forbidden.intersection(supplied):
            raise ToolResultError("Scope and authorization are server-owned")
        args = dict(supplied)
        for key, value in (
            ("request_id", self.state["request_id"]),
            ("context_version", self.context.context_version),
        ):
            if name == "get_request_context" and key == "context_version":
                continue
            if value is None:
                raise ToolResultError("get_request_context must establish the context first")
            if key in args and args[key] != value:
                raise ToolResultError(f"Cannot change confirmed scope: {key}")
            args[key] = value
        Draft202012Validator(
            input_schema(self.tools[name]), format_checker=FormatChecker()
        ).validate(args)
        if any(
            isinstance(args.get(k), str) and not args[k].strip()
            for k in ("query", "amm_reference_exact")
        ):
            raise ToolResultError("Query must not be blank")
        if name == "get_evidence":
            known = {r.ref_id for r in self.context.records if r.source != "HISTORY"}
            if args.get("evidence_id") not in known:
                raise ToolResultError("get_evidence requires a previously retrieved evidence_id")
        for code in args.get("candidate_jasc", []):
            if code not in {row["code"] for row in self.context.jasc_candidates}:
                raise ToolResultError("candidate_jasc must first be verified by R01")
        if args.get("cursor") and self.context.cursors.get(args["cursor"]) != {
            "tool": name,
            "arguments": _signature(args),
        }:
            raise ToolResultError("Cursor belongs to another query or context")
        return args

    def _trace(
        self, name: str, args: dict, question: str, result: dict, attempts: int, started_at: str
    ) -> None:
        status, error = result.get("status"), result.get("error") or {}
        outcome = (
            "ERROR"
            if result.get("isError") or status == "ERROR"
            else "NEEDS_INPUT"
            if status == "NEEDS_INPUT"
            else "EMPTY"
            if status in {"NO_DATA", "NO_MATCH"}
            else "OK"
        )
        rows = result.get("items") or (
            [result["result"]] if isinstance(result.get("result"), dict) else []
        )
        rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        ids = [
            r.get("evidence_id", r.get("history_id", r.get("id")))
            for r in rows
            if any(r.get(k) for k in ("evidence_id", "history_id", "id"))
        ]
        self.context.traces.append(
            ToolTrace(
                tool=name,
                question=question,
                arguments=args,
                outcome=outcome,
                attempts=attempts,
                item_ids=ids,
                error_code=error.get("code"),
                response=copy.deepcopy(result),
                started_at=started_at,
                finished_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
        )

    def _hold(self, reason: str, message: str, questions: list[str] | None = None) -> None:
        self.context.hold = InternalHold(
            reason=reason, summary=message, unresolved_questions=questions or []
        )

    def _invalidate(self, reason: str, message: str) -> None:
        self.context.records.clear()
        self.context.final_evidence.clear()
        self.context.departments.clear()
        self.context.jasc_candidates.clear()
        self.context.cursors.clear()
        self.context.final_validation = "HUMAN"
        self._hold(reason, message)

    async def call(self, name: str, supplied: dict, question: str) -> dict:
        started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if self.calls >= self.settings.max_tool_calls:
            return _failure("TOOL_LIMIT")
        self.calls += 1
        if name not in self.tools:
            result = _failure("TOOL_NOT_AVAILABLE", name)
            self._trace(name, supplied, question, result, 0, started_at)
            return result
        if name == "search_departments":
            self.context.departments = []
        try:
            args = self.scoped_arguments(name, supplied)
        except (ToolResultError, SchemaError):
            result = _failure("INVALID_ARGUMENT")
            self._trace(name, supplied, question, result, 0, started_at)
            return result
        for attempt in range(1, self.settings.tool_retries + 2):
            try:
                raw = await asyncio.wait_for(
                    self.tools[name].ainvoke(
                        {"name": name, "args": args, "id": str(uuid4()), "type": "tool_call"}
                    ),
                    timeout=self.settings.tool_timeout,
                )
                result = decode_result(raw)
            except Exception as exc:
                if transient_error(exc):
                    result = _failure("UPSTREAM_UNAVAILABLE")
                    if attempt <= self.settings.tool_retries:
                        continue
                elif isinstance(exc, ToolException):
                    # adapters 0.2.2 converts MCP isError into ToolException(text).
                    # The team's server includes the full envelope as JSON text.
                    try:
                        result = {**decode_result(str(exc)), "isError": True}
                    except ToolResultError:
                        result = _failure("TOOL_ERROR")
                elif isinstance(exc, (ToolResultError, ValidationError)):
                    result = _failure(
                        "INVALID_TOOL_RESULT" if isinstance(exc, ToolResultError) else "TOOL_ERROR"
                    )
                else:
                    raise
            error = result.get("error") or {}
            if result.get("isError") or result.get("status") == "ERROR":
                result = {**result, "status": "ERROR", "isError": True}
                if error.get("retryable") is True and attempt <= self.settings.tool_retries:
                    delay = error.get("retry_after_ms") or 0
                    if type(delay) is int and 0 <= delay <= self.settings.tool_timeout * 1000:
                        await asyncio.sleep(delay / 1000)
                        continue
                if error.get("code") == "CONTEXT_VERSION_CONFLICT":
                    self._invalidate(
                        "CONTEXT_CHANGED", "접수 범위가 변경되어 기존 조사·검토를 폐기했습니다."
                    )
                elif error.get("code") in {
                    "INDEX_VERSION_UNAVAILABLE",
                    "SCOPE_VIOLATION",
                    "FORBIDDEN",
                    "NOT_FOUND",
                }:
                    self._hold(
                        "VALIDATION_FAILED", f"검색 범위·권한·고정 버전 확인 필요: {error['code']}"
                    )
                break
            previous = self.context.model_copy(deep=True)
            try:
                self._accept(name, args, result)
            except ScopeViolation as exc:
                self.context.__dict__.update(previous.__dict__)
                self._invalidate(exc.reason, str(exc))
                result = _failure(exc.code, str(exc))
            except (ToolResultError, ValidationError, KeyError, TypeError, ValueError) as exc:
                self.context.__dict__.update(previous.__dict__)
                result = _failure("INVALID_TOOL_RESULT", str(exc))
            break
        self._trace(name, args, question, result, attempt, started_at)
        return result

    def _accept(self, name: str, args: dict, result: dict) -> None:
        status = result.get("status")
        if status not in {"OK", "NO_DATA", "NO_MATCH", "NEEDS_INPUT"}:
            raise ToolResultError("Missing or invalid business status")
        if result.get("request_id") != self.state["request_id"]:
            raise ScopeViolation(
                "REQUEST_MISMATCH", "VALIDATION_FAILED", "다른 접수의 도구 응답입니다."
            )
        if (
            name != "get_request_context"
            and result.get("context_version") != self.context.context_version
        ):
            raise ScopeViolation(
                "CONTEXT_VERSION_CONFLICT", "CONTEXT_CHANGED", "검색 범위 버전이 변경됐습니다."
            )
        if status == "NEEDS_INPUT":
            self._hold(
                "NEEDS_INPUT",
                "검색 범위 또는 접수 정보 보완이 필요합니다.",
                result.get("missing_fields") or [],
            )
            return
        singular = name in {"get_request_context", "get_evidence"}
        value = result.get("result") if singular else result.get("items")
        if status == "OK" and (
            (singular and not isinstance(value, dict))
            or (not singular and not isinstance(value, list))
        ):
            raise ToolResultError("Wrong items/result shape")
        if status != "OK" and value not in (None, []):
            raise ToolResultError("Empty business status must not include records")
        if name == "get_request_context":
            if status == "OK":
                self._request_context(value, result)
            return
        if status == "OK" and not singular and not value:
            raise ToolResultError("OK search must include records; use NO_DATA or NO_MATCH")
        if name in RAG_TOOLS:
            self._scope(name, result)
        if status != "OK":
            return
        rows = [value] if singular else value
        if any(not isinstance(row, dict) for row in rows):
            raise ToolResultError("Expected result objects")
        if name == "search_departments":
            self.context.departments = [
                self._department(row) for row in rows if self._eligible_department(row)
            ]
        elif name == "search_jasc_codes":
            by_code = {row["code"]: row for row in self.context.jasc_candidates}
            for row in rows:
                if not re.fullmatch(r"[0-9]{4}", row.get("code", "")):
                    raise ToolResultError("Invalid JASC code")
                by_code[row["code"]] = copy.deepcopy(row)
            self.context.jasc_candidates = list(by_code.values())
        else:
            for row in rows:
                self.record(name, row, result, args)
        if result.get("next_cursor"):
            self.context.cursors[result["next_cursor"]] = {
                "tool": name,
                "arguments": _signature(args),
            }

    def _request_context(self, value: dict, result: dict) -> None:
        version = value.get("context_version")
        if type(version) is not int or version < 1 or result.get("context_version") != version:
            raise ToolResultError("Invalid context version")
        scope = value.get("resolved_scope")
        if not isinstance(scope, dict) or "model_key" not in scope or "operator_code" not in scope:
            raise ToolResultError("Missing resolved_scope")
        if self.context.context_version is not None and version != self.context.context_version:
            raise ScopeViolation(
                "CONTEXT_VERSION_CONFLICT",
                "CONTEXT_CHANGED",
                "접수 범위가 변경되어 기존 판단을 사용할 수 없습니다.",
            )
        if self.context.context_version is not None and scope != self.context.applied_scope:
            raise ScopeViolation(
                "SCOPE_CHANGED_WITHOUT_VERSION",
                "VALIDATION_FAILED",
                "같은 컨텍스트 버전에서 검색 범위가 변경됐습니다.",
            )
        aircraft = value.get("aircraft") or {}
        if aircraft.get("registration") != self.state["tail_number"]:
            raise ScopeViolation(
                "AIRCRAFT_MISMATCH", "VALIDATION_FAILED", "접수 기체가 실행 입력과 다릅니다."
            )
        if aircraft.get("model_raw") not in (None, self.state["aircraft_model"]):
            raise ScopeViolation(
                "MODEL_MISMATCH", "VALIDATION_FAILED", "접수 모델이 실행 입력과 다릅니다."
            )
        if scope.get("model_key") not in (None, self.state["aircraft_model"].replace("-", "")):
            raise ScopeViolation(
                "MODEL_MISMATCH", "VALIDATION_FAILED", "SDR 모델 키가 접수 모델과 다릅니다."
            )
        if (value.get("report") or {}).get("symptom") != self.state["symptom_text"]:
            raise ScopeViolation(
                "REPORT_CHANGED", "CONTEXT_CHANGED", "신고 내용이 실행 입력과 달라졌습니다."
            )
        self.context.context_version = version
        self.context.request_context = copy.deepcopy(value)
        self.context.applied_scope = copy.deepcopy(scope)
        if value.get("status") in TERMINAL:
            self._hold("REQUEST_STOPPED", "접수가 이미 배정·수락·사람 검토 또는 종결 상태입니다.")
        elif value.get("missing_fields"):
            self._hold("NEEDS_INPUT", "접수 정보 보완이 필요합니다.", value["missing_fields"])

    def _scope(self, name: str, result: dict) -> None:
        scope, snapshots = result.get("applied_scope"), result.get("snapshot_ids")
        if (
            not isinstance(scope, dict)
            or not isinstance(snapshots, list)
            or not snapshots
            or any(not isinstance(x, str) or not x for x in snapshots)
        ):
            raise ToolResultError("Missing applied scope or fixed snapshots")
        technical = name == "search_technical_docs" or (
            name == "get_evidence"
            and (result.get("result") or {}).get("source_type") == "airbus_technical_article"
        )
        if technical and scope.get("operator_filter_applied") is not False:
            raise ScopeViolation(
                "SCOPE_VIOLATION",
                "VALIDATION_FAILED",
                "공개 기술자료에 운항사 필터를 적용하면 안 됩니다.",
            )
        if name != "search_jasc_codes":
            if (
                scope.get("model_key") != self.context.applied_scope.get("model_key")
                or scope.get("model_filter_applied") is not True
            ):
                raise ScopeViolation(
                    "SCOPE_VIOLATION", "VALIDATION_FAILED", "정확한 모델 범위가 일치하지 않습니다."
                )
            if not technical and (
                not scope.get("operator_code")
                or scope.get("operator_code") != self.context.applied_scope.get("operator_code")
                or scope.get("operator_filter_applied") is not True
            ):
                raise ScopeViolation(
                    "SCOPE_VIOLATION", "VALIDATION_FAILED", "SDR 운항사 범위가 일치하지 않습니다."
                )
            if not technical and scope.get("locked_filters") != self.context.applied_scope.get(
                "locked_filters"
            ):
                raise ScopeViolation(
                    "SCOPE_VIOLATION", "VALIDATION_FAILED", "사용자 고정 조건이 변경됐습니다."
                )
        family = "technical" if technical else "rag"
        pinned = self.context.snapshots.get(family)
        if pinned is not None and set(pinned) != set(snapshots):
            raise ScopeViolation(
                "SNAPSHOT_CHANGED", "SNAPSHOT_CHANGED", "고정 데이터·색인 버전이 변경됐습니다."
            )
        self.context.snapshots[family] = list(snapshots)

    def _eligible_department(self, row: dict) -> bool:
        if row.get("active") is False or row.get("availability_status") not in (None, "available"):
            return False
        models = row.get("supported_models")
        return (
            not models
            or self.state["aircraft_model"] in models
            or self.context.applied_scope.get("model_key") in models
        )

    @staticmethod
    def _department(row: dict) -> dict:
        code = row.get("code", row.get("department_id"))
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,7}", code):
            raise ToolResultError(
                "B03 must expose departments.code or department_id equal to that code"
            )
        return {**copy.deepcopy(row), "code": code}

    def record(self, name: str, row: dict, envelope: dict, args: dict) -> None:
        source = SOURCE_TYPES.get(row.get("source_type"))
        if name == "get_aircraft_history":
            source = source or "HISTORY"
        if source is None or (name in SEARCH_SOURCES and source not in SEARCH_SOURCES[name]):
            raise ToolResultError("Unknown or mismatched evidence source_type")
        identifier = (
            row.get("history_id", row.get("id")) if source == "HISTORY" else row.get("evidence_id")
        )
        if not isinstance(identifier, str) or not identifier:
            raise ToolResultError("Missing history id or issued evidence_id")
        if source == "HISTORY" and not re.fullmatch(r"MH-[0-9]{6}", identifier):
            raise ToolResultError("Maintenance history ID must be MH- plus six digits")
        if source in {"SDR", "AMM"} and not identifier.startswith(f"EV-{source}-"):
            raise ToolResultError("Use the issued EV-SDR/EV-AMM evidence_id")
        if source != "HISTORY" and any(
            not isinstance(row.get(k), str) or not row[k].strip()
            for k in ("source_id", "source_version", "evidence_group_id")
        ):
            raise ToolResultError("Evidence requires source identity, version and original group")
        if source == "HISTORY":
            expected = (self.context.request_context.get("aircraft") or {}).get("aircraft_id")
            if row.get("aircraft_id") not in (None, expected) or row.get("tail_number") not in (
                None,
                self.state["tail_number"],
            ):
                raise ScopeViolation(
                    "SCOPE_VIOLATION", "VALIDATION_FAILED", "다른 기체의 내부 이력입니다."
                )
        for key in ("operator_code", "model_key"):
            if (
                source in {"SDR", "AMM"}
                and row.get(key) is not None
                and row[key] != self.context.applied_scope.get(key)
            ):
                raise ScopeViolation(
                    "SCOPE_VIOLATION", "VALIDATION_FAILED", "근거 원문의 검색 범위가 다릅니다."
                )
        if source in {"SDR", "AMM"}:
            locked = self.context.applied_scope.get("locked_filters") or {}
            codes = locked.get("jasc_codes") or []
            if row.get("reported_jasc") is not None and codes and row["reported_jasc"] not in codes:
                raise ScopeViolation(
                    "SCOPE_VIOLATION", "VALIDATION_FAILED", "고정 JASC 조건과 원문 코드가 다릅니다."
                )
            if row.get("difficulty_date"):
                day = date.fromisoformat(row["difficulty_date"])
                lower, upper = locked.get("difficulty_from"), locked.get("difficulty_to")
                if (lower and day < date.fromisoformat(lower)) or (
                    upper and day > date.fromisoformat(upper)
                ):
                    raise ScopeViolation(
                        "SCOPE_VIOLATION",
                        "VALIDATION_FAILED",
                        "고정 발견일 범위를 벗어난 근거입니다.",
                    )
        if source == "TECHNICAL_DOC":
            previous = next((r for r in self.context.records if r.ref_id == identifier), None)
            applicability = row.get(
                "applicability_status",
                previous.metadata.get("applicability_status") if previous else None,
            )
            if applicability not in {"supported", "conditional", "unknown"} or (
                applicability == "unknown"
                and not args.get("include_unknown_applicability")
                and name != "get_evidence"
            ):
                raise ToolResultError("Technical applicability is not eligible")
        content = evidence_text(row)
        if content is None:
            raise ToolResultError("Evidence requires original text")
        old = next((r for r in self.context.records if r.ref_id == identifier), None)
        if name == "get_evidence" and (old is None or identifier != args.get("evidence_id")):
            raise ToolResultError("R05 returned an unexpected evidence_id")
        if old is not None:
            for key in ("source_version", "source_id", "evidence_group_id"):
                if old.metadata.get(key) is not None and row.get(key) != old.metadata[key]:
                    raise ScopeViolation(
                        "EVIDENCE_CHANGED", "VALIDATION_FAILED", f"근거의 {key}가 변경됐습니다."
                    )
            if source != old.source:
                raise ScopeViolation(
                    "SOURCE_CHANGED", "VALIDATION_FAILED", "근거 출처가 변경됐습니다."
                )
            before_locator = old.metadata.get("locator", old.metadata.get("source_locator"))
            after_locator = row.get("locator", row.get("source_locator"))
            if before_locator is not None and after_locator != before_locator:
                raise ScopeViolation(
                    "LOCATOR_CHANGED", "VALIDATION_FAILED", "근거 원문 위치가 변경됐습니다."
                )
            if content != old.content and not (
                name == "get_evidence" and (old.content in content or content in old.content)
            ):
                raise ScopeViolation(
                    "EVIDENCE_CHANGED", "VALIDATION_FAILED", "근거 원문이 변경됐습니다."
                )
        metadata = {
            **(old.metadata if old else {}),
            **copy.deepcopy(row),
            "retrieval": {
                k: copy.deepcopy(envelope.get(k))
                for k in (
                    "context_version",
                    "applied_scope",
                    "snapshot_ids",
                    "trace_id",
                    "warnings",
                    "counts",
                )
            },
        }
        record = RetrievedRecord(
            source=source, ref_id=identifier, content=content, metadata=metadata
        )
        self.context.records = [r for r in self.context.records if r.ref_id != identifier] + [
            record
        ]


def validate_evidence(evidence: list, context: AgentContext) -> None:
    registry = {(r.source, r.ref_id): r for r in context.records}
    for item in evidence:
        record = registry.get((item.source, item.ref_id))
        if record is None or item.excerpt not in record.content:
            raise EvidenceValidationError("Unknown evidence id/source or non-original quote")
        spans = record.metadata.get("quote_spans") or []
        if spans and not any(item.excerpt in span.get("text", "") for span in spans):
            raise EvidenceValidationError("Quote outside issued evidence spans")


def validate_department(code: str, context: AgentContext) -> None:
    if code not in {d.get("code") for d in context.departments}:
        raise EvidenceValidationError("Department is not an eligible current department code")


def evidence_groups(context: AgentContext) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for record in context.records:
        group = record.metadata.get("evidence_group_id") or f"ungrouped:{record.ref_id}"
        groups.setdefault(group, []).append(record.ref_id)
    return groups
