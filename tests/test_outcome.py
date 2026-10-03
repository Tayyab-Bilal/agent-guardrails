import pytest
from conftest import Harness, cfg

from agent_guardrails import RunOutcome, StaticPermissionSource


@pytest.fixture
def outcome():
    return RunOutcome()


def harness(backend, store, source, outcome, **kw):
    return Harness(backend, store, source, outcome=outcome, **kw)


async def test_blocked_and_rejected_steps_are_recorded_as_skipped_not_failed(
        backend, store, source, outcome):
    h = harness(backend, store, source, outcome)
    await h.call("delete_task", task_id="A")  # blocked
    await h.call("update_task", task_id="A", status="done")  # held
    (d,) = store.list_for_run("run-1")
    store.apply_decisions("run-1", {d.draft_id: "rejected"})
    await h.call("update_task", task_id="A", status="done")  # rejected draft
    assert [(s.tool, s.kind, s.skipped) for s in outcome.steps] == [
        ("delete_task", "blocked", True), ("update_task", "held", False),
        ("update_task", "rejected", True)]


async def test_all_blocked_run_finalises_failed(backend, store, outcome):
    h = harness(backend, store, StaticPermissionSource(cfg()), outcome)
    await h.call("create_task", title="x")
    await h.call("delete_project", project_id="p1")
    assert outcome.status == "failed"


async def test_blocked_plus_one_executed_write_is_completed(backend, store, source, outcome):
    h = harness(backend, store, source, outcome)
    await h.call("delete_task", task_id="A")
    await h.call("create_task", title="x")
    assert outcome.status == "completed"
    assert [s.kind for s in outcome.steps] == ["blocked", "executed"]


async def test_empty_run_is_completed_not_failed(outcome):
    assert outcome.status == "completed"


async def test_outage_parks_and_misconfig_fails_the_run(backend, store, outcome):
    class Down:
        async def fetch(self, tenant_id, user_id):
            raise TimeoutError("slow")

    await harness(backend, store, Down(), outcome).call("create_task", title="x")
    assert outcome.status == "parked"
    bad = RunOutcome()
    await harness(backend, store, StaticPermissionSource("nope"), bad).call("create_task", title="x")
    assert bad.status == "failed" and bad.steps[0].kind == "misconfigured"


async def test_failed_write_is_recorded_as_failed(backend, store, source, outcome, monkeypatch):
    async def boom(tool, args):
        raise RuntimeError("500")

    monkeypatch.setattr(backend, "write", boom)
    with pytest.raises(RuntimeError):
        await harness(backend, store, source, outcome).call("create_task", title="x")
    assert outcome.steps[0].kind == "failed" and not outcome.steps[0].skipped


def test_unknown_step_kind_is_rejected(outcome):
    with pytest.raises(ValueError):
        outcome.record("t", "vibes")
