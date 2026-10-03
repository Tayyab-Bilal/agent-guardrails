# API reference

Everything below is importable from `agent_guardrails` unless a module is named.

## Wiring

### `guard_tools(tools, *, run_id, tenant_id, user_id, permissions, store, describer, max_mutations=150, outcome=None, retry_backoff=0.5) -> list[StructuredTool]`
Returns new tools whose mutating coroutines go through the gate. Read tools are returned as-is.
Raises `ValueError` for any tool that is not mapped, explicitly denied, or listed as read-only.
`outcome` (a `RunOutcome`) records every call. `retry_backoff` is the base delay, in seconds, for
the bounded retry of transient permission-fetch errors (two retries, doubling).

### `guard(*, tool_name, tool_args, run_id, tenant_id, user_id, permissions, store, describer, retry_backoff=0.5) -> Decision`
The pure decision for one proposed call. An existing draft wins over today's mode.

### `Decision(action, reason, mode=None, draft=None)` and `Action`
`Action` is `EXECUTE | HOLD | SKIP | BLOCK | UNAVAILABLE | MISCONFIGURED`. `reason` is the text
shown to the model.

### `Mode`
`AUTONOMOUS | SUGGEST | DISABLED`.

## Drafts and the ledger

### `DraftStore(path=":memory:", *, hmac_key: bytes)`
SQLite-backed. Every state change is a conditional `UPDATE`.

| Method | Purpose |
|---|---|
| `create_pending(run_id, tool, args, description) -> Draft` | insert or return the existing draft for this exact action |
| `get_by_key(run_id, key)`, `list_for_run(run_id)`, `list_pending(run_id)` | reads |
| `list_stuck(run_id=None)` | rows left `executing` after a crash |
| `apply_decisions(run_id, {draft_id: "approved" \| "rejected"}) -> int` | only pending rows change |
| `claim(draft_id) -> bool` | atomic `approved -> executing` |
| `mark_posted(draft_id, result)`, `mark_failed(draft_id, reason)` | finish a claimed row |
| `begin_autonomous(run_id, tool, args, description) -> Draft \| None` | claim an autonomous write first; re-claims a `failed` row; `None` if done, in flight or crashed |
| `verify(draft) -> bool` | HMAC check of the stored arguments |
| `begin_run(run_id)`, `run_state(run_id)`, `transition_run(run_id, frm, to) -> bool` | run state, used by `RunManager` |

### `Draft`
Frozen dataclass: `draft_id, run_id, idempotency_key, tool_name, arguments_json, arguments_hash,
description, mode, status, result_json, created_at, updated_at`, plus `.arguments`.

### `replay_approved(run_id, tools_by_name, store) -> {"posted": [...], "failed": [...]}`
Runs every approved draft from its stored arguments. Safe to call twice or at once.

## Runs and approvals

### `RunManager(store, sink)`
| Method | Purpose |
|---|---|
| `start(run_id)` | register the run as `running` |
| `state(run_id) -> str \| None` | current state |
| `finish(run_id, outcome=None) -> str` | set `awaiting_approval` (emits the event once), `completed`, `failed` or `parked` |
| `async decide(run_id, decisions, tools_by_name) -> dict` | settle all pending drafts in one call, then replay |

States: `running`, `awaiting_approval`, `resuming` (the single-flight claim), `completed`,
`failed`, `parked`. `decide` raises `ConflictError` if the run is not awaiting approval and
`ValueError` if `decisions` does not cover exactly the pending drafts (the claim is released).

### `ConflictError`
Has `status_code = 409`.

### `EventSink` and `ListSink`
`EventSink` is a protocol with `emit(event: dict) -> None`. `ListSink` keeps events in
`.events`. The event is CloudEvents-shaped: `specversion`, `id`, `source`, `type`
(`run.awaiting_approval`), `time`, `datacontenttype`, and `data = {"run_id", "drafts": [{"draft_id",
"tool", "description"}]}`.

### `RunOutcome`
`record(tool, kind, detail="")`, `.steps` (each `Step(tool, kind, detail)` with `.skipped`), and
`.status` (`completed | failed | parked`). Kinds: `executed, failed, held, blocked, rejected,
duplicate, unavailable, misconfigured`.

## Describing approvals

### `DeterministicDescriber(lookup=None)` and `LLMDescriber(llm, lookup=None)`
`async describe(tool, args, action) -> str`. `lookup` is `async (kind, id) -> str | None`.
`llm` is any object with `async complete(prompt) -> str`.

### `resolve_names(args, lookup, *, max_lookups=12, timeout=6.0) -> dict[(kind, id), name]`
Module `agent_guardrails.resolve`. Also exports `collect_ids(args)` and `NameLookup`.

### `strip_ids(value)`
Module `agent_guardrails.describe`. Removes id-like keys and UUID-like values, recursively.

## Permission sources

A source is any object with `async fetch(tenant_id, user_id) -> dict`.

| Class | Notes |
|---|---|
| `StaticPermissionSource(config)` | in memory; mutate `.config` in tests |
| `FilePermissionSource(path)` | reads the file on every fetch |
| `HttpPermissionSource(url, get_json, headers=None)` | `get_json` is an async `(url, headers) -> dict`; `url` may contain `{tenant_id}` and `{user_id}` |

`PermissionsUnavailable` (transient) and `PermissionsMisconfigured` (terminal) both carry
`.cause`. They come from `load_permissions` in `agent_guardrails.permissions`.

## Modules

`agent_guardrails.resolver` holds `TOOL_TO_ACTION`, `KNOWN_UNMAPPED`, `READ_TOOLS`,
`mode_for` and `normalize_permissions`. `agent_guardrails.failure` holds `classify` and
`run_with_retry`. `agent_guardrails.idempotency` holds `idempotency_key`, `args_hash` and
`normalize_args`.

```python
from agent_guardrails.failure import Failure, classify

assert classify(TimeoutError("slow")) is Failure.TRANSIENT
assert classify(ValueError("422 Unprocessable")) is Failure.TERMINAL
```
