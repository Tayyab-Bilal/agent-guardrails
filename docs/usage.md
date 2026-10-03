# Usage guide

A fuller walk through the public API. Every `python` block here is executed by
`tests/test_docs.py`. Blocks in this file share one namespace, in order.

## 1. Wire up a run

Build the pieces once per run: a permission source, a draft store, a describer, and optionally
a `RunOutcome` that records what happened to each call.

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

backend = AcmeBackend()
backend.tasks = {"t1": {"title": "Draft spec", "status": "open"}}
store = DraftStore(hmac_key=b"keep-this-in-a-secrets-manager")  # ":memory:" by default
sink, outcome = ListSink(), RunOutcome()
runs = RunManager(store, sink)

tools = {t.name: t for t in guard_tools(
    build_tools(backend), run_id="run-1", tenant_id="acme", user_id="sam",
    permissions=StaticPermissionSource(CONFIG), store=store,
    describer=DeterministicDescriber(), outcome=outcome)}
runs.start("run-1")
```

`guard_tools` returns new tool objects. Hand those to your agent. The originals are untouched,
so the same list can be guarded for many runs.

## 2. Let the agent call tools

```python
async def agent_turn():
    created = await tools["create_task"].ainvoke({"title": "Q4 review"})      # Autonomous
    held = await tools["update_task"].ainvoke({"task_id": "t1", "status": "done"})  # Suggest
    denied = await tools["delete_task"].ainvoke({"task_id": "t1"})              # unmapped
    return created, held, denied

created, held, denied = asyncio.run(agent_turn())
assert created == {"task_id": "t2"}
assert held.startswith("AWAITING APPROVAL")
assert denied.startswith("NOT ALLOWED")
```

The strings are written for the model. They say what happened and what not to do next.

## 3. Park the run and approve

`finish` looks at the recorded steps and the pending drafts and sets the run state. A run with
pending drafts becomes `awaiting_approval` and emits one `run.awaiting_approval` event.

```python
assert runs.finish("run-1", outcome) == "awaiting_approval"
event = sink.events[0]
assert event["type"] == "run.awaiting_approval"
(draft,) = event["data"]["drafts"]
print(draft["description"])  # Update Task for ...: status='done'

async def human_answers():
    return await runs.decide("run-1", {draft["draft_id"]: "approved"}, tools)

result = asyncio.run(human_answers())
assert result["state"] == "running" and len(result["posted"]) == 1
assert backend.tasks["t1"]["status"] == "done"
```

`decide` must be given a decision for every pending draft. Approve and reject can be mixed. A
second call raises `ConflictError` (`status_code == 409`):

```python
from agent_guardrails import ConflictError

try:
    asyncio.run(runs.decide("run-1", {draft["draft_id"]: "approved"}, tools))
except ConflictError as exc:
    assert exc.status_code == 409
```

## 4. Readable approvals

Give the describer an async `lookup(kind, id)`. It resolves names for ids anywhere in the
arguments, including inside a bulk list. At most 12 lookups run, under one 6 second deadline.

```python
async def lookup(kind, id_):
    return {"t1": "Draft spec", "t2": "Q4 review"}.get(id_)

describer = DeterministicDescriber(lookup)
text = asyncio.run(describer.describe(
    "bulk_update_tasks",
    {"updates": [{"task_id": "t1", "status": "done"}, {"task_id": "t2", "status": "done"}]},
    "Update Task"))
assert text == "Update Task for 'Draft spec', 'Q4 review': updates=<2 items>"
```

To let an LLM write the sentence, pass any object with `async complete(prompt) -> str`. It only
sees id-stripped arguments and the names, and any failure falls back to the deterministic text.

```python
from agent_guardrails import LLMDescriber


class ScriptedLLM:
    async def complete(self, prompt: str) -> str:
        return "Mark the Draft spec task as done"


llm_text = asyncio.run(LLMDescriber(ScriptedLLM(), lookup).describe(
    "update_task", {"task_id": "t1", "status": "done"}, "Update Task"))
assert llm_text == "Mark the Draft spec task as done"
```

## 5. Permission sources and failure verdicts

A source is any object with `async fetch(tenant_id, user_id) -> dict`. Three are included.
`HttpPermissionSource` takes an async `get_json(url, headers)` you provide:

```python
from agent_guardrails import HttpPermissionSource


async def get_json(url, headers):  # wrap httpx, aiohttp, ... here
    return CONFIG


source = HttpPermissionSource("https://permissions.example/{tenant_id}/{user_id}", get_json,
                              headers={"authorization": "Bearer ..."})
assert asyncio.run(source.fetch("acme", "sam")) == CONFIG
```

When the fetch fails, the gate does not guess:

| Cause | Verdict | What the model sees |
|---|---|---|
| timeout, connection error, 5xx, 429 | retried twice, then `UNAVAILABLE` | `SYSTEM UNAVAILABLE: ... the run is parked as resumable` |
| 4xx, malformed payload, bad file | `MISCONFIGURED`, never retried | `MISCONFIGURED: <real cause>` |

Neither is ever reported as "disabled". `RunOutcome.status` then gives `parked` (resumable)
or `failed` (terminal).

```python
class Down:
    async def fetch(self, tenant_id, user_id):
        raise TimeoutError("timed out")


down_tools = {t.name: t for t in guard_tools(
    build_tools(backend), run_id="run-2", tenant_id="acme", user_id="sam",
    permissions=Down(), store=store, describer=DeterministicDescriber(),
    retry_backoff=0)}  # no sleeping in this example
out = asyncio.run(down_tools["create_task"].ainvoke({"title": "x"}))
assert out.startswith("SYSTEM UNAVAILABLE")
```

## 6. Run outcomes

`RunOutcome` is filled by the gate, not by the model. Blocked and rejected calls are skipped
steps (policy outcomes). A run where every write was blocked is `failed`.

```python
blocked_only = RunOutcome()
blocked_tools = {t.name: t for t in guard_tools(
    build_tools(backend), run_id="run-3", tenant_id="acme", user_id="sam",
    permissions=StaticPermissionSource({"categories": []}), store=store,
    describer=DeterministicDescriber(), outcome=blocked_only)}
asyncio.run(blocked_tools["create_task"].ainvoke({"title": "x"}))
assert blocked_only.steps[0].skipped and blocked_only.status == "failed"
```

## 7. Failed autonomous writes are retryable

If an autonomous write raises, its ledger row ends `failed`. Calling the same action again
claims that row back (an atomic `failed -> executing` update) and tries again. A row stuck in
`executing` (a crash) is never re-run; `store.list_stuck()` lists them for a human.

## 8. Lower level pieces

`guard(...)` is the pure decision function behind the gate. `replay_approved(run_id, tools,
store)` executes approved drafts from their stored arguments. Both are in
[api.md](api.md).
