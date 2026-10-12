"""Staffing (T4): the triggers that make HR ask whether to recommend a hire — found by rule, no model is called.

Contract: design/studio/ARCHITECTURE.md §5.3 (the kind `hire_recommendation`) · §5.9 (the quiet push and the
daily report's sweep) · §3.2 (`hire_signals`). A trigger is one of

- `no_staff`      a task is filed or proposed for a part with nobody in service;
- `no_placement`  the part has staff but nobody placed in the task's project;
- `backlog`       one employee has more waiting (`backlog`) tasks than `studio_hire_backlog_limit`;
- `request`       an employee reported a need (`Staffing.request`; the tool that calls it is T5's).

The key of a trigger is the part and the project: `<department_id | new:<part name>>:<project_id | ->`. A part
with no staff by nature (art) is a trigger only when a task points at it. How a task points at a part is read
without a column of its own (`part_of`): `department_id` in its proposal's payload, else the part of its
assignee (retired ones count), else the part of the employee the proposal suggests.

The same key is not put to HR twice (`gate`): not while a recommendation of that key waits, not for
`studio_hire_cooldown_days` after one was skipped, and HR is asked about a key once in that time (`hire_signals`).
What passes is handed to `ask(signal)` — `inbox.Hooks.staffing_ask`; the turn that judges is T5's. If `ask` says
False (nobody to ask) or raises, the record of the question is taken back (`unheard`). A sweep reads a task's
proposal whatever its state (`proposal_of`), as `request` does.

Nothing here raises into its caller: a look that fails is logged (`Staffing.look`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from .store import StudioStore

log = logging.getLogger(__name__)

TRIGGERS = ("no_staff", "no_placement", "backlog", "request")
VIAS = ("task", "report", "request")
HR = "hr"
KIND = "hire_recommendation"
OPEN_STATUSES = ("proposed", "backlog")  # the tasks a sweep looks at: nobody has begun them


def key_of(department_id: str | None = None, project_id: str | None = None, *, part_name: str | None = None) -> str:
    """`<part>:<project>` — the part is a department id, or `new:<name>` for a part not made yet."""
    part = department_id if department_id else f"new:{(part_name or '').strip()}"
    return f"{part}:{project_id or '-'}"


def keys_of(payload: dict) -> set[str]:
    """The keys a recommendation answers to: its own, and the trigger's when it names one (`signal_key`)."""
    return {value for value in (payload.get("key"), payload.get("signal_key")) if isinstance(value, str) and value}


def since(days: float, clock: Callable[[], datetime] = datetime.now) -> str | None:
    """The oldest timestamp that is still inside the cooldown (the format of `store.now()`); None = no wait."""
    if not days or days <= 0:
        return None
    return (clock() - timedelta(days=days)).isoformat(timespec="seconds")


def held(rows: list[dict], keys: set[str], limit: str | None) -> tuple[str, dict] | None:
    """Why a recommendation of one of `keys` cannot be raised now, from the `hire_recommendation` rows: `open`
    (a pending one) or `skipped` (one skipped at `limit` or later). None = nothing stands in the way."""
    for row in rows:
        if keys_of(row["payload"] or {}) & keys:
            if row["status"] == "pending":
                return "open", row
    for row in rows:
        if keys_of(row["payload"] or {}) & keys and row["status"] == "decided" and row["decision"] == "skip":
            if limit is not None and (row["closed_at"] or "") >= limit:
                return "skipped", row
    return None


def part_of(store: StudioStore, task: dict, item: dict | None = None) -> str | None:
    """The part a task is for: `department_id` of its proposal's payload, else the part of its assignee, else of
    the employee the proposal suggests. None when it says none (then no trigger comes of the task). A part that
    is gone or archived is none."""
    payload = (item or {}).get("payload") or {}

    def usable(department_id: Any) -> str | None:
        department = store.get_department(department_id) if isinstance(department_id, str) and department_id else None
        return department_id if department is not None and department["archived_at"] is None else None

    if isinstance(payload.get("department_id"), str) and payload["department_id"]:
        return usable(payload["department_id"])  # a part named is the answer, usable or not
    for employee_id in (task.get("assignee_id"), payload.get("suggested_assignee_id")):
        employee = store.get_employee(employee_id) if isinstance(employee_id, str) and employee_id else None
        if employee is not None:
            return usable(employee["department_id"])
    return None


