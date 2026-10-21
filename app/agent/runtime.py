from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache

from langchain_core.tools import BaseTool

from app.agent.mcp_client import get_tools
from app.agent.model import StructuredModel, from_env
from app.agent.settings import Settings

ToolProvider = Callable[..., Awaitable[list[BaseTool]]]


@dataclass(frozen=True)
class AgentRuntime:
    model: StructuredModel
    tools: ToolProvider
    settings: Settings = Settings()


@lru_cache(maxsize=1)
def get_runtime() -> AgentRuntime:
    # Configuration only: request records are never kept on this shared runtime.
    from dotenv import load_dotenv

    load_dotenv()
    settings = Settings.from_env()
    return AgentRuntime(model=from_env(settings.model_timeout), tools=get_tools, settings=settings)
