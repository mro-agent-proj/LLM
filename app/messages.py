"""Inputs and stage events used by the maintenance investigation worker."""

from typing import Literal

from app.agent.schemas import GraphState
from app.server.contracts import RunRequest, make_event

__all__ = ["RunRequest", "event", "stage_event"]


def event(request_id: str, kind: str, payload: dict) -> dict:
    return make_event(request_id, kind, payload).model_dump(mode="json")


def stage_event(
    state: GraphState,
    stage: Literal["CLASSIFYING", "INVESTIGATING", "REVIEWING"],
    output: dict | None = None,
) -> dict:
    payload = {"stage": stage, "attempt": state["retry_count"] + 1}
    if output is not None:
        payload["output"] = output
    item = event(state["request_id"], "STAGE_RESULT" if output else "STAGE_STARTED", payload)
    return {"stage_events": [*state.get("stage_events", []), item]}
