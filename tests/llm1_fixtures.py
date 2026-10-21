import json

from app.agent.classification import ClassificationDecision
from app.agent.schemas import Classification, NextStep
from tests.support import DemoTools, ScriptedDemoModel


class BusinessTools(DemoTools):
    def __init__(self):
        super().__init__()
        self.server_calls = []

    async def __call__(self, *servers):
        self.server_calls.append(servers)
        from app.agent.mcp_client import BUSINESS_TOOLS, RAG_TOOLS

        allowed = set()
        if "business" in servers:
            allowed.update(BUSINESS_TOOLS)
        if "rag" in servers:
            allowed.update(RAG_TOOLS)
        return [t for t in self.tools if t.name in allowed]


class FullModel(ScriptedDemoModel):
    def __init__(self, sufficient=True, supplements=0, **changes):
        super().__init__(supplements)
        self.output = Classification(
            ata_chapter="32",
            linked_history_ids=["MH-000123"],
            sufficient=sufficient,
            proposed_department="HYD",
            rationale="같은 기체의 휠 증상과 담당 부서를 연결했다.",
        )
        self.output = self.output.model_copy(update=changes)

    async def complete(self, schema, system, context):
        data = json.loads(context)
        if schema is ClassificationDecision:
            self.calls.append((schema.__name__, data))
            return ClassificationDecision(classification=self.output, missing_information=None)
        if (
            schema is NextStep
            and data["stage"] == "INVESTIGATING"
            and not any(r["source"] == "SDR" for r in data["evidence_records"])
        ):
            return NextStep(
                action="CALL",
                question="관련 SDR 사례를 확인한다.",
                tool_name="search_sdr_cases",
                arguments_json='{"query":"wheel"}',
                hold_reason=None,
            )
        return await super().complete(schema, system, context)
