import asyncio
import threading

import pytest
from conftest import Harness, cfg

from agent_guardrails import ConflictError, ListSink, RunManager, RunOutcome, StaticPermissionSource


@pytest.fixture
def sink():
    return ListSink()


@pytest.fixture
def runs(store, sink):
    return RunManager(store, sink)


async def _park(h, runs, outcome=None):
    runs.start(h.run_id)
    await h.call("update_task", task_id="A", status="done")
    await h.call("update_task", task_id="B", status="blocked")
    return runs.finish(h.run_id, outcome)


async def test_run_with_drafts_parks_and_emits_one_cloudevent_with_all_drafts(h, runs, sink):
    assert await _park(h, runs) == "awaiting_approval"
    assert runs.state("run-1") == "awaiting_approval"
    (event,) = sink.events
    assert event["specversion"] == "1.0" and event["type"] == "run.awaiting_approval"
    assert event["data"]["run_id"] == "run-1" and len(event["data"]["drafts"]) == 2
    assert {d["draft_id"] for d in event["data"]["drafts"]} == {
        d.draft_id for d in h.store.list_pending("run-1")}
    runs.finish("run-1")  # finishing again changes nothing and does not re-emit
    assert len(sink.events) == 1


async def test_run_without_drafts_completes_and_emits_nothing(h, runs, sink):
    runs.start("run-1")
    await h.call("create_task", title="x")
    assert runs.finish("run-1") == "completed" and sink.events == []


async def test_decide_settles_all_drafts_mixed_and_replays_only_approved(h, runs, backend, store):
    await _park(h, runs)
    a, b = store.list_pending("run-1")
    result = await runs.decide("run-1", {a.draft_id: "approved", b.draft_id: "rejected"}, h.by_name)
    assert (result["state"], result["approved"], result["rejected"]) == ("running", 1, 1)
    assert len(result["posted"]) == 1 and backend.tasks["A"]["status"] == "done"
    assert backend.tasks["B"]["status"] == "open" and len(backend.calls) == 1
    assert runs.state("run-1") == "running"


async def test_second_decision_gets_conflict_409(h, runs, store):
    await _park(h, runs)
    ids = {d.draft_id: "approved" for d in store.list_pending("run-1")}
    await runs.decide("run-1", ids, h.by_name)
    with pytest.raises(ConflictError) as e:
        await runs.decide("run-1", ids, h.by_name)
    assert e.value.status_code == 409


async def test_concurrent_decisions_exactly_one_wins(h, runs, backend, store):
    await _park(h, runs)
    ids = {d.draft_id: "approved" for d in store.list_pending("run-1")}
    results = await asyncio.gather(*(runs.decide("run-1", ids, h.by_name) for _ in range(5)),
                                   return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, ConflictError) for r in results) == 4
    assert len(backend.calls) == 2  # each approved action ran once


def test_run_claim_is_atomic_across_threads(store):
    store.begin_run("r")
    store.transition_run("r", "running", "awaiting_approval")
    wins: list[bool] = []
    barrier = threading.Barrier(16)

    def go():
        barrier.wait()
        wins.append(store.transition_run("r", "awaiting_approval", "resuming"))

    threads = [threading.Thread(target=go) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


async def test_decide_before_parking_is_a_conflict(h, runs):
    runs.start("run-1")
    with pytest.raises(ConflictError):
        await runs.decide("run-1", {}, h.by_name)
    with pytest.raises(ConflictError):  # unknown run
        await runs.decide("nope", {}, h.by_name)


async def test_decisions_must_cover_exactly_the_pending_drafts_and_release_the_claim(
        h, runs, store, backend):
    await _park(h, runs)
    a, b = store.list_pending("run-1")
    with pytest.raises(ValueError, match="missing"):
        await runs.decide("run-1", {a.draft_id: "approved"}, h.by_name)
    with pytest.raises(ValueError, match="unknown"):
        await runs.decide("run-1", {a.draft_id: "approved", b.draft_id: "approved", "x": "approved"},
                          h.by_name)
    with pytest.raises(ValueError):
        await runs.decide("run-1", {a.draft_id: "approved", b.draft_id: "maybe"}, h.by_name)
    assert runs.state("run-1") == "awaiting_approval" and backend.calls == []  # nothing consumed
    await runs.decide("run-1", {a.draft_id: "rejected", b.draft_id: "rejected"}, h.by_name)
    assert backend.calls == []


async def test_all_blocked_run_finishes_failed_and_outage_finishes_parked(backend, store, sink):
    runs = RunManager(store, sink)
    out = RunOutcome()
    h = Harness(backend, store, StaticPermissionSource(cfg()), run_id="r1", outcome=out)
    runs.start("r1")
    await h.call("create_task", title="x")
    assert runs.finish("r1", out) == "failed"

    class Down:
        async def fetch(self, tenant_id, user_id):
            raise TimeoutError("slow")

    out2 = RunOutcome()
    h2 = Harness(backend, store, Down(), run_id="r2", outcome=out2)
    runs.start("r2")
    await h2.call("create_task", title="x")
    assert runs.finish("r2", out2) == "parked" and sink.events == []
