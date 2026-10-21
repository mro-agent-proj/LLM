"""Offline scripted workflow demo. Every aircraft/case/decision below is synthetic."""

import asyncio
import json
from typing import Any

from langchain_core.tools import tool

from app.agent.runtime import AgentRuntime
from app.agent.schemas import Evidence, Investigation, NextStep, Review
from app.agent.workflow import build_graph, completion_payload

DEMO_CONTENT = "SYNTHETIC CASE: wheel vibration after landing; inspection was requested."


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
        self.rows = (
            []
            if empty
            else [
                {
                    "id": "SDR-DEMO-001",
                    "source": "SDR",
                    "title": "합성 시험 사례",
                    "content": DEMO_CONTENT,
                    "metadata": {"aircraft_model": "A320-214", "airline_code": "TST", "mock": True},
                }
            ]
        )
        self.departments = [
            {
                "id": "HYD",
                "code": "HYD",
                "name": "합성 유압·착륙장치팀",
                "ata_chapters": ["29", "32"],
                "active": True,
            }
        ]
        self.calls: list[tuple[str, dict]] = []

        @tool
        async def list_departments() -> dict:
            """담당 부서의 등록 코드와 담당 ATA를 확인한다."""
            self.calls.append(("list_departments", {}))
            return {"items": self.departments}

        @tool
        async def search_sdr(
            query: str,
            aircraft_model: str | None = None,
            ata_chapter: str | None = None,
            top_k: int = 5,
        ) -> dict:
            """현재 모델 범위의 SDR 사례에서 증상·보고된 조치를 조사한다."""
            self.calls.append(("search_sdr", {"query": query, "aircraft_model": aircraft_model}))
            return {"items": self.rows[:top_k]}

        @tool
        async def get_document(id: str) -> dict:
            """이미 검색된 문서 ID의 원문을 확인한다."""
            self.calls.append(("get_document", {"id": id}))
            return {"items": [row for row in self.rows if row["id"] == id]}

        self.tools = [list_departments, search_sdr, get_document]

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
    """Test double. Does not measure LLM reasoning or real maintenance performance."""

    def __init__(self, supplements: int = 0):
        self.supplements = supplements
        self.calls: list[tuple[str, dict]] = []

    async def complete(self, schema, system: str, context: str) -> Any:
        payload = json.loads(context)
        self.calls.append((schema.__name__, payload))
        if schema is NextStep:
            searched = any(item["tool"] == "search_sdr" for item in payload.get("observations", []))
            if (
                payload["stage"] == "INVESTIGATING"
                and not payload["evidence_records"]
                and not searched
            ):
                return NextStep(
                    action="CALL",
                    question="유사 증상에 어떤 조사 이력이 보고됐는가?",
                    tool_name="search_sdr",
                    arguments_json='{"query":"wheel vibration"}',
                    hold_reason=None,
                )
            return finish()
        if schema is Investigation:
            return Investigation(
                ata_chapter="32",
                finding="합성 사례에 근거한 휠 진동 조사 요청.",
                severity="MEDIUM",
                evidence=[
                    Evidence(
                        source="SDR",
                        ref_id="SDR-DEMO-001",
                        excerpt="wheel vibration after landing",
                        why="증상·발생 단계 비교에 사용한 합성 근거.",
                    )
                ],
                proposed_department="HYD",
                rationale="합성 사례의 증상·담당 업무를 비교했다. 실제 원인은 미확정이다.",
            )
        if schema is Review:
            if self.supplements:
                self.supplements -= 1
                return Review(
                    decision="SUPPLEMENT",
                    department=None,
                    comments="합성 재조사 경로 시험.",
                    supplement_request="이전 사례의 발생 단계를 다시 비교하라.",
                )
            return Review(
                decision="PASS",
                department="HYD",
                supplement_request=None,
                comments="합성 사례 인계 시험 통과. 실제 정비 판단이 아니다.",
            )
        raise AssertionError(schema)


async def main() -> None:
    for name, empty in [("정상 인계", False), ("근거 없음", True)]:
        runtime = AgentRuntime(model=ScriptedDemoModel(), tools=DemoTools(empty=empty))
        state = await build_graph(runtime).ainvoke(example_state())
        output = await completion_payload(state, runtime)
        print(json.dumps({"scenario": name, "mock": True, "payload": output}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
