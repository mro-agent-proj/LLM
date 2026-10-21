"""Obtain delegated MCP credentials without placing them in prompts or checkpoints."""

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.messages import RunRequest


@dataclass(frozen=True)
class Credentials:
    rag_token: str = field(repr=False)
    business_token: str = field(repr=False)


async def credentials_for(run: RunRequest, headers: dict[str, Any]) -> Credentials:
    # The publishing service supplies a per-request delegation in protected AMQP headers.
    # An optional internal broker can refresh credentials on redelivery after expiry.
    mode = os.getenv("MCP_DELEGATION_TRANSPORT", "unconfigured")
    broker = os.getenv("MCP_DELEGATION_URL")
    if mode == "broker":
        if not broker:
            raise RuntimeError("MCP_DELEGATION_URL is required")
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
    # Legacy team branch header names are accepted explicitly; conflicts fail closed.
    if mode not in {"amqp_headers", "unconfigured"}:
        raise ValueError("Unsupported delegation transport")
    return Credentials(
        _header(headers, "x-mro-rag-token", "rag_token"),
        _header(headers, "x-mro-business-token", "business_token"),
    )


def _header(headers, canonical, legacy):
    current, previous = headers.get(canonical), headers.get(legacy)
    if current is not None and previous is not None and current != previous:
        raise ValueError("Conflicting delegation headers")
    return _token(current if current is not None else previous)


def _token(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Delegation token must be a nonempty string")
    return value
