import asyncio
import os
from typing import Any, Protocol, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class StructuredModel(Protocol):
    async def complete(self, schema: type[T], system: str, context: str) -> T: ...


class LangChainModel:
    """One structured call per tool decision, then a separate final-output call."""

    def __init__(self, model: Any, timeout: float = 45):
        self.model = model
        self.timeout = timeout

    async def complete(self, schema: type[T], system: str, context: str) -> T:
        runnable = self.model.with_structured_output(schema)
        output = await asyncio.wait_for(
            runnable.ainvoke([SystemMessage(content=system), HumanMessage(content=context)]),
            timeout=self.timeout,
        )
        return output if isinstance(output, schema) else schema.model_validate(output)


def from_env(timeout: float) -> LangChainModel:
    from langchain.chat_models import init_chat_model

    model_name = os.getenv("LLM_MODEL")
    provider = os.getenv("LLM_PROVIDER")
    api_key = os.getenv("LLM_API_KEY")
    if not api_key and provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
    if not model_name or not api_key:
        raise RuntimeError("Set LLM_MODEL and LLM_API_KEY before live execution")
    options: dict[str, Any] = {"api_key": api_key, "max_retries": 0}
    if provider:
        options["model_provider"] = provider
    return LangChainModel(init_chat_model(model_name, **options), timeout=timeout)
