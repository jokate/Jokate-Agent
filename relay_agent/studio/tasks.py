"""Task and campaign state machine (S-2): the only code that changes tasks.status.

Contract: design/studio/ARCHITECTURE.md §4.1–4.5 (states, C1 · C2, T1–T11, F0–F10, A1–A4, errors, order of
execution), §4.9 (employee status), §4.10 (campaign status; its automatic progress is campaigns.py).

One transaction per creation / transition holds: the guarded UPDATE of the task, its task_events row (who ·
when · why), the inbox item that is 1:1 with proposed · blocked · review (created, decided or cancelled),
the task room (C2 · T1 open it, T3 seats the assignee), the assignee's derived status, and the stream events
in the order of the §4.2 WS column.

Not here: runs, the queue, the harness gate, the editor check, apply / rollback / commit, pushes. Those are
`Effects` — S-8 · S-11 · S-12 · M-7 fill it in. This module only fixes when each hook runs (§4.5 "실행 순서"):
check -> create_run (T3) or before (T7 · T9 · T11) -> the transaction -> after.

Decision values, payload shapes and the REST / MCP mapping belong to S-5 · S-3 · S-6. They call
`transition()` instead of writing inbox.status themselves, so a task and its item always move together.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .store import StudioStore, Tx, now

log = logging.getLogger(__name__)

# --- identifiers (§1.5, §3.12, §5.2) ------------------------------------------------------------------
STATES = ("proposed", "backlog", "working", "blocked", "review", "done", "rejected", "cancelled")
TERMINAL = ("done", "rejected", "cancelled")
TASK_TYPES = ("feature", "fix", "chore", "doc", "data")
ACTOR_KINDS = ("owner", "employee", "system")
EVENT_KINDS = ("created", "transition", "assigned", "queued", "unqueued", "progress", "edited", "deps", "run",
               "limit", "flag", "guard")
RECORD_KINDS = ("progress", "edited", "deps", "run", "limit", "flag", "guard")  # record(): a row, no columns
BLOCKING_KINDS = ("design_approval", "structure_choice", "question", "pre_approval", "budget", "risky_command")
DECISIONS = {  # §5.2, the kinds bound to a task
    "task_proposal": ("approve", "reject"),
    "design_approval": ("approve", "revise"),
    "structure_choice": ("choose", "revise"),
    "question": ("answer",),
    "pre_approval": ("approve", "reject", "revise"),
    "budget": ("approve", "reject"),
    "risky_command": ("approve", "reject"),
    "final_review": ("pass", "rework", "revert"),
}
CODES = ("illegal_transition", "actor_not_allowed", "precondition_failed", "missing_fields", "stale_state")

# --- the 64 cells (§4.2, §4.4, §4.5) ------------------------------------------------------------------
CREATE = {"proposed": "C1", "backlog": "C2"}
ALLOWED = {
    ("proposed", "backlog"): "T1", ("proposed", "rejected"): "T2", ("backlog", "working"): "T3",
    ("working", "blocked"): "T4", ("blocked", "working"): "T5", ("working", "review"): "T6",
    ("review", "done"): "T7", ("review", "working"): "T8", ("review", "backlog"): "T9",
    ("backlog", "cancelled"): "T10", ("blocked", "cancelled"): "T11",
}


def _forbidden(frm: str, to: str) -> str:
    """The F number of a pair that is not in ALLOWED — the rules of the §4.5 table, in its order."""
    if frm == to:
        return "F0"
    if frm in TERMINAL:
        return "F9"
    if frm == "proposed" and to in ("working", "blocked", "review", "done"):
        return "F1"
    if frm == "backlog" and to in ("blocked", "review", "done"):
        return "F2"
    if to == "done":
        return "F3"   # working · blocked
    if frm == "blocked" and to == "review":
        return "F4"
    if to == "rejected":
        return "F5"   # backlog · working · blocked · review
    if to == "proposed":
        return "F6"
    if to == "backlog":
        return "F7"   # working · blocked
    if frm == "review" and to == "blocked":
        return "F8"
    if to == "cancelled":
        return "F10"  # proposed · working · review
    raise AssertionError(f"unclassified pair {frm} -> {to}")


FORBIDDEN = {(f, t): _forbidden(f, t) for f in STATES for t in STATES if (f, t) not in ALLOWED}
assert len(ALLOWED) == 11 and len(FORBIDDEN) == 53, "§4.4: 11 allowed cells, 53 forbidden"

FORBIDDEN_WHY = {
    "F0": "같은 상태로 가는 것은 전이가 아니다",
    "F1": "직원이 등록한 작업은 owner 의 승인을 거친다",
    "F2": "런 없이 건너뛸 수 없다",
    "F3": "자동 완료 금지. 반드시 review 를 거친다",
    "F4": "결정을 받고 working 으로 돌아온 뒤에 검수를 요청한다",
    "F5": "rejected 는 제안 거절 전용이다. 승인된 작업을 그만두는 것은 취소(cancelled)다",
    "F6": "승인을 무를 수 없다",
    "F7": "backlog 로 돌리는 길은 검수의 되돌리기(T9)뿐이다",
    "F8": "review 에는 final_review 가 pending 이라 차단 결재를 만들 수 없다",
    "F9": "종결된 작업이다. 이어서 할 일은 부모를 연결한 새 작업으로 만든다",
    "F10": "취소는 런이 돌지 않는 backlog · blocked 에서만 받는다",
}

RULE_ACTORS = {  # the "누가" column of §4.2. Anyone else is actor_not_allowed (A1–A4)
    "C1": ("employee",), "C2": ("owner",),
    "T1": ("owner",), "T2": ("owner",), "T3": ("system",), "T4": ("employee", "system"), "T5": ("owner",),
    "T6": ("employee",), "T7": ("owner",), "T8": ("owner",), "T9": ("owner",), "T10": ("owner",),
    "T11": ("owner",),
}
T4_KINDS = {  # §4.3 T4: which blocking kind each actor may file
    "employee": ("question", "structure_choice", "pre_approval"),
    "system": ("question", "risky_command", "budget", "design_approval"),
}
STATE_INBOX = {"proposed": ("task_proposal",), "blocked": BLOCKING_KINDS, "review": ("final_review",)}  # §4.1
CREATES = ("T4", "T6")                               # the transition files the item
DECIDES = ("T1", "T2", "T5", "T7", "T8", "T9")       # the transition is the owner's decision of the item
RULE_DECISION = {"T1": "approve", "T2": "reject", "T7": "pass", "T8": "rework", "T9": "revert"}  # T5: any value
PRE_EFFECT = ("T7", "T9", "T11")                     # run cleanup · apply · rollback come before step 1 (§4.5)
INBOX_EVENT = {"T1": "inbox.decided", "T2": "inbox.decided", "T4": "inbox.created", "T5": "inbox.decided",
               "T6": "inbox.created", "T7": "inbox.decided", "T8": "inbox.decided", "T9": "inbox.decided",
               "T11": "inbox.cancelled"}
TEXT_FIELDS = ("title", "reason", "spec", "done_criteria")
EDITABLE = TEXT_FIELDS + ("type", "is_milestone", "campaign_id", "assignee_id")  # T1 edits (§5.2), no depends_on
BACKLOG_FIELDS = TEXT_FIELDS + ("type", "relay", "campaign_id")  # edit(): only in backlog, out of the line (§6.4)
OPEN_FIELDS = ("is_milestone", "sort_order")                     # edit(): until the task is closed
DEP_STATES = ("proposed", "backlog")                             # task_deps rows go in and out here only (§3.11)

DEFAULT_REASON = {  # no task_events row is left without a "why"
    "T1": "제안을 승인했다", "T2": "제안을 거절했다", "T3": "슬롯을 얻어 시작했다", "T4": "결재를 올렸다",
    "T5": "결재를 결정했다", "T6": "검수를 요청했다", "T7": "검수를 통과시켰다", "T8": "재작업을 요청했다",
    "T9": "변경을 되돌렸다", "T10": "작업을 취소했다", "T11": "작업을 취소했다",
    "queued": "시작을 요청했다", "unqueued": "줄에서 뺐다", "assigned": "담당을 바꿨다",
    "progress": "진행 상황", "edited": "작업을 고쳤다", "deps": "선행 관계를 바꿨다", "run": "런 상태가 바뀌었다",
    "limit": "한도에 닿았다", "flag": "선행 작업이 반려됐다", "guard": "가드 훅이 명령을 거부했다",
}

# --- campaigns (§4.10) --------------------------------------------------------------------------------
CAMPAIGN_STATES = ("draft", "running", "paused", "done", "cancelled")
CAMPAIGN_ALLOWED = {  # (from, to) -> who
    ("draft", "running"): "owner", ("running", "paused"): "owner", ("paused", "running"): "owner",
    ("running", "done"): "system",
    ("draft", "cancelled"): "owner", ("running", "cancelled"): "owner", ("paused", "cancelled"): "owner",
}
CAMPAIGN_MODES = ("nonstop", "milestone", "each")  # campaigns.mode (§3.9); what each fills is `_deps_filled`
CAMPAIGN_FIELDS = ("title", "goal", "mode")        # edit_campaign(): until the campaign has ended
CAMPAIGN_ENDED = ("done", "cancelled")
STARTED = ("working", "blocked", "review")         # "이미 시작된" 뒤 작업: what T8 · T9 mark (§4.3 T8, §5.9)


# What `Effects.after` hears in place of 'campaign' when a campaign has just gone to `running` (started or
# resumed): the look that follows says its notices anew (§5.9). An edit and every other status say 'campaign'.
CAMPAIGN_STARTED = "campaign_started"
CAMPAIGN_REASON = "캠페인 자동 진행"  # the `reason` of the `queued` event the system leaves (§4.3 T3 의 계기)
# the `reason` of the `unqueued` event the system leaves when it takes a task out of the line of a campaign that
# goes to that status (§4.10 '멈춤'). `campaigns._stood` knows these rows by it
CAMPAIGN_HELD = {"paused": "캠페인을 일시정지해 줄에서 뺐다", "cancelled": "캠페인을 취소해 줄에서 뺐다"}


def campaign_queued(events: list[dict]) -> bool:
    """Is the task's place in the line one the automatic progress of its campaign took (the newest `queued` row
    is the system's, with CAMPAIGN_REASON)? A working task the system put in the line again to go on with its
    run has another reason, and a start the owner asked for is the owner's."""
    asked = [event for event in events if event["kind"] == "queued"]
    return bool(asked) and asked[-1]["actor_kind"] == "system" and asked[-1]["reason"] == CAMPAIGN_REASON


