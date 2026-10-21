"""Inputs and stage events used by the maintenance investigation worker."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.schemas import Classification, GraphState


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    request_id: UUID
    tail_number: str = Field(min_length=1, max_length=16)
    aircraft_model: str = Field(min_length=1, max_length=80)
    airline_code: str = Field(pattern=r"^[A-Z]{3}$")
    symptom_text: str = Field(min_length=1, max_length=20000)
    classification: Classification | None = None

    @field_validator("tail_number", "aircraft_model", "symptom_text")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be blank")
        return value

    def initial_state(self) -> GraphState:
        return {
            "request_id": str(self.request_id),
            "tail_number": self.tail_number,
            "aircraft_model": self.aircraft_model,
            "airline_code": self.airline_code,
            "symptom_text": self.symptom_text,
            "classification": self.classification,
            "investigations": [],
            "reviews": [],
            "retry_count": 0,
            "stage_events": [],
        }


def event(request_id: str, kind: str, payload: dict) -> dict:
    return {
        "event_id": str(uuid4()),
        "request_id": request_id,
        "type": kind,
        "occurred_at": utc_now(),
        "payload": payload,
    }


def stage_event(
    state: GraphState,
    stage: Literal["INVESTIGATING", "REVIEWING"],
    output: dict | None = None,
) -> dict:
    payload = {"stage": stage, "attempt": state["retry_count"] + 1}
    if output is not None:
        payload["output"] = output
    item = event(state["request_id"], "STAGE_RESULT" if output else "STAGE_STARTED", payload)
    return {"stage_events": [*state.get("stage_events", []), item]}
