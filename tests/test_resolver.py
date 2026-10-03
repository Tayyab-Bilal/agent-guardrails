import pytest
from conftest import DEFAULT, Harness, cfg
from langchain_core.tools import StructuredTool

from agent_guardrails import DeterministicDescriber, Mode, guard
from agent_guardrails.acme_tools import AcmeBackend, build_tools
from agent_guardrails.gate import guard_tools, unclassified_tools
from agent_guardrails.resolver import (
    KNOWN_UNMAPPED,
    MUTATING_TOOLS,
    READ_TOOLS,
    TOOL_TO_ACTION,
    mode_for,
    normalize_permissions,
)


def test_every_mutating_tool_is_mapped_or_explicitly_denied():
    """CI guardrail: a new tool in the registry fails here until it is classified."""
    tools = build_tools(AcmeBackend())
    assert unclassified_tools(tools) == []
    assert not set(TOOL_TO_ACTION) & KNOWN_UNMAPPED
    assert not MUTATING_TOOLS & READ_TOOLS
    assert MUTATING_TOOLS <= {t.name for t in tools}


def test_new_unclassified_tool_is_caught_and_refused(h):
    """No self-declared metadata needed: an unknown name is refused, write or not."""
    async def archive_project(project_id: str) -> str:
        return "archived"

    new = StructuredTool.from_function(coroutine=archive_project, name="archive_project",
                                       description="x")
    assert unclassified_tools([new]) == ["archive_project"]
    with pytest.raises(ValueError, match="archive_project"):
        guard_tools([new], **h.kw)


async def test_unmapped_tool_is_denied(backend, store, source):
    # Even a tenant that "allowed" deleting projects cannot enable an unmapped tool.
    source.config = cfg(("Projects", "Delete Project", "Autonomous"))
    h = Harness(backend, store, source)
    out = await h.call("delete_project", project_id="p1")
    assert out.startswith("NOT ALLOWED")
    assert "p1" in backend.projects and backend.calls == []
    assert mode_for("delete_project", normalize_permissions(source.config)) is Mode.DISABLED


async def test_invented_tool_is_denied(store, source):
    d = await guard(tool_name="drop_database", tool_args={}, run_id="r", tenant_id="t",
                    user_id="u", permissions=source, store=store,
                    describer=DeterministicDescriber())
    assert d.action.value == "block"


async def test_empty_config_denies_everything(backend, store):
    from agent_guardrails import StaticPermissionSource

    h = Harness(backend, store, StaticPermissionSource({"categories": []}))
    for name, args in [("create_task", {"title": "x"}), ("update_task", {"task_id": "A", "status": "d"})]:
        assert (await h.call(name, **args)).startswith("NOT ALLOWED")
    assert backend.calls == [] and store.list_for_run("run-1") == []


def test_unknown_mode_string_denies():
    lookup = normalize_permissions(cfg(("Tasks", "Create Task", "YOLO"),
                                       ("Tasks", "Update Task", "autonomous")))  # wrong case too
    assert lookup == {}
    assert mode_for("create_task", lookup) is Mode.DISABLED
    assert normalize_permissions(DEFAULT)[("Tasks", "Create Task")] is Mode.AUTONOMOUS