def classify(from_status: str, to_status: str) -> str:
    """The cell of the §4.4 table: T1–T11 (allowed) or F0–F10 (forbidden)."""
    pair = (from_status, to_status)
    if pair in ALLOWED:
        return ALLOWED[pair]
    if pair not in FORBIDDEN:
        raise ValueError(f"not task states: {from_status!r} -> {to_status!r}")
    return FORBIDDEN[pair]


def _blank(value: Any) -> bool:
    return not (isinstance(value, str) and value.strip())


def _why(*candidates: Any) -> str:
    return next(c.strip() for c in candidates if not _blank(c))


def _one_line(text: str | None) -> str:
    """inbox.title is one line of at most 80 characters (§5.1)."""
    lines = (text or "").strip().splitlines()
    return lines[0].strip()[:80] if lines else ""


# --- who --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Actor:
    """The "누가" of §1.3: owner, an employee (with its id) or the server itself."""
    kind: str
    id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ACTOR_KINDS:
            raise ValueError(f"actor kind must be one of {ACTOR_KINDS}: {self.kind!r}")
        if (self.kind == "employee") != (self.id is not None):
            raise ValueError("only an employee actor carries an id")

    @classmethod
    def employee(cls, employee_id: str) -> "Actor":
        return cls("employee", employee_id)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id}


OWNER = Actor("owner")
SYSTEM = Actor("system")


# --- errors -----------------------------------------------------------------------------------------------
class TransitionError(Exception):
    """A refused creation or transition (§4.5). `code` is one of CODES; as_dict() is the §6.3 body."""

    def __init__(self, code: str, detail: str, *, from_status: str | None = None, to_status: str | None = None,
                 rule: str | None = None, failed: list[str] | None = None, fields: list[str] | None = None):
        if code not in CODES:
            raise ValueError(f"TransitionError code must be one of {CODES}: {code!r}")
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.from_status = from_status
        self.to_status = to_status
        self.rule = rule                    # T1–T11 · C1 · C2, or the F number for illegal_transition
        self.failed = list(failed or [])    # precondition_failed: snake_case codes; [] when no code is defined
        self.fields = list(fields or [])    # missing_fields: the empty field names

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"detail": self.detail, "code": self.code}
        if self.code == "illegal_transition":
            out["from"], out["to"] = self.from_status, self.to_status
        elif self.code == "precondition_failed":
            out["failed"] = list(self.failed)
        elif self.code == "missing_fields":
            out["fields"] = list(self.fields)
        return out


class TaskNotFound(LookupError):
    """No task with that id (the callers' `not_found`; not a TransitionError code)."""


class CampaignNotFound(LookupError):
    """No campaign with that id."""


# --- what is injected -------------------------------------------------------------------------------------
class Effects:
    """Everything a transition does outside the studio DB. The default does nothing (and cannot make a run).

    `rule` is C1 · C2 · T1–T11. A hook that refuses raises TransitionError('precondition_failed',
    failed=[codes]); the codes reach the caller as they are."""

    def create_run(self, task: dict) -> str:
        """T3: make the task run (still pending) and return its id. Raising leaves the task in backlog."""
        raise RuntimeError("no run factory is wired (S-8)")

    def cancel_run(self, run_id: str) -> None:
        """T3: the transaction failed after create_run — close the run that was made (§4.5)."""

    def check(self, rule: str, task: dict) -> None:
        """A gate without side effects, after the DB conditions: harness gate (T6), "the run has stopped" and
        "the changes can be reverted" (T7 · T9 · T11), "already applied" (T8)."""

    def check_start(self, task: dict, stage_models: dict | None = None) -> list[dict]:
        """A `start` request that asks to be looked at (`request_start(vet=True)`, §9.5 '두 번 본다'): what
        stands in the way of this task's run with that override — `[{code, stage, message}]`, [] when nothing
        does. `stage_models` None: the override the task keeps. It never raises."""
        return []

    def before(self, rule: str, task: dict) -> str | None:
        """T7 · T9 · T11: run cleanup, then apply + commit (T7) or rollback (T9 · T11). Raising = no transition.
        A text it returns — what it could not do while the transition goes on all the same (a run the engine
        has no more is not reverted) — is put after the `reason` of the transition row."""

    def staffing(self, task: dict, item: dict | None) -> None:
        """A task has just been filed (C1 with its `task_proposal` item, C2 without): look at whether it brings a
        reason to ask HR for a hire (studio/staffing.py, §5.3). Called before `after`; an exception is logged and
        the task stands. The default does nothing."""

    def after(self, what: str, row: dict, event_ids: tuple[int, ...]) -> None:
        """After the commit: push the stream events, start / resume the run, send the push. `what` is the rule,
        the task_events kind of a non-transition change, 'campaign', or CAMPAIGN_STARTED for a campaign that has
        just gone to `running`. The change stands even if this raises:
        the exception reaches the caller as it is, and the run T3 made is not closed (cancel_run is for a failed
        commit only) — the implementation (S-8) catches and records what must not be lost.
        What `transition(committed=)` was given has run by then: a decision text is in the run before this
        resumes it (§5.5), so the implementation only approves and advances."""


@dataclass(frozen=True)
class Outcome:
    task: dict
    rule: str | None                    # C1 · C2 · T1–T11; None for a non-transition change
    event: dict                         # the task_events row of this call (for T*, the `transition` row)
    inbox: dict | None = None           # the item this call created, decided or cancelled
    room: dict | None = None            # the task room this call opened or seated the assignee in
    stream_ids: tuple[int, ...] = ()    # stream_events written, in order


