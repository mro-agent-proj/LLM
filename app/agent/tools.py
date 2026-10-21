"""Per-request tool execution and provenance. Never treats an error as zero results."""

import asyncio
import json
from typing import Any
from uuid import uuid4

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, ToolException
from pydantic import ValidationError

from app.agent.mcp_client import BUSINESS_TOOLS, RAG_TOOLS
from app.agent.schemas import AgentContext, GraphState, RetrievedRecord, ToolTrace
from app.agent.settings import Settings

SOURCE_BY_TOOL = {
    "get_maintenance_history": "HISTORY",
    "search_similar_history": "HISTORY",
    "search_sdr": "SDR",
    "search_amm": "AMM",
}


def input_schema(tool: BaseTool) -> dict:
    # MCP tools supply JSON Schema; locally declared tools normally use Pydantic.
    if isinstance(tool.args_schema, dict):
        return tool.args_schema
    return tool.get_input_schema().model_json_schema()


class EvidenceValidationError(ValueError):
    pass


class ToolResultError(ValueError):
    pass


def decode_result(raw: Any) -> dict[str, Any]:
    if isinstance(raw, ToolMessage):
        if raw.status == "error":
            return {"isError": True, "error": {"code": "TOOL_ERROR"}}
        artifact = raw.artifact
        if isinstance(artifact, dict) and isinstance(artifact.get("structured_content"), dict):
            return artifact["structured_content"]
        raw = raw.content
    if isinstance(raw, dict):
        if raw.get("isError"):
            return raw
        if isinstance(raw.get("structuredContent"), dict):
            return raw["structuredContent"]
        return raw
    if isinstance(raw, list):
        blocks = [
            item.get("text", "")
            for item in raw
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        raw = "\n".join(blocks)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ToolResultError("Tool text must contain a JSON object") from exc
        if isinstance(parsed, dict):
            return parsed
    raise ToolResultError("Tool returned no JSON object")


def get_context(state: GraphState) -> AgentContext:
    data = state.get("agent_context")
    context = (
        AgentContext.model_validate(data)
        if data
        else AgentContext(
            request_id=state["request_id"],
        )
    )
    if context.request_id != state["request_id"]:
        raise EvidenceValidationError("AI context belongs to another request")
    return context


def in_scope(item: dict, state: GraphState) -> bool:
    metadata = item.get("metadata") or {}
    for key in ("aircraft_model", "airline_code"):
        actual = metadata.get(key, item.get(key))
        if actual is not None and actual != state[key]:
            return False
    return True


def evidence_text(item: dict) -> str | None:
    content = item.get("content")
    if isinstance(content, str) and content.strip():
        return content
    # History CSV records can be returned without a combined content field.
    fields = ("symptom_text", "action_taken", "outcome")
    parts = [
        f"{key}: {item[key]}"
        for key in fields
        if isinstance(item.get(key), str) and item[key].strip()
    ]
    return "\n".join(parts) or None


class ToolGateway:
    def __init__(
        self, tools: list[BaseTool], state: GraphState, context: AgentContext, settings: Settings
    ):
        self.tools = {tool.name: tool for tool in tools if tool.name in BUSINESS_TOOLS | RAG_TOOLS}
        self.state = state
        self.context = context
        self.settings = settings
        self.calls = 0

    def catalogue(self) -> list[dict]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": input_schema(tool),
            }
            for tool in self.tools.values()
        ]

    def scoped_arguments(self, name: str, supplied: dict) -> dict:
        properties = input_schema(self.tools[name]).get("properties", {})
        if set(supplied) - set(properties):
            raise ToolResultError("Unknown tool arguments")
        args = dict(supplied)
        for key in ("tail_number", "aircraft_model", "airline_code", "request_id"):
            if key in properties:
                if key in args and args[key] != self.state[key]:
                    raise ToolResultError(f"Cannot change confirmed scope: {key}")
                args[key] = self.state[key]
        for key in ("limit", "top_k"):
            if key in args:
                maximum = 100 if name == "get_maintenance_history" else 20
                if type(args[key]) is not int or not 1 <= args[key] <= maximum:
                    raise ToolResultError(f"Invalid {key}")
        if "query" in args and (not isinstance(args["query"], str) or len(args["query"]) > 2000):
            raise ToolResultError("Invalid query")
        if name == "get_document":
            known = {record.ref_id for record in self.context.records if record.source != "HISTORY"}
            if args.get("id") not in known:
                raise ToolResultError("get_document requires a previously retrieved document id")
        return args

    async def call(self, name: str, supplied: dict, question: str) -> dict:
        if name not in self.tools:
            return {"isError": True, "error": {"code": "TOOL_NOT_ALLOWED"}}
        if self.calls >= self.settings.max_tool_calls:
            return {"isError": True, "error": {"code": "TOOL_LIMIT"}}
        self.calls += 1
        if name == "list_departments":
            self.context.departments = []  # A failed refresh must not reuse a stale directory.
        try:
            args = self.scoped_arguments(name, supplied)
        except ToolResultError:
            self.context.traces.append(
                ToolTrace(
                    tool=name,
                    question=question,
                    arguments=supplied,
                    outcome="ERROR",
                    attempts=0,
                    error_code="INVALID_ARGUMENT",
                )
            )
            return {"isError": True, "error": {"code": "INVALID_ARGUMENT"}}
        error = "TOOL_ERROR"
        for attempt in range(1, self.settings.tool_retries + 2):
            try:
                raw = await asyncio.wait_for(
                    self.tools[name].ainvoke(
                        {
                            "name": name,
                            "args": args,
                            "id": str(uuid4()),
                            "type": "tool_call",
                        }
                    ),
                    timeout=self.settings.tool_timeout,
                )
                result = decode_result(raw)
            except (TimeoutError, ConnectionError, OSError):
                error = "TOOL_UNAVAILABLE"
                continue
            except ToolException:
                error = "TOOL_ERROR"
                break
            except ValidationError:
                error = "INVALID_ARGUMENT"
                break
            except ToolResultError:
                error = "INVALID_TOOL_RESULT"
                break
            if result.get("isError") or result.get("status") == "ERROR":
                details = result.get("error") or {}
                error = details.get("code", "TOOL_ERROR")
                if details.get("retryable") is True:
                    continue
                break
            items = result.get("items")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                error = "INVALID_TOOL_RESULT"
                break
            if any(
                item.get("metadata") is not None and not isinstance(item["metadata"], dict)
                for item in items
            ):
                error = "INVALID_TOOL_RESULT"
                break
            items = [item for item in items if in_scope(item, self.state)]
            if name == "get_maintenance_history":
                items = [
                    item
                    for item in items
                    if item.get(
                        "tail_number",
                        (item.get("metadata") or {}).get("tail_number", self.state["tail_number"]),
                    )
                    == self.state["tail_number"]
                ]
            previous_records = list(self.context.records)
            try:
                self.record(name, items)
            except (ToolResultError, EvidenceValidationError, ValidationError):
                self.context.records = previous_records
                error = "INVALID_TOOL_RESULT"
                break
            self.context.traces.append(
                ToolTrace(
                    tool=name,
                    question=question,
                    arguments=args,
                    outcome="OK" if items else "EMPTY",
                    attempts=attempt,
                    item_ids=[item["id"] for item in items if "id" in item],
                )
            )
            return {**result, "items": items}
        self.context.traces.append(
            ToolTrace(
                tool=name,
                question=question,
                arguments=args,
                outcome="ERROR",
                attempts=attempt,
                error_code=error,
            )
        )
        return {"isError": True, "error": {"code": error}}

    def record(self, name: str, items: list[dict]) -> None:
        if name == "list_departments":
            self.context.departments = [item for item in items if item.get("active", True) is True]
            return
        for item in items:
            source = SOURCE_BY_TOOL.get(name)
            if name == "get_document":
                source = item.get("source")
                matched = [
                    record.source
                    for record in self.context.records
                    if record.ref_id == item.get("id")
                ]
                if source is not None and set(matched) != {source}:
                    raise ToolResultError("Document source does not match its retrieved id")
                if source is None:
                    source = matched[0] if len(set(matched)) == 1 else None
            if source is None:
                continue  # aircraft and departments are not Evidence types in this PDF.
            identifier, content = item.get("id"), evidence_text(item)
            if not isinstance(identifier, str) or not identifier or content is None:
                raise ToolResultError("Evidence requires id and original content")
            metadata = dict(item.get("metadata") or {})
            for key in (
                "aircraft_model",
                "airline_code",
                "tail_number",
                "source_case_id",
                "source_version",
                "snapshot_id",
                "evidence_group_id",
                "material_type",
                "applicability_status",
                "mock",
            ):
                if key in item and key not in metadata:
                    metadata[key] = item[key]
            record = RetrievedRecord(
                source=source,
                ref_id=identifier,
                content=content,
                metadata=metadata,
            )
            old = [
                r
                for r in self.context.records
                if (r.source, r.ref_id) == (record.source, record.ref_id)
            ]
            if old:
                for key in ("source_version", "snapshot_id"):
                    before, after = old[0].metadata.get(key), metadata.get(key)
                    if before is not None and after is not None and before != after:
                        raise EvidenceValidationError("Evidence version changed during this run")
                record.metadata = {**old[0].metadata, **metadata}
            if old and old[0].content != content:
                # A full document may extend its search snippet; a changed version may not.
                if name != "get_document" or old[0].content not in content:
                    raise EvidenceValidationError("Conflicting content for one evidence id")
                self.context.records.remove(old[0])
            elif old:
                self.context.records.remove(old[0])
            self.context.records.append(record)


def validate_evidence(evidence: list, context: AgentContext) -> None:
    registry = {(record.source, record.ref_id): record for record in context.records}
    for item in evidence:
        record = registry.get((item.source, item.ref_id))
        if record is None or item.excerpt not in record.content:
            raise EvidenceValidationError("Unknown evidence id/source or non-original quote")


def validate_department(code: str, context: AgentContext) -> None:
    codes = {item.get("code") for item in context.departments}
    if code not in codes:
        raise EvidenceValidationError("Department is not in the current directory")
