import asyncio
import logging
import os
from typing import Any, Protocol, TypeVar

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)


class StructuredModel(Protocol):
    async def complete(self, schema: type[T], system: str, context: str) -> T: ...


class LangChainModel:
    """One structured call per tool decision, then a separate final-output call."""

    def __init__(self, model: Any, timeout: float = 45):
        self.model = model
        self.timeout = timeout

    async def complete(self, schema: type[T], system: str, context: str) -> T:
        runnable = self.model.with_structured_output(schema)
        messages = [SystemMessage(content=system), HumanMessage(content=context)]
        for attempt in range(2):
            try:
                output = await asyncio.wait_for(runnable.ainvoke(messages), timeout=self.timeout)
                return output if isinstance(output, schema) else schema.model_validate(output)
            except ValidationError as exc:
                # Field paths and error types only: the offending values may hold model output.
                logger.warning(
                    "%s output failed validation (attempt %d): %s",
                    schema.__name__,
                    attempt + 1,
                    [(".".join(map(str, e["loc"])), e["type"]) for e in exc.errors()],
                )
                # One repair round: a malformed structured output should not end the whole run.
                if attempt:
                    raise
                problems = "; ".join(
                    f"{'.'.join(map(str, e['loc'])) or schema.__name__}: {e['msg']}"
                    for e in exc.errors()
                )
                messages = [
                    *messages,
                    AIMessage(content="(이전 출력이 스키마 검증에 실패했습니다)"),
                    HumanMessage(
                        content=f"출력이 스키마 규칙을 어겼습니다: {problems}. "
                        "규칙에 맞게 다시 출력하세요."
                    ),
                ]
        raise AssertionError("unreachable")


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
