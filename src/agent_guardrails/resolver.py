"""Static tool -> action map plus tenant config -> Mode. Pure functions, no LLM, no I/O."""

from typing import Any

from .modes import Mode

# tool name -> (category, action) as shown in the tenant's permission settings.
TOOL_TO_ACTION: dict[str, tuple[str, str]] = {
    "create_task": ("Tasks", "Create Task"),
    "update_task": ("Tasks", "Update Task"),
    "bulk_update_tasks": ("Tasks", "Update Task"),
    "create_project": ("Projects", "Create Project"),
    "schedule_meeting": ("Meetings", "Schedule Meeting"),
}

# Write tools that are denied on purpose. They are NOT mapped to a "nearby" action (say,
# delete_task -> Update Task) because a tenant who allowed edits never agreed to deletion.
# Listing them here keeps the coverage test honest: every write tool must be classified.
KNOWN_UNMAPPED: set[str] = {"delete_project", "delete_task", "remove_member"}

MUTATING_TOOLS: set[str] = set(TOOL_TO_ACTION) | KNOWN_UNMAPPED

# Read tools pass through ungated. This is an explicit allow-list, not "everything else":
# a tool in none of the three sets is refused at wiring time instead of silently running.
READ_TOOLS: set[str] = {"list_tasks", "get_task"}

_MODE_STRINGS = {"Autonomous": Mode.AUTONOMOUS, "Suggest Only": Mode.SUGGEST, "Disabled": Mode.DISABLED}


def normalize_permissions(config: dict[str, Any]) -> dict[tuple[str, str], Mode]:
    """Flatten tenant config. Unknown mode strings are dropped, which means deny."""
    lookup: dict[tuple[str, str], Mode] = {}
    for category in config.get("categories", []):
        for action in category.get("actions", []):
            mode = _MODE_STRINGS.get(action.get("mode"))
            if mode is not None:
                lookup[(category.get("name"), action.get("name"))] = mode
    return lookup


def mode_for(tool: str, lookup: dict[tuple[str, str], Mode]) -> Mode:
    """Unmapped tool or missing action -> DISABLED (deny by default)."""
    key = TOOL_TO_ACTION.get(tool)
    return lookup.get(key, Mode.DISABLED) if key else Mode.DISABLED
