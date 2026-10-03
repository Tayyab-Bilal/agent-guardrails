"""Run approved drafts from what was stored, never from anything the LLM says now."""

from typing import Any, Awaitable, Callable

from .drafts import Draft, DraftStore


async def execute_draft(draft: Draft, store: DraftStore, fn: Callable[..., Awaitable[Any]] | None,
                        schema: Any = None, missing: str = "tool is not available") -> tuple[str, Any]:
    """Claim, verify, validate, run, record. Returns ('skipped'|'posted'|'failed', payload).

    Claiming first is what makes this safe under races: whoever loses the conditional
    UPDATE does nothing (payload = the draft's current status). A crash after the claim leaves
    'executing', which is never re-run; a human has to look, because the side effect may or
    may not have happened.
    """
    if not store.claim(draft.draft_id):
        current = store.get_by_key(draft.run_id, draft.idempotency_key)
        return "skipped", current.status if current else None
    if not store.verify(draft):
        reason = "stored arguments failed signature check; refusing to run"
    elif fn is None:
        reason = missing
    else:
        try:
            args = draft.arguments
            if hasattr(schema, "model_validate"):  # same validation the agent's call went through
                ok = schema.model_validate(args)  # keep nested models as objects, as the agent call had
                args = {k: getattr(ok, k) for k in type(ok).model_fields}
            result = await fn(**args)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
        else:
            store.mark_posted(draft.draft_id, result)
            return "posted", result
    store.mark_failed(draft.draft_id, reason)
    return "failed", reason


async def replay_approved(run_id: str, tools_by_name: dict[str, Any],
                          store: DraftStore) -> dict[str, list[dict[str, Any]]]:
    """Execute every approved draft of a run. Safe to call twice, or at the same time."""
    out: dict[str, list[dict[str, Any]]] = {"posted": [], "failed": []}
    for draft in store.list_for_run(run_id):
        if draft.status != "approved":
            continue
        tool = tools_by_name.get(draft.tool_name)
        if tool is None:
            fn, schema, why = None, None, f"tool '{draft.tool_name}' is not available at replay"
        else:
            # Call the real coroutine under the gate wrapper: the gate would re-enter this path.
            fn = getattr(tool.coroutine, "_wrapped", tool.coroutine)
            schema = tool.args_schema
            why = f"tool '{draft.tool_name}' has no async implementation"
        status, payload = await execute_draft(draft, store, fn, schema, why)
        if status == "posted":
            out["posted"].append({"draft_id": draft.draft_id, "tool": draft.tool_name})
        elif status == "failed":
            out["failed"].append({"draft_id": draft.draft_id, "tool": draft.tool_name,
                                  "reason": payload})
    return out
