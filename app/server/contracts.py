import json
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, field_validator

from app.agent.schemas import ContractModel, GraphState


class RunRequest(ContractModel):
    request_id: str
    tail_number: str = Field(min_length=1, max_length=40)
    aircraft_model: str = Field(min_length=1, max_length=80)
    airline_code: str = Field(pattern=r"^[A-Z]{3}$")
    symptom_text: str = Field(min_length=1, max_length=10000)

    @field_validator("request_id")
    @classmethod
    def uuid_request(cls, value: str) -> str:
        return str(UUID(value))

    def initial_state(self) -> GraphState:
        return {
            **self.model_dump(),
            "classification": None,
            "investigations": [],
            "reviews": [],
            "retry_count": 0,
        }


class AgentEvent(ContractModel):
    event_id: str
    request_id: str
    type: Literal["STAGE_STARTED", "STAGE_RESULT", "COMPLETED", "FAILED"]
    occurred_at: str
    payload: dict


def make_event(request_id: str, kind: str, payload: dict) -> AgentEvent:
    # Same semantic event retains its identity on redelivery; no process-local random IDs.
    identity = json.dumps([request_id, kind, payload], sort_keys=True, ensure_ascii=False)
    return AgentEvent(
        event_id=str(uuid5(NAMESPACE_URL, "mro-agent-event:" + identity)),
        request_id=request_id,
        type=kind,
        occurred_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        payload=payload,
    )
