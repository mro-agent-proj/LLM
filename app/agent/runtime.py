from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache

from langchain_core.tools import BaseTool

from app.agent.mcp_client import MCPToolProvider, get_tools
from app.agent.model import StructuredModel, from_env
from app.agent.settings import Settings

ToolProvider = Callable[..., Awaitable[list[BaseTool]]]


@dataclass(frozen=True)
class AgentRuntime:
    model: StructuredModel
    tools: ToolProvider
    settings: Settings = Settings()


_active_runtime: ContextVar[AgentRuntime | None] = ContextVar("mro_request_runtime", default=None)


@contextmanager
def using_runtime(runtime: AgentRuntime):
    """Bind delegation/runtime to one async request, never a process-global token."""
    token = _active_runtime.set(runtime)
    try:
        yield runtime
    finally:
        _active_runtime.reset(token)


def get_runtime() -> AgentRuntime:
    return _active_runtime.get() or _default_runtime()


@lru_cache(maxsize=1)
def _default_runtime() -> AgentRuntime:
    # Configuration only: request records are never kept on this shared runtime.
    from dotenv import load_dotenv

    load_dotenv()
    settings = Settings.from_env()
    return AgentRuntime(model=from_env(settings.model_timeout), tools=get_tools, settings=settings)


def runtime_for_request(*, rag_token: str, business_token: str | None = None) -> AgentRuntime:
    """Runner supplies Backend-issued delegation; tokens never enter model/checkpoint data."""
    from dotenv import load_dotenv

    load_dotenv()
    settings = Settings.from_env()
    return AgentRuntime(
        model=from_env(settings.model_timeout),
        tools=MCPToolProvider(rag_token=rag_token, business_token=business_token),
        settings=settings,
    )
