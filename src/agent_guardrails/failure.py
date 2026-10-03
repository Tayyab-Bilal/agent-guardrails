"""Is a failure worth retrying? Unknown failures are terminal: retrying a write blindly is worse."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any


class Failure(str, Enum):
    TRANSIENT = "transient"
    TERMINAL = "terminal"


# Status codes only count in a status context ("HTTP 503", "status: 404", "503 Service
# Unavailable"), so "created 500 tasks" is not an outage and "task 1404" is not a 404.
_STATUS = re.compile(
    r"(?:status(?: code)?|http|code)[\s:=]*(\d{3})\b"
    r"|\b(\d{3})\s+(?:client|server)\s+error"
    r"|\b(\d{3})\s+(?:bad request|unauthori[sz]ed|forbidden|not found|unprocessable|"
    r"too many|internal server|bad gateway|service unavailable|gateway time)", re.I)
_TERMINAL_WORDS = re.compile(r"validation|not found|access denied", re.I)
_TRANSIENT_WORDS = re.compile(r"timeout|timed out|connection reset", re.I)
_TRANSIENT_CLASSES = ("Timeout", "Connect", "RemoteProtocol")  # httpx / requests exception names


def _status(exc_or_result: Any) -> int | None:
    for holder in (exc_or_result, getattr(exc_or_result, "response", None)):
        code = getattr(holder, "status_code", None)
        if isinstance(code, int):
            return code
    m = _STATUS.search(str(exc_or_result))
    return int(next(g for g in m.groups() if g)) if m else None


def classify(exc_or_result: Any) -> Failure:
    hint = getattr(exc_or_result, "transient", None)  # typed errors say it themselves
    if isinstance(hint, bool):
        return Failure.TRANSIENT if hint else Failure.TERMINAL
    code, text = _status(exc_or_result), str(exc_or_result)
    if code in (400, 401, 403, 404, 422) or _TERMINAL_WORDS.search(text):
        return Failure.TERMINAL  # checked first: a 404 body that says "timeout" is still a 404
    transient = code == 429 or (code is not None and 500 <= code <= 599)
    named = isinstance(exc_or_result, BaseException) and any(
        k in type(exc_or_result).__name__ for k in _TRANSIENT_CLASSES)
    if transient or named or isinstance(exc_or_result, (TimeoutError, ConnectionError)) \
            or _TRANSIENT_WORDS.search(text):
        return Failure.TRANSIENT
    return Failure.TERMINAL


async def run_with_retry(fn: Callable[[], Awaitable[Any]], max_retries: int = 2,
                         backoff: float = 0.5) -> Any:
    """Retry transient exceptions only, with exponential backoff."""
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as exc:
            if attempt == max_retries or classify(exc) is Failure.TERMINAL:
                raise
            await asyncio.sleep(backoff * 2**attempt)
