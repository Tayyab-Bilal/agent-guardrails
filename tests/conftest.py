import pytest

from agent_guardrails import DeterministicDescriber, DraftStore, StaticPermissionSource, guard_tools
from agent_guardrails.acme_tools import AcmeBackend, build_tools


def cfg(*rows: tuple[str, str, str]) -> dict:
    """cfg(("Tasks", "Create Task", "Autonomous"), ...) -> tenant config payload."""
    cats: dict[str, list] = {}
    for cat, action, mode in rows:
        cats.setdefault(cat, []).append({"name": action, "mode": mode})
    return {"categories": [{"name": n, "actions": a} for n, a in cats.items()]}


DEFAULT = cfg(
    ("Tasks", "Create Task", "Autonomous"),
    ("Tasks", "Update Task", "Suggest Only"),
    ("Projects", "Create Project", "Disabled"),
    ("Meetings", "Schedule Meeting", "Suggest Only"),
)


class Harness:
    """A guarded Acme toolset wired to a store and a (mutable) permission source."""

    def __init__(self, backend, store, source, run_id="run-1", tenant_id="acme", **kw):
        self.backend, self.store, self.source, self.run_id = backend, store, source, run_id
        self.kw = dict(run_id=run_id, tenant_id=tenant_id, user_id="u1", permissions=source,
                       store=store, describer=DeterministicDescriber(), retry_backoff=0, **kw)
        self.tools = guard_tools(build_tools(backend), **self.kw)
        self.by_name = {t.name: t for t in self.tools}

    async def call(self, name: str, **args):
        return await self.by_name[name].ainvoke(args)


@pytest.fixture
def backend():
    b = AcmeBackend()
    b.tasks = {"A": {"title": "a", "status": "open"}, "B": {"title": "b", "status": "open"}}
    return b


@pytest.fixture
def store():
    return DraftStore(hmac_key=b"test-key")


@pytest.fixture
def source():
    return StaticPermissionSource(DEFAULT)


@pytest.fixture
def h(backend, store, source):
    return Harness(backend, store, source)
