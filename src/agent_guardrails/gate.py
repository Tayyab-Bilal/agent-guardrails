"""The single choke point. Every mutating tool is wrapped here, before the agent sees it."""

from typing import Any, Sequence

from langchain_core.tools import StructuredTool

from .describe import Describer
from .drafts import DraftStore
from .guardian import Action, guard
from .outcome import RunOutcome
from .permissions import PermissionSource
from .replay import execute_draft
from .resolver import MUTATING_TOOLS, READ_TOOLS, TOOL_TO_ACTION


def unclassified_tools(tools: Sequence[StructuredTool]) -> list[str]:
    """Tools that are neither mapped, explicitly denied, nor listed as read-only."""
    known = MUTATING_TOOLS | READ_TOOLS
    return [t.name for t in tools if t.name not in known]


def guard_tools(tools: Sequence[StructuredTool], *, run_id: str, tenant_id: str, user_id: str,
                permissions: PermissionSource, store: DraftStore, describer: Describer,
                max_mutations: int = 150, outcome: RunOutcome | None = None,
                retry_backoff: float = 0.5) -> list[StructuredTool]:
    """Return NEW tools whose mutating coroutines are gated (name, description, schema unchanged).

    The input tools are never modified, so one shared list can be guarded for many tenants or
    runs without the last caller's context leaking into anyone else's agent. Read tools are
    returned as-is. Guarding an already guarded tool re-wraps from the original coroutine.

    If `outcome` is given, every call is recorded on it (see outcome.py) by this code, not by
    the model. Only code holding the returned copies is gated: the originals stay ungated.
    """
    missing = unclassified_tools(tools)
    if missing:  # fail closed: a new tool must be mapped, denied or declared read-only first
        raise ValueError(f"tools not in TOOL_TO_ACTION, KNOWN_UNMAPPED or READ_TOOLS: {missing}")
    def record(tool: str, kind: str, detail: str = "") -> None:
        if outcome is not None:
            outcome.record(tool, kind, detail)

    budget = {"used": 0}  # per guard_tools call: not shared across calls, lost on restart

    def wrap(tool: StructuredTool, real: Any) -> Any:
        name, schema = tool.name, tool.args_schema

        def over_budget() -> bool:
            return budget["used"] >= max_mutations

        async def gated(**kwargs: Any) -> Any:
            d = await guard(tool_name=name, tool_args=kwargs, run_id=run_id, tenant_id=tenant_id,
                            user_id=user_id, permissions=permissions, store=store,
                            describer=describer, retry_backoff=retry_backoff)
            if d.action is not Action.EXECUTE:  # SKIP/BLOCK/HOLD never touch the budget
                skipped = "rejected" if d.draft and d.draft.status == "rejected" else "duplicate"
                record(name, {Action.HOLD: "held", Action.BLOCK: "blocked",
                              Action.UNAVAILABLE: "unavailable",
                              Action.MISCONFIGURED: "misconfigured"}.get(d.action, skipped),
                       d.reason)
                return d.reason
            if over_budget():
                record(name, "blocked", "budget exceeded")
                return (f"BUDGET EXCEEDED: this run already made {max_mutations} changes. "
                        "Stop making changes and report progress.")
            if d.draft is not None:  # approved: run the stored call, not the arguments just sent
                status, payload = await execute_draft(d.draft, store, real, schema)
                if status == "skipped":  # lost the claim: no side effect, no budget
                    record(name, "duplicate", f"claim lost ({payload})")
                    if payload == "posted":
                        return "ALREADY DONE: this approved action was already carried out."
                    return "ALREADY IN PROGRESS: this approved action was already claimed."
                budget["used"] += 1
                record(name, "executed" if status == "posted" else "failed")
                if status == "posted":
                    return payload
                return f"FAILED: the approved action did not complete ({payload}). Do not retry."
            # Autonomous: claim a ledger row first, so a crash or a concurrent twin never re-runs it.
            row = store.begin_autonomous(run_id, name, kwargs, TOOL_TO_ACTION[name][1])
            if row is None:  # lost the race: no side effect, no budget
                record(name, "duplicate", "ledger entry exists")
                return "ALREADY IN PROGRESS OR DONE: this exact action already has a ledger entry."
            budget["used"] += 1
            try:
                result = await real(**kwargs)
            except Exception as exc:
                store.mark_failed(row.draft_id, f"{type(exc).__name__}: {exc}")
                record(name, "failed", f"{type(exc).__name__}: {exc}")
                raise
            store.mark_posted(row.draft_id, result or {})
            record(name, "executed")
            return result

        gated._wrapped = real  # type: ignore[attr-defined]
        return gated

    out = []
    for tool in tools:
        if tool.name not in MUTATING_TOOLS:
            out.append(tool)
            continue
        real = getattr(tool.coroutine, "_wrapped", tool.coroutine)
        if real is None:
            raise ValueError(f"write tool '{tool.name}' has no coroutine to gate")
        # func=None: otherwise tool.invoke() on the copy would run the ungated sync path.
        out.append(tool.model_copy(update={"coroutine": wrap(tool, real), "func": None}))
    return out
