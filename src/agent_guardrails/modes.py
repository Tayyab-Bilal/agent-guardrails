"""The three per-action choices an organisation can make."""

from enum import Enum


class Mode(str, Enum):
    AUTONOMOUS = "autonomous"  # do it
    SUGGEST = "suggest"  # ask me first
    DISABLED = "disabled"  # never