def proposal_of(store: StudioStore, task_id: str) -> dict | None:
    """The `task_proposal` item of a task, whatever its state: the pending one, else the latest. A proposal that
    was approved (the task is `backlog` now) still says which part the task is for. None = it has none."""
    rows = store.list_inbox(task_id=task_id, kind="task_proposal")  # oldest first
    pending = [row for row in rows if row["status"] == "pending"]
    return pending[0] if pending else (rows[-1] if rows else None)


TRIGGER_TEXT = {
    "no_staff": "맡을 파트에 재직 직원이 없는데 작업이 등록·제안됐다",
    "no_placement": "맡을 파트에 직원은 있지만 그 프로젝트에 배치된 직원이 없다",
    "backlog": "한 직원의 대기(backlog) 작업이 한도를 넘었다",
    "request": "직원이 도움이 필요하다고 알렸다",
}
BRIEF_TASKS = 10  # the tasks a brief lists by name


def brief(store: StudioStore, signal: dict) -> str:
    """The text HR's judging turn gets (T5 · §5.3 '판단 요청'): what the trigger is, how the staff stands now,
    what waits, and the instruction. HR may raise a recommendation with `hire_recommend(signal_key=<key>)` or
    raise nothing; either way the turn ends with an empty output. Read from the store at the time of asking."""
    department_id, project_id = signal.get("department_id"), signal.get("project_id")
    department = store.get_department(department_id) if department_id else None
    project = store.get_project(project_id) if project_id else None
    lines = [f"[채용 계기 · {signal['trigger']}] {TRIGGER_TEXT.get(signal['trigger'], signal['trigger'])}.",
             f"- key: `{signal['key']}`",
             f"- 파트: {department['name'] + ' ' if department else ''}(id `{department_id}`)",
             f"- 프로젝트: {project['name'] + ' ' if project else ''}(id `{project_id}`)",
             f"- 알림 경로: {signal.get('via', '')}"]
    if signal.get("numbers"):
        lines.append("- 수치: " + ", ".join(f"{k}={v}" for k, v in signal["numbers"].items()))
    if signal.get("employee_id"):
        who = store.get_employee(signal["employee_id"])
        lines.append(f"- 요청한 직원: {who['name'] if who else ''} (id `{signal['employee_id']}`)")
    if signal.get("reason"):
        lines.append(f"- 사유: {signal['reason']}")
    if signal.get("role"):
        lines.append(f"- 필요한 역할: {signal['role']}")
    ids = list(signal.get("task_ids") or [])
    if ids:
        lines.append(f"- 관련 작업 {len(ids)}개:")
        for task_id in ids[:BRIEF_TASKS]:
            task = store.get_task(task_id)
            lines.append(f"  - `{task_id}` " + (f"{task['title']} [{task['status']}]" if task else "(없는 작업)"))
        if len(ids) > BRIEF_TASKS:
            lines.append(f"  - … 외 {len(ids) - BRIEF_TASKS}개")
    lines.append("지금의 직원·파트:")
    for part in store.list_departments():
        staff = store.list_employees(department_id=part["id"])
        placed = sum(1 for e in staff if project_id and e["project_id"] == project_id)
        lines.append(f"- {part['name']} (id `{part['id']}`): 재직 {len(staff)}명"
                     + (f", 이 프로젝트 배치 {placed}명" if project_id else ""))
    waiting: dict[str, int] = {}
    for status in OPEN_STATUSES:
        for task in store.list_tasks(status=status):
            owner = store.get_employee(task["assignee_id"]) if task.get("assignee_id") else None
            name = f"{status}/{owner['name'] if owner else '담당 없음'}"
            waiting[name] = waiting.get(name, 0) + 1
    lines.append("대기 작업(상태/담당별): " + (", ".join(f"{k} {v}건" for k, v in sorted(waiting.items())) or "없다"))
    lines += ["",
              "이것은 owner 의 글이 아니라 서버가 올린 판단 요청이다. 위 직원·파트·작업을 보고 채용이 필요한지 판단하라. "
              f"필요하면 `hire_recommend(..., signal_key=\"{signal['key']}\")` 로 권장을 올린다. 필요 없으면 아무것도 "
              "올리지 마라. 어느 쪽이든 output 은 비우고 owner 에게 되묻지 않는다. 권장은 결재로 owner 에게 가고, "
              "owner 가 면담을 열면 그때 이 방에서 채용 면담을 잇는다."]
    return "\n".join(lines)


