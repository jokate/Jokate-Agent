"""Studio store: runs/studio/studio.sqlite (design/studio/ARCHITECTURE.md §3.1–3.2, §7.2).

Same shape as HistoryStore (one connection, one lock, rows as dicts) with four differences: foreign keys
are on, the connection is in autocommit mode with explicit BEGIN IMMEDIATE / COMMIT, `transaction()`
groups statements, and the schema is versioned (schema.MIGRATIONS, PRAGMA user_version).

Every CRUD method takes `tx=`. Without it the call is its own statement(s). Inside
`with store.transaction() as tx:` it must be passed: the lock is not re-entrant, so the same thread calling
the store without it gets StudioStoreError instead of hanging.

Rows come back as dicts: `<x>_json` columns parsed under the key `<x>` (inbox.decision_json is
`decision_data`, because `decision` is its own column), 0/1 flags as bool. Writes take the same keys.

The store enforces what the DB enforces, plus "update_task cannot change status". The code checks named
in §3 (repo lookup, builtin departments, empty messages, dependency cycles) belong to the callers.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from .schema import MIGRATIONS

log = logging.getLogger(__name__)


class StudioStoreError(Exception):
    """The store was used in a way it does not allow (e.g. a call without tx= inside a transaction)."""


class StudioSchemaError(StudioStoreError):
    """The DB cannot be opened by this code (it is newer than MIGRATIONS, or MIGRATIONS is malformed)."""


def now() -> str:
    """Local time to the second, no zone — the format of history._now() (§3.1)."""
    return datetime.now().isoformat(timespec="seconds")


# --- row <-> column conversion ------------------------------------------------------------------------
_BOOL = frozenset({"builtin", "muted", "is_milestone", "upstream_rejected"})
_JSON_KEY = {"decision_json": "decision_data"}  # the one key that is not "suffix removed"
_JSON_COLUMN = {  # key the caller writes -> column
    "default_mcp": "default_mcp_json", "default_models": "default_models_json",
    "mentions": "mentions_json", "attachments": "attachments_json", "meta": "meta_json",
    "stage_models": "stage_models_json", "detail": "detail_json", "payload": "payload_json",
    "decision_data": "decision_json", "settings": "settings_json", "data": "data_json",
}


def _decode(row: sqlite3.Row) -> dict:
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key.endswith("_json"):
            out[_JSON_KEY.get(key, key[:-5])] = None if value is None else json.loads(value)
        elif key in _BOOL:
            out[key] = None if value is None else bool(value)
        else:
            out[key] = value
    return out


def _encode(fields: dict) -> dict:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _JSON_COLUMN:
            out[_JSON_COLUMN[key]] = None if value is None else json.dumps(value, ensure_ascii=False)
        elif key in _BOOL:
            out[key] = None if value is None else int(bool(value))
        else:
            out[key] = value
    return out


# --- what each table accepts --------------------------------------------------------------------------
@dataclass(frozen=True)
class _Table:
    name: str
    create: frozenset[str]            # keys create_* accepts (JSON columns by their caller-side key)
    update: frozenset[str]            # keys update_* may change
    prefix: str = ""                  # id prefix (§3.1); "" = no generated text id
    key: tuple[str, ...] = ("id",)
    stamp: str = "created_at"         # filled with now() on insert
    touch: bool = False               # has updated_at


def _table(name: str, create: str, update: str = "", **kw) -> _Table:
    return _Table(name, frozenset(create.split()), frozenset(update.split()), **kw)


PROJECTS = _table(
    "projects", "name path engine_version mcp_port repo",
    "name path engine_version mcp_port repo archived_at", prefix="prj_", touch=True)
DEPARTMENTS = _table(
    "departments", "name builtin default_relay default_mcp default_models guide_template sort_order",
    "name default_relay default_mcp default_models guide_template sort_order archived_at",
    prefix="dep_", touch=True)
EMPLOYEES = _table(
    "employees", "name department_id project_id status",
    "name department_id project_id status retired_at", prefix="emp_", touch=True)
ROOMS = _table(
    "rooms", "kind title employee_id task_id department_id owner_read_id muted",
    "title owner_read_id muted archived_at", prefix="room_", touch=True)
ROOM_MEMBERS = _table(
    "room_members", "room_id employee_id session_id conversation_id fed_message_id",
    "session_id conversation_id fed_message_id left_at", key=("room_id", "employee_id"), stamp="joined_at")
MESSAGES = _table("messages", "room_id author_kind author_id body mentions attachments meta run_id")
CAMPAIGNS = _table(
    "campaigns", "project_id title goal mode status runner_id",
    "title goal mode status runner_id started_at finished_at", prefix="cmp_", touch=True)
TASKS = _table(
    "tasks",
    "project_id campaign_id parent_id title reason spec done_criteria type status proposer_kind proposer_id"
    " assignee_id is_milestone relay stage_models sort_order",
    # no `status`: only studio/tasks.py changes it, inside a transaction (§4.5)
    "campaign_id parent_id title reason spec done_criteria type assignee_id is_milestone relay stage_models"
    " run_id queued_at exchange_count upstream_rejected sort_order started_at finished_at",
    prefix="task_", touch=True)
TASK_DEPS = _table("task_deps", "task_id depends_on_id", key=("task_id", "depends_on_id"))
TASK_EVENTS = _table(
    "task_events", "task_id actor_kind actor_id kind rule from_status to_status reason detail", stamp="at")
INBOX = _table(
    "inbox", "kind title project_id task_id requester_kind requester_id run_id payload",
    "status title run_id payload decision decision_comment decision_data closed_at reminded_at delivery"
    " delivered_at", prefix="inb_")
DEVICES = _table(
    "devices", "name platform token_hash fcm_token settings",
    "name platform fcm_token settings last_seen_at revoked_at", prefix="dev_")


class Tx:
    """What `transaction()` hands out: statement execution on the open transaction, nothing else."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._open = True
        self.emitted: list[int] = []  # stream_events ids written in this transaction (S-4 pushes them after commit)

    def exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        if not self._open:
            raise StudioStoreError("this transaction has ended")
        return self._conn.execute(sql, params)

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [_decode(r) for r in self.exec(sql, params).fetchall()]

    def row(self, sql: str, params: tuple = ()) -> dict | None:
        r = self.exec(sql, params).fetchone()
        return None if r is None else _decode(r)


