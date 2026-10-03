# agent-guardrails

Deterministic permissions and human-in-the-loop approval for LLM agents: the model proposes, code decides.

> A clean-room re-implementation of work I designed and built for a production
> multi-tenant AI workspace platform. No employer code; all names and data are fictional.

[![CI](https://github.com/Tayyab-Bilal/agent-guardrails/actions/workflows/ci.yml/badge.svg)](https://github.com/Tayyab-Bilal/agent-guardrails/actions/workflows/ci.yml)

## The problem

An autonomous agent runs at 3 a.m. with nobody watching. If the model calls `delete_project`,
it happens on real data. Organisations need a per-action choice, **do it / ask me first /
never**, that holds even when the model misbehaves: prompt injection, invented tools, retried
runs.

Approvals are asynchronous. A human answers hours later, possibly in another process, with many
drafts outstanding, and exactly the approved action must then run **exactly once**. The setting
here is a fictional "Acme Workspace" app (tasks, projects, meetings).

## What it does

- **Three modes per action** (Autonomous, Suggest Only, Disabled) read fresh from tenant config on
  every gated call. `resolver.py`, `permissions.py`
- **Pure, deny-by-default resolver.** A static map ties each write tool to one action; tools
  denied on purpose are listed; anything else is refused. A test and a wiring check fail on a new
  unclassified tool. `resolver.py`, `gate.py`
- **One choke point.** `guard_tools` returns gated copies of the tools, keeping name and schema.
  `gate.py`
- **A decision engine** where an existing draft for the exact action wins over today's mode.
  `guardian.py`
- **Per-action drafts** unique on (run, idempotency key), with argument normalisation and an
  HMAC over the stored arguments. `drafts.py`, `idempotency.py`
- **Failure verdicts.** A permissions outage is retried and then parked as resumable; a bad
  config is terminal with its real cause. Neither is ever reported as "disabled".
  `failure.py`, `guardian.py`
- **Run outcomes.** Blocked and rejected calls are recorded as skipped steps, and a run where
  every write was blocked ends `failed`. `outcome.py`
- **Approve and resume.** A run with drafts parks as `awaiting_approval`, emits a
  CloudEvents-shaped event, and one `decide` call settles every draft behind an atomic
  single-flight claim (a second call gets 409). `runs.py`
- **Human-readable approvals.** Bounded id-to-name lookups (12 at most, 6 s total, including
  nested bulk arguments), then a sentence from a deterministic or LLM describer, with ids stripped
  recursively. `resolve.py`, `describe.py`
- **Deterministic replay** of approved drafts from stored arguments, exactly once. `replay.py`
- **Exactly-once autonomous writes** through a ledger row claimed before the call; a failed write
  stays retryable, a crashed one is never re-run. `drafts.py`, `gate.py`
- **A swappable permission backend**: static, file, or HTTP through an injected `get_json`.
  `permissions.py`

## Quickstart

```bash
uv venv .venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/python examples/demo.py
```

Excerpt of the demo output (an agent runs unattended, then a human answers):

```text
--- the agent runs unattended ---
1. create_task (Autonomous)        {'task_id': 't6'}
2. update_task (Suggest)           AWAITING APPROVAL: "Update Task for 'Draft spec': status='done'" was sent to ...
3. bulk update, 4 tasks (Suggest)  AWAITING APPROVAL: "Update Task for 'Review budget', 'Book venue', 'Send invites' and 1 more: updates=<4 items>" ...
4. delete_project                  NOT ALLOWED: 'delete_project' has no permission mapping. This is a policy outcome, ...
6. run finished as                 awaiting_approval

--- hours later, a human answers with ONE call ---
7. decide (reject / approve)       {'state': 'running', 'approved': 1, 'rejected': 1, 'posted': 1, 'failed': 0}
8. a second decision               409 Conflict

--- the permissions service goes down ---
10. create_task                    SYSTEM UNAVAILABLE: the permissions service could not be reached (...). Nothing was done. ...
```

## Usage guide

A complete, runnable run (this block is executed by the test suite). More in
[docs/usage.md](docs/usage.md); the signatures are in [docs/api.md](docs/api.md).

```python
import asyncio

from agent_guardrails import (
    DeterministicDescriber, DraftStore, ListSink, RunManager, RunOutcome,
    StaticPermissionSource, guard_tools,
)
from agent_guardrails.acme_tools import AcmeBackend, build_tools

CONFIG = {"categories": [
    {"name": "Tasks", "actions": [{"name": "Create Task", "mode": "Autonomous"},
                                  {"name": "Update Task", "mode": "Suggest Only"}]},
]}


async def main() -> None:
    backend = AcmeBackend()
    backend.tasks = {"t1": {"title": "Draft spec", "status": "open"}}

    async def lookup(kind, id_):  # id -> name, so the approver never sees an id
        return backend.tasks.get(id_, {}).get("title")

    store, sink, outcome = DraftStore(hmac_key=b"a-real-secret"), ListSink(), RunOutcome()
    runs = RunManager(store, sink)
    tools = {t.name: t for t in guard_tools(
        build_tools(backend), run_id="run-1", tenant_id="acme", user_id="sam",
        permissions=StaticPermissionSource(CONFIG), store=store,
        describer=DeterministicDescriber(lookup), outcome=outcome)}
    runs.start("run-1")

    await tools["create_task"].ainvoke({"title": "Q4 review"})                  # runs
    held = await tools["update_task"].ainvoke({"task_id": "t1", "status": "done"})  # drafted
    denied = await tools["delete_task"].ainvoke({"task_id": "t1"})              # refused
    assert held.startswith("AWAITING APPROVAL") and denied.startswith("NOT ALLOWED")

    assert runs.finish("run-1", outcome) == "awaiting_approval"
    (draft,) = sink.events[0]["data"]["drafts"]
    assert draft["description"] == "Update Task for 'Draft spec': status='done'"

    # Later, possibly in another process: one call settles every draft, then replays.
    result = await runs.decide("run-1", {draft["draft_id"]: "approved"}, tools)
    assert len(result["posted"]) == 1 and backend.tasks["t1"]["status"] == "done"


asyncio.run(main())
```

All LLMs sit behind a tiny `TextLLM` protocol, and the tests use fakes. No network, no API keys.

## How it works

```mermaid
sequenceDiagram
    participant A as Agent (LLM)
    participant G as gate (guard_tools)
    participant U as guard()
    participant S as DraftStore (SQLite)
    participant R as RunManager
    participant H as Human
    participant T as Real tool

    A->>G: update_task(task_id, status)
    G->>U: tool, args, run, tenant
    U->>S: existing draft for this exact action?
    alt Autonomous
        U-->>G: EXECUTE
        G->>S: ledger row "executing" (unique key; loser stops)
        G->>T: run
        G->>S: mark posted / failed (failed can be claimed again)
    else Suggest Only
        U->>S: create pending draft (unique key)
        U-->>G: HOLD
        G-->>A: "AWAITING APPROVAL ... do not retry"
        R->>S: run running -> awaiting_approval, emit event
        H->>R: decide(run, all decisions)
        R->>S: claim run (conditional UPDATE) or 409
        R->>S: apply decisions, replay approved
        R->>T: run stored arguments (signature checked)
    else Disabled or unmapped
        U-->>G: BLOCK
        G-->>A: "NOT ALLOWED ... policy outcome, not an error"
    else Permissions unreachable or invalid
        U-->>G: UNAVAILABLE (retried, then park) or MISCONFIGURED (terminal)
        G-->>A: "SYSTEM UNAVAILABLE ..." or "MISCONFIGURED: cause"
    end
```

The flow for one gated call:

1. The agent calls a guarded tool. The gate runs `guard()` before any side effect.
2. `guard()` looks for a draft for this exact action (run + tool + canonical arguments). If one
   exists it wins: approved executes, rejected or posted is skipped, pending holds.
3. With no draft, policy decides. Unmapped tools are blocked before any network call. Otherwise
   permissions are fetched fresh. A transient failure is retried twice, then reported as
   `SYSTEM UNAVAILABLE`. A bad payload is `MISCONFIGURED`.
4. Autonomous calls claim a ledger row and then run. Suggest Only calls create a draft, with a
   sentence written from resolved names. Disabled calls are refused.
5. Every outcome is recorded on the `RunOutcome`. When the agent stops, `RunManager.finish`
   sets the run state. Pending drafts park the run and emit the event.
6. `decide` claims the run, settles all drafts, and replays the approved ones from their stored
   arguments. Each draft is itself claimed with a conditional `UPDATE`.

## Design decisions

1. **Model proposes, code decides.** Allow or deny is a pure function of a static map and tenant
   config. The LLM only proposes calls and may write a sentence. *Rejected:* asking the model to
   judge its own call, which prompt injection can talk around.
2. **Deny by default.** Unknown, unmapped or malformed means `DISABLED`. *Rejected:* mapping
   `delete_task` to the nearest allowed action ("Update Task"), because a tenant who allowed edits
   never agreed to deletion.
3. **Gate before side effects, at one choke point.** Every write tool is wrapped at one
   registration site. Here the wrapper returns copies, so only code holding the guarded list is
   gated; the originals are not. *Rejected:* checking inside each tool, which one new tool forgets.
4. **Policy on the action type, approval on the concrete instance.** The key hashes run, tool and
   canonical arguments, so one approval cannot be replayed against a different target.
   *Rejected:* approving "update_task" in general.
5. **Approved means these exact arguments.** The stored call runs, never a fresh LLM
   regeneration, and an HMAC over the stored text is verified first. *Rejected:* re-asking the
   model to redo the call after approval.
6. **Exactly once.** An idempotency key per action plus an atomic claim before every side effect.
   A crashed `executing` row is never re-run, since the effect may have happened; a cleanly
   `failed` autonomous row may be claimed again. *Rejected:* retrying anything not marked done.
7. **Fail closed, and never present an infrastructure failure as a policy decision.** An outage
   is `SYSTEM UNAVAILABLE` (park, resumable) and a bad config is `MISCONFIGURED: <cause>`.
   *Rejected:* treating a failed fetch as "Disabled", which sent users to check settings that
   were fine.
8. **Data is data.** Approval text has no raw ids and tool arguments cannot change the gate.
   Attacker-shaped text in a field is just text. *Rejected:* letting argument content select a mode.
9. **Policy outcomes are not failures, but all-blocked is.** Blocked and rejected steps are
   skipped steps. A run where every write was blocked finalises `failed` so it never reads as
   success. *Rejected:* relying on the model to report what was refused.
10. **One decision call, one atomic claim.** `decide` settles all drafts at once behind a
    conditional `UPDATE` on the run row; the loser gets 409. *Rejected:* per-draft decisions,
    which leave a run half-resumed and race when two reviewers click.
11. **Soft-yield instead of `interrupt()`.** Durable drafts are reconciled when the agent is
    re-invoked, because resume is driven from outside and fans out across many drafts. A later
    `interrupt()` spike confirmed it: nested agents without checkpointers never propagated the
    interrupt. *Rejected:* LangGraph `interrupt()`.

## In production

These are results of the **original system**, not something this repo measured.

- Gated every write tool of a multi-agent codebase: 31 mutating tools, 12 mapped to a permission
  action and 19 explicitly denied. The permission catalogue had 15 categories and 73 actions.
- Verified end to end on a real dev tenant: 39 of 40 assertions. Invented tools were denied, all
  destructive tools were disabled, an empty config denied everything, a rejected draft changed
  nothing, and a mixed approve/reject run executed only the approved action.
- Approval flow E2E 18/18. Suggest Only, approve, resume, replay verified by read-back 3/3.
- In QA the system refused a prompt-injection attempt to raise its own autonomy.
- A report of 13 "identical" approvals turned out to be 13 distinct actions. After four fixes
  they collapsed into one approval with a named target.
- The coverage test did its job: when other teams added write tools later, it made them
  register those tools, and the new tools failed closed.
- About 2k lines across 12 modules. Delivered dark in five phases (drafts and resolver, decision
  engine, live on writes, pause/resume/replay, calendar and LLM-written descriptions), building
  against a simulated backend until the real endpoints existed. Only the autonomous routes used
  the gate; interactive chat was unchanged.

What this repo simplifies:

- SQLite and one locked connection instead of Postgres. The statements are the same;
  Postgres adds row locks (`SELECT ... FOR UPDATE`) for stale step state.
- A static permission source, a file, or an injected `get_json` instead of the real backend.
- A scripted fake instead of a provider for the sentence, and a `ListSink` instead of a
  CloudEvents webhook.
- 4 mapped and 3 denied tools in the Acme catalogue, not 31.
- The original compared a plain arguments hash; this repo signs with an HMAC. The per-run mutation
  budget is also a repo addition.
- The approval-loop backstop (an "already executed" marker and replay-drift guard) is not here.

## Testing

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```

No network and no LLM. The suite runs in a couple of seconds.

| Rule | Test file / examples |
|---|---|
| Unmapped, invented and unclassified tools are denied; coverage guardrail | `test_resolver.py` |
| Autonomous ledger: exactly once, resume is a no-op, crash never re-run | `test_guardian.py` |
| Failed autonomous write is retryable, atomically, only if still Autonomous | `test_failed_autonomous_write_stays_retryable`, `test_failed_reclaim_is_atomic_only_one_retry_wins` |
| Existing draft beats a mode change; rejected never executes | `test_guardian.py` |
| Attacker text in arguments cannot change the gate | `test_attacker_text_in_args_cannot_raise_autonomy` |
| Unavailable is retried then parked, misconfigured is terminal, neither says "disabled" | `test_permissions.py` |
| HTTP source classifies errors through `classify` | `test_http_source_classifies_errors_through_the_classifier` |
| Blocked/rejected are skipped; all-blocked run fails | `test_outcome.py` |
| Run parks with one event; one decision settles all; concurrent decide gives one winner and 409s | `test_runs.py` |
| Bounded lookups: 12 max, timeout, nested bulk args, 13 targets read as one named approval | `test_describe.py` |
| Replay uses stored arguments, signature checked, concurrent replays run once | `test_replay.py` |
| Gate keeps schema, never mutates input, sync path cannot bypass | `test_gate.py` |
| Every README and docs example runs | `test_docs.py` |

## Limits & known trade-offs

- **Same-name targets are not handled.** In the original, a write to one of several same-named
  items was refused in code unless the plan named the id. That part was built and in review when
  this was written, and it is not in this repo.
- **Not a sandbox.** Only the copies `guard_tools` returns are gated. A caller holding the
  original tool list can bypass it.
- **No state-injection test.** The original kept LangGraph state injection working; this repo
  keeps schemas but has no LangGraph `ToolNode` test.
- **A crashed `executing` row needs a human.** `store.list_stuck()` lists them. A parked
  (`parked`) run has no built-in resume trigger.
- **Identical calls are deduplicated by design.** The same tool with the same normalised
  arguments in one run is one action. Zero-padded numeric strings normalise to ints.
- **Mutation budget** (default 150) is per `guard_tools` call and in memory. Only calls that
  actually run consume it.
- **The HMAC key** must be rotated only when no approved drafts are outstanding. It does not
  protect against someone who holds the key.
- **Single process locking.** One SQLite connection behind a lock; use Postgres for many workers.
- **Approval text** for bulk calls shows names and a count, not every item.

## Project layout

```text
src/agent_guardrails/
  modes.py        the three modes
  resolver.py     tool -> action map, KNOWN_UNMAPPED, READ_TOOLS, config -> Mode
  permissions.py  sources (static, file, HTTP), Unavailable vs Misconfigured, load_permissions
  failure.py      transient vs terminal classification, run_with_retry
  idempotency.py  argument normalisation, args hash, idempotency key
  drafts.py       SQLite drafts + ledger + run state, conditional-UPDATE state machine
  resolve.py      bounded async id -> name lookups
  describe.py     id-free approval sentences (deterministic or LLM)
  guardian.py     guard(): existing draft wins, then policy and failure verdicts
  gate.py         guard_tools(): the choke point, mutation budget, outcome recording
  outcome.py      RunOutcome: steps recorded by code, run status
  runs.py         RunManager, awaiting_approval event, decide() with single-flight claim
  replay.py       run approved drafts from stored arguments, exactly once
  acme_tools.py   fictional backend and tools for tests and the demo
examples/demo.py  end-to-end story with fakes
docs/             usage.md, api.md
tests/            one file per area, plus test_docs.py
```

MIT licensed.
