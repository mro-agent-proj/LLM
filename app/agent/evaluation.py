"""Bounded evaluation: actual LLM + synthetic tools, or scripted contract regression.

Expected outcomes stay in this evaluator and never enter model/tool context. This is
not a real-aircraft diagnosis benchmark or a measurement of the team's search corpus.
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass

from app.agent.demo import DemoTools, ScriptedDemoModel, example_state
from app.agent.model import from_env
from app.agent.runtime import AgentRuntime
from app.agent.tools import evidence_groups, get_context, validate_evidence
from app.agent.workflow import build_graph, completion_payload


@dataclass
class EvaluationCase:
    case_id: str
    tools: DemoTools
    expected_outcome: str
    expected_department: str | None = None


class MeasuredModel:
    def __init__(self, model):
        self.model = model
        self.calls = 0
        self.seconds = 0.0

    async def complete(self, schema, system, context):
        self.calls += 1
        started = time.perf_counter()
        try:
            return await self.model.complete(schema, system, context)
        finally:
            self.seconds += time.perf_counter() - started


def cases() -> list[EvaluationCase]:
    normal = EvaluationCase("supported_handoff", DemoTools(), "ASSIGN", "HYD")
    no_data = DemoTools(empty=True)
    no_data.history = []
    no_data.amm_rows = []
    no_data.technical = []
    missing = DemoTools()

    def needs_input(name, args, result):
        if name == "get_request_context":
            result.update(status="NEEDS_INPUT", result=None, missing_fields=["operator_code"])
        return result

    missing.on_call = needs_input
    wrong_model = DemoTools()
    wrong_model.rows[0]["model_key"] = "A330343"
    changed_source = DemoTools()

    def source_changed(name, args, result):
        if name == "get_evidence":
            result["result"]["source_version"] = "DEMO-v2"
        return result

    changed_source.on_call = source_changed
    changed_index = DemoTools()

    def index_changed(name, args, result):
        if name == "get_evidence":
            result["snapshot_ids"] = ["DEMO-OTHER-RELEASE"]
        return result

    changed_index.on_call = index_changed
    unavailable = DemoTools()
    unavailable.departments[0]["active"] = False
    stopped = DemoTools()
    stopped.status = "HUMAN_REVIEW"
    return [
        normal,
        EvaluationCase("no_case_evidence", no_data, "HUMAN_REVIEW"),
        EvaluationCase("scope_needs_input", missing, "HUMAN_REVIEW"),
        EvaluationCase("wrong_model_evidence", wrong_model, "HUMAN_REVIEW"),
        EvaluationCase("source_version_changed", changed_source, "HUMAN_REVIEW"),
        EvaluationCase("index_snapshot_changed", changed_index, "HUMAN_REVIEW"),
        EvaluationCase("department_unavailable", unavailable, "HUMAN_REVIEW"),
        EvaluationCase("already_human_review", stopped, "HUMAN_REVIEW"),
    ]


async def evaluate(case: EvaluationCase, model, *, live: bool, timeout: float = 180) -> dict:
    measured = MeasuredModel(model)
    runtime = AgentRuntime(model=measured, tools=case.tools)
    start = time.perf_counter()
    result = {
        "case_id": case.case_id,
        "evaluation_kind": "actual_llm_synthetic_tools" if live else "scripted_contract_regression",
        "data_origin": "synthetic",
        "expected_outcome": case.expected_outcome,
    }
    try:
        async with asyncio.timeout(timeout):
            state = await build_graph(runtime).ainvoke(example_state())
            payload = await completion_payload(state, runtime)
        context = get_context(state)
        if payload["outcome"] == "ASSIGN":
            validate_evidence(context.final_evidence, context)
        passed = payload["outcome"] == case.expected_outcome and (
            case.expected_department is None
            or payload.get("department") == case.expected_department
        )
        result.update(
            payload=payload,
            expected_workflow_matched=passed,
            model_calls=measured.calls,
            model_seconds=round(measured.seconds, 4),
            tool_calls=len(context.traces),
            retry_count=state["retry_count"],
            context_refresh_count=context.context_refresh_count,
            distinct_source_groups=len(evidence_groups(context)),
            traces=[t.model_dump(mode="json") for t in context.traces],
            investigations=[i.model_dump(mode="json") for i in state["investigations"]],
            reviews=[r.model_dump(mode="json") for r in state["reviews"]],
            token_usage=None,
            human_semantic_review_required=live,
        )
    except Exception as exc:
        result.update(
            expected_workflow_matched=False,
            error_type=type(exc).__name__,
            model_calls=measured.calls,
        )
    result["total_seconds"] = round(time.perf_counter() - start, 4)
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live-model",
        action="store_true",
        help="Calls the configured provider and incurs usage; uses synthetic tools only",
    )
    parser.add_argument("--case", action="append", choices=[c.case_id for c in cases()])
    parser.add_argument("--timeout-seconds", type=float, default=180)
    args = parser.parse_args()
    if not 0 < args.timeout_seconds <= 240:
        parser.error("timeout must be greater than zero and at most 240 seconds")
    model = None
    if args.live_model:
        from dotenv import load_dotenv

        load_dotenv()
        model = from_env(45)
    results = []
    for case in cases():
        if args.case and case.case_id not in args.case:
            continue
        results.append(
            await evaluate(
                case,
                model if args.live_model else ScriptedDemoModel(),
                live=args.live_model,
                timeout=args.timeout_seconds,
            )
        )
    print(
        json.dumps(
            {
                "live_model_used": args.live_model,
                "case_count": len(results),
                "workflow_matches": sum(r["expected_workflow_matched"] for r in results),
                "limitations": [
                    "Synthetic workflow labels; not aircraft fault diagnosis accuracy",
                    "Team MCP retrieval quality is not evaluated",
                    "Token cost unavailable from the current model adapter; null is not zero",
                ],
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
