"""What happened to each gated call, recorded by code, never by the model.

A blocked or rejected call is a policy outcome, not a failure. It is recorded as a skipped
step. But a run in which every write was blocked did nothing useful and must not read as
success, so it finalises `failed`. That is the "blocked looked like success" bug this fixes.
"""

from dataclasses import dataclass, field

# kind -> is it a skipped step (a policy outcome, nothing was attempted)?
KINDS = {"executed": False, "failed": False, "held": False, "blocked": True, "rejected": True,
         "duplicate": True, "unavailable": False, "misconfigured": False}


@dataclass(frozen=True)
class Step:
    tool: str
    kind: str
    detail: str = ""

    @property
    def skipped(self) -> bool:
        return KINDS[self.kind]


@dataclass
class RunOutcome:
    steps: list[Step] = field(default_factory=list)

    def record(self, tool: str, kind: str, detail: str = "") -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown step kind {kind!r}")
        self.steps.append(Step(tool, kind, detail))

    @property
    def status(self) -> str:
        """parked (resumable) | failed | completed. Waiting for approval is decided from drafts."""
        kinds = {s.kind for s in self.steps}
        if "misconfigured" in kinds:
            return "failed"  # terminal, with a real cause
        if "unavailable" in kinds:
            return "parked"  # an outage, not a decision: resume later
        if self.steps and kinds == {"blocked"}:
            return "failed"
        return "completed"