class Staffing:
    """The triggers. One per request is fine: the only state is in the DB. `ask(signal)` is what HR is asked
    through (`Hooks.staffing_ask`)."""

    def __init__(self, store: StudioStore, *, ask: Callable[[dict], bool | None] | None = None,
                 backlog_limit: int = 5, cooldown_days: float = 3, clock: Callable[[], datetime] = datetime.now):
        self.store = store
        self.ask = ask if ask is not None else (lambda signal: False)  # nobody to ask: nothing is recorded
        self.backlog_limit = backlog_limit
        self.cooldown_days = cooldown_days
        self.clock = clock

    # --- the gate ------------------------------------------------------------------------------------------
    def gate(self, signal: dict) -> tuple[bool, str]:
        """Let a trigger through to HR once: (asked, why). `why` is `asked`, `open` (a recommendation of the key
        waits), `skipped` (one was skipped within the cooldown), `recently` (HR was asked within it) or `unheard`
        (`ask` returned False — nobody to ask — or raised: the record of the question is taken back, so the key
        is not held for the cooldown). The check and the record are one transaction; `ask` is called after it,
        outside. A True or None from `ask` is a question put."""
        key = signal["key"]
        limit = since(self.cooldown_days, self.clock)
        stamp = self.clock().isoformat(timespec="seconds")
        with self.store.transaction() as tx:
            rows = self.store.list_inbox(kind=KIND, tx=tx)
            blocked = held(rows, {key}, limit)
            if blocked:
                return False, blocked[0]
            last = tx.row("SELECT department_id, project_id, trigger, asked_at FROM hire_signals WHERE key = ?", (key,))
            if last is not None and limit is not None and last["asked_at"] >= limit:
                return False, "recently"
            tx.exec("INSERT INTO hire_signals (key, department_id, project_id, trigger, asked_at)"
                    " VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET department_id = excluded.department_id,"
                    " project_id = excluded.project_id, trigger = excluded.trigger, asked_at = excluded.asked_at",
                    (key, signal.get("department_id"), signal.get("project_id"), signal["trigger"], stamp))
        try:
            heard = self.ask(signal)
        except Exception:
            self._forget(key, stamp, last)
            raise
        if heard is False:
            self._forget(key, stamp, last)
            return False, "unheard"
        return True, "asked"

    def _forget(self, key: str, stamp: str, last: Any) -> None:
        """Take back the record of a question nobody heard: the row written at `stamp` goes, and the older one
        it replaced (`last`) comes back. A row someone else wrote since (another `asked_at`) is left alone."""
        with self.store.transaction() as tx:
            if last is None:
                tx.exec("DELETE FROM hire_signals WHERE key = ? AND asked_at = ?", (key, stamp))
            else:
                tx.exec("UPDATE hire_signals SET department_id = ?, project_id = ?, trigger = ?, asked_at = ?"
                        " WHERE key = ? AND asked_at = ?",
                        (last["department_id"], last["project_id"], last["trigger"], last["asked_at"], key, stamp))

    # --- (가) · (나): a task is filed or proposed ------------------------------------------------------------
    def on_task(self, task: dict, item: dict | None = None, *, via: str = "task") -> list[dict]:
        """The triggers a task brings; the ones the gate let through, as the signals HR was asked with."""
        asked: list[dict] = []
        for signal in self._signals(task, item, via):
            if self.gate(signal)[0]:
                asked.append(signal)
        return asked

    def _signals(self, task: dict, item: dict | None, via: str) -> list[dict]:
        signals: list[dict] = []
        part = part_of(self.store, task, item)
        if part is not None and part != HR:
            staff = self.store.list_employees(department_id=part)
            project_id = task["project_id"]
            if not staff:
                signals.append(self._signal("no_staff", via, part, project_id, [task["id"]], {"staff": 0}))
            elif not any(e["project_id"] == project_id for e in staff):
                signals.append(self._signal("no_placement", via, part, project_id, [task["id"]],
                                            {"staff": len(staff), "placed": 0}))
        employee = self.store.get_employee(task["assignee_id"]) if task.get("assignee_id") else None
        if (employee is not None and employee["retired_at"] is None and employee["department_id"] != HR
                and self.backlog_limit > 0):
            waiting = self.store.list_tasks(assignee_id=employee["id"], status="backlog")
            if len(waiting) > self.backlog_limit:
                signals.append(self._signal(
                    "backlog", via, employee["department_id"], employee["project_id"] or task["project_id"],
                    [t["id"] for t in waiting], {"waiting": len(waiting), "limit": self.backlog_limit},
                    employee_id=employee["id"]))
        return signals

    @staticmethod
    def _signal(trigger: str, via: str, department_id: str, project_id: str | None, task_ids: list[str],
                numbers: dict, **extra: Any) -> dict:
        return {"trigger": trigger, "via": via, "key": key_of(department_id, project_id),
                "department_id": department_id, "project_id": project_id, "task_ids": task_ids,
                "numbers": numbers, **extra}

    # --- (다): an employee reported a need -----------------------------------------------------------------
    def request(self, employee_id: str, *, reason: str, department_id: str | None = None,
                project_id: str | None = None, task_id: str | None = None, role: str = "") -> dict:
        """What an employee's request for help lands on: `{asked, key, why}`. Only an employee in service is
        heard. The part is `department_id`, else the part of the task, else the employee's own; the project is
        `project_id`, else the task's, else the employee's. `why` is the gate's, or `invalid` (and the key is
        None) when the employee is not in service, the reason is empty, or there is no part or project to name."""
        employee = self.store.get_employee(employee_id)
        text = reason.strip() if isinstance(reason, str) else ""
        if employee is None or employee["retired_at"] is not None or not text:
            return {"asked": False, "key": None, "why": "invalid"}
        task = self.store.get_task(task_id) if task_id else None
        part = department_id or employee["department_id"]
        if not department_id and task:
            part = part_of(self.store, task, proposal_of(self.store, task["id"])) or part
        department = self.store.get_department(part) if part else None
        project = project_id or (task["project_id"] if task else None) or employee["project_id"]
        if (department is None or department["archived_at"] is not None or part == HR
                or not project or self.store.get_project(project) is None):
            return {"asked": False, "key": None, "why": "invalid"}
        signal = self._signal("request", "request", part, project, [task["id"]] if task else [],
                              {"staff": len(self.store.list_employees(department_id=part))},
                              employee_id=employee_id, reason=text, role=(role or "").strip())
        asked, why = self.gate(signal)
        return {"asked": asked, "key": signal["key"], "why": why}

    # --- (라): the daily report's sweep ----------------------------------------------------------------------
    def sweep(self) -> list[dict]:
        """Every task nobody has begun, once: the triggers they bring. A task's proposal is read whatever its
        state (`proposal_of`), so a proposed task and an approved one (`backlog`) bring the same. The gate keeps
        each key to one question, so many tasks of one part cost one."""
        asked: list[dict] = []
        for status in OPEN_STATUSES:
            for task in self.store.list_tasks(status=status):
                try:
                    item = proposal_of(self.store, task["id"])  # whatever its state: an approved one names the part too
                    asked += self.on_task(task, item, via="report")
                except Exception:  # noqa: BLE001 - one task that cannot be read does not stop the others
                    log.exception("studio: 작업 %s 의 채용 계기를 보지 못했습니다", task["id"])
        return asked

    def look(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call `fn` and swallow what it raises (logged): a look at the staffing must not stop the task that
        was filed, nor the report."""
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("studio: 채용 계기를 보는 중 실패했습니다")
            return None
