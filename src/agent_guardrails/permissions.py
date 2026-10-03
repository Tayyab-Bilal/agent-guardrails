"""Where tenant permissions come from, and the two ways fetching them can fail.

The split matters: a timeout must make the caller retry or park the run, never tell the
user "Disabled in your settings". A malformed payload is a real, terminal cause.
"""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from .failure import Failure, classify, run_with_retry
from .modes import Mode
from .resolver import normalize_permissions


class PermissionsError(Exception):
    transient: bool  # read by failure.classify, so run_with_retry retries only the transient kind

    def __init__(self, cause: str):
        super().__init__(cause)
        self.cause = cause


class PermissionsUnavailable(PermissionsError):
    """Transient (timeout, connection, 5xx): retry or park the run."""

    transient = True


class PermissionsMisconfigured(PermissionsError):
    """Terminal (malformed payload, 4xx): carries the real cause."""

    transient = False


class PermissionSource(Protocol):
    async def fetch(self, tenant_id: str, user_id: str) -> dict[str, Any]: ...


class StaticPermissionSource:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    async def fetch(self, tenant_id: str, user_id: str) -> dict[str, Any]:
        return self.config


class FilePermissionSource:
    """Stands in for the backend HTTP call: reads a JSON file on every fetch (never cached)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    async def fetch(self, tenant_id: str, user_id: str) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise PermissionsMisconfigured(f"invalid JSON in {self.path.name}: {exc}") from exc
        except (FileNotFoundError, PermissionError) as exc:  # a wrong path is a config bug, not an outage
            raise PermissionsMisconfigured(f"cannot read {self.path.name}: {exc}") from exc


GetJson = Callable[[str, dict[str, str]], Awaitable[dict[str, Any]]]


class HttpPermissionSource:
    """Fetches the tenant config over HTTP through an injected `get_json(url, headers)`.

    Injecting the callable keeps this module free of an HTTP dependency (wrap httpx, aiohttp,
    anything) and lets tests use a fake. Errors go through `failure.classify`, so an outage
    becomes `PermissionsUnavailable` and a 4xx or bad body becomes `PermissionsMisconfigured`.
    `url` may contain `{tenant_id}` and `{user_id}`.
    """

    def __init__(self, url: str, get_json: GetJson, headers: dict[str, str] | None = None):
        self.url, self.get_json, self.headers = url, get_json, headers or {}

    async def fetch(self, tenant_id: str, user_id: str) -> dict[str, Any]:
        url = self.url.format(tenant_id=tenant_id, user_id=user_id)
        try:
            return await self.get_json(url, dict(self.headers))
        except PermissionsError:
            raise
        except Exception as exc:
            cause = f"{type(exc).__name__}: {exc}"
            if classify(exc) is Failure.TRANSIENT:
                raise PermissionsUnavailable(cause) from exc
            raise PermissionsMisconfigured(cause) from exc


def _validate(payload: Any) -> None:
    """Reject anything that is not {"categories": [{"name": str, "actions": [{"name": str, "mode": str}]}]}."""
    bad = PermissionsMisconfigured("permission payload does not match the expected shape")
    if not isinstance(payload, dict) or not isinstance(payload.get("categories", []), list):
        raise bad
    for category in payload.get("categories", []):
        if not isinstance(category, dict) or not isinstance(category.get("name"), str):
            raise bad
        if not isinstance(category.get("actions", []), list):
            raise bad
        for action in category.get("actions", []):
            if not isinstance(action, dict):
                raise bad
            if not isinstance(action.get("name"), str) or not isinstance(action.get("mode"), str):
                raise bad


async def load_permissions(
    source: PermissionSource, tenant_id: str, user_id: str, *, retries: int = 2,
    backoff: float = 0.5,
) -> dict[tuple[str, str], Mode]:
    """Fetch fresh (retrying transient errors), classify any failure into a typed verdict."""
    try:
        payload = await run_with_retry(lambda: source.fetch(tenant_id, user_id), retries, backoff)
    except PermissionsError:
        raise
    except Exception as exc:  # same transient/terminal rules as tool failures
        cause = f"{type(exc).__name__}: {exc}"
        if classify(exc) is Failure.TRANSIENT:
            raise PermissionsUnavailable(cause) from exc
        raise PermissionsMisconfigured(cause) from exc
    _validate(payload)
    return normalize_permissions(payload)
