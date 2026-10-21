import asyncio

import pytest

from app.agent.model import LangChainModel
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
