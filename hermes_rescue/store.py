"""Rescue's own records: incidents, snapshots and the guard switch, in one small SQLite file.

The file lives in ``<hermes home>/plugin-data/hermes-rescue/rescue.db`` (via ``plugin_db``), so
each profile has its own and the Desktop backend (``plugin_api.py``) reads the same rows.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Callable, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,          -- guard_block | guard_ask | guard_allowed | failed_turn | degraded_turn | undo
    title TEXT NOT NULL,
    fix TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS incidents_ts ON incidents(ts);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    label TEXT NOT NULL,
    files TEXT NOT NULL,         -- JSON list of {orig, stored, kind, existed, size}
    folder TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | kept | unused | restored
    tool_call_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

INCIDENT_LIMIT = 2000
MAX_ALLOW_SECONDS = 120 * 60   # /rescue allow tops out at two hours; a longer value was not set by Rescue


class Store:
    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self._connect = connect
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = self._connect()
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
        return self._conn

    def _run(self, sql: str, params: tuple = (), *, fetch: str = "") -> Any:
        with self._lock:
            db = self._db()
            cur = db.execute(sql, params)
            result = cur.fetchall() if fetch == "all" else cur.fetchone() if fetch == "one" else cur.lastrowid
            db.commit()
            return result

    # ---- incidents ----------------------------------------------------------------------------
    def add_incident(self, kind: str, title: str, fix: str = "", *, session_id: str = "",
                     platform: str = "", data: Optional[dict] = None) -> int:
        row = self._run(
            "INSERT INTO incidents (ts, kind, title, fix, session_id, platform, data) VALUES (?,?,?,?,?,?,?)",
            (time.time(), kind, title[:500], fix[:500], session_id, platform, json.dumps(data or {}, default=str)))
        if row and row % 100 == 0:
            self._run("DELETE FROM incidents WHERE id <= ?", (row - INCIDENT_LIMIT,))
        return row

    def update_incident(self, incident_id: int, kind: str, title: str, fix: str = "", *,
                        data: Optional[dict] = None) -> None:
        self._run("UPDATE incidents SET kind=?, title=?, fix=?, data=? WHERE id=?",
                  (kind, title[:500], fix[:500], json.dumps(data or {}, default=str), incident_id))

    def incidents(self, limit: int = 20, since: float = 0.0) -> list[dict]:
        rows = self._run("SELECT id, ts, kind, title, fix, session_id, platform, data FROM incidents "
                         "WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since, limit), fetch="all")
        return [dict(id=r[0], ts=r[1], kind=r[2], title=r[3], fix=r[4], session_id=r[5], platform=r[6],
                     data=json.loads(r[7] or "{}")) for r in rows]

    def counts(self, since: float) -> dict[str, int]:
        rows = self._run("SELECT kind, count(*) FROM incidents WHERE ts >= ? GROUP BY kind", (since,), fetch="all")
        return {k: n for k, n in rows}

    # ---- snapshots ----------------------------------------------------------------------------
    def add_snapshot(self, label: str, files: list[dict], *, folder: str = "", session_id: str = "",
                     tool_call_id: str = "") -> int:
        return self._run("INSERT INTO snapshots (ts, label, files, folder, session_id, tool_call_id) "
                         "VALUES (?,?,?,?,?,?)",
                         (time.time(), label[:300], json.dumps(files), folder, session_id, tool_call_id))

    def set_snapshot_status_for_call(self, tool_call_id: str, status: str) -> None:
        if tool_call_id:
            self._run("UPDATE snapshots SET status=? WHERE tool_call_id=? AND status='pending'", (status, tool_call_id))

    def set_snapshot_status(self, snap_id: int, status: str) -> None:
        self._run("UPDATE snapshots SET status=? WHERE id=?", (status, snap_id))

    def snapshots(self, limit: int = 10, usable_only: bool = True) -> list[dict]:
        where = "WHERE status IN ('pending','kept')" if usable_only else ""
        rows = self._run(f"SELECT id, ts, label, files, session_id, status, folder FROM snapshots {where} "
                         "ORDER BY id DESC LIMIT ?", (limit,), fetch="all")
        return [self._snap(r) for r in rows]

    @staticmethod
    def _snap(r) -> dict:
        return dict(id=r[0], ts=r[1], label=r[2], files=json.loads(r[3]), session_id=r[4], status=r[5], folder=r[6])

    def snapshot(self, snap_id: int) -> Optional[dict]:
        r = self._run("SELECT id, ts, label, files, session_id, status, folder FROM snapshots WHERE id=?",
                      (snap_id,), fetch="one")
        return self._snap(r) if r else None

    def old_snapshot_ids(self, keep: int) -> list[int]:
        rows = self._run("SELECT id FROM snapshots ORDER BY id DESC LIMIT -1 OFFSET ?", (keep,), fetch="all")
        return [r[0] for r in rows]

    def delete_snapshot_row(self, snap_id: int) -> None:
        self._run("DELETE FROM snapshots WHERE id=?", (snap_id,))

    # ---- guard switch -------------------------------------------------------------------------
    def get(self, key: str, default: str = "") -> str:
        r = self._run("SELECT value FROM kv WHERE key=?", (key,), fetch="one")
        return r[0] if r else default

    def set(self, key: str, value: str) -> None:
        self._run("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))

    def allowed_until(self) -> float:
        try:
            return float(self.get("allow_until", "0") or 0)
        except ValueError:
            return 0.0

    def guard_lifted(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        until = self.allowed_until()
        return now < until <= now + MAX_ALLOW_SECONDS + 60
