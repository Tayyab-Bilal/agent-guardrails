import asyncio

import pytest
from langchain_core.tools import StructuredTool

from agent_guardrails import DeterministicDescriber, DraftStore, guard_tools, replay_approved


async def _draft(h, task="A", status="done"):
    await h.call("update_task", task_id=task, status=status)
    return [d for d in h.store.list_for_run("run-1") if f"'{status}'" in d.description][-1]


async def test_mixed_approve_reject_executes_only_approved(h, backend, store):
    a = await _draft(h, "A", "done")
    b = await _draft(h, "B", "blocked")
    assert store.apply_decisions("run-1", {a.draft_id: "approved", b.draft_id: "rejected"}) == 2
    res = await replay_approved("run-1", h.by_name, store)
    assert [p["draft_id"] for p in res["posted"]] == [a.draft_id] and res["failed"] == []
    assert backend.tasks["A"]["status"] == "done" and backend.tasks["B"]["status"] == "open"
    assert len(backend.calls) == 1


async def test_replay_uses_stored_args_not_new_llm_args(h, backend, store):
    a = await _draft(h, "A", "done")
    store.apply_decisions("run-1", {a.draft_id: "approved"})
    # The model now proposes a different change to the same task: new key, new draft, held.
    assert (await h.call("update_task", task_id="A", status="deleted")).startswith("AWAITING")
    await replay_approved("run-1", h.by_name, store)
    assert backend.tasks["A"]["status"] == "done"
    assert backend.calls == [("update_task", {"task_id": "A", "status": "done"})]


async def test_edited_stored_args_without_key_refuses_to_run(h, backend, store):
    import hashlib
    import hmac as hm
    import json

    a = await _draft(h, "A", "done")
    store.apply_decisions("run-1", {a.draft_id: "approved"})
    evil = json.dumps({"status": "done", "task_id": "B"}, sort_keys=True)
    msg = f"{a.draft_id}\x00{a.tool_name}\x00{evil}".encode()
    forgeries = [
        hm.new(b"attacker-key", msg, "sha256").hexdigest(),  # real format, wrong key
        hashlib.sha256(msg).hexdigest(),  # real format, no key at all
    ]
    for forged in forgeries:
        store._write("UPDATE drafts SET arguments_json=?, arguments_hash=? WHERE draft_id=?",
                     (evil, forged, a.draft_id))
        assert not store.verify(store.get_by_key("run-1", a.idempotency_key))
    res = await replay_approved("run-1", h.by_name, store)
    assert res["posted"] == [] and "signature" in res["failed"][0]["reason"]
    assert backend.calls == [] and store.list_for_run("run-1")[0].status == "failed"


def test_signature_is_bound_to_the_draft_row(store):
    args = {"task_id": "A", "status": "x"}  # identical tool + args, different rows
    a = store.create_pending("r1", "update_task", args, "d")
    b = store.create_pending("r2", "update_task", args, "d")
    assert a.arguments_json == b.arguments_json and a.arguments_hash != b.arguments_hash
    store._write("UPDATE drafts SET arguments_hash=? WHERE draft_id=?", (a.arguments_hash, b.draft_id))
    assert store.verify(a) and not store.verify(store.get_by_key("r2", b.idempotency_key))


def test_edit_that_normalises_to_the_same_args_still_fails_verification(store):
    d = store.create_pending("r", "update_task", {"task_id": "007", "status": "x"}, "d")
    store._write("UPDATE drafts SET arguments_json=? WHERE draft_id=?",
                 ('{"status": "x", "task_id": "7"}', d.draft_id))
    assert not store.verify(store.get_by_key("r", d.idempotency_key))


def test_hmac_key_must_be_nonempty_bytes():
    for bad in (b"", "secret", None):
        with pytest.raises(ValueError):
            DraftStore(hmac_key=bad)


def test_list_stuck_returns_executing_rows(store):
    d = store.create_pending("r", "update_task", {"task_id": "A", "status": "x"}, "d")
    store.apply_decisions("r", {d.draft_id: "approved"})
    assert store.list_stuck() == []
    store.claim(d.draft_id)
    assert [x.draft_id for x in store.list_stuck("r")] == [d.draft_id]
    assert store.list_stuck("other") == [] and len(store.list_stuck()) == 1


async def test_nested_model_argument_survives_suggest_approve_replay(backend, store, source):
    from pydantic import BaseModel

    class Slot(BaseModel):
        room: str
        minutes: int

    got = []

    async def schedule_meeting(title: str, slot: Slot) -> dict:
        got.append(slot)
        return {"ok": True}

    tool = StructuredTool.from_function(coroutine=schedule_meeting, name="schedule_meeting",
                                        description="x")
    (g,) = guard_tools([tool], run_id="r", tenant_id="t", user_id="u", permissions=source,
                       store=store, describer=DeterministicDescriber())
    assert (await g.ainvoke({"title": "Sync", "slot": {"room": "A", "minutes": 30}})
            ).startswith("AWAITING")
    (d,) = store.list_for_run("r")
    assert d.arguments["slot"] == {"room": "A", "minutes": 30}  # real JSON, not a repr string
    store.apply_decisions("r", {d.draft_id: "approved"})
    res = await replay_approved("r", {"schedule_meeting": g}, store)
    assert len(res["posted"]) == 1 and got == [Slot(room="A", minutes=30)]


