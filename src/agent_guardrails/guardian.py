"""The decision function: what should happen to this one proposed call?"""

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .describe import Describer
from .drafts import Draft, DraftStore
from .idempotency import idempotency_key
from .modes import Mode
from .permissions import (
    PermissionsMisconfigured,
    PermissionSource,
    PermissionsUnavailable,
    load_permissions,
)
from .resolver import TOOL_TO_ACTION, mode_for


class Action(str, Enum):
    EXECUTE = "execute"
    HOLD = "hold"
    SKIP = "skip"
    BLOCK = "block"
    UNAVAILABLE = "unavailable"  # permissions service down: park the run, it is not a policy answer
    MISCONFIGURED = "misconfigured"  # permissions payload/config is wrong: terminal, real cause


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str  # written for the model: says what happened and what not to do next
    mode: Mode | None = None
    draft: Draft | None = None


_SKIP_TEXT = {
    "rejected": "DECLINED BY USER: a human reviewed this exact action and said no. "
                "This is not an error; do not retry, continue with other steps.",
    "posted": "ALREADY DONE: this exact action has already been carried out. Do not repeat it.",
    "executing": "ALREADY IN PROGRESS: this exact action is being carried out. Do not repeat it.",
    "failed": "ALREADY ATTEMPTED: this action ran and failed earlier in this run. "
              "Do not repeat it; report it.",
}


async def guard(*, tool_name: str, tool_args: dict[str, Any], run_id: str, tenant_id: str,
                user_id: str, permissions: PermissionSource, store: DraftStore,
                describer: Describer, retry_backoff: float = 0.5) -> Decision:
    # 1. A draft for this exact action outranks today's mode: the human already answered.
    draft = store.get_by_key(run_id, idempotency_key(run_id, tool_name, tool_args))
    if draft is not None:
        if draft.status == "approved":
            return Decision(Action.EXECUTE, "approved by a human", Mode.SUGGEST, draft)
        if draft.status == "pending":
            return Decision(Action.HOLD, _awaiting(draft.description), Mode.SUGGEST, draft)
        if not (draft.status == "failed" and draft.mode == Mode.AUTONOMOUS.value):
            return Decision(Action.SKIP, _SKIP_TEXT[draft.status], draft=draft)
        # A failed autonomous write stays retryable, but only if policy still says Autonomous.
        verdict = await _load(permissions, tenant_id, user_id, retry_backoff)
        if isinstance(verdict, Decision):
            return verdict
        if mode_for(tool_name, verdict) is Mode.AUTONOMOUS:
            return Decision(Action.EXECUTE, "retrying a failed autonomous write", Mode.AUTONOMOUS)
        return Decision(Action.SKIP, _SKIP_TEXT["failed"], draft=draft)

    # 2. Otherwise policy decides. Unmapped tools are denied before any network call.
    if tool_name not in TOOL_TO_ACTION:
        return Decision(Action.BLOCK, f"NOT ALLOWED: '{tool_name}' has no permission mapping. "
                        "This is a policy outcome, not an error; do not retry or work around it.",
                        Mode.DISABLED)
    lookup = await _load(permissions, tenant_id, user_id, retry_backoff)  # fresh every call
    if isinstance(lookup, Decision):
        return lookup
    mode = mode_for(tool_name, lookup)
    action_name = TOOL_TO_ACTION[tool_name][1]
    if mode is Mode.AUTONOMOUS:
        return Decision(Action.EXECUTE, "allowed", mode)
    if mode is Mode.SUGGEST:
        description = await describer.describe(tool_name, tool_args, action_name)
        draft = store.create_pending(run_id, tool_name, tool_args, description)
        return Decision(Action.HOLD, _awaiting(description), mode, draft)
    return Decision(Action.BLOCK, f"NOT ALLOWED: '{action_name}' is disabled by the organisation's "
                    "settings. This is a policy outcome, not an error; do not retry.", mode)


async def _load(permissions: PermissionSource, tenant_id: str, user_id: str,
                backoff: float) -> dict | Decision:
    """Fetch permissions (transient errors are retried inside). Failures become verdicts.

    Neither verdict is ever worded as "disabled": telling a user their setting is off when the
    service was down is a lie that sends them hunting in the wrong place.
    """
    try:
        return await load_permissions(permissions, tenant_id, user_id, backoff=backoff)
    except PermissionsUnavailable as exc:
        return Decision(Action.UNAVAILABLE,
                        "SYSTEM UNAVAILABLE: the permissions service could not be reached "
                        f"({exc.cause}). Nothing was done. This is not a policy decision. "
                        "Stop making changes; the run is parked as resumable.")
    except PermissionsMisconfigured as exc:
        return Decision(Action.MISCONFIGURED,
                        f"MISCONFIGURED: permissions cannot be used ({exc.cause}). Nothing was "
                        "done. This is not a policy decision and retrying will not help; "
                        "report it to an administrator.")


def _awaiting(description: str) -> str:
    return (f'AWAITING APPROVAL: "{description}" was sent to a human for approval. '
            "Do not retry; continue with independent steps.")
