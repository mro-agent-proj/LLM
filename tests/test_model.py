import asyncio

import pytest

from app.agent.model import LangChainModel, from_env
from app.agent.schemas import Review


@pytest.mark.asyncio
async def test_model_adapter_uses_schema_and_parses_dictionary():
    seen = []

    class Runnable:
        async def ainvoke(self, messages):
            seen.extend(messages)
            return {
                "decision": "HUMAN",
                "department": None,
                "comments": "현장 정보 필요",
                "supplement_request": None,
            }

    class Model:
        def with_structured_output(self, schema):
            assert schema is Review
            return Runnable()

    result = await LangChainModel(Model()).complete(Review, "system", "request")
    assert result.decision == "HUMAN"
    assert [message.type for message in seen] == ["system", "human"]


@pytest.mark.asyncio
async def test_model_timeout_propagates_to_runner():
    class Runnable:
        async def ainvoke(self, messages):
            await asyncio.sleep(0.1)

    class Model:
        def with_structured_output(self, schema):
            return Runnable()

    with pytest.raises(TimeoutError):
        await LangChainModel(Model(), timeout=0.005).complete(Review, "system", "request")


def test_openai_environment_key_fallback_and_explicit_llm_key_priority(monkeypatch):
    captured = []
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")

    def initialize(name, **options):
        captured.append((name, options))
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", initialize)
    from_env(45)
    assert captured[-1][1]["api_key"] == "synthetic-openai-key"
    monkeypatch.setenv("LLM_API_KEY", "synthetic-explicit-key")
    from_env(45)
    assert captured[-1][1]["api_key"] == "synthetic-explicit-key"
    monkeypatch.delenv("LLM_API_KEY")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        from_env(45)


def test_classification_wins_when_a_model_also_fills_missing_information():
    from app.agent.classification import ClassificationDecision
    from app.agent.schemas import Classification

    decision = ClassificationDecision(
        classification=Classification(
            ata_chapter="79",
            linked_history_ids=[],
            sufficient=False,
            proposed_department=None,
            rationale="오일 압력 계통",
        ),
        missing_information="현장 오일량 확인 필요",
        link_evidence=[],
    )
    assert decision.classification is not None and decision.missing_information is None


@pytest.mark.asyncio
async def test_malformed_structured_output_is_repaired_once_then_fails():
    from pydantic import BaseModel, Field

    from app.agent.model import LangChainModel

    class Out(BaseModel):
        code: str = Field(pattern=r"^[0-9]{2}$")

    class Fake:
        def __init__(self, outputs):
            self.outputs, self.seen = list(outputs), []

        def with_structured_output(self, schema):
            fake = self

            class Runnable:
                async def ainvoke(self, messages):
                    fake.seen.append(messages)
                    return fake.outputs.pop(0)

            return Runnable()

    fake = Fake([{"code": "bad"}, {"code": "32"}])
    assert (await LangChainModel(fake).complete(Out, "s", "c")).code == "32"
    assert "스키마 규칙" in fake.seen[1][-1].content
    with pytest.raises(Exception):
        await LangChainModel(Fake([{"code": "x"}, {"code": "y"}])).complete(Out, "s", "c")
