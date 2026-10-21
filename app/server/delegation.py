"""Optional carrier adapter. AMQP header names below are a proposal, not a team agreement."""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, SecretStr

from app.agent.mcp_client import MCPToolProvider
from app.agent.model import StructuredModel
from app.agent.runtime import AgentRuntime
from app.agent.settings import Settings
from app.server.contracts import RunRequest


class Delegation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_token: SecretStr
    rag_token: SecretStr


class HeaderRuntimeFactory:
    """Opt in only once Backend supplies these per-request signed tokens and DB contexts."""

    def __init__(self, model: StructuredModel, settings: Settings):
        self.model, self.settings = model, settings

    async def __call__(self, request: RunRequest, headers: Mapping) -> AgentRuntime:
        tokens = {}
        for field, header in (
            ("business_token", "x-mro-business-token"),
            ("rag_token", "x-mro-rag-token"),
        ):
            value = headers.get(header)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Missing request-scoped MCP delegation")
            tokens[field] = value
        delegation = Delegation(**tokens)
        return AgentRuntime(
            model=self.model,
            settings=self.settings,
            tools=MCPToolProvider(
                business_token=delegation.business_token.get_secret_value(),
                rag_token=delegation.rag_token.get_secret_value(),
            ),
        )
