"""Human-readable one-liners for approval requests. They never contain raw ids."""

import re
from typing import Any, Protocol

from .resolve import NameLookup, resolve_names

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _is_id_key(key: str) -> bool:
    k = key.lower()
    return k in ("id", "ids") or k.endswith(("_id", "_ids")) or key.endswith(("Id", "ID", "Ids", "IDs"))


def strip_ids(value: Any) -> Any:
    """Remove id-like keys and UUID-looking values, recursively. Ids mean nothing to a human."""
    if isinstance(value, dict):
        return {k: strip_ids(v) for k, v in value.items()
                if not _is_id_key(k) and not (isinstance(v, str) and _UUID.search(v))}
    if isinstance(value, list):
        return [strip_ids(v) for v in value if not (isinstance(v, str) and _UUID.search(v))]
    return value


class Describer(Protocol):
    async def describe(self, tool: str, args: dict[str, Any], action: str) -> str: ...


def _shown(value: Any) -> str:
    return f"<{len(value)} items>" if isinstance(value, list) else repr(value)


def _names_text(names: list[str]) -> str:
    head = ", ".join(repr(n) for n in names[:3])
    return head + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def render(action: str, args: dict[str, Any], names: list[str]) -> str:
    """The deterministic sentence: action, then the named targets, then the stripped details."""
    shown = strip_ids(args)
    parts = ", ".join(f"{k}={_shown(v)}" for k, v in sorted(shown.items()) if v is not None)
    head = f"{action} for {_names_text(names)}" if names else action
    return f"{head}: {parts}" if parts else head


async def _names(lookup: NameLookup | None, args: dict[str, Any]) -> list[str]:
    return list((await resolve_names(args, lookup)).values()) if lookup else []


class DeterministicDescriber:
    """`lookup(kind, id)` is an optional async id -> name resolver (a task title, say), so two
    approvals for different targets do not read the same. Bounded: see resolve.py."""

    def __init__(self, lookup: NameLookup | None = None):
        self.lookup = lookup

    async def describe(self, tool: str, args: dict[str, Any], action: str) -> str:
        return render(action, args, await _names(self.lookup, args))


class TextLLM(Protocol):
    async def complete(self, prompt: str) -> str: ...


class LLMDescriber:
    """Nicer wording from an LLM, but it only sees stripped args plus names and can never block."""

    def __init__(self, llm: TextLLM, lookup: NameLookup | None = None):
        self.llm, self.lookup = llm, lookup

    async def describe(self, tool: str, args: dict[str, Any], action: str) -> str:
        names = await _names(self.lookup, args)
        prompt = ("In one short sentence, describe this request for a human approver.\n"
                  f"Action: {action}\nTargets: {names}\nDetails: {strip_ids(args)}")
        try:
            text = (await self.llm.complete(prompt)).strip()
            if text and not _UUID.search(text):
                return text
        except Exception:  # a flaky describer must never stop an approval request
            pass
        return render(action, args, names)
