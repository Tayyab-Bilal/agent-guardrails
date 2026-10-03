import pytest
from conftest import Harness, cfg
from langchain_core.tools import StructuredTool

from agent_guardrails import (
    DeterministicDescriber,
    DraftStore,
    StaticPermissionSource,
    guard_tools,
)
from agent_guardrails.acme_tools import build_tools
from agent_guardrails.resolver import MUTATING_TOOLS


def test_gate_preserves_tool_schema_and_rewrapping_does_not_stack(backend, store, source):
    tools = build_tools(backend)
    before = {t.name: (t.description, t.args_schema.model_json_schema(), t.args) for t in tools}
    h = Harness(backend, store, source)
    for t in h.tools:
        assert (t.description, t.args_schema.model_json_schema(), t.args) == before[t.name]
    again = guard_tools(h.tools, **h.kw)  # guarding guarded tools wraps the original, once
    for t in again:
        if t.name in MUTATING_TOOLS:
            assert t.coroutine._wrapped is not h.by_name[t.name].coroutine
            assert not hasattr(t.coroutine._wrapped, "_wrapped")


def test_guard_tools_leaves_input_tools_untouched(backend, store, source):
    tools = build_tools(backend)
    originals = {t.name: t.coroutine for t in tools}
    Harness(backend, store, source)  # builds its own tools; guard the shared list directly too
    guarded = guard_tools(tools, run_id="r", tenant_id="t", user_id="u", permissions=source,
                          store=store, describer=DeterministicDescriber())
    assert {t.name: t.coroutine for t in tools} == originals
    assert all(g is not t for g, t in zip(guarded, tools, strict=True) if t.name in MUTATING_TOOLS)


def test_sync_invoke_cannot_bypass_gate(h):
    ran = []

    async def acreate(title: str) -> str:
        return "async"

    def screate(title: str) -> str:
        ran.append(title)
        return "sync"

    both = StructuredTool.from_function(func=screate, coroutine=acreate, name="create_task",
                                        description="x")
    (guarded,) = guard_tools([both], **h.kw)
    with pytest.raises(NotImplementedError):
        guarded.invoke({"title": "x"})
    assert ran == []
    assert both.func is screate  # the caller's tool still has its sync path


async def test_interleaved_guarded_copies_each_keep_their_own_context(backend):
    shared = build_tools(backend)
    store = DraftStore(hmac_key=b"k")
    base = dict(user_id="u", store=store, describer=DeterministicDescriber())
    a = {t.name: t for t in guard_tools(
        shared, run_id="runA", tenant_id="A", permissions=StaticPermissionSource(cfg()), **base)}
    b = {t.name: t for t in guard_tools(
        shared, run_id="runB", tenant_id="B",
        permissions=StaticPermissionSource(cfg(("Tasks", "Create Task", "Autonomous"))), **base)}
    # B was guarded last; A's agent must still be denied, and B's must still run under runB
    assert (await a["create_task"].ainvoke({"title": "x"})).startswith("NOT ALLOWED")
    assert backend.calls == []
    assert isinstance(await b["create_task"].ainvoke({"title": "y"}), dict)
    assert len(store.list_for_run("runB")) == 1 and store.list_for_run("runA") == []


async def test_read_tools_are_not_gated(backend, store):
    h = Harness(backend, store, StaticPermissionSource(cfg()))  # deny-all config
    assert not hasattr(h.by_name["list_tasks"].coroutine, "_wrapped")
    assert len(await h.call("list_tasks")) == 2
    assert store.list_for_run("run-1") == []
