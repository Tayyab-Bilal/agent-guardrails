"""Turn the ids in a tool call into human names, with hard bounds.

An approver should read "Update Task for 'Draft spec'", not a UUID. Name lookups hit the
backend, so they are capped (a bulk call must not fan out into hundreds of requests) and
share one overall deadline (a slow backend must not stall the run). Whatever resolved in time
is used; the rest is simply left out of the sentence.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

NameLookup = Callable[[str, str], Awaitable[str | None]]  # (kind, id) -> display name

MAX_LOOKUPS = 12
TIMEOUT_SECONDS = 6.0

_ID_KEY = re.compile(r"(.*?)_?(ids?)", re.IGNORECASE)


def _is_scalar_id(v: Any) -> bool:
    return isinstance(v, (str, int)) and not isinstance(v, bool)


def collect_ids(value: Any) -> list[tuple[str, str]]:
    """Find (kind, id) pairs anywhere in nested args, e.g. a list of task updates.

    The kind comes from the key: `task_id` and `task_ids` are both kind "task".
    """
    found: list[tuple[str, str]] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for key, child in v.items():
                m = _ID_KEY.fullmatch(key)
                if m and (_is_scalar_id(child) or isinstance(child, list)):
                    kind = m.group(1).lower() or "item"
                    for one in child if isinstance(child, list) else [child]:
                        if _is_scalar_id(one):
                            found.append((kind, str(one)))
                else:
                    walk(child)
        elif isinstance(v, list):
            for child in v:
                walk(child)

    walk(value)
    return list(dict.fromkeys(found))  # unique, in first-seen order


async def resolve_names(args: dict[str, Any], lookup: NameLookup, *, max_lookups: int = MAX_LOOKUPS,
                        timeout: float = TIMEOUT_SECONDS) -> dict[tuple[str, str], str]:
    """Look up at most `max_lookups` ids at once, waiting at most `timeout` seconds in total."""
    wanted = collect_ids(args)[:max_lookups]
    if not wanted:
        return {}
    tasks = {asyncio.ensure_future(lookup(kind, id_)): (kind, id_) for kind, id_ in wanted}
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    names: dict[tuple[str, str], str] = {}
    for task in done:
        if task.cancelled() or task.exception() is not None:
            continue  # a failed lookup just means no name for that id
        if name := task.result():
            names[tasks[task]] = str(name)
    return {key: names[key] for key in wanted if key in names}