class StudioStore:
    def __init__(self, path: Path, migrations: tuple = MIGRATIONS):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # timeout: a second server starting at the same moment waits for the migration lock (§3.2)
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._tx_thread: int | None = None  # thread inside transaction(), to catch a forgotten tx=
        self.migrated: list[int] = []       # versions this instance applied (empty = DB was up to date)
        # called with the stream_events ids of a transaction once it is committed (§4.5 의 2); see transaction()
        self.commit_listeners: list[Callable[[tuple[int, ...]], None]] = []
        try:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate(tuple(migrations))
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        self._guard()
        with self._lock:
            self._conn.close()

    # --- schema version (§3.2) --------------------------------------------------------------------------
    def _user_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self, migrations: tuple) -> None:
        versions = [v for v, _ in migrations]
        if versions != sorted(set(versions)) or (versions and versions[0] < 1):
            raise StudioSchemaError(f"MIGRATIONS versions must ascend from 1: {versions}")
        latest = versions[-1] if versions else 0
        current = self._user_version()
        if current > latest:
            raise StudioSchemaError(
                f"{self.path} is at schema version {current}, this code knows up to {latest}. "
                "Update the server; the studio layer is not started.")
        pending = [(v, statements) for v, statements in migrations if v > current]
        for version, statements in pending:
            if self._apply_step(version, statements):
                self.migrated.append(version)

    def _apply_step(self, version: int, statements) -> bool:
        """One migration step = one transaction. False when another process got there first.

        The first step this instance runs on a non-empty DB copies the file to `<name>.bak-v<version before>`."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._user_version()  # re-read under the write lock
                if current >= version:
                    self._conn.execute("ROLLBACK")
                    return False
                if current >= 1 and not self.migrated:
                    # We hold the write lock and have not written yet, so the file is exactly version `current`
                    # (default rollback journal). Outside the lock a late server would copy the upgraded file.
                    shutil.copy2(self.path, self.path.with_name(f"{self.path.name}.bak-v{current}"))
                for sql in statements:
                    self._conn.execute(sql)
                self._conn.execute(f"PRAGMA user_version = {int(version)}")
                self._conn.execute("COMMIT")
                return True
            except BaseException:
                self._rollback()
                raise

    def _rollback(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")

    # --- execution ----------------------------------------------------------------------------------------
    def _guard(self) -> None:
        if self._tx_thread == threading.get_ident():
            raise StudioStoreError("inside transaction(): pass tx=tx (the store lock is not re-entrant)")

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        """Several statements as one: BEGIN IMMEDIATE under the lock, COMMIT on exit, ROLLBACK on an exception.

        A commit that wrote stream events is then announced: `commit_listeners` get the ids, on this thread
        and outside the lock, so a listener may read the store. Nothing is announced after a rollback. A
        listener that raises is logged and not passed on — the change is committed either way."""
        self._guard()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            tx = Tx(self._conn)
            self._tx_thread = threading.get_ident()
            try:
                yield tx
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
            finally:
                tx._open = False
                self._tx_thread = None
        if tx.emitted:
            ids = tuple(tx.emitted)
            for listener in tuple(self.commit_listeners):
                try:
                    listener(ids)
                except Exception:  # noqa: BLE001 - announcing must not undo or hide a committed change
                    log.exception("studio: a commit listener failed (stream events %s)", ids)

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """One statement, committed on its own."""
        self._guard()
        with self._lock:
            return self._conn.execute(sql, params)

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        self._guard()
        with self._lock:
            return [_decode(r) for r in self._conn.execute(sql, params).fetchall()]

    def _run(self, sql: str, params: tuple, tx: Tx | None) -> sqlite3.Cursor:
        return self._exec(sql, params) if tx is None else tx.exec(sql, params)

    def _all(self, sql: str, params: tuple, tx: Tx | None) -> list[dict]:
        return self._rows(sql, params) if tx is None else tx.rows(sql, params)

    # --- shared CRUD --------------------------------------------------------------------------------------
    def _get(self, t: _Table, key: dict, tx: Tx | None) -> dict | None:
        where = " AND ".join(f"{c} = ?" for c in key)
        rows = self._all(f"SELECT * FROM {t.name} WHERE {where}", tuple(key.values()), tx)
        return rows[0] if rows else None

    def _select(self, t: _Table, filters: dict | None = None, tx: Tx | None = None, *,
                extra: tuple[str, ...] = (), order: str = "rowid", limit: int | None = None) -> list[dict]:
        """`filters`: column -> value ("col >" style keys carry their operator); None values are skipped."""
        conds, args = list(extra), []
        for key, value in (filters or {}).items():
            if value is None:
                continue
            conds.append(f"{key} ?" if " " in key else f"{key} = ?")
            args.append(value)
        sql = f"SELECT * FROM {t.name}"
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += f" ORDER BY {order}"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        return self._all(sql, tuple(args), tx)

    def _insert(self, t: _Table, fields: dict, tx: Tx | None) -> dict:
        """None values are left out, so the column default (or NULL) applies."""
        unknown = set(fields) - t.create - ({"id"} if t.prefix else set())
        if unknown:
            raise ValueError(f"{t.name}: cannot set {sorted(unknown)} on create")
        row = _encode({k: v for k, v in fields.items() if v is not None})
        if t.prefix and "id" not in row:
            row["id"] = t.prefix + uuid.uuid4().hex[:8]
        row[t.stamp] = now()
        if t.touch:
            row["updated_at"] = row[t.stamp]
        marks = ", ".join("?" * len(row))
        cur = self._run(f"INSERT INTO {t.name} ({', '.join(row)}) VALUES ({marks})", tuple(row.values()), tx)
        generated = t.key == ("id",) and not t.prefix  # AUTOINCREMENT log tables
        key = {"id": cur.lastrowid} if generated else {k: row[k] for k in t.key}
        return self._get(t, key, tx)

    def _update(self, t: _Table, key: dict, fields: dict, tx: Tx | None) -> dict | None:
        """The changed row, or None when there is no such row. A None value writes NULL."""
        unknown = set(fields) - t.update
        if unknown:
            raise ValueError(f"{t.name}: cannot change {sorted(unknown)}")
        row = _encode(fields)
        if t.touch:
            row["updated_at"] = now()
        if row:
            sets = ", ".join(f"{c} = ?" for c in row)
            where = " AND ".join(f"{c} = ?" for c in key)
            self._run(f"UPDATE {t.name} SET {sets} WHERE {where}", (*row.values(), *key.values()), tx)
        return self._get(t, key, tx)

    # --- projects -----------------------------------------------------------------------------------------
    def create_project(self, name: str, path: str, repo: str, *, tx: Tx | None = None, **fields) -> dict:
        return self._insert(PROJECTS, dict(fields, name=name, path=path, repo=repo), tx)

    def get_project(self, project_id: str, *, tx: Tx | None = None) -> dict | None:
        return self._get(PROJECTS, {"id": project_id}, tx)

    def list_projects(self, *, include_archived: bool = False, tx: Tx | None = None) -> list[dict]:
        return self._select(PROJECTS, tx=tx, extra=() if include_archived else ("archived_at IS NULL",))

    def update_project(self, project_id: str, *, tx: Tx | None = None, **fields) -> dict | None:
        return self._update(PROJECTS, {"id": project_id}, fields, tx)

    def delete_project(self, project_id: str, *, tx: Tx | None = None) -> bool:
        """Take a project's row out; False when there is none. Nothing that names it goes with it: while an
        employee, a task or a room does, this is `sqlite3.IntegrityError` and the row stays (the cleanup of a
        failed job, M-8 — the one place a project is deleted; everywhere else it is archived)."""
        cur = self._run("DELETE FROM projects WHERE id = ?", (project_id,), tx)
        return cur.rowcount == 1

    # --- departments --------------------------------------------------------------------------------------
    def create_department(self, name: str, *, tx: Tx | None = None, **fields) -> dict:
        return self._insert(DEPARTMENTS, dict(fields, name=name), tx)

    def get_department(self, department_id: str, *, tx: Tx | None = None) -> dict | None:
        return self._get(DEPARTMENTS, {"id": department_id}, tx)

    def list_departments(self, *, include_archived: bool = False, tx: Tx | None = None) -> list[dict]:
        return self._select(DEPARTMENTS, tx=tx, order="sort_order, rowid",
                            extra=() if include_archived else ("archived_at IS NULL",))

    def update_department(self, department_id: str, *, tx: Tx | None = None, **fields) -> dict | None:
        return self._update(DEPARTMENTS, {"id": department_id}, fields, tx)

    # --- employees ----------------------------------------------------------------------------------------
    def create_employee(self, name: str, department_id: str, *, tx: Tx | None = None, **fields) -> dict:
        return self._insert(EMPLOYEES, dict(fields, name=name, department_id=department_id), tx)

    def get_employee(self, employee_id: str, *, tx: Tx | None = None) -> dict | None:
        return self._get(EMPLOYEES, {"id": employee_id}, tx)

    def list_employees(self, *, department_id: str | None = None, project_id: str | None = None,
                       include_retired: bool = False, tx: Tx | None = None) -> list[dict]:
        return self._select(EMPLOYEES, {"department_id": department_id, "project_id": project_id}, tx,
                            extra=() if include_retired else ("retired_at IS NULL",))

    def update_employee(self, employee_id: str, *, tx: Tx | None = None, **fields) -> dict | None:
        return self._update(EMPLOYEES, {"id": employee_id}, fields, tx)

    # --- rooms --------------------------------------------------------------------------------------------
    def create_room(self, kind: str, *, tx: Tx | None = None, **fields) -> dict:
        return self._insert(ROOMS, dict(fields, kind=kind), tx)

    def get_room(self, room_id: str, *, tx: Tx | None = None) -> dict | None:
        return self._get(ROOMS, {"id": room_id}, tx)

    def list_rooms(self, *, kind: str | None = None, employee_id: str | None = None, task_id: str | None = None,
                   department_id: str | None = None, include_archived: bool = False,
                   tx: Tx | None = None) -> list[dict]:
        filters = {"kind": kind, "employee_id": employee_id, "task_id": task_id, "department_id": department_id}
        return self._select(ROOMS, filters, tx, extra=() if include_archived else ("archived_at IS NULL",))

    def update_room(self, room_id: str, *, tx: Tx | None = None, **fields) -> dict | None:
        return self._update(ROOMS, {"id": room_id}, fields, tx)

    # --- room_members (key: room_id + employee_id) --------------------------------------------------------
    def create_room_member(self, room_id: str, employee_id: str, *, tx: Tx | None = None, **fields) -> dict:
        return self._insert(ROOM_MEMBERS, dict(fields, room_id=room_id, employee_id=employee_id), tx)

    def get_room_member(self, room_id: str, employee_id: str, *, tx: Tx | None = None) -> dict | None:
        return self._get(ROOM_MEMBERS, {"room_id": room_id, "employee_id": employee_id}, tx)

    def list_room_members(self, *, room_id: str | None = None, employee_id: str | None = None,
                          include_left: bool = False, tx: Tx | None = None) -> list[dict]:
        return self._select(ROOM_MEMBERS, {"room_id": room_id, "employee_id": employee_id}, tx,
                            extra=() if include_left else ("left_at IS NULL",))

    def update_room_member(self, room_id: str, employee_id: str, *, tx: Tx | None = None,
                           **fields) -> dict | None:
        return self._update(ROOM_MEMBERS, {"room_id": room_id, "employee_id": employee_id}, fields, tx)

    # --- messages (append-only) ---------------------------------------------------------------------------
    def create_message(self, room_id: str, author_kind: str, *, tx: Tx | None = None, **fields) -> dict:
        return self._insert(MESSAGES, dict(fields, room_id=room_id, author_kind=author_kind), tx)

    def get_message(self, message_id: int, *, tx: Tx | None = None) -> dict | None:
        return self._get(MESSAGES, {"id": message_id}, tx)

    def list_messages(self, room_id: str, *, after_id: int | None = None, before_id: int | None = None,
                      limit: int = 50, tx: Tx | None = None) -> list[dict]:
        """Oldest first, same meaning as the REST query (§6.4). `after_id` alone: the first `limit` rows after
        it (0 = from the start). Otherwise the newest `limit` rows: of the room, before `before_id`, or
        between the two."""
        filters = {"room_id": room_id, "id >": after_id, "id <": before_id}
        if after_id is not None and before_id is None:
            return self._select(MESSAGES, filters, tx, order="id", limit=limit)
        return self._select(MESSAGES, filters, tx, order="id DESC", limit=limit)[::-1]

    # --- campaigns ----------------------------------------------------------------------------------------
    def create_campaign(self, project_id: str, title: str, goal: str, *, tx: Tx | None = None,
                        **fields) -> dict:
        return self._insert(CAMPAIGNS, dict(fields, project_id=project_id, title=title, goal=goal), tx)

    def get_campaign(self, campaign_id: str, *, tx: Tx | None = None) -> dict | None:
        return self._get(CAMPAIGNS, {"id": campaign_id}, tx)

    def list_campaigns(self, *, project_id: str | None = None, status: str | None = None,
                       tx: Tx | None = None) -> list[dict]:
        return self._select(CAMPAIGNS, {"project_id": project_id, "status": status}, tx)

    def update_campaign(self, campaign_id: str, *, tx: Tx | None = None, **fields) -> dict | None:
        return self._update(CAMPAIGNS, {"id": campaign_id}, fields, tx)

    # --- tasks --------------------------------------------------------------------------------------------
    def create_task(self, project_id: str, *, title: str, reason: str, spec: str, done_criteria: str,
                    type: str, status: str, proposer_kind: str, tx: Tx | None = None, **fields) -> dict:
        """`status` is required: the column has no default (the first state is C1 `proposed` or C2 `backlog`)."""
        fields.update(project_id=project_id, title=title, reason=reason, spec=spec,
                      done_criteria=done_criteria, type=type, status=status, proposer_kind=proposer_kind)
        return self._insert(TASKS, fields, tx)

    def get_task(self, task_id: str, *, tx: Tx | None = None) -> dict | None:
        return self._get(TASKS, {"id": task_id}, tx)

    def list_tasks(self, *, project_id: str | None = None, campaign_id: str | None = None,
                   status: str | None = None, assignee_id: str | None = None, parent_id: str | None = None,
                   tx: Tx | None = None) -> list[dict]:
        filters = {"project_id": project_id, "campaign_id": campaign_id, "status": status,
                   "assignee_id": assignee_id, "parent_id": parent_id}
        return self._select(TASKS, filters, tx, order="sort_order, rowid")

    def update_task(self, task_id: str, *, tx: Tx | None = None, **fields) -> dict | None:
        if "status" in fields:
            raise ValueError("tasks.status is changed only by the transitions in studio/tasks.py (§4.5)")
        return self._update(TASKS, {"id": task_id}, fields, tx)

    def list_queued_tasks(self, *, tx: Tx | None = None) -> list[dict]:
        """The line of §4.6: every task with `queued_at`, the oldest request first."""
        return self._select(TASKS, None, tx, extra=("queued_at IS NOT NULL",), order="queued_at, id")

    # --- task_deps (the one table whose rows are deleted) -------------------------------------------------
    def create_task_dep(self, task_id: str, depends_on_id: str, *, tx: Tx | None = None) -> dict:
        return self._insert(TASK_DEPS, {"task_id": task_id, "depends_on_id": depends_on_id}, tx)

    def list_task_deps(self, *, task_id: str | None = None, depends_on_id: str | None = None,
                       tx: Tx | None = None) -> list[dict]:
        return self._select(TASK_DEPS, {"task_id": task_id, "depends_on_id": depends_on_id}, tx)

    def delete_task_dep(self, task_id: str, depends_on_id: str, *, tx: Tx | None = None) -> bool:
        cur = self._run("DELETE FROM task_deps WHERE task_id = ? AND depends_on_id = ?",
                        (task_id, depends_on_id), tx)
        return cur.rowcount == 1

    # --- task_events (append-only) ------------------------------------------------------------------------
    def create_task_event(self, task_id: str, actor_kind: str, kind: str, *, tx: Tx | None = None,
                          **fields) -> dict:
        return self._insert(TASK_EVENTS, dict(fields, task_id=task_id, actor_kind=actor_kind, kind=kind), tx)

    def list_task_events(self, task_id: str, *, after_id: int = 0, limit: int | None = None,
                         tx: Tx | None = None) -> list[dict]:
        """Ascending by id: the rows after `after_id`, the first `limit` of them when a limit is given."""
        return self._select(TASK_EVENTS, {"task_id": task_id, "id >": after_id}, tx, order="id", limit=limit)

    # --- inbox --------------------------------------------------------------------------------------------
    def create_inbox_item(self, kind: str, title: str, requester_kind: str, *, tx: Tx | None = None,
                          **fields) -> dict:
        return self._insert(INBOX, dict(fields, kind=kind, title=title, requester_kind=requester_kind), tx)

    def get_inbox_item(self, inbox_id: str, *, tx: Tx | None = None) -> dict | None:
        return self._get(INBOX, {"id": inbox_id}, tx)

    def list_inbox(self, *, status: str | None = None, kind: str | None = None, project_id: str | None = None,
                   task_id: str | None = None, run_id: str | None = None, closed_first: bool = False,
                   before: str | None = None, closed_at: str | None = None, limit: int | None = None,
                   delivery: str | None = None, tx: Tx | None = None) -> list[dict]:
        """Oldest first by `created_at` — how long an item has waited. `closed_first`: the closed items, the
        newest closed first, and `before` is the `closed_at` cursor: the ones closed earlier than it (§6.4).
        `closed_at`: only the ones closed at exactly that time — the rest of the second a page ends in.
        `delivery`: only the items in that state of §3.13 — `pending` is the decision texts still owed (§4.8)."""
        filters = {"status": status, "kind": kind, "project_id": project_id, "task_id": task_id, "run_id": run_id,
                   "closed_at": closed_at, "delivery": delivery}
        if closed_first:
            filters["closed_at <"] = before
            return self._select(INBOX, filters, tx, extra=("closed_at IS NOT NULL",),
                                order="closed_at DESC, rowid DESC", limit=limit)
        return self._select(INBOX, filters, tx, order="created_at, rowid", limit=limit)

    def count_inbox(self, *, status: str | None = None, tx: Tx | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM inbox" + (" WHERE status = ?" if status is not None else "")
        return self._all(sql, (status,) if status is not None else (), tx)[0]["n"]

    def update_inbox_item(self, inbox_id: str, *, tx: Tx | None = None, **fields) -> dict | None:
        return self._update(INBOX, {"id": inbox_id}, fields, tx)

    # --- devices ------------------------------------------------------------------------------------------
    def create_device(self, name: str, token_hash: str, *, tx: Tx | None = None, **fields) -> dict:
        return self._insert(DEVICES, dict(fields, name=name, token_hash=token_hash), tx)

    def get_device(self, device_id: str, *, tx: Tx | None = None) -> dict | None:
        return self._get(DEVICES, {"id": device_id}, tx)

    def get_device_by_token_hash(self, token_hash: str, *, tx: Tx | None = None) -> dict | None:
        return self._get(DEVICES, {"token_hash": token_hash}, tx)

    def list_devices(self, *, include_revoked: bool = False, tx: Tx | None = None) -> list[dict]:
        return self._select(DEVICES, tx=tx, extra=() if include_revoked else ("revoked_at IS NULL",))

    def update_device(self, device_id: str, *, tx: Tx | None = None, **fields) -> dict | None:
        return self._update(DEVICES, {"id": device_id}, fields, tx)

    # --- read models of the REST lists (§6.4) -------------------------------------------------------------
    def count_tasks_by_status(self, project_id: str, *, tx: Tx | None = None) -> dict[str, int]:
        """`task_counts` of a project: every status of §4.1 is a key, 0 included."""
        from .tasks import STATES  # tasks.py owns the list and imports this module, hence the late import

        sql = "SELECT status, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY status"
        found = {r["status"]: r["n"] for r in self._all(sql, (project_id,), tx)}
        return {status: found.get(status, 0) for status in STATES}

    def count_tasks_by_campaign(self, *, tx: Tx | None = None) -> dict[str, dict[str, int]]:
        """Campaign id -> {status: count}, only the statuses that occur (`progress` of §6.4 fills the zeros).
        A campaign without tasks is not a key."""
        sql = ("SELECT campaign_id, status, COUNT(*) AS n FROM tasks WHERE campaign_id IS NOT NULL"
               " GROUP BY campaign_id, status")
        out: dict[str, dict[str, int]] = {}
        for r in self._all(sql, (), tx):
            out.setdefault(r["campaign_id"], {})[r["status"]] = r["n"]
        return out

    def count_unread(self, room_id: str | None = None, *, tx: Tx | None = None) -> dict[str, int]:
        """`unread` per room: messages after rooms.owner_read_id that the owner did not write.
        Rooms with none are left out."""
        sql = ("SELECT m.room_id AS room_id, COUNT(*) AS n FROM messages m JOIN rooms r ON r.id = m.room_id"
               " WHERE m.id > r.owner_read_id AND m.author_kind <> 'owner'")
        args: tuple = ()
        if room_id is not None:
            sql, args = sql + " AND m.room_id = ?", (room_id,)
        return {r["room_id"]: r["n"] for r in self._all(sql + " GROUP BY m.room_id", args, tx)}

    def last_messages(self, room_id: str | None = None, *, tx: Tx | None = None) -> dict[str, dict]:
        """`last_message` per room: the row with the highest id. Rooms without messages are left out."""
        where, args = ("", ()) if room_id is None else (" WHERE room_id = ?", (room_id,))
        sql = f"SELECT * FROM messages WHERE id IN (SELECT MAX(id) FROM messages{where} GROUP BY room_id)"
        return {r["room_id"]: r for r in self._all(sql, args, tx)}

    def set_message_attachments(self, tx: Tx, message_id: int, attachments: list[dict]) -> dict | None:
        """Fill attachments_json of a message created in this same transaction: the paths carry the
        AUTOINCREMENT id, unknown before the INSERT (§6.6). Seen from outside, the message is still written
        once and never changed (§3.8) — which is why this takes the Tx like emit() and has no tx=None form."""
        if not isinstance(tx, Tx):
            raise StudioStoreError("set_message_attachments() belongs inside the transaction that created the message")
        tx.exec("UPDATE messages SET attachments_json = ? WHERE id = ?",
                (json.dumps(attachments, ensure_ascii=False), message_id))
        return self.get_message(message_id, tx=tx)

    # --- stream_events (§7.2) -----------------------------------------------------------------------------
    def emit(self, tx: Tx, type: str, data: dict | None = None, *, project_id: str | None = None,
             room_id: str | None = None, task_id: str | None = None) -> int:
        """Write a stream event in the transaction that changes the state, so both stay or both vanish."""
        if not isinstance(tx, Tx):
            raise StudioStoreError("emit() belongs inside transaction(): pass its tx")
        cur = tx.exec(
            "INSERT INTO stream_events (type, at, project_id, room_id, task_id, data_json) VALUES (?, ?, ?, ?, ?, ?)",
            (type, now(), project_id, room_id, task_id, json.dumps(data or {}, ensure_ascii=False)))
        tx.emitted.append(cur.lastrowid)
        return cur.lastrowid

    def list_stream_events(self, *, after_id: int = 0, limit: int = 500, tx: Tx | None = None) -> list[dict]:
        sql = "SELECT * FROM stream_events WHERE id > ? ORDER BY id LIMIT ?"
        return self._all(sql, (after_id, int(limit)), tx)

    def stream_bounds(self, *, tx: Tx | None = None) -> dict:
        """`{last_id, oldest_id}` in one read: the highest id handed out so far and the lowest id still stored.

        last_id comes from sqlite_sequence, so it does not go back when pruning empties the table. With no
        row stored, oldest_id is last_id + 1: a cursor at last_id has missed nothing (§7.1)."""
        sql = ("SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'stream_events'), 0) AS last_id,"
               " (SELECT MIN(id) FROM stream_events) AS oldest_id")
        bounds = self._all(sql, (), tx)[0]
        if bounds["oldest_id"] is None:
            bounds["oldest_id"] = bounds["last_id"] + 1
        return bounds

    def prune_stream_events(self, *, days: int = 7, max_rows: int = 50_000) -> int:
        """Delete what is past the retention of §7.2 — older than `days`, or beyond the newest `max_rows`,
        whichever is reached first — and return how many rows went.

        Always a prefix: everything up to the last row that is too old. The ids that stay have no hole, so
        `oldest_id` says exactly from where a cursor can still resume."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")  # the format of now()
        sql = ("DELETE FROM stream_events WHERE id <= MAX("
               "COALESCE((SELECT MAX(id) FROM stream_events WHERE at < ?), 0), "
               "COALESCE((SELECT id FROM stream_events ORDER BY id DESC LIMIT 1 OFFSET ?), 0))")
        return self._exec(sql, (cutoff, int(max_rows))).rowcount

    def list_task_runs(self, *, run_id: str | None = None, status: str | None = None,
                       tx: Tx | None = None) -> list[dict]:
        """The tasks that hold a run, each as whose run it is (§7.4 `run.live`): run_id, employee_id (the
        assignee), room_id (the task room), task_id, project_id."""
        sql = ("SELECT t.run_id AS run_id, t.assignee_id AS employee_id, r.id AS room_id, t.id AS task_id,"
               " t.project_id AS project_id FROM tasks t LEFT JOIN rooms r ON r.task_id = t.id"
               " WHERE t.run_id IS NOT NULL")
        args: tuple = ()
        if run_id is not None:
            sql, args = sql + " AND t.run_id = ?", args + (run_id,)
        if status is not None:
            sql, args = sql + " AND t.status = ?", args + (status,)
        return self._all(sql + " ORDER BY t.rowid", args, tx)
