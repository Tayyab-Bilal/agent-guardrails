"""A fictional 'Acme Workspace' backend and its LangChain tools, used by tests and the demo."""

import asyncio
from datetime import datetime
from typing import Any

from langchain_core.tools import StructuredTool


class AcmeBackend:
    """In-memory workspace. `calls` records every write so tests can count side effects."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.projects: dict[str, dict[str, Any]] = {"p1": {"name": "Website relaunch"}}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.delay = 0.0  # widen race windows in tests

    async def write(self, tool: str, args: dict[str, Any]) -> None:
        self.calls.append((tool, args))
        await asyncio.sleep(self.delay)


def build_tools(b: AcmeBackend) -> list[StructuredTool]:
    async def create_task(title: str, project_id: str | None = None) -> dict:
        await b.write("create_task", {"title": title})
        task_id = f"t{len(b.tasks) + 1}"
        b.tasks[task_id] = {"title": title, "status": "open", "project_id": project_id}
        return {"task_id": task_id}

    async def update_task(task_id: str, status: str) -> dict:
        await b.write("update_task", {"task_id": task_id, "status": status})
        b.tasks[task_id]["status"] = status
        return {"task_id": task_id, "status": status}

    async def bulk_update_tasks(updates: list[dict[str, str]]) -> dict:
        for u in updates:
            await b.write("update_task", dict(u))
            b.tasks[u["task_id"]]["status"] = u["status"]
        return {"updated": len(updates)}

    async def create_project(name: str) -> dict:
        await b.write("create_project", {"name": name})
        b.projects[f"p{len(b.projects) + 1}"] = {"name": name}
        return {"ok": True}

    async def schedule_meeting(title: str, when: datetime, notes: str | None = None) -> dict:
        await b.write("schedule_meeting", {"title": title})
        return {"ok": True}

    async def delete_project(project_id: str) -> dict:
        await b.write("delete_project", {"project_id": project_id})
        b.projects.pop(project_id, None)
        return {"ok": True}

    async def delete_task(task_id: str) -> dict:
        await b.write("delete_task", {"task_id": task_id})
        b.tasks.pop(task_id, None)
        return {"ok": True}

    async def remove_member(project_id: str, user_id: str) -> dict:
        await b.write("remove_member", {"project_id": project_id})
        return {"ok": True}

    async def list_tasks() -> list:
        return [{"task_id": k, **v} for k, v in b.tasks.items()]

    writes = [create_task, update_task, bulk_update_tasks, create_project, schedule_meeting,
              delete_project, delete_task, remove_member]
    tools = [StructuredTool.from_function(coroutine=fn, name=fn.__name__,
                                          description=fn.__name__.replace("_", " ")) for fn in writes]
    tools.append(StructuredTool.from_function(coroutine=list_tasks, name="list_tasks",
                                              description="list tasks"))
    return tools