async def test_numeric_string_id_survives_approve_then_replay(h, backend, store):
    backend.tasks["42"] = {"title": "t", "status": "open"}
    await h.call("update_task", task_id="42", status="done")
    (d,) = store.list_for_run("run-1")
    assert d.arguments["task_id"] == "42"  # original, not coerced to 42
    store.apply_decisions("run-1", {d.draft_id: "approved"})
    res = await replay_approved("run-1", h.by_name, store)
    assert len(res["posted"]) == 1 and backend.tasks["42"]["status"] == "done"


async def test_stored_args_are_revalidated_against_tool_schema(h, backend, store):
    a = await _draft(h, "A", "done")
    store.apply_decisions("run-1", {a.draft_id: "approved"})
    bad = '{"task_id": "A"}'  # schema requires status
    d = store.get_by_key("run-1", a.idempotency_key)
    store._write("UPDATE drafts SET arguments_json=? WHERE draft_id=?", (bad, d.draft_id))
    store._write("UPDATE drafts SET arguments_hash=? WHERE draft_id=?",
                 (store._sign(d.draft_id, d.tool_name, bad), d.draft_id))
    res = await replay_approved("run-1", h.by_name, store)
    assert res["posted"] == [] and "ValidationError" in res["failed"][0]["reason"]
    assert backend.calls == []


async def test_sync_only_tool_gets_accurate_message_at_replay(h, backend, store):
    a = await _draft(h, "A", "done")
    store.apply_decisions("run-1", {a.draft_id: "approved"})

    class SyncOnly:
        coroutine = None
        args_schema = None

    res = await replay_approved("run-1", {"update_task": SyncOnly()}, store)
    assert "no async implementation" in res["failed"][0]["reason"]


async def test_gate_reports_already_done_when_a_replay_won_the_race(h, backend, store, monkeypatch):
    a = await _draft(h, "A", "done")
    store.apply_decisions("run-1", {a.draft_id: "approved"})
    real_claim = store.claim

    def other_worker_wins(draft_id):  # someone else takes the claim and finishes first
        real_claim(draft_id)
        store.mark_posted(draft_id, {})
        return False

    monkeypatch.setattr(store, "claim", other_worker_wins)
    out = await h.call("update_task", task_id="A", status="done")
    assert out.startswith("ALREADY DONE") and backend.calls == []


def test_finish_only_applies_to_executing_rows(store):
    d = store.create_pending("r", "update_task", {"task_id": "A", "status": "x"}, "d")
    store.mark_posted(d.draft_id, {"ok": 1})
    store.mark_failed(d.draft_id, "nope")
    assert store.get_by_key("r", d.idempotency_key).status == "pending"  # not 'posted' / 'failed'
    store.apply_decisions("r", {d.draft_id: "approved"})
    store.mark_posted(d.draft_id, {"ok": 1})
    assert store.get_by_key("r", d.idempotency_key).status == "approved"


async def test_concurrent_replays_execute_exactly_once(h, backend, store, monkeypatch):
    a = await _draft(h, "A", "done")
    store.apply_decisions("run-1", {a.draft_id: "approved"})
    backend.delay = 0.02
    snapshot = store.list_for_run("run-1")  # both replays read 'approved' before either has claimed
    monkeypatch.setattr(store, "list_for_run", lambda run_id: snapshot)
    r1, r2 = await asyncio.gather(*(replay_approved("run-1", h.by_name, store) for _ in range(2)))
    assert len(r1["posted"]) + len(r2["posted"]) == 1
    assert len(backend.calls) == 1


async def test_second_replay_changes_nothing(h, backend, store):
    a = await _draft(h, "A", "done")
    store.apply_decisions("run-1", {a.draft_id: "approved"})
    await replay_approved("run-1", h.by_name, store)
    again = await replay_approved("run-1", h.by_name, store)
    assert again == {"posted": [], "failed": []} and len(backend.calls) == 1


def test_claim_is_atomic_across_threads(store):
    from concurrent.futures import ThreadPoolExecutor

    d = store.create_pending("r", "update_task", {"task_id": "A", "status": "x"}, "d")
    store.apply_decisions("r", {d.draft_id: "approved"})
    with ThreadPoolExecutor(16) as pool:
        wins = list(pool.map(lambda _: store.claim(d.draft_id), range(16)))
    assert wins.count(True) == 1


async def test_crash_mid_execution_is_not_rerun(h, backend, store):
    a = await _draft(h, "A", "done")
    store.apply_decisions("run-1", {a.draft_id: "approved"})
    assert store.claim(a.draft_id)  # worker claimed, then died before running or recording
    assert await replay_approved("run-1", h.by_name, store) == {"posted": [], "failed": []}
    assert (await h.call("update_task", task_id="A", status="done")).startswith("ALREADY")
    assert backend.calls == [] and store.list_for_run("run-1")[0].status == "executing"


async def test_tool_missing_at_replay_fails_with_reason(h, backend, store):
    a = await _draft(h, "A", "done")
    store.apply_decisions("run-1", {a.draft_id: "approved"})
    res = await replay_approved("run-1", {}, store)
    assert "not available" in res["failed"][0]["reason"] and backend.calls == []


async def test_failing_tool_is_marked_failed_not_retried(h, backend, store):
    a = await _draft(h, "ghost", "done")  # no such task: the backend raises KeyError
    store.apply_decisions("run-1", {a.draft_id: "approved"})
    res = await replay_approved("run-1", h.by_name, store)
    assert len(res["failed"]) == 1
    assert (await replay_approved("run-1", h.by_name, store))["failed"] == []
