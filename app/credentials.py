"""Obtain delegated MCP credentials without placing them in prompts or checkpoints."""

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.messages import RunRequest


@dataclass(frozen=True)
class Credentials:
    rag_token: str | None = field(default=None, repr=False)
    business_token: str | None = field(default=None, repr=False)


async def credentials_for(run: RunRequest, headers: dict[str, Any]) -> Credentials:
    # The publishing service supplies a per-request delegation in protected AMQP headers.
    # An optional internal broker can refresh credentials on redelivery after expiry.
    broker = os.getenv("MCP_DELEGATION_URL")
    if broker:
        service_key = os.getenv("MCP_DELEGATION_KEY")
        if not service_key:
            raise RuntimeError("MCP_DELEGATION_KEY is required for the delegation endpoint")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(
                broker,
                headers={"Authorization": f"Bearer {service_key}"},
                json={"request_id": str(run.request_id)},
            )
            response.raise_for_status()
            data = response.json()
            if data.get("request_id") != str(run.request_id):
                raise RuntimeError("Delegation belongs to another maintenance request")
            return Credentials(_token(data.get("rag_token")), _token(data.get("business_token")))
    return Credentials(
        _token(headers.get("rag_token")),
        _token(headers.get("business_token")),
    )


def _token(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Delegation token must be a nonempty string")
    return value
