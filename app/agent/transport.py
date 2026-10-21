"""Retry only identifiable transient transport failures, including async task groups."""

import httpx


def transient_error(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(transient_error(e) for e in exc.exceptions)
    return isinstance(exc, (TimeoutError, ConnectionError, OSError, httpx.TransportError))
