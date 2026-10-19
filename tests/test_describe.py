import asyncio
import uuid

from agent_guardrails import DeterministicDescriber, LLMDescriber, resolve_names
from agent_guardrails.describe import strip_ids
from agent_guardrails.resolve import collect_ids

U = str(uuid.uuid4())
ARGS = {"title": "Q4 review", "task_id": 7, "owner": {"user_id": "u9", "name": "Sam", "ref": U},
        "ids": [1, 2], "links": [U, "docs"], "ProjectID": "x"}


async def test_description_contains_no_ids():
    text = await DeterministicDescriber().describe("create_task", ARGS, "Create Task")
    assert text.startswith("Create Task: ") and "title='Q4 review'" in text and "Sam" in text
    for leak in (U, "u9", "task_id", "ids", "ProjectID", "7"):
        assert leak not in text
    assert strip_ids(ARGS)["links"] == ["docs"]


class BoomLLM:
    async def complete(self, prompt):
        raise RuntimeError("down")


class LeakyLLM:
    async def complete(self, prompt):
        return f"Create task {U}"


class GoodLLM:
    seen = ""

    async def complete(self, prompt):
        GoodLLM.seen = prompt
        return "Create the Q4 review task"


async def test_llm_describer_falls_back_on_error():
    fallback = await DeterministicDescriber().describe("create_task", ARGS, "Create Task")
    assert await LLMDescriber(BoomLLM()).describe("create_task", ARGS, "Create Task") == fallback
    assert await LLMDescriber(LeakyLLM()).describe("create_task", ARGS, "Create Task") == fallback


async def test_llm_describer_sees_only_stripped_args():
    out = await LLMDescriber(GoodLLM()).describe("create_task", ARGS, "Create Task")
    assert out == "Create the Q4 review task" and U not in GoodLLM.seen and "u9" not in GoodLLM.seen


async def test_name_resolver_names_the_target_without_ids():
    async def lookup(kind, id_):
        return {"7": "Q4 review"}.get(id_)

    d = DeterministicDescriber(lookup)
    text = await d.describe("update_task", {"task_id": 7, "status": "done"}, "Update Task")
    assert text == "Update Task for 'Q4 review': status='done'"
    assert await d.describe("update_task", {"task_id": 8, "status": "x"}, "Update Task") \
        == "Update Task: status='x'"


async def test_llm_describer_gets_resolved_names_but_no_ids():
    async def lookup(kind, id_):
        return "Q4 review"

    out = await LLMDescriber(GoodLLM(), lookup).describe(
        "update_task", {"task_id": "abc-123", "status": "done"}, "Update Task")
    assert out == "Create the Q4 review task"
    assert "Q4 review" in GoodLLM.seen and "abc-123" not in GoodLLM.seen


# --- bounded id -> name resolution ---

def _bulk(n):
    return {"updates": [{"task_id": f"t{i}", "status": "done"} for i in range(n)]}


def test_ids_are_collected_from_nested_bulk_args():
    assert collect_ids(_bulk(2)) == [("task", "t0"), ("task", "t1")]
    assert collect_ids({"project_ids": ["p1", "p2"], "owner": {"user_id": "u1"}, "n": True}) == [
        ("project", "p1"), ("project", "p2"), ("user", "u1")]


async def test_resolver_makes_at_most_12_lookups():
    calls = []

    async def lookup(kind, id_):
        calls.append(id_)
        return f"name-{id_}"

    names = await resolve_names(_bulk(30), lookup)
    assert len(calls) == 12 and len(names) == 12


async def test_resolver_has_an_overall_timeout_and_keeps_what_finished():
    async def lookup(kind, id_):
        if id_ == "slow":
            await asyncio.sleep(5)
        return id_.upper()

    started = asyncio.get_running_loop().time()
    names = await resolve_names({"task_ids": ["a", "slow", "b"]}, lookup, timeout=0.05)
    assert asyncio.get_running_loop().time() - started < 1
    assert list(names.values()) == ["A", "B"]


async def test_failing_lookup_just_leaves_the_name_out():
    async def lookup(kind, id_):
        if id_ == "bad":
            raise RuntimeError("backend down")
        return id_

    assert list((await resolve_names({"task_ids": ["bad", "ok"]}, lookup)).values()) == ["ok"]


async def test_bulk_approval_names_its_targets_and_has_no_ids():
    async def lookup(kind, id_):
        return f"Task {id_}"

    text = await DeterministicDescriber(lookup).describe(
        "bulk_update_tasks", _bulk(13), "Update Task")
    assert text == "Update Task for 'Task t0', 'Task t1', 'Task t2' and 9 more: updates=<13 items>"
    assert "task_id" not in text and "t12" not in text
