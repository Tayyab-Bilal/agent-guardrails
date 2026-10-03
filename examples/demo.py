"""End-to-end story with a fake backend and scripted tool calls. No LLM, no network."""

import asyncio

from agent_guardrails import (
    ConflictError,
    DeterministicDescriber,
    DraftStore,
    ListSink,
    RunManager,
    RunOutcome,
    StaticPermissionSource,
    guard,
    guard_tools,
    replay_approved,
)
from agent_guardrails.acme_tools import AcmeBackend, build_tools

CONFIG = {"categories": [
    {"name": "Tasks", "actions": [{"name": "Create Task", "mode": "Autonomous"},
                                  {"name": "Update Task", "mode": "Suggest Only"}]},
    # Tenant even "allows" this, but delete_project has no mapping, so it stays denied.
    {"name": "Projects", "actions": [{"name": "Delete Project", "mode": "Autonomous"}]},
]}


def say(step: str, text: object) -> None:
    print(f"{step:<34} {text}")


async def main() -> None:
    backend = AcmeBackend()
    backend.tasks = {"t1": {"title": "Draft spec", "status": "open"},
                     "t2": {"title": "Review budget", "status": "open"},
                     "t3": {"title": "Book venue", "status": "open"},
                     "t4": {"title": "Send invites", "status": "open"},
                     "t5": {"title": "Order catering", "status": "open"}}

    async def lookup(kind: str, id_: str) -> str | None:  # id -> human name for approval text
        return backend.tasks.get(id_, {}).get("title") if kind == "task" else None

    store, sink, outcome = DraftStore(hmac_key=b"demo-secret-from-your-secrets-manager"), ListSink(), RunOutcome()
    runs, run = RunManager(store, sink), "run-42"
    kw = dict(run_id=run, tenant_id="acme", user_id="sam", store=store,
              permissions=StaticPermissionSource(CONFIG), describer=DeterministicDescriber(lookup))
    tools = {t.name: t for t in guard_tools(build_tools(backend), outcome=outcome, **kw)}

    print("--- the agent runs unattended ---")
    runs.start(run)
    say("1. create_task (Autonomous)", await tools["create_task"].ainvoke({"title": "Q4 review"}))
    say("2. update_task (Suggest)",
        await tools["update_task"].ainvoke({"task_id": "t1", "status": "done"}))
    bulk = [{"task_id": f"t{i}", "status": "done"} for i in (2, 3, 4, 5)]
    say("3. bulk update, 4 tasks (Suggest)", await tools["bulk_update_tasks"].ainvoke({"updates": bulk}))
    say("4. delete_project", await tools["delete_project"].ainvoke({"project_id": "p1"}))
    invented = await guard(tool_name="wipe_workspace", tool_args={}, **kw)
    say("5. invented tool", f"{invented.action.value.upper()}: {invented.reason}")
    say("6. run finished as", runs.finish(run, outcome))
    say("   steps recorded", [(s.tool, s.kind) for s in outcome.steps])

    print("\n--- the approval UI is notified ---")
    event = sink.events[0]
    print(f"   {event['type']} ({event['specversion']}), {len(event['data']['drafts'])} drafts:")
    for d in event["data"]["drafts"]:
        print(f"   needs approval: {d['description']}")

    print("\n--- hours later, a human answers with ONE call ---")
    update, bulk_draft = store.list_pending(run)
    result = await runs.decide(run, {update.draft_id: "rejected", bulk_draft.draft_id: "approved"}, tools)
    say("7. decide (reject / approve)",
        {k: (len(v) if isinstance(v, list) else v) for k, v in result.items()})
    try:
        await runs.decide(run, {update.draft_id: "approved", bulk_draft.draft_id: "approved"}, tools)
    except ConflictError as exc:
        say("8. a second decision", f"{exc.status_code} Conflict")
    a, b = await asyncio.gather(replay_approved(run, tools, store), replay_approved(run, tools, store))
    say("9. two more replays at once", f"{a} {b}")

    print("\n--- the permissions service goes down ---")
    class Down:
        async def fetch(self, tenant_id: str, user_id: str) -> dict:
            raise TimeoutError("permissions service timed out")

    down = {t.name: t for t in guard_tools(build_tools(backend), **{**kw, "run_id": "run-43",
            "permissions": Down()}, retry_backoff=0)}
    say("10. create_task", await down["create_task"].ainvoke({"title": "x"}))

    print("\n--- final state ---")
    print("task statuses:", {k: v["status"] for k, v in backend.tasks.items()})
    print("backend writes:", len(backend.calls))
    print(f"{'tool':<18}{'mode':<12}{'status':<10}description")
    for d in store.list_for_run(run):
        print(f"{d.tool_name:<18}{d.mode:<12}{d.status:<10}{d.description}")


if __name__ == "__main__":
    asyncio.run(main())
