"""Synthetic dependencies for automated tests; never imported by the service."""

import copy
import json
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from app.agent.classification import SymptomTriage
from app.agent.schemas import (
    Evidence,
    Investigation,
    InvestigationAssessment,
    NextStep,
    Review,
    ReviewAssessment,
)

DEMO_CONTENT = "SYNTHETIC CASE: wheel vibration after landing; inspection was requested."
INPUTS = json.loads(Path(__file__).with_name("tool_inputs.json").read_text())["schemas"]


def example_state() -> dict:
    return {
        "request_id": "DEMO-REQUEST-001",
        "tail_number": "DEMO-TAIL",
        "aircraft_model": "A320-214",
        "airline_code": "TST",
        "symptom_text": "합성 신고: 착륙 후 휠 진동. 시연용 접수 우선순위 MEDIUM.",
        "classification": None,
        "investigations": [],
        "reviews": [],
        "retry_count": 0,
    }


class DemoTools:
    def __init__(self, empty: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.version = 1
        self.status = "INVESTIGATING"
        self.scope = {
            "operator_code": "DEMO-US-OPERATOR",
            "model_key": "A320214",
            "operator_filter_applied": True,
            "model_filter_applied": True,
            "locked_filters": {"jasc_codes": [], "difficulty_from": None, "difficulty_to": None},
        }
        self.snapshots = ["DEMO-RELEASE", "DEMO-JASC-INDEX", "DEMO-SDR-INDEX", "DEMO-AMM-INDEX"]
        self.departments = [
            {
                "department_id": "HYD",
                "name": "합성 유압·착륙장치팀",
                "responsibilities": ["wheel inspection"],
                "ata_chapters": "29,32",
                "supported_models": ["A320-214"],
                "availability_status": "available",
                "active": True,
                "mock": True,
            }
        ]
        self.rows = (
            []
            if empty
            else [
                {
                    "evidence_id": "EV-SDR-DEMO-001",
                    "source_type": "sdr",
                    "source_id": "SDR-DEMO-001",
                    "source_version": "DEMO-v1",
                    "evidence_group_id": "DEMO-GROUP-001",
                    "case_id": "SDR-DEMO-001",
                    "operator_code": "DEMO-US-OPERATOR",
                    "model_key": "A320214",
                    "difficulty_date": "2026-10-01",
                    "submission_date": "2026-10-03",
                    "reported_jasc": "3230",
                    "narrative_excerpt": DEMO_CONTENT,
                    "quote": DEMO_CONTENT,
                    "reported_actions": ["inspection was requested"],
                    "cited_references": [],
                    "source_locator": {"record_key": "SDR-DEMO-001"},
                    "mock": True,
                }
            ]
        )
        self.amm_rows = [
            {
                "evidence_id": "EV-AMM-DEMO-001",
                "source_type": "derived_amm_reference",
                "source_id": "AMM-DEMO-001",
                "source_version": "DEMO-v1",
                "evidence_group_id": "DEMO-GROUP-001",
                "source_case_id": "SDR-DEMO-001",
                "cited_amm_ref_raw": "32-XX-XX",
                "cited_amm_ref_normalized": "32-XX-XX",
                "reference_level": "chapter_section",
                "quote": "SYNTHETIC AMM citation context only.",
                "symptom": "wheel vibration",
                "reported_action": None,
                "outcome": None,
                "relationship_status": "ambiguous",
                "applicability_status": "not_verified",
                "reported_jasc": "3230",
                "mock": True,
            }
        ]
        self.history = [
            {
                "history_id": "MH-000123",
                "evidence_id": "EV-HISTORY-DEMO-001",
                "source_type": "mock_aircraft_history",
                "source_id": "MH-000123",
                "source_version": "DEMO-history-v1",
                "evidence_group_id": "DEMO-HISTORY-GROUP",
                "aircraft_id": "DEMO-AIRCRAFT",
                "tail_number": "DEMO-TAIL",
                "symptom": "착륙 후 휠 진동",
                "observations": ["after landing"],
                "actions": ["inspection requested"],
                "outcome": None,
                "occurred_at": "2026-10-01T00:00:00Z",
                "reported_at": "2026-10-01T00:00:00Z",
                "closed_at": None,
                "ata_chapter": "32",
                "jasc_code": "3230",
                "department_id": "HYD",
                "source_sdr_ids": ["SDR-DEMO-001"],
                "mock": True,
            }
        ]
        self.technical = [
            {
                "evidence_id": "EV-TECH-DEMO-001",
                "source_type": "airbus_technical_article",
                "source_id": "TECH-DEMO-001",
                "source_version": "DEMO-tech-v1",
                "evidence_group_id": "DEMO-TECH-GROUP",
                "title": "Synthetic technical background",
                "quote": "SYNTHETIC: inspect operating conditions before comparing cases.",
                "applicability_status": "supported",
                "applicability_basis": "Synthetic A320 family scope",
                "configuration_conditions": [],
                "mock": True,
            }
        ]
        self.on_call = None
        self.tools = [self._make_tool(name) for name in INPUTS]

    def _make_tool(self, name: str):
        async def invoke(**args):
            self.calls.append((name, args))
            result = self.response(name, args)
            if self.on_call:
                replacement = self.on_call(name, args, result)
                if replacement is not None:
                    result = replacement
            return result

        return StructuredTool.from_function(
            coroutine=invoke,
            name=name,
            description=f"SYNTHETIC contract fixture: {name}",
            args_schema=INPUTS[name],
        )

    def response(self, name: str, args: dict) -> dict:
        scope = copy.deepcopy(self.scope)
        if name == "search_jasc_codes":
            scope.update(operator_filter_applied=False, model_filter_applied=False)
        data = {
            "status": "OK",
            "request_id": args["request_id"],
            "context_version": self.version,
            "trace_id": f"DEMO-TRACE-{len(self.calls)}",
            "applied_scope": scope,
            "snapshot_ids": list(self.snapshots),
            "counts": {"scope_count": None, "eligible_count": None, "returned_count": 0},
            "next_cursor": None,
            "warnings": [],
            "missing_fields": [],
            "error": None,
        }
        if name == "get_request_context":
            data["result"] = {
                "report": {
                    "symptom": example_state()["symptom_text"],
                    "reporter_role": "ground_maintenance",
                    "reported_at": "2026-10-21T00:00:00Z",
                },
                "aircraft": {
                    "aircraft_id": "DEMO-AIRCRAFT",
                    "registration": "DEMO-TAIL",
                    "model_raw": "A320-214",
                },
                "resolved_scope": scope,
                "context_version": self.version,
                "request_version": 1,
                "status": self.status,
                "missing_fields": [],
                "locked_filters": scope["locked_filters"],
            }
        elif name == "search_departments":
            data["items"] = copy.deepcopy(self.departments)
        elif name == "get_aircraft_history":
            data["items"] = copy.deepcopy(self.history)
        elif name == "search_jasc_codes":
            rows = [
                {
                    "code": "3230",
                    "title": "Synthetic wheel classification",
                    "definition": None,
                    "source_url": "https://example.invalid/synthetic",
                    "codebook_version": "DEMO-codebook",
                }
            ]
            data["items"] = [r for r in rows if "codes" not in args or r["code"] in args["codes"]]
        elif name == "search_sdr_cases":
            data["items"] = copy.deepcopy(self.rows[: args.get("limit", 5)])
        elif name == "search_amm_references":
            data["items"] = copy.deepcopy(self.amm_rows[: args.get("limit", 5)])
            data["warnings"] = ["AMM_REFERENCE_ONLY: not verified"]
        elif name == "get_sdr_amm_references":
            data["items"] = [
                {**copy.deepcopy(r), "sdr_evidence_id": "EV-" + r["source_case_id"]}
                for r in self.amm_rows
                if "EV-" + r["source_case_id"] in args["evidence_ids"]
            ]
        elif name == "search_technical_docs":
            data["items"] = [
                copy.deepcopy(r)
                for r in self.technical
                if r["applicability_status"] != "unknown"
                or args.get("include_unknown_applicability")
            ]
            data["snapshot_ids"] = ["DEMO-TECH-RELEASE", "DEMO-TECH-INDEX"]
            data["applied_scope"]["operator_filter_applied"] = False
        elif name == "get_evidence":
            row = next(
                (
                    r
                    for r in self.rows + self.amm_rows + self.technical
                    if r["evidence_id"] == args["evidence_id"]
                ),
                None,
            )
            if row:
                data["result"] = {
                    k: row[k]
                    for k in (
                        "evidence_id",
                        "source_type",
                        "source_id",
                        "source_version",
                        "evidence_group_id",
                    )
                }
                quote = row["quote"]
                data["result"].update(
                    text=quote,
                    quote=quote,
                    locator={"record_key": row["source_id"]},
                    quote_spans=[{"start": 0, "end": len(quote), "text": quote}],
                    parent_source_ids=[],
                )
                if row["source_type"] == "airbus_technical_article":
                    data["snapshot_ids"] = ["DEMO-TECH-RELEASE", "DEMO-TECH-INDEX"]
                    data["applied_scope"]["operator_filter_applied"] = False
            else:
                data["result"] = None
        count = (
            len(data.get("items", [])) if "items" in data else int(data.get("result") is not None)
        )
        data["counts"]["returned_count"] = count
        if not count:
            data["status"] = "NO_MATCH"
        return data

    async def __call__(self, *servers) -> list:
        return self.tools


def finish() -> NextStep:
    return NextStep(
        action="FINISH",
        question="조회한 합성 근거로 인계안을 작성할 수 있는가?",
        tool_name=None,
        arguments_json=None,
        hold_reason=None,
    )


class ScriptedDemoModel:
    """Response double. Passing this demo is not an LLM quality measurement."""

    def __init__(self, supplements: int = 0, triage_ata: list[str] | None = None):
        self.supplements = supplements
        # LLM1 step 1: ATA hypotheses from the symptom alone (the demo symptom is a wheel issue).
        self.triage_ata = triage_ata or ["32"]
        self.calls: list[tuple[str, dict]] = []

    async def complete(self, schema, system: str, context: str) -> Any:
        payload = json.loads(context)
        self.calls.append((schema.__name__, payload))
        if schema is SymptomTriage:
            return SymptomTriage(candidate_ata=self.triage_ata, rationale="합성 증상의 대상 계통.")
        if schema is NextStep:
            searched = any(
                item["tool"] == "search_sdr_cases" for item in payload.get("observations", [])
            )
            if (
                payload["stage"] == "INVESTIGATING"
                and not payload["evidence_records"]
                and not searched
            ):
                return NextStep(
                    action="CALL",
                    question="유사 증상에 어떤 조사 이력이 보고됐는가?",
                    tool_name="search_sdr_cases",
                    arguments_json='{"query":"wheel vibration"}',
                    hold_reason=None,
                )
            return finish()
        if schema is InvestigationAssessment:
            return InvestigationAssessment(
                investigation=Investigation(
                    ata_chapter="32",
                    finding="합성 사례에 근거한 휠 진동 조사 요청.",
                    severity="MEDIUM",
                    evidence=[
                        Evidence(
                            source="SDR",
                            ref_id="EV-SDR-DEMO-001",
                            excerpt="wheel vibration after landing",
                            why="증상·발생 단계 비교에 사용한 합성 근거.",
                        )
                    ],
                    proposed_department="HYD",
                    rationale="합성 근거 기반 인계. 실제 원인은 미확정이다.",
                ),
                technical_basis_ids=[],
            )
        if schema is ReviewAssessment:
            if self.supplements:
                self.supplements -= 1
                review = Review(
                    decision="SUPPLEMENT",
                    department=None,
                    comments="합성 재조사 경로 시험.",
                    supplement_request="이전 사례의 발생 단계를 다시 비교하라.",
                )
            else:
                review = Review(
                    decision="PASS",
                    department="HYD",
                    supplement_request=None,
                    comments="합성 사례 인계 시험. 실제 정비 판단이 아니다.",
                )
            return ReviewAssessment(review=review, additional_evidence=[], technical_basis_ids=[])
        raise AssertionError(schema)