class TaskMachine:
    def __init__(self, store: StudioStore, effects: Effects | None = None,
                 deps_ready: Callable[[dict], bool] | None = None):
        self.store = store
        self.effects = effects if effects is not None else Effects()
        self.deps_ready = deps_ready if deps_ready is not None else self._deps_filled

    # --- reads ----------------------------------------------------------------------------------------------
    def _task(self, task_id: str, tx: Tx | None = None) -> dict:
        task = self.store.get_task(task_id, tx=tx)
        if task is None:
            raise TaskNotFound(f"task {task_id} not found")
        return task

    def _employed(self, employee_id: str | None) -> dict | None:
        employee = self.store.get_employee(employee_id) if employee_id else None
        return employee if employee is not None and employee["retired_at"] is None else None

    def _pending_inbox(self, task_id: str) -> dict | None:
        items = self.store.list_inbox(task_id=task_id, status="pending")
        return items[0] if items else None  # inbox_one_pending_per_task

    def _room(self, task_id: str, tx: Tx | None = None) -> dict | None:
        rooms = self.store.list_rooms(kind="task", task_id=task_id, include_archived=True, tx=tx)
        return rooms[0] if rooms else None

    def _deps_filled(self, task: dict) -> bool:
        """§4.6 "선행 조건": what counts as filled follows the campaign's mode (no campaign = `each`)."""
        campaign = self.store.get_campaign(task["campaign_id"]) if task["campaign_id"] else None
        mode = campaign["mode"] if campaign else "each"
        for dep in self.store.list_task_deps(task_id=task["id"]):
            before = self._task(dep["depends_on_id"])
            strict = mode == "each" or (mode == "milestone" and before["is_milestone"])
            if before["status"] not in (("done",) if strict else ("review", "done")):
                return False
        return True

    def _waits_for(self, tx: Tx, start: str, target: str) -> bool:
        """Does `start` wait for `target`, directly or through other tasks (the rows of task_deps)?"""
        seen, todo = set(), [start]
        while todo:
            current = todo.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            todo.extend(d["depends_on_id"] for d in self.store.list_task_deps(task_id=current, tx=tx))
        return False

    def _dep_problems(self, tx: Tx, task_id: str | None, project_id: str, ids: list[str]) -> list[str]:
        """§3.11, in the transaction that writes the rows: each predecessor exists, is of the same project,
        and putting it in closes no cycle. `task_id` is None for a task being created — nothing waits for it
        yet, so it cannot close one."""
        problems = []
        for dep_id in ids:
            before = self.store.get_task(dep_id, tx=tx)
            if dep_id == task_id:
                problems.append("작업은 자기 자신을 선행으로 둘 수 없다")
            elif before is None:
                problems.append(f"선행 작업이 없다: {dep_id}")
            elif before["project_id"] != project_id:
                problems.append(f"선행 작업의 프로젝트가 다르다: {dep_id}")
            elif task_id is not None and self._waits_for(tx, dep_id, task_id):
                problems.append(f"순환이 생긴다: {dep_id} 가 이 작업을 기다린다")
        return problems

    # --- writes shared by everything ------------------------------------------------------------------------
    def _event(self, tx: Tx, task_id: str, actor: Actor, kind: str, *, reason: str, rule: str | None = None,
               from_status: str | None = None, to_status: str | None = None, detail: dict | None = None) -> dict:
        return self.store.create_task_event(
            task_id, actor.kind, kind, actor_id=actor.id, rule=rule, from_status=from_status,
            to_status=to_status, reason=reason, detail=detail or {}, tx=tx)

    def _emit_updated(self, tx: Tx, task: dict, event: dict) -> None:
        self.store.emit(tx, "task.updated", {"task": task, "change": event["kind"], "event_id": event["id"]},
                        project_id=task["project_id"], task_id=task["id"])

    def _emit_inbox(self, tx: Tx, type: str, item: dict) -> None:
        data = item
        if type == "inbox.created" and "package" in (item["payload"] or {}):  # §7.4: payload without package
            data = {**item, "payload": {k: v for k, v in item["payload"].items() if k != "package"}}
        self.store.emit(tx, type, {"inbox": data}, project_id=item["project_id"], task_id=item["task_id"])

    def _open_room(self, tx: Tx, task: dict) -> dict:
        """The task room is made when the task first enters backlog (§3.6: C2 · T1)."""
        room = self.store.create_room("task", title=task["title"], task_id=task["id"], tx=tx)
        self.store.emit(tx, "room.updated", {"room": room, "members": []}, project_id=task["project_id"],
                        room_id=room["id"], task_id=task["id"])
        return room

    def _join_room(self, tx: Tx, task: dict) -> dict:
        """T3 seats the assignee in the task room (§3.7)."""
        room = self._room(task["id"], tx) or self._open_room(tx, task)
        member = self.store.get_room_member(room["id"], task["assignee_id"], tx=tx)
        if member is not None and member["left_at"] is None:
            return room
        if member is None:
            self.store.create_room_member(room["id"], task["assignee_id"], tx=tx)
        else:
            self.store.update_room_member(room["id"], task["assignee_id"], left_at=None, tx=tx)
        members = self.store.list_room_members(room_id=room["id"], tx=tx)
        self.store.emit(tx, "room.updated", {"room": room, "members": members}, project_id=task["project_id"],
                        room_id=room["id"], task_id=task["id"])
        return room

    def recompute_employee(self, tx: Tx, employee_id: str) -> str | None:
        """§4.9, in the transaction that changed a task. The new status when it changed, else None."""
        employee = self.store.get_employee(employee_id, tx=tx)
        if employee is None or employee["status"] == "on_leave":  # rule 1: the owner's leave wins
            return None
        held = {r["status"] for r in tx.rows(
            "SELECT DISTINCT status FROM tasks WHERE assignee_id = ? AND status IN ('working', 'blocked')",
            (employee_id,))}
        # the same rules as employees.derive_status — written out here: this module imports nothing but the store
        status = "working" if "working" in held else "blocked" if "blocked" in held else "idle"
        if status == employee["status"]:
            return None
        self.store.update_employee(employee_id, status=status, tx=tx)
        self.store.emit(tx, "employee.status",
                        {"employee_id": employee_id, "status": status, "prev": employee["status"]},
                        project_id=employee["project_id"])
        return status

    @staticmethod
    def _stale(task: dict, to: str | None, rule: str | None = None) -> TransitionError:
        return TransitionError("stale_state", "검사한 뒤에 다른 요청이 먼저 바꿨다. 다시 읽고 다시 요청한다",
                               from_status=task["status"], to_status=to, rule=rule)

    @staticmethod
    def _unmet(problems: list[str], task: dict | None, to: str | None, rule: str | None) -> TransitionError:
        # failed stays []: §4.3 defines no codes for these conditions, the sentence says what is missing (§6.3)
        return TransitionError("precondition_failed", " / ".join(problems),
                               from_status=task["status"] if task else None, to_status=to, rule=rule, failed=[])

    # --- C1 · C2 --------------------------------------------------------------------------------------------
    def create_task(self, actor: Actor, to: str, project_id: str, *, title: str, reason: str, spec: str,
                    done_criteria: str, type: str, campaign_id: str | None = None, parent_id: str | None = None,
                    assignee_id: str | None = None, is_milestone: bool = False, relay: str | None = None,
                    stage_models: dict | None = None, sort_order: int | None = None,
                    inbox: dict | None = None, depends_on: list[str] | None = None) -> Outcome:
        """C1 (an employee registers -> proposed, with its task_proposal item) or C2 (the owner -> backlog, with
        the task room). `to` is the first state asked for, so A4 is refused by actor, not by guessing.

        `inbox` (C1 only): {"title", "payload"} — extra payload keys of §5.3 such as suggested_assignee_id.
        `assignee_id` is written on C2 only; a proposal carries its suggestion in the payload. The suggestion
        is not checked for employment (§4.3 C1 asks that of the registering employee only): T1 checks it when
        its edits make someone the assignee.
        `depends_on`: ids of the tasks this one waits for (§3.11), checked and written with the task."""
        rule = CREATE.get(to)
        if rule is None:
            raise TransitionError("illegal_transition",
                                  f"작업은 proposed(직원 등록)나 backlog(내가 등록)로만 만든다. {to} 로는 만들 수 없다",
                                  from_status=None, to_status=to)
        if actor.kind not in RULE_ACTORS[rule]:
            raise TransitionError("actor_not_allowed",
                                  f"{rule}(→ {to})은 {' · '.join(RULE_ACTORS[rule])} 만 한다. {actor.kind} 는 할 수 없다",
                                  to_status=to, rule=rule)
        text = {"title": title, "reason": reason, "spec": spec, "done_criteria": done_criteria}
        empty = [name for name, value in text.items() if _blank(value)]
        if empty:
            raise TransitionError("missing_fields", f"비어 있다: {', '.join(empty)}", to_status=to, rule=rule,
                                  fields=empty)
        if type not in TASK_TYPES:
            raise ValueError(f"task type must be one of {TASK_TYPES}: {type!r}")
        problems = []
        if self.store.get_project(project_id) is None:
            problems.append("프로젝트가 없다")
        if rule == "C1" and self._employed(actor.id) is None:
            problems.append("등록하는 직원이 재직 중이 아니다")
        if parent_id is not None and self.store.get_task(parent_id) is None:
            problems.append("부모 작업이 없다")
        if campaign_id is not None:
            campaign = self.store.get_campaign(campaign_id)
            if campaign is None or campaign["project_id"] != project_id:
                problems.append("캠페인이 없거나 프로젝트가 다르다")
        if rule == "C2" and assignee_id is not None and self._employed(assignee_id) is None:
            problems.append("담당으로 정한 직원이 재직 중이 아니다")
        if problems:
            raise self._unmet(problems, None, to, rule)

        extra = dict((inbox or {}).get("payload") or {})
        deps = list(dict.fromkeys(depends_on or []))  # each id once, in the order given
        with self.store.transaction() as tx:
            unmet = self._dep_problems(tx, None, project_id, deps)
            if unmet:
                raise self._unmet(unmet, None, to, rule)
            task = self.store.create_task(
                project_id, title=title, reason=reason, spec=spec, done_criteria=done_criteria, type=type,
                status=to, proposer_kind=actor.kind, proposer_id=actor.id, campaign_id=campaign_id,
                parent_id=parent_id, assignee_id=assignee_id if rule == "C2" else None,
                is_milestone=is_milestone, relay=relay, stage_models=stage_models, sort_order=sort_order, tx=tx)
            for dep_id in deps:
                self.store.create_task_dep(task["id"], dep_id, tx=tx)
            inbox_id = "inb_" + uuid.uuid4().hex[:8] if rule == "C1" else None
            detail: dict[str, Any] = {"inbox_id": inbox_id} if inbox_id else {}
            if deps:
                detail["depends_on"] = deps
            event = self._event(tx, task["id"], actor, "created", rule=rule, to_status=to, reason=reason.strip(),
                                detail=detail)
            self.store.emit(tx, "task.created", {"task": task}, project_id=project_id, task_id=task["id"])
            item = room = None
            if rule == "C1":
                payload = {**extra, **text, "type": type}  # §5.3: the copy taken at registration
                if rule == "C1" and assignee_id is not None:
                    payload.setdefault("suggested_assignee_id", assignee_id)
                for key, value in (("parent_id", parent_id), ("campaign_id", campaign_id)):
                    if value is not None:
                        payload[key] = value
                item = self.store.create_inbox_item(
                    "task_proposal", _one_line((inbox or {}).get("title") or title), actor.kind, id=inbox_id,
                    requester_id=actor.id, project_id=project_id, task_id=task["id"], payload=payload, tx=tx)
                self._emit_inbox(tx, "inbox.created", item)
            else:
                room = self._open_room(tx, task)
            ids = tuple(tx.emitted)
        try:
            self.effects.staffing(task, item)
        except Exception:  # noqa: BLE001 - a look at the staffing never undoes the filing (§5.3)
            log.exception("studio: 작업 %s 의 채용 계기를 보지 못했습니다", task["id"])
        self.effects.after(rule, task, ids)
        return Outcome(task=task, rule=rule, event=event, inbox=item, room=room, stream_ids=ids)

    # --- T1–T11 ---------------------------------------------------------------------------------------------
    def transition(self, task_id: str, to: str, actor: Actor, *, reason: str = "", inbox: dict | None = None,
                   inbox_id: str | None = None, decision: str | None = None, comment: str = "",
                   data: dict | None = None, edits: dict | None = None, depends_on: list[str] | None = None,
                   reset_exchange: bool | None = None,
                   committed: Callable[[Outcome], None] | None = None) -> Outcome:
        """Move a task to `to`, or raise TransitionError (nothing is written then, except what T3 records
        when the run cannot be made).

        reason   why, for the task_events row. Empty: the comment, the new item's title, or a default sentence.
        inbox    T4 · T6: the item to file — {"kind" (T4), "title", "payload"}. The requester is the actor.
        inbox_id T1 · T2 · T5 · T7 · T8 · T9 · T11: the item the caller means; refused if another one is pending.
        decision T5: required, a value of that kind (§5.2). The other deciding rules imply theirs.
        comment · data  stored on the decided item (decision_comment, decision_json).
        edits    T1: columns of EDITABLE to change with the approval.
        depends_on  T1: the tasks this one waits for from now on (§3.11). Checked and written in the transaction
                 of the transition, so a refused approval leaves the rows as they were. None: not touched.
        reset_exchange  T5: zero exchange_count. Default: the item's payload.reason is 'exchange_limit'.
        committed  called with the Outcome after the commit and before Effects.after: what has to be in place
                 when the run is resumed (§5.5: the decision text goes in first). Effects.after runs even if
                 it raises.

        Raises ValueError for a call that is the caller's bug, not a refusal (S-3 · S-5 · S-6 validate the
        request first): T4 without an inbox kind of BLOCKING_KINDS, T5 without a decision of the pending item's
        kind, or inbox= · inbox_id= · decision= · edits= · depends_on= given to a rule that does not take it (or
        a value the rule does not take). Nothing is written then.
        An exception of Effects.after comes after the commit: the transition stands and the caller gets it."""
        task = self._task(task_id)
        frm = task["status"]
        rule = classify(frm, to)
        if rule.startswith("F"):
            raise TransitionError("illegal_transition", f"{frm} → {to} 는 금지된 전이다 ({rule}: {FORBIDDEN_WHY[rule]})",
                                  from_status=frm, to_status=to, rule=rule)
        self._check_actor(rule, task, to, actor, inbox)
        pending = self._check_conditions(rule, task, to, inbox_id)
        if inbox and (rule not in CREATES or (rule == "T6" and inbox.get("kind") not in (None, "final_review"))):
            raise ValueError(f"inbox= does not fit {rule}: T4 files a blocking kind, T6 a final_review")
        if edits and rule != "T1":
            raise ValueError("edits= belongs to T1 (approving a proposal)")
        if depends_on is not None and rule != "T1":
            raise ValueError("depends_on= belongs to T1 (approving a proposal)")
        decision = self._decision(rule, pending, decision)
        changed = self._check_edits(task, to, edits or {}) if rule == "T1" else {}
        if reset_exchange is None:
            reset_exchange = ((pending or {}).get("payload") or {}).get("reason") == "exchange_limit"
        why = _why(reason, comment, (inbox or {}).get("title"), DEFAULT_REASON[rule])

        self.effects.check(rule, task)
        run_id = None
        if rule == "T3":
            run_id = self._create_run(task, to)
        elif rule in PRE_EFFECT:
            noted = self.effects.before(rule, task)
            if noted:  # what the effect could not do, kept where the transition is read (§4.3 `run_gone` 의 예외)
                why = f"{why} — {noted}"
        try:
            outcome = self._commit(rule, task, to, actor, why, run_id=run_id, pending=pending, inbox=inbox or {},
                                   decision=decision, comment=comment or "", data=data, changed=changed,
                                   reset=rule == "T5" and bool(reset_exchange), deps=depends_on)
        except BaseException:
            if run_id is not None:
                self.effects.cancel_run(run_id)  # §4.5: step 1 failed, close the run made for it
            raise
        try:
            if committed is not None:
                committed(outcome)  # before the run is started or resumed below (§5.5)
        finally:
            self.effects.after(rule, outcome.task, outcome.stream_ids)
        return outcome

    def _check_actor(self, rule: str, task: dict, to: str, actor: Actor, inbox: dict | None) -> None:
        def refuse(detail: str) -> TransitionError:
            return TransitionError("actor_not_allowed", detail, from_status=task["status"], to_status=to, rule=rule)

        if actor.kind not in RULE_ACTORS[rule]:
            raise refuse(f"{rule}({task['status']} → {to})은 {' · '.join(RULE_ACTORS[rule])} 만 한다. "
                         f"{actor.kind} 는 할 수 없다")
        if rule == "T6" and actor.id != task["assignee_id"]:
            raise refuse("검수 요청은 그 작업의 담당 직원만 한다")
        if rule == "T4":
            kind = (inbox or {}).get("kind")
            if kind not in BLOCKING_KINDS:
                raise ValueError(f"T4 needs inbox={{'kind': ...}} with one of {BLOCKING_KINDS}: {kind!r}")
            if kind not in T4_KINDS[actor.kind]:
                raise refuse(f"{actor.kind} 는 {kind} 결재를 올릴 수 없다")
            if actor.kind == "employee":
                room = self._room(task["id"])
                member = self.store.get_room_member(room["id"], actor.id) if room else None
                if member is None or member["left_at"] is not None:
                    raise refuse("그 작업방의 구성원인 직원만 결재를 올린다")

    def _check_conditions(self, rule: str, task: dict, to: str, inbox_id: str | None) -> dict | None:
        """The DB side of §4.3 "언제". Returns the pending item the transition will decide or cancel."""
        problems: list[str] = []
        pending = None
        if rule in DECIDES or rule == "T11":
            pending = self._pending_inbox(task["id"])
            kinds = STATE_INBOX[task["status"]]
            if pending is None or pending["kind"] not in kinds:
                problems.append(f"이 작업에 pending 인 결재({' · '.join(kinds)})가 없다")
            elif inbox_id is not None and pending["id"] != inbox_id:
                problems.append(f"이 작업이 기다리는 결재는 {pending['id']} 다. {inbox_id} 가 아니다")
        elif inbox_id is not None:
            raise ValueError(f"{rule} does not decide or cancel an inbox item")
        if rule == "T3":
            if task["queued_at"] is None:
                problems.append("시작 요청이 없다 (줄 서 있지 않다)")
            if task["assignee_id"] is None:
                problems.append("담당이 없다")
        if rule == "T6" and task["queued_at"] is not None:
            problems.append("런이 도는 중이 아니다 (슬롯을 기다리고 있다)")
        if problems:
            raise self._unmet(problems, task, to, rule)
        return pending

    @staticmethod
    def _decision(rule: str, pending: dict | None, decision: str | None) -> str | None:
        """The value written to inbox.decision. A mismatch is the caller's bug (S-5 validates the request)."""
        if rule not in DECIDES:
            if decision is not None:
                raise ValueError(f"{rule} does not decide an inbox item")
            return None
        if rule in RULE_DECISION:
            if decision not in (None, RULE_DECISION[rule]):
                raise ValueError(f"{rule} is the decision {RULE_DECISION[rule]!r}, not {decision!r}")
            return RULE_DECISION[rule]
        allowed = DECISIONS[pending["kind"]]
        if decision not in allowed:  # no default: a forgotten value must not become an approval
            raise ValueError(f"T5 needs decision= one of {allowed} for a {pending['kind']} item: {decision!r}")
        return decision

    def _check_edits(self, task: dict, to: str, edits: dict) -> dict:
        unknown = sorted(set(edits) - set(EDITABLE))
        if unknown:
            raise ValueError(f"T1 edits cannot change {unknown} (allowed: {EDITABLE})")
        changed = {key: value for key, value in edits.items() if task[key] != value}
        empty = [key for key in TEXT_FIELDS if key in changed and _blank(changed[key])]
        if empty:
            raise TransitionError("missing_fields", f"고친 값이 비어 있다: {', '.join(empty)}",
                                  from_status=task["status"], to_status=to, rule="T1", fields=empty)
        if "type" in changed and changed["type"] not in TASK_TYPES:
            raise ValueError(f"task type must be one of {TASK_TYPES}: {changed['type']!r}")
        if "is_milestone" in changed:
            changed["is_milestone"] = int(bool(changed["is_milestone"]))
        problems = []
        if changed.get("campaign_id") is not None:
            campaign = self.store.get_campaign(changed["campaign_id"])
            if campaign is None or campaign["project_id"] != task["project_id"]:
                problems.append("캠페인이 없거나 프로젝트가 다르다")
        if changed.get("assignee_id") is not None and self._employed(changed["assignee_id"]) is None:
            problems.append("담당으로 정한 직원이 재직 중이 아니다")
        if problems:
            raise self._unmet(problems, task, to, "T1")
        return changed

    def _create_run(self, task: dict, to: str) -> str:
        """T3 makes the run before step 1 (§4.5). If it cannot: back out of the line, leave a `run` row with
        the error, stay in backlog (§4.3) — and tell the caller."""
        try:
            run_id = self.effects.create_run(task)
            if _blank(run_id):
                raise RuntimeError("create_run returned no run id")
            return run_id
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            fresh, ids = None, ()
            with self.store.transaction() as tx:
                cur = tx.exec("UPDATE tasks SET queued_at = NULL, updated_at = ? WHERE id = ? AND status = 'backlog'",
                              (now(), task["id"]))
                if cur.rowcount == 1:
                    fresh = self._task(task["id"], tx)
                    event = self._event(tx, task["id"], SYSTEM, "run", reason=f"런을 만들지 못했다: {error}",
                                        detail={"error": error, "phase": "create"})
                    self._emit_updated(tx, fresh, event)
                    ids = tuple(tx.emitted)
            if fresh is not None:
                self.effects.after("run", fresh, ids)
            raise TransitionError("precondition_failed", f"런을 만들지 못해 backlog 에 그대로 뒀다: {error}",
                                  from_status=task["status"], to_status=to, rule="T3",
                                  failed=list(getattr(exc, "failed", []) or [])) from exc

    def _set_columns(self, tx: Tx, task: dict, cols: dict, guard: str = "", guard_args: tuple = ()) -> bool:
        """One UPDATE (the CHECKs of §3.10 see status and its columns together), guarded by the status read."""
        sets = ", ".join(f"{column} = ?" for column in cols)
        cur = tx.exec(f"UPDATE tasks SET {sets} WHERE id = ? AND status = ?{guard}",
                      (*cols.values(), task["id"], task["status"], *guard_args))
        return cur.rowcount == 1

    def _commit(self, rule: str, task: dict, to: str, actor: Actor, why: str, *, run_id: str | None,
                pending: dict | None, inbox: dict, decision: str | None, comment: str, data: dict | None,
                changed: dict, reset: bool, deps: list[str] | None = None) -> Outcome:
        """Step 1 of §4.5: UPDATE -> task_events -> inbox -> employee status, and the stream events.
        `deps` (T1): the predecessors go in with it, after the `edited` · `assigned` rows."""
        frm, stamp = task["status"], now()
        cols: dict[str, Any] = {"status": to, **changed}
        guard, guard_args = "", ()
        detail: dict[str, Any] = {"run_id": task["run_id"]} if task["run_id"] else {}  # T9 · T11 keep the old id
        if rule in ("T2", "T7", "T10", "T11"):
            cols["finished_at"] = stamp
        if rule == "T3":
            cols.update(run_id=run_id, queued_at=None, started_at=task["started_at"] or stamp)
            guard = " AND queued_at IS NOT NULL AND assignee_id IS NOT NULL"
            detail["run_id"] = run_id
        if rule in ("T4", "T9", "T10"):
            cols["queued_at"] = None
        if rule in ("T5", "T8"):
            cols["queued_at"] = stamp  # waits for a slot to resume the run (§4.6)
        if rule in ("T9", "T11"):
            cols["run_id"] = None
        if rule == "T9" or reset:
            cols["exchange_count"] = 0
        if task["upstream_rejected"] and to not in STARTED:  # T7 · T9 · T11: what stood on the rejected result
            cols["upstream_rejected"] = 0                    # is closed or taken back — the mark goes with it
            detail["upstream_rejected"] = False
        if rule == "T6":
            guard, guard_args = " AND queued_at IS NULL AND assignee_id = ?", (actor.id,)
        cols["updated_at"] = stamp
        new_id = "inb_" + uuid.uuid4().hex[:8] if rule in CREATES else None
        inbox_id = new_id or (pending["id"] if pending else None)
        if inbox_id:
            detail["inbox_id"] = inbox_id
        if decision:
            detail["decision"] = decision

        with self.store.transaction() as tx:
            if not self._set_columns(tx, task, cols, guard, guard_args):
                raise self._stale(task, to, rule)
            fresh = self._task(task["id"], tx)
            extra: list[dict] = []
            if rule == "T4" and task["queued_at"] is not None:  # blocked cannot hold queued_at (§3.10)
                extra.append(self._event(tx, task["id"], actor, "unqueued", reason="결재를 올려 줄에서 빠졌다",
                                         detail={"queued_at": task["queued_at"]}))
            event = self._event(tx, task["id"], actor, "transition", rule=rule, from_status=frm, to_status=to,
                                reason=why, detail=detail)
            if rule == "T1":
                extra += self._edit_events(tx, task, actor, changed)
                if deps is not None:
                    extra += self._approve_deps(tx, task, to, actor, deps)
            item = self._write_inbox(tx, rule, fresh, actor, inbox, new_id, pending, decision, comment, data, stamp)
            # stream order = the WS column of §4.2: inbox.* -> task.transitioned -> employee.status (§7.4)
            if item is not None:
                self._emit_inbox(tx, INBOX_EVENT[rule], item)
            self.store.emit(
                tx, "task.transitioned",
                {"task": fresh, "rule": rule, "from": frm, "to": to, "actor": actor.as_dict(), "reason": why,
                 "event_id": event["id"], "inbox_id": item["id"] if item else None},
                project_id=fresh["project_id"], task_id=fresh["id"])
            if fresh["assignee_id"]:
                self.recompute_employee(tx, fresh["assignee_id"])
            for row in extra:
                self._emit_updated(tx, fresh, row)
            room = None
            if rule == "T1":
                room = self._open_room(tx, fresh)
            elif rule == "T3":
                room = self._join_room(tx, fresh)
            if rule in ("T8", "T9"):
                self._flag_later(tx, fresh, rule, stamp)
            ids = tuple(tx.emitted)
        return Outcome(task=fresh, rule=rule, event=event, inbox=item, room=room, stream_ids=ids)

    def _flag_later(self, tx: Tx, task: dict, rule: str, stamp: str) -> None:
        """T8 · T9 (§4.3): the owner sent `task` back, so what had already started on its result is marked —
        `upstream_rejected`, a `flag` row and `task.updated` for every task that waits for it directly and is
        in STARTED, in the transaction of the transition. The line in that task's room is the inbox's (§5.9
        "선행 반려"). A task that only waits in backlog has nothing to look at again. The mark goes when the
        marked task itself leaves STARTED (T7 · T9 · T11, in `_commit`: the column, and `upstream_rejected:
        false` in the detail of that transition row) — not when the predecessor is passed again: that is when
        the task is to be looked at again."""
        for dep in self.store.list_task_deps(depends_on_id=task["id"], tx=tx):
            later = self.store.get_task(dep["task_id"], tx=tx)
            if later is None or later["status"] not in STARTED:
                continue
            tx.exec("UPDATE tasks SET upstream_rejected = 1, updated_at = ? WHERE id = ?", (stamp, later["id"]))
            event = self._event(tx, later["id"], SYSTEM, "flag", reason=DEFAULT_REASON["flag"],
                                detail={"upstream_task_id": task["id"], "rule": rule})
            self._emit_updated(tx, self._task(later["id"], tx), event)

    def _edit_events(self, tx: Tx, task: dict, actor: Actor, changed: dict) -> list[dict]:
        out = []
        # is_milestone goes to the DB as 0/1 (_check_edits) and reads back as a bool: the row says what PATCH says
        fields = {key: {"from": task[key], "to": bool(value) if key == "is_milestone" else value}
                  for key, value in changed.items() if key != "assignee_id"}
        if fields:
            out.append(self._event(tx, task["id"], actor, "edited", reason="승인하면서 고쳤다",
                                   detail={"fields": fields}))
        if "assignee_id" in changed:
            out.append(self._event(tx, task["id"], actor, "assigned", reason="승인하면서 담당을 정했다",
                                   detail={"from": task["assignee_id"], "to": changed["assignee_id"]}))
        return out

    def _replace_deps(self, tx: Tx, task_id: str, have: list[str], want: list[str]) -> None:
        for dep_id in have:
            if dep_id not in want:
                self.store.delete_task_dep(task_id, dep_id, tx=tx)
        for dep_id in want:
            if dep_id not in have:
                self.store.create_task_dep(task_id, dep_id, tx=tx)

    def _approve_deps(self, tx: Tx, task: dict, to: str, actor: Actor, deps: list[str]) -> list[dict]:
        """T1 with `depends_on`: the checks of §3.11 and the rows, in the transaction of the transition — a
        refusal here (or of anything after it) takes the whole approval back. [] when the set is already that."""
        want = list(dict.fromkeys(deps))  # each id once, in the order given
        have = [d["depends_on_id"] for d in self.store.list_task_deps(task_id=task["id"], tx=tx)]
        if set(want) == set(have):
            return []
        problems = self._dep_problems(tx, task["id"], task["project_id"], [d for d in want if d not in have])
        if problems:
            raise self._unmet(problems, task, to, "T1")
        self._replace_deps(tx, task["id"], have, want)
        return [self._event(tx, task["id"], actor, "deps", reason="승인하면서 선행 관계를 정했다",
                            detail={"from": have, "to": want})]

    def _write_inbox(self, tx: Tx, rule: str, task: dict, actor: Actor, inbox: dict, new_id: str | None,
                     pending: dict | None, decision: str | None, comment: str, data: dict | None,
                     stamp: str) -> dict | None:
        if rule in CREATES:  # inbox.run_id is always the task run (§4.3 T4)
            kind = "final_review" if rule == "T6" else inbox["kind"]
            return self.store.create_inbox_item(
                kind, _one_line(inbox.get("title") or task["title"]), actor.kind, id=new_id,
                requester_id=actor.id, project_id=task["project_id"], task_id=task["id"], run_id=task["run_id"],
                payload=inbox.get("payload") or {}, tx=tx)
        if pending is None:
            return None
        if rule == "T11":  # closed undecided: decision stays NULL, delivery stays none (§5.1)
            cur = tx.exec("UPDATE inbox SET status = 'cancelled', closed_at = ? WHERE id = ? AND status = 'pending'",
                          (stamp, pending["id"]))
        else:  # one statement, the form of §3.13. Only T5 · T8 leave a decision text to feed the run
            cur = tx.exec(
                "UPDATE inbox SET status = 'decided', decision = ?, decision_comment = ?, decision_json = ?,"
                " closed_at = ?, delivery = ? WHERE id = ? AND status = 'pending'",
                (decision, comment, json.dumps(data or {}, ensure_ascii=False), stamp,
                 "pending" if rule in ("T5", "T8") else "none", pending["id"]))
        if cur.rowcount != 1:
            raise self._stale(task, task["status"], rule)
        if rule in PRE_EFFECT:  # the run goes no further: undelivered decision texts are dropped (§3.13)
            tx.exec("UPDATE inbox SET delivery = 'none' WHERE task_id = ? AND delivery = 'pending'", (task["id"],))
            self._void_grants(tx, task["id"])
        return self.store.get_inbox_item(pending["id"], tx=tx)

    @staticmethod
    def _void_grants(tx: Tx, task_id: str) -> None:
        """T7 · T9 · T11 close the unused one-time grants of the task's approved risky_command items (§10.5):
        no task run is left to use one. A grant that was used stays as it is; T8 keeps them all."""
        rows = tx.rows("SELECT id, decision_json FROM inbox WHERE task_id = ? AND kind = 'risky_command'"
                       " AND status = 'decided' AND decision = 'approve'", (task_id,))
        for row in rows:
            data = row["decision_data"] or {}
            grant = data.get("grant")
            if isinstance(grant, dict) and grant.get("used_at") is None and not grant.get("void"):
                tx.exec("UPDATE inbox SET decision_json = ? WHERE id = ?",
                        (json.dumps({**data, "grant": {**grant, "void": True}}, ensure_ascii=False), row["id"]))

    # --- changes that are not transitions (task.updated) ----------------------------------------------------
    def _change(self, task: dict, actor: Actor, kind: str, reason: str, *, cols: dict | None = None,
                guard: str = "", detail: dict | None = None) -> Outcome:
        with self.store.transaction() as tx:
            if cols is not None and not self._set_columns(tx, task, {**cols, "updated_at": now()}, guard):
                raise self._stale(task, None)
            fresh = self._task(task["id"], tx)
            event = self._event(tx, task["id"], actor, kind, reason=_why(reason, DEFAULT_REASON[kind]),
                                detail=detail)
            self._emit_updated(tx, fresh, event)
            ids = tuple(tx.emitted)
        self.effects.after(kind, fresh, ids)
        return Outcome(task=fresh, rule=None, event=event, stream_ids=ids)

    def request_start(self, task_id: str, actor: Actor, *, reason: str = "", stage_models: dict | None = None,
                      vet: bool = False) -> Outcome:
        """The trigger of T3 (§4.3): check the request-time conditions and join the line. The task stays in
        backlog; the queue (studio/queue.py) does T3 when it has a slot.

        stage_models  the override of this start (§6.4 · §9.5 의 3): written to `stage_models_json` together with
                 `queued_at` — `{}` clears the one the task kept, None leaves it as it is.
        vet      also ask `Effects.check_start` (§9.5 '두 번 본다'): what it finds is `precondition_failed` with
                 its codes in `failed`, and nothing is written. The REST `start` asks; without it T3 alone looks."""
        task = self._task(task_id)
        frm = task["status"]
        cell = classify(frm, "working")
        if cell.startswith("F"):
            raise TransitionError("illegal_transition",
                                  f"{frm} → working 은 금지된 전이다 ({cell}: {FORBIDDEN_WHY[cell]})",
                                  from_status=frm, to_status="working", rule=cell)
        if actor.kind == "employee":  # A1
            raise TransitionError("actor_not_allowed", "직원은 작업의 시작을 요청할 수 없다",
                                  from_status=frm, to_status="working", rule="T3")
        problems = []
        if cell != "T3":
            problems.append("시작 요청은 backlog 작업에만 한다")
        elif task["queued_at"] is not None:
            problems.append("이미 시작을 요청했다 (줄 서 있다)")
        else:
            assignee = self.store.get_employee(task["assignee_id"]) if task["assignee_id"] else None
            if assignee is None:
                problems.append("담당이 없다")
            else:
                if assignee["retired_at"] is not None:
                    problems.append("담당이 재직 중이 아니다")
                if assignee["status"] == "on_leave":
                    problems.append("담당이 휴직 중이다")
                if assignee["project_id"] != task["project_id"]:
                    problems.append("담당이 이 작업의 프로젝트에 배치돼 있지 않다")
            if not self.deps_ready(task):
                problems.append("선행 작업이 채워지지 않았다")
        if problems:
            raise self._unmet(problems, task, "working", "T3")
        if vet:
            found = self.effects.check_start(task, stage_models)
            if found:
                told = " / ".join(str(p.get("message") or p.get("code")) for p in found)
                raise TransitionError("precondition_failed", f"이 설정으로는 런을 만들 수 없다: {told}",
                                      from_status=frm, to_status="working", rule="T3",
                                      failed=list(dict.fromkeys(str(p.get("code")) for p in found)))
        cols: dict[str, Any] = {"queued_at": now()}
        detail = None
        if stage_models is not None:  # the column is NOT NULL: no override is '{}' (§3.10)
            cols["stage_models_json"] = json.dumps(stage_models, ensure_ascii=False)
            detail = {"stage_models": stage_models}
        return self._change(task, actor, "queued", reason, cols=cols, guard=" AND queued_at IS NULL", detail=detail)

    def unqueue(self, task_id: str, actor: Actor, *, reason: str = "") -> Outcome:
        """Leave the line without changing state: the owner takes a start request back, or the queue starts
        resuming a working task's run (§4.3 T5)."""
        task = self._task(task_id)
        if actor.kind == "employee":
            raise TransitionError("actor_not_allowed", "직원은 줄에서 작업을 뺄 수 없다", from_status=task["status"])
        if task["queued_at"] is None:
            raise self._unmet(["줄 서 있지 않다"], task, None, None)
        return self._change(task, actor, "unqueued", reason, cols={"queued_at": None},
                            guard=" AND queued_at IS NOT NULL", detail={"queued_at": task["queued_at"]})

    def requeue(self, task_id: str, actor: Actor, *, reason: str = "") -> Outcome:
        """Join the line again without changing state: a working task whose run was cut off and goes on by
        itself (§4.7 '스스로 다시 잇는다', §4.8 의 1). Only the system does it; the queue takes the run up when it
        has a slot. `Effects.after` hears `queued`, as for a start request."""
        task = self._task(task_id)
        if actor.kind != "system":
            raise TransitionError("actor_not_allowed", "끊긴 런을 다시 줄에 세우는 것은 system 만 한다",
                                  from_status=task["status"])
        problems = []
        if task["status"] != "working":
            problems.append("working 작업만 다시 줄에 세운다")
        if task["queued_at"] is not None:
            problems.append("이미 줄 서 있다")
        if not task["run_id"]:
            problems.append("이을 런이 없다")
        if problems:
            raise self._unmet(problems, task, None, None)
        return self._change(task, actor, "queued", reason or "끊긴 런을 다시 잇는다", cols={"queued_at": now()},
                            guard=" AND queued_at IS NULL", detail={"resume": True})

    def assign(self, task_id: str, actor: Actor, employee_id: str | None, *, reason: str = "") -> Outcome:
        """Set or clear the assignee. Only in backlog (§3.10), and not while the task is in the line — the
        start request was checked against the assignee it had."""
        task = self._task(task_id)
        if actor.kind != "owner":
            raise TransitionError("actor_not_allowed", "담당은 owner 만 정한다", from_status=task["status"])
        problems = []
        if task["status"] != "backlog":
            problems.append("담당은 backlog 에서만 바꾼다")
        elif task["queued_at"] is not None:
            problems.append("줄 서 있는 작업의 담당은 바꿀 수 없다. 시작 요청을 먼저 거둔다")
        if employee_id == task["assignee_id"]:
            problems.append("이미 그 담당이다")
        elif employee_id is not None and self._employed(employee_id) is None:
            problems.append("그 직원이 재직 중이 아니다")
        if problems:
            raise self._unmet(problems, task, None, None)
        return self._change(task, actor, "assigned", reason, cols={"assignee_id": employee_id},
                            guard=" AND queued_at IS NULL", detail={"from": task["assignee_id"], "to": employee_id})

    def edit(self, task_id: str, actor: Actor, fields: dict, *, reason: str = "") -> Outcome | None:
        """The owner changes columns of a task (PATCH of §6.4). BACKLOG_FIELDS only in backlog and out of the
        line — the start request was checked against the task as it was (provisional); OPEN_FIELDS until the
        task is closed. None when no value differs: nothing is written then.

        Raises ValueError for a call that is the caller's bug (S-3 validates the request first): a key outside
        the two lists, an unknown type, or a value its column does not take."""
        allowed = BACKLOG_FIELDS + OPEN_FIELDS
        unknown = sorted(set(fields) - set(allowed))
        if unknown:
            raise ValueError(f"edit() cannot change {unknown} (allowed: {allowed})")
        if "type" in fields and fields["type"] not in TASK_TYPES:
            raise ValueError(f"task type must be one of {TASK_TYPES}: {fields['type']!r}")
        if "is_milestone" in fields and fields["is_milestone"] not in (True, False):
            raise ValueError(f"is_milestone must be true or false: {fields['is_milestone']!r}")
        order = fields.get("sort_order", 0)
        if isinstance(order, bool) or not isinstance(order, int):
            raise ValueError(f"sort_order must be an integer: {order!r}")
        task = self._task(task_id)
        if actor.kind != "owner":
            raise TransitionError("actor_not_allowed", "작업은 owner 만 고친다", from_status=task["status"])
        changed = {key: value for key, value in fields.items() if task[key] != value}
        if not changed:
            return None
        empty = [key for key in TEXT_FIELDS if key in changed and _blank(changed[key])]
        if empty:
            raise TransitionError("missing_fields", f"고친 값이 비어 있다: {', '.join(empty)}",
                                  from_status=task["status"], fields=empty)
        narrow = [key for key in BACKLOG_FIELDS if key in changed]
        problems = []
        if narrow and task["status"] != "backlog":
            problems.append(f"{' · '.join(narrow)} 은(는) backlog 에서만 고친다")
        elif narrow and task["queued_at"] is not None:
            problems.append("줄 서 있는 작업은 고칠 수 없다. 시작 요청을 먼저 거둔다")
        if task["status"] in TERMINAL and any(key in changed for key in OPEN_FIELDS):
            problems.append("끝난 작업은 고칠 수 없다")
        if changed.get("campaign_id") is not None:
            campaign = self.store.get_campaign(changed["campaign_id"])
            if campaign is None or campaign["project_id"] != task["project_id"]:
                problems.append("캠페인이 없거나 프로젝트가 다르다")
        if problems:
            raise self._unmet(problems, task, None, None)
        if "is_milestone" in changed:
            changed["is_milestone"] = bool(changed["is_milestone"])
        detail = {"fields": {key: {"from": task[key], "to": value} for key, value in changed.items()}}
        cols = {key: int(value) if key == "is_milestone" else value for key, value in changed.items()}
        return self._change(task, actor, "edited", reason, cols=cols,
                            guard=" AND queued_at IS NULL" if narrow else "", detail=detail)

    def set_deps(self, task_id: str, actor: Actor, depends_on: list[str], *, reason: str = "") -> Outcome | None:
        """The owner replaces the set of tasks this one waits for (PUT of §6.4). Only in DEP_STATES (§3.11)
        and out of the line; the checks and the rows share one transaction. None when the set is already
        that: nothing is written then."""
        want = list(dict.fromkeys(depends_on))  # each id once, in the order given
        with self.store.transaction() as tx:
            task = self._task(task_id, tx)
            if actor.kind != "owner":
                raise TransitionError("actor_not_allowed", "선행 관계는 owner 만 바꾼다", from_status=task["status"])
            have = [d["depends_on_id"] for d in self.store.list_task_deps(task_id=task_id, tx=tx)]
            if set(want) == set(have):
                return None
            problems = []
            if task["status"] not in DEP_STATES:
                problems.append(f"선행 관계는 {' · '.join(DEP_STATES)} 에서만 바꾼다")
            elif task["queued_at"] is not None:
                problems.append("줄 서 있는 작업의 선행 관계는 바꿀 수 없다. 시작 요청을 먼저 거둔다")
            problems += self._dep_problems(tx, task_id, task["project_id"], [d for d in want if d not in have])
            if problems:
                raise self._unmet(problems, task, None, None)
            self._replace_deps(tx, task_id, have, want)
            if not self._set_columns(tx, task, {"updated_at": now()}, " AND queued_at IS NULL"):
                raise self._stale(task, None)
            fresh = self._task(task_id, tx)
            event = self._event(tx, task_id, actor, "deps", reason=_why(reason, DEFAULT_REASON["deps"]),
                                detail={"from": have, "to": want})
            self._emit_updated(tx, fresh, event)
            ids = tuple(tx.emitted)
        self.effects.after("deps", fresh, ids)
        return Outcome(task=fresh, rule=None, event=event, stream_ids=ids)

    def record(self, task_id: str, actor: Actor, kind: str, reason: str = "", detail: dict | None = None) -> Outcome:
        """A task_events row that changes no column: progress · edited · deps · run · limit · flag · guard."""
        if kind not in RECORD_KINDS:
            raise ValueError(f"record() writes one of {RECORD_KINDS}: {kind!r}")
        return self._change(self._task(task_id), actor, kind, reason, detail=detail)

    def count_exchange(self, task_id: str, by: int = 1) -> dict:
        """§4.6: `exchange_count` + `by` — the turns the mentions of one message put in (S-9; `tools.room_post`).
        The UPDATE adds, so two messages at once both count. `task.updated` goes out with `change: exchange` and
        no `event_id`: the count is a column and the message is its record, so no task_events row is written.
        Whatever the status — the limit is looked at before the message is made, not here. Returns the task."""
        if by < 1:
            raise ValueError(f"count_exchange() counts the turns that went in: by={by!r}")
        with self.store.transaction() as tx:
            cur = tx.exec("UPDATE tasks SET exchange_count = exchange_count + ?, updated_at = ? WHERE id = ?",
                          (by, now(), task_id))
            if cur.rowcount != 1:
                raise TaskNotFound(f"task {task_id} not found")
            fresh = self._task(task_id, tx)
            self.store.emit(tx, "task.updated", {"task": fresh, "change": "exchange", "event_id": None},
                            project_id=fresh["project_id"], task_id=fresh["id"])
            ids = tuple(tx.emitted)
        self.effects.after("exchange", fresh, ids)
        return fresh

    # --- campaigns (§4.10) ----------------------------------------------------------------------------------
    def transition_campaign(self, campaign_id: str, to: str, actor: Actor) -> dict:
        """Change a campaign's status: the table, the guarded UPDATE and campaign.updated — and, for `paused` ·
        `cancelled`, the tasks the automatic progress had put in the line leave it in that transaction
        (`_hold_line`). Starting the tasks of a running campaign is campaigns.py's: `Effects.after` hears
        CAMPAIGN_STARTED for `running` and 'campaign' otherwise. Who and why are not recorded: §4.10 asks for no
        record and there is no campaign events table. A `to` that is not a campaign state is a ValueError (the
        caller's bug)."""
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignNotFound(f"campaign {campaign_id} not found")
        if to not in CAMPAIGN_STATES:
            raise ValueError(f"not a campaign state: {to!r}")
        frm = campaign["status"]
        who = CAMPAIGN_ALLOWED.get((frm, to))
        if who is None:
            raise TransitionError("illegal_transition", f"캠페인은 {frm} 에서 {to} 로 갈 수 없다",
                                  from_status=frm, to_status=to)
        if actor.kind != who:
            raise TransitionError("actor_not_allowed", f"캠페인의 {frm} → {to} 는 {who} 만 한다. {actor.kind} 는 할 수 없다",
                                  from_status=frm, to_status=to)
        stamp = now()
        cols: dict[str, Any] = {"status": to, "updated_at": stamp}
        if to == "running":
            cols["started_at"] = campaign["started_at"] or stamp
        if to in ("done", "cancelled"):
            cols["finished_at"] = stamp
        sets = ", ".join(f"{column} = ?" for column in cols)
        with self.store.transaction() as tx:
            if to == "done":
                marks = ", ".join("?" * len(TERMINAL))
                left = tx.row(f"SELECT COUNT(*) AS n FROM tasks WHERE campaign_id = ? AND status NOT IN ({marks})",
                              (campaign_id, *TERMINAL))["n"]
                if left:
                    raise TransitionError("precondition_failed", f"끝나지 않은 작업이 {left}개 있다",
                                          from_status=frm, to_status=to, failed=[])
            cur = tx.exec(f"UPDATE campaigns SET {sets} WHERE id = ? AND status = ?",
                          (*cols.values(), campaign_id, frm))
            if cur.rowcount != 1:
                raise self._stale(campaign, to)
            fresh = self.store.get_campaign(campaign_id, tx=tx)
            self.store.emit(tx, "campaign.updated", {"campaign": fresh}, project_id=fresh["project_id"])
            if to in CAMPAIGN_HELD:
                self._hold_line(tx, fresh, stamp)
            ids = tuple(tx.emitted)
        self.effects.after(CAMPAIGN_STARTED if to == "running" else "campaign", fresh, ids)
        return fresh

    def _hold_line(self, tx: Tx, campaign: dict, stamp: str) -> None:
        """A campaign that goes to `paused` or `cancelled` starts nothing new (§4.10 '멈춤'): in the transaction
        of that change the backlog tasks that stand in the line because the automatic progress put them there
        (`campaign_queued`) leave it — the system's `unqueued` row with CAMPAIGN_HELD as its reason and
        `task.updated`, as `unqueue` writes them, after `campaign.updated`. One commit: the queue does not know
        of campaigns, and it finds none of these in the line from then on, whenever the server stops. A task
        that got a slot but not yet its T3 leaves as well (the queue's thread then makes no run); one whose T3
        was first is working and is not touched. What the owner asked to start stays, and so does a working
        task that waits for a slot."""
        for task in self.store.list_tasks(campaign_id=campaign["id"], status="backlog", tx=tx):
            if task["queued_at"] is None or not campaign_queued(self.store.list_task_events(task["id"], tx=tx)):
                continue
            cols = {"queued_at": None, "updated_at": stamp}
            if not self._set_columns(tx, task, cols, " AND queued_at IS NOT NULL"):
                continue
            event = self._event(tx, task["id"], SYSTEM, "unqueued", reason=CAMPAIGN_HELD[campaign["status"]],
                                detail={"queued_at": task["queued_at"]})
            self._emit_updated(tx, self._task(task["id"], tx), event)

    def create_campaign(self, actor: Actor, project_id: str, *, title: str, goal: str,
                        mode: str = "milestone") -> dict:
        """The owner opens a campaign: a `draft`, which starts nothing (§4.10). `campaign.updated` says that it
        is there — §7.4 has no event of its own for that. An empty title or goal is `missing_fields`, a project
        that is not there `precondition_failed`; a `mode` outside CAMPAIGN_MODES is a ValueError (the caller's
        bug)."""
        if mode not in CAMPAIGN_MODES:
            raise ValueError(f"campaign mode must be one of {CAMPAIGN_MODES}: {mode!r}")
        if actor.kind != "owner":
            raise TransitionError("actor_not_allowed", f"캠페인은 owner 만 만든다. {actor.kind} 는 할 수 없다",
                                  to_status="draft")
        empty = [name for name, value in (("title", title), ("goal", goal)) if _blank(value)]
        if empty:
            raise TransitionError("missing_fields", f"비어 있다: {', '.join(empty)}", to_status="draft", fields=empty)
        if _blank(project_id) or self.store.get_project(project_id) is None:
            raise self._unmet(["프로젝트가 없다"], None, "draft", None)
        with self.store.transaction() as tx:
            fresh = self.store.create_campaign(project_id, title, goal, mode=mode, status="draft", tx=tx)
            self.store.emit(tx, "campaign.updated", {"campaign": fresh}, project_id=fresh["project_id"])
            ids = tuple(tx.emitted)
        self.effects.after("campaign", fresh, ids)
        return fresh

    def edit_campaign(self, campaign_id: str, actor: Actor, fields: dict) -> dict | None:
        """The owner changes `title` · `goal` · `mode` of a campaign that has not ended (PATCH of §6.4). The
        mode may change on the way: every look reads it (§4.10 의 4), so it holds from the next start — what
        stands in the line or has started is left as it is. None when no value differs: nothing is written.

        Raises ValueError for a call that is the caller's bug: a key outside CAMPAIGN_FIELDS, a mode outside
        CAMPAIGN_MODES."""
        unknown = sorted(set(fields) - set(CAMPAIGN_FIELDS))
        if unknown:
            raise ValueError(f"edit_campaign() cannot change {unknown} (allowed: {CAMPAIGN_FIELDS})")
        if "mode" in fields and fields["mode"] not in CAMPAIGN_MODES:
            raise ValueError(f"campaign mode must be one of {CAMPAIGN_MODES}: {fields['mode']!r}")
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignNotFound(f"campaign {campaign_id} not found")
        frm = campaign["status"]
        if actor.kind != "owner":
            raise TransitionError("actor_not_allowed", f"캠페인은 owner 만 고친다. {actor.kind} 는 할 수 없다",
                                  from_status=frm)
        if frm in CAMPAIGN_ENDED:
            raise TransitionError("precondition_failed", f"끝난 캠페인({frm})은 고칠 수 없다", from_status=frm,
                                  failed=[])
        changed = {key: value for key, value in fields.items() if campaign[key] != value}
        if not changed:
            return None
        empty = [key for key in ("title", "goal") if key in changed and _blank(changed[key])]
        if empty:
            raise TransitionError("missing_fields", f"고친 값이 비어 있다: {', '.join(empty)}", from_status=frm,
                                  fields=empty)
        sets = ", ".join(f"{column} = ?" for column in changed)
        with self.store.transaction() as tx:
            cur = tx.exec(f"UPDATE campaigns SET {sets}, updated_at = ? WHERE id = ? AND status = ?",
                          (*changed.values(), now(), campaign_id, frm))
            if cur.rowcount != 1:
                raise self._stale(campaign, None)
            fresh = self.store.get_campaign(campaign_id, tx=tx)
            self.store.emit(tx, "campaign.updated", {"campaign": fresh}, project_id=fresh["project_id"])
            ids = tuple(tx.emitted)
        self.effects.after("campaign", fresh, ids)
        return fresh
