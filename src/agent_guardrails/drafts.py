"""Durable drafts: the approval record and the exactly-once ledger, in one SQLite table.

Every state change is a conditional UPDATE, so correctness comes from the database and not
from Python timing. A lock around one connection keeps coroutines and threads safe.
Postgres would use the same statements; only the connection handling changes.

`arguments_hash` is an HMAC (keyed with a server secret) over draft id, tool and canonical
arguments. It detects edits to the stored row by anyone who lacks the key. It does not
protect against someone who holds the key.
"""

import hmac
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .idempotency import idempotency_key, jsonable
from .modes import Mode

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL, arguments_hash TEXT NOT NULL,
    description TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (run_id, idempotency_key)
)"""

_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL
)"""


@dataclass(frozen=True)
class Draft:
    draft_id: str
    run_id: str
    idempotency_key: str
    tool_name: str
    arguments_json: str
    arguments_hash: str
    description: str
    mode: str
    status: str  # pending -> approved|rejected ; approved -> executing -> posted|failed
    result_json: str | None
    created_at: str
    updated_at: str

    @property
    def arguments(self) -> dict[str, Any]:
        return json.loads(self.arguments_json)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DraftStore:
    def __init__(self, path: str = ":memory:", *, hmac_key: bytes):
        if not isinstance(hmac_key, bytes) or not hmac_key:
            raise ValueError("hmac_key must be non-empty bytes")
        self._key = hmac_key
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._write(_SCHEMA)
        self._write(_RUNS)

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self._conn:
            return self._conn.execute(sql, params).rowcount

    def _read(self, sql: str, params: tuple = ()) -> list[Draft]:
        with self._lock:
            return [Draft(**dict(r)) for r in self._conn.execute(sql, params).fetchall()]

    def _sign(self, draft_id: str, tool_name: str, arguments_json: str) -> str:
        """Signs the exact stored string, so any edit to the row (even "007" -> "7") breaks it."""
        msg = f"{draft_id}\x00{tool_name}\x00{arguments_json}"
        return hmac.new(self._key, msg.encode(), "sha256").hexdigest()

    def verify(self, draft: Draft) -> bool:
        """True if the stored arguments still match the signature made at creation."""
        expected = self._sign(draft.draft_id, draft.tool_name, draft.arguments_json)
        return hmac.compare_digest(expected, draft.arguments_hash)

    def _insert(self, run_id: str, tool_name: str, args: dict, description: str, mode: Mode,
                status: str) -> bool:
        """INSERT OR IGNORE on UNIQUE(run_id, key); True if this call created the row.

        The ORIGINAL arguments are stored (what the tool will receive); only the key and the
        signature use the normalised form.
        """
        ts, draft_id = _now(), uuid.uuid4().hex
        args_json = json.dumps(jsonable(args), sort_keys=True)
        return self._write(
            "INSERT OR IGNORE INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (draft_id, run_id, idempotency_key(run_id, tool_name, args), tool_name,
             args_json, self._sign(draft_id, tool_name, args_json),
             description, mode.value, status, None, ts, ts),
        ) == 1

    def create_pending(self, run_id: str, tool_name: str, args: dict, description: str) -> Draft:
        """Insert-or-return-existing: the same action twice never makes a second draft."""
        self._insert(run_id, tool_name, args, description, Mode.SUGGEST, "pending")
        return self.get_by_key(run_id, idempotency_key(run_id, tool_name, args))  # type: ignore[return-value]

    def get_by_key(self, run_id: str, key: str) -> Draft | None:
        rows = self._read("SELECT * FROM drafts WHERE run_id=? AND idempotency_key=?", (run_id, key))
        return rows[0] if rows else None

    def list_for_run(self, run_id: str) -> list[Draft]:
        return self._read("SELECT * FROM drafts WHERE run_id=? ORDER BY created_at, rowid", (run_id,))

    def list_pending(self, run_id: str) -> list[Draft]:
        return self._read(
            "SELECT * FROM drafts WHERE run_id=? AND status='pending' ORDER BY created_at, rowid",
            (run_id,))

    def list_stuck(self, run_id: str | None = None) -> list[Draft]:
        """Rows left in 'executing' (a worker died mid-run). Never re-run automatically: a human
        must check whether the side effect happened, then resolve the row."""
        if run_id is None:
            return self._read("SELECT * FROM drafts WHERE status='executing' ORDER BY updated_at")
        return self._read("SELECT * FROM drafts WHERE status='executing' AND run_id=?", (run_id,))

    def apply_decisions(self, run_id: str, decisions: dict[str, str]) -> int:
        """Human answers. Only rows still pending change, so a decision can't be overwritten."""
        changed = 0
        for draft_id, decision in decisions.items():
            if decision not in ("approved", "rejected"):
                raise ValueError(f"bad decision {decision!r}")
            changed += self._write(
                "UPDATE drafts SET status=?, updated_at=? "
                "WHERE draft_id=? AND run_id=? AND status='pending'",
                (decision, _now(), draft_id, run_id),
            )
        return changed

    def claim(self, draft_id: str) -> bool:
        """Atomic approved -> executing. Exactly one caller sees rowcount 1 and may run it."""
        return self._write(
            "UPDATE drafts SET status='executing', updated_at=? "
            "WHERE draft_id=? AND status='approved'",
            (_now(), draft_id),
        ) == 1

    def mark_posted(self, draft_id: str, result: Any) -> None:
        self._finish(draft_id, "posted", result)

    def mark_failed(self, draft_id: str, reason: str) -> None:
        self._finish(draft_id, "failed", {"error": reason})

    def _finish(self, draft_id: str, status: str, result: Any) -> None:
        self._write(
            "UPDATE drafts SET status=?, result_json=?, updated_at=? "
            "WHERE draft_id=? AND status='executing'",
            (status, json.dumps(result, default=str), _now(), draft_id),
        )

    def begin_autonomous(self, run_id: str, tool_name: str, args: dict, description: str) -> Draft | None:
        """Claim an autonomous write BEFORE running it: insert the ledger row as 'executing'.

        Returns the row if this caller won, None if the same action already has a row (done,
        in flight, or crashed mid-run). A crash leaves 'executing', so a resume never re-runs it.
        The one exception is a row that cleanly ended 'failed': it is claimed again with an
        atomic failed -> executing UPDATE, so a failed autonomous write stays retryable.
        """
        key = idempotency_key(run_id, tool_name, args)
        if not self._insert(run_id, tool_name, args, description, Mode.AUTONOMOUS, "executing"):
            if self._write(
                "UPDATE drafts SET status='executing', updated_at=? WHERE run_id=? "
                "AND idempotency_key=? AND mode=? AND status='failed'",
                (_now(), run_id, key, Mode.AUTONOMOUS.value),
            ) != 1:
                return None
        return self.get_by_key(run_id, key)

    # --- run state (see runs.py). Each change is a conditional UPDATE, like the drafts. ---

    def begin_run(self, run_id: str) -> None:
        self._write("INSERT OR IGNORE INTO runs VALUES (?, 'running', ?)", (run_id, _now()))

    def run_state(self, run_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return row["status"] if row else None

    def transition_run(self, run_id: str, frm: str, to: str) -> bool:
        """Atomic frm -> to. Exactly one concurrent caller sees True."""
        return self._write("UPDATE runs SET status=?, updated_at=? WHERE run_id=? AND status=?",
                           (to, _now(), run_id, frm)) == 1
