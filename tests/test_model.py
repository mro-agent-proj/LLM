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
