"""Run states and the approve/resume layer.

running -> awaiting_approval -> resuming -> running (or completed / failed / parked)

A run that holds drafts parks as `awaiting_approval` and emits one CloudEvents-shaped event so
the approval UI can tell a human. The human then answers with ONE `decide` call that settles
every draft (mixed approve/reject is fine). The claim on the run row is a conditional UPDATE, so
two decisions can never both replay the same drafts: the loser gets `ConflictError` (HTTP 409).
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from .drafts import DraftStore
from .outcome import RunOutcome
from .replay import replay_approved

EVENT_SOURCE = "acme-workspace/agent-guardrails"


class ConflictError(Exception):
    """A decision is already being (or has been) applied to this run. Map to HTTP 409."""

    status_code = 409


class EventSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


class ListSink:
    """Collects events in memory: for tests and the demo. Production posts a webhook."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def awaiting_approval_event(run_id: str, drafts: list[Any]) -> dict[str, Any]:
    return {
        "specversion": "1.0", "id": uuid.uuid4().hex, "source": EVENT_SOURCE,
        "type": "run.awaiting_approval", "time": datetime.now(UTC).isoformat(),
        "datacontenttype": "application/json",
        "data": {"run_id": run_id, "drafts": [
            {"draft_id": d.draft_id, "tool": d.tool_name, "description": d.description}
            for d in drafts]},
    }


class RunManager:
    def __init__(self, store: DraftStore, sink: EventSink):
        self.store, self.sink = store, sink

    def start(self, run_id: str) -> None:
        self.store.begin_run(run_id)

    def state(self, run_id: str) -> str | None:
        return self.store.run_state(run_id)

    def finish(self, run_id: str, outcome: RunOutcome | None = None) -> str:
        """Called when the agent stops. Decides the final state from the recorded steps and drafts.

        An outage or a misconfiguration wins over waiting: the run cannot be approved into
        health. Pending drafts park the run and emit the event exactly once.
        """
        outcome = outcome or RunOutcome()
        pending = self.store.list_pending(run_id)
        status = outcome.status
        if status == "completed" and pending:
            status = "awaiting_approval"
        if self.store.transition_run(run_id, "running", status) and status == "awaiting_approval":
            self.sink.emit(awaiting_approval_event(run_id, pending))
        return self.state(run_id) or status

    async def decide(self, run_id: str, decisions: dict[str, str],
                     tools_by_name: dict[str, Any]) -> dict[str, Any]:
        """Settle every pending draft in one call, then replay what was approved.

        `decisions` maps draft_id -> "approved" | "rejected" and must cover exactly the run's
        pending drafts. Raises ConflictError if the run is not awaiting approval (including a
        concurrent or repeated decision) and ValueError for a decision set that does not match.
        """
        if not self.store.transition_run(run_id, "awaiting_approval", "resuming"):
            raise ConflictError(f"run {run_id!r} is not awaiting approval (state: "
                                f"{self.state(run_id)!r}); a decision was already made")
        try:
            pending = {d.draft_id for d in self.store.list_pending(run_id)}
            if set(decisions) != pending:
                raise ValueError("decisions must cover exactly the pending drafts: "
                                 f"missing {sorted(pending - set(decisions))}, "
                                 f"unknown {sorted(set(decisions) - pending)}")
            if bad := {v for v in decisions.values() if v not in ("approved", "rejected")}:
                raise ValueError(f"bad decision values {sorted(map(repr, bad))}")  # before any write
            self.store.apply_decisions(run_id, decisions)
        except ValueError:
            self.store.transition_run(run_id, "resuming", "awaiting_approval")  # release the claim
            raise
        try:
            replayed = await replay_approved(run_id, tools_by_name, self.store)
        finally:
            self.store.transition_run(run_id, "resuming", "running")
        counts = list(decisions.values())
        return {"state": "running", "approved": counts.count("approved"),
                "rejected": counts.count("rejected"), **replayed}
