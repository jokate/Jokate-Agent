"""Inbox (S-5): the nine kinds of items the owner decides — payload checks, filing, the decision, and what
follows a decision.

Contract: design/studio/ARCHITECTURE.md §5.1–5.7 · §5.9–5.10, §3.13 (the table and `delivery`), §4.3 (T1 · T2 ·
T4 · T5, and T7–T9 through `final_review`), §4.7 (when a decision text goes into a run), §10.5 (the one-time
grant of an approved `risky_command`), §6.4 (the three paths that call this).

A task and its item move together, so everything bound to a task goes through `TaskMachine.transition`:
filing is T4 · T6, deciding is T1 · T2 · T5 · T7 · T8 · T9. That transaction holds the guarded
`UPDATE … WHERE status = 'pending'`; this module turns its lost race into `already_decided` (§5.1 의 2 · 5).
`hire` has no task: it is written here, with the same guarded UPDATE.

Outside the studio DB a decision needs the engine and the turns of S-7 · S-8. Both are injected: `Effects`
(tasks.py) resumes the run and applies / rolls back, `Hooks` (here) feeds the decision text, commits an
employee folder and starts a chat turn. The defaults do nothing.

A `design_approval` · `budget` item may have no task: the stop of a run that is no task's (§5.8 나). `file_run`
files it — studio/runs.py calls that once the engine has let the run go — and its decision goes straight to
the engine through `Hooks` (`approve_run` · `cancel_run` · `advance_run`). A run released outside the inbox
closes its item as cancelled: `close_run`, `sweep_runs`, and `stale_state` at the decision.

An `employee.yaml` an approval writes (hire · guide_amend) passes the checks of §9.4 first: `employees.check`
with the `checks=` this Inbox was made with (`employees.context`).

Not here: the `package` of a final review (S-12), pushes and the 2-hour reminder (N-1 · N-2).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from . import employees
from .store import StudioStore, Tx, now
from .tasks import DECISIONS as TASK_DECISIONS
from .tasks import (BLOCKING_KINDS, EDITABLE, OWNER, STARTED, TASK_TYPES, TEXT_FIELDS, Actor, TaskMachine,
                    TaskNotFound, TransitionError)

log = logging.getLogger(__name__)

# --- the tables of §5.2 -----------------------------------------------------------------------------------
KINDS = ("task_proposal", "hire", "design_approval", "structure_choice", "question", "pre_approval", "budget",
         "risky_command", "final_review")  # the order of §3.13 and §5.2
DECISIONS = {kind: TASK_DECISIONS.get(kind, ("approve", "reject", "revise")) for kind in KINDS}  # + hire
COMMENT_REQUIRED = {"task_proposal": ("reject",), "hire": ("revise",), "design_approval": ("revise",),
                    "structure_choice": ("revise",), "pre_approval": ("revise",), "final_review": ("rework",)}
NOTIFY_OK = ("task_proposal", "hire", "design_approval", "pre_approval", "budget")  # a plain approve only (N-3)
VIAS = ("app", "notification", "dashboard")
DATA_KEYS = {  # (kind, decision) -> the keys `data` may carry; nothing else is taken
    ("task_proposal", "approve"): ("edits",), ("hire", "approve"): ("edits",),
    ("structure_choice", "choose"): ("option_id",), ("question", "answer"): ("option_id",),
    **{("final_review", decision): ("judgment_marks",) for decision in TASK_DECISIONS["final_review"]},
}
TARGET = {  # (kind, decision) -> the state the task goes to (§5.5): T1 · T2 · T7 · T8 · T9, and T5 for the rest
    ("task_proposal", "approve"): "backlog", ("task_proposal", "reject"): "rejected",
    ("final_review", "pass"): "done", ("final_review", "rework"): "working", ("final_review", "revert"): "backlog",
    **{(kind, decision): "working" for kind in BLOCKING_KINDS for decision in TASK_DECISIONS[kind]},
}
PROPOSAL_EDITS = EDITABLE + ("depends_on",)
MARKS = ("should_have_asked", "ok")
CODES = ("not_found", "already_decided", "invalid_decision", "missing_fields", "precondition_failed", "conflict",
         "engine_error", "actor_not_allowed", "stale_state")
RUN_KINDS = ("design_approval", "budget")  # what an item without a task may be: the stop of a run (§5.8 나)
# One decision of such an item at a time, and no sweep meanwhile: between engine.approve and the write of the
# decision the run is `pending`, which a sweep would read as "released outside" and close the item.
_RUN_LOCK = threading.Lock()

# --- payloads (§5.3, §5.4) --------------------------------------------------------------------------------
PAYLOAD_REQUIRED = {
    "task_proposal": ("title", "type", "reason", "spec", "done_criteria"),
    "hire": ("action", "draft_id", "reason"),
    "design_approval": ("stage", "stop_reason"),
    "structure_choice": ("question", "options"),
    "question": ("reason", "body"),
    "pre_approval": ("what", "why", "if_not"),
    "budget": ("scope", "stage", "spent_usd", "limit_usd", "stop_reason"),
    "risky_command": ("command", "tool", "rule_id", "reason"),
    "final_review": ("summary", "checklist", "exchange_count"),
}
NUMBER_KEYS = ("spent_usd", "limit_usd", "exchange_count")
LIST_KEYS = ("options", "checklist")
HIRE_BODY = {  # action -> its body fields
    "hire": ("draft",),
    "guide_amend": ("target_employee_id", "file", "new_text", "diff", "base_sha256"),
    "policy": ("new_text", "diff", "base_sha256"),
}
MAY_BE_EMPTY = ("diff", "base_sha256")  # must be there, "" is a value (no file yet, nothing to show)
DRAFT_REQUIRED = ("name", "department_id", "persona_md", "guide_md", "employee_yaml")
DRAFT_KEYS = DRAFT_REQUIRED + ("project_id", "first_message")
AMEND_FILES = ("PERSONA.md", "GUIDE.md", "employee.yaml")  # MEMORY.md is not amended (§9.1)
SCOPES = ("run", "stage")
CHECK_STATUS = ("verified_auto", "verified_manual", "unverified")
EVIDENCE_KINDS = ("screenshot", "log", "file", "url")
QUESTION_OPTIONS = {  # reason -> the options the server puts in; None = the employee's own (provisional)
    "asked": None, "run_failed": ("retry", "wrap_up"), "run_cancelled": ("retry", "wrap_up"),
    "no_review_request": (), "exchange_limit": ("continue", "wrap_up"), "editor_unresponsive": ("retry",),
}
OPTION_LABEL = {"retry": "다시 시도", "wrap_up": "지금까지 한 것으로 검수 요청", "continue": "다시 세고 계속"}

# --- decision texts (§5.6) --------------------------------------------------------------------------------
KIND_LABEL = {"task_proposal": "작업 제안", "hire": "채용", "design_approval": "설계 승인",
              "structure_choice": "구조 선택", "question": "질문", "pre_approval": "사전 승인", "budget": "예산",
              "risky_command": "위험 명령", "final_review": "최종 검수"}
DECISION_LABEL = {"approve": "승인", "reject": "반려", "revise": "수정 요청", "choose": "선택", "answer": "답변",
                  "pass": "통과", "rework": "재작업", "revert": "되돌리기"}
ANSWERS = {  # question: by reason (`asked`, `no_review_request`) or by the option the owner picked
    "asked": "질문의 답이다: `{label}`.",
    "retry": "멈췄던 자리에서 이어서 하라.",
    "continue": "왕복 수를 다시 셌다. 이어서 하라.",
    "no_review_request": "검수 요청 없이 끝났다. 남은 일을 마치고 `task_request_review` 를 올려라.",
    "wrap_up": "새 일을 벌이지 말고 지금까지 한 것으로 `task_request_review` 를 올려라. 검증하지 못한 완료 기준은 "
               "`unverified` 로 적어라.",
}
SENTENCES = {
    ("design_approval", "approve"): "설계가 승인됐다. 다음 단계를 진행하라.",
    ("design_approval", "revise"): "설계를 코멘트대로 고쳐서 진행하라. 무엇을 고쳤는지 작업방에 적어라.",
    ("structure_choice", "choose"): "구조는 선택지 `{id}`(`{label}`)로 정해졌다. 이 구조로 진행하라.",
    ("structure_choice", "revise"): "선택지를 다시 내라. 코멘트를 반영해 `ask_owner(kind = choice)` 를 다시 올려라.",
    ("pre_approval", "approve"): "올린 변경이 승인됐다. 올린 범위 안에서 진행하라.",
    ("pre_approval", "reject"): "올린 변경은 하지 않는다. 그 변경 없이 할 수 있는 범위로 진행하고, 못 하면 `ask_owner` 로 "
                                "다시 물어라.",
    ("pre_approval", "revise"): "올린 변경을 코멘트대로 바꿔서 진행하라.",
    ("budget", "approve"): "예산이 늘었다. 이어서 진행하라.",
    ("budget", "reject"): "예산을 더 쓰지 않는다. " + ANSWERS["wrap_up"],
    ("risky_command", "approve"): "이 명령을 한 번 실행해도 된다: `{command}`. 다른 명령은 다시 결재를 받는다.",
    ("risky_command", "reject"): "이 명령은 실행하지 않는다: `{command}`. 다른 방법으로 진행하거나 `ask_owner` 로 물어라.",
    ("final_review", "rework"): "검수에서 재작업이 나왔다. 코멘트를 반영하고 다시 `task_request_review` 를 올려라.",
}
NOTES = {  # the system message a decision leaves in a 1:1 room (§5.5). Not fed to a run; wording is S-5's
    ("task_proposal", "approve"): "제안한 작업이 승인됐다: {title}",
    ("task_proposal", "reject"): "제안한 작업이 거절됐다: {title}",
    ("hire", "reject"): "올린 안이 반려됐다: {title}",
    ("hire", "revise"): "올린 안을 코멘트대로 고쳐서 다시 제출하라: {title}",
}
UPSTREAM_NOTE = {  # §5.9 "선행 반려": the line in the room of a task that had started behind the one sent back
    "rework": '선행 반려: 이 작업이 기다린 선행 작업 "{title}" ({id}) 이 검수에서 재작업으로 돌아갔다. '
              "그 결과에 기대어 한 일을 다시 확인하라.",
    "revert": '선행 반려: 이 작업이 기다린 선행 작업 "{title}" ({id}) 의 변경을 되돌렸다. '
              "그 결과에 기대어 한 일을 다시 확인하라.",
}
GREETING = "안녕하세요, {name}입니다. 잘 부탁드립니다."  # the first message when the draft has none (§5.5)
HR = "hr"
SYSTEM_ROOM = "room_system"
EMPLOYEE_FILES = employees.FILES  # §9.1, in the order they are written


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _one_of(value: Any, choices) -> bool:
    return isinstance(value, str) and value in choices


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _one_line(text: Any) -> str:
    """inbox.title is one line of at most 80 characters (§5.1)."""
    lines = text.strip().splitlines() if isinstance(text, str) else []
    return lines[0].strip()[:80] if lines else ""


def _lacks(key: str, value: Any) -> bool:
    if key in NUMBER_KEYS:
        return not _number(value)
    if key in LIST_KEYS:
        return not (isinstance(value, list) and value)
    return not _text(value)


def _options_ok(options: Any, *, detail: bool, minimum: int) -> bool:
    if not isinstance(options, list) or len(options) < minimum:
        return False
    ids = []
    for option in options:
        if not isinstance(option, dict) or not _text(option.get("id")) or not _text(option.get("label")):
            return False
        if detail and not isinstance(option.get("detail"), str):
            return False
        ids.append(option["id"])
    return len(set(ids)) == len(ids)


def _hire_problems(payload: dict) -> list[str]:
    action = payload.get("action")
    if not _one_of(action, HIRE_BODY):
        return ["action"]
    if action == "hire":
        draft = payload.get("draft")
        if not isinstance(draft, dict):
            return ["draft"]
        bad = [f"draft.{key}" for key in DRAFT_REQUIRED if not _text(draft.get(key))]
        return bad + [f"draft.{key}" for key in ("project_id", "first_message")
                      if draft.get(key) is not None and not isinstance(draft[key], str)]
    bad = [key for key in HIRE_BODY[action]
           if not (isinstance(payload.get(key), str) if key in MAY_BE_EMPTY else _text(payload.get(key)))]
    if action == "guide_amend" and "file" not in bad and payload["file"] not in AMEND_FILES:
        bad.append("file")
    return bad


def validate_payload(kind: str, payload: Any) -> list[str]:
    """The keys of a payload that break §5.3 · §5.4: a required one missing or empty first, then a value out
    of its set. [] = it can be filed."""
    if kind not in KINDS:
        raise ValueError(f"inbox kind must be one of {KINDS}: {kind!r}")
    if not isinstance(payload, dict):
        return ["payload"]
    bad = [key for key in PAYLOAD_REQUIRED[kind] if _lacks(key, payload.get(key))]

    def flag(key: str, wrong: bool) -> None:
        if wrong and key not in bad:
            bad.append(key)

    if kind == "task_proposal":
        flag("type", not _one_of(payload.get("type"), TASK_TYPES))
    elif kind == "hire":
        bad += [key for key in _hire_problems(payload) if key not in bad]
    elif kind == "structure_choice":
        options = payload.get("options")
        flag("options", not _options_ok(options, detail=True, minimum=2))
        if "options" not in bad and payload.get("recommended") is not None:
            flag("recommended", payload["recommended"] not in [option["id"] for option in options])
    elif kind == "question":
        flag("reason", not _one_of(payload.get("reason"), QUESTION_OPTIONS))
        if payload.get("options") is not None:
            flag("options", not _options_ok(payload["options"], detail=False, minimum=0))
    elif kind == "budget":
        flag("scope", not _one_of(payload.get("scope"), SCOPES))
    elif kind == "final_review":
        checklist, count = payload.get("checklist"), payload.get("exchange_count")
        flag("checklist", not (isinstance(checklist, list) and checklist and all(
            isinstance(c, dict) and _text(c.get("criterion")) and _one_of(c.get("status"), CHECK_STATUS)
            for c in checklist)))
        flag("exchange_count", not (isinstance(count, int) and not isinstance(count, bool) and count >= 0))
        evidence, calls = payload.get("evidence"), payload.get("judgment_calls")
        flag("evidence", evidence is not None and not (isinstance(evidence, list) and all(
            isinstance(e, dict) and _one_of(e.get("kind"), EVIDENCE_KINDS) for e in evidence)))
        flag("judgment_calls", calls is not None and not (isinstance(calls, list) and all(
            isinstance(c, dict) and _text(c.get("text")) for c in calls)))
    return bad


def grant_key(tool: str, command: str) -> str:
    """§10.5: sha256(tool + newline + the command with its whitespace tidied). The same key = the same command."""
    return hashlib.sha256((tool + "\n" + " ".join(command.split())).encode("utf-8")).hexdigest()


def _sentence(item: dict) -> str:
    kind, payload, data = item["kind"], item["payload"] or {}, item["decision_data"] or {}
    option = next((o for o in payload.get("options") or []
                   if isinstance(o, dict) and o.get("id") == data.get("option_id")), None)
    if kind == "question":
        reason = payload.get("reason")
        if reason == "no_review_request":
            return ANSWERS[reason]
        if option is None:
            return ""  # a comment alone: head and comment, no instruction sentence
        if reason == "asked":
            return ANSWERS["asked"].replace("{label}", str(option.get("label", "")))
        return ANSWERS.get(option["id"], "")
    text = SENTENCES.get((kind, item["decision"]), "")
    if kind == "structure_choice" and option is not None:
        text = text.replace("{id}", str(option["id"])).replace("{label}", str(option.get("label", "")))
    if kind == "risky_command":
        text = text.replace("{command}", str(payload.get("command", "")))
    return text


def _compose(item: dict, sentence: str) -> str:
    lines = [f"[결재 {item['id']} · {KIND_LABEL[item['kind']]} · "
             f"{DECISION_LABEL.get(item['decision'], item['decision'])}]"]
    if sentence:
        lines.append(sentence)
    comment = (item["decision_comment"] or "").strip()
    if comment:
        lines.append(f"코멘트: {comment}")
    return "\n".join(lines)


def decision_text(item: dict) -> str:
    """§5.6, for a decided item: the head, the instruction sentence of that kind and decision, and the comment
    when there is one. What goes into the run and into the task room."""
    return _compose(item, _sentence(item))


# --- errors -----------------------------------------------------------------------------------------------
class InboxError(Exception):
    """A refusal of §5.1. as_dict() is the §6.3 body: `{detail, code}` plus what the code adds — `inbox`
    (already_decided · stale_state), `allowed` (invalid_decision), `fields` (missing_fields), `failed` (precondition_failed),
    `field` (conflict)."""

    def __init__(self, code: str, detail: str, **extra: Any):
        if code not in CODES:
            raise ValueError(f"InboxError code must be one of {CODES}: {code!r}")
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = dict(extra)
        if code == "precondition_failed":
            self.extra.setdefault("failed", [])  # §6.3: always there; §5.5 defines no codes

    def as_dict(self) -> dict:
        return {"detail": self.detail, "code": self.code, **self.extra}


# --- what is injected -------------------------------------------------------------------------------------
class Hooks:
    """What a decision asks of the engine and of the turns (S-7 · S-8). The default does nothing."""

    def interject(self, run_id: str, text: str) -> bool:
        """Feed a decision text to the run the item names (`RelayEngine.interject`, §4.7). True = it went in
        now. False = not now — the run is still going, or nothing is wired: the item keeps
        `delivery = 'pending'` and the progress thread delivers it when it comes back (S-8). A ValueError
        (a `done` run the engine does not open again, S-12) is read as False."""
        return False

    def commit(self, folder: Path, message: str) -> None:
        """git commit of the folder a hire approval wrote into (H-3)."""

    def chat_turn(self, employee_id: str, room_id: str) -> None:
        """Start that employee's chat turn in that room, so it reads the system message just written (S-7)."""

    # --- what a mention asks (S-9; §4.6 · §8.4 · §9.5). With nothing wired no turn goes in -------------------
    def turn_failed(self, employee_id: str, room_id: str) -> list[str]:
        """Asked before the message of a mention is made (§9.5 의 5 가): the codes that keep that employee's
        turn in that room from being made — `provider_unguarded`. [] = none, as far as can be said now."""
        return []

    def room_turn(self, employee_id: str, room_id: str) -> bool:
        """Put in that employee's turn in that room: in a task room the read turn of a member who is not the
        assignee. True = it is on its way. False = no run was made (the room has been told why), or nothing
        is wired."""
        return False

    def tell_run(self, run_id: str, text: str) -> bool:
        """Have a run that may be at work read `text` (`RelayEngine.interject`): a message of the task room,
        for the assignee, whose turn there is the task's run. True = the run has it — in the conversation
        that is going on, or with its next stage. False = the engine has no such run, the run is `done`, or
        nothing is wired."""
        return False

    # --- a run that is no task's (§5.8 나). With nothing wired its item stays pending ------------------------
    def run_status(self, run_id: str) -> str | None:
        """The engine's status of that run; `gone` when it has no such run. None = no engine is wired."""
        return None

    def approve_run(self, run_id: str, note: str = "") -> bool:
        """`engine.approve` of a run that waits for approval — after `engine.interject(note)` when there is a
        note (the comment of a `revise`). False = nothing is wired. ValueError = the run does not wait for
        approval any more: then nothing was done to it, the note included."""
        return False

    def cancel_run(self, run_id: str) -> bool:
        """`engine.cancel` of a run that waits for approval (the reject of a `budget`). False and ValueError
        as in approve_run."""
        return False

    def advance_run(self, run_id: str) -> None:
        """Have the approved run go on, as `/runs/{id}/approve` does after `engine.approve`."""


# --- the inbox --------------------------------------------------------------------------------------------
class Inbox:
    """Files and decides items. It keeps no state of its own: one per request is fine."""

    def __init__(self, store: StudioStore, machine: TaskMachine | None = None, hooks: Hooks | None = None, *,
                 studio_dir: Path | str | None = None, checks: employees.Checks | None = None,
                 max_exchanges: int | None = None):
        self.max_exchanges = max_exchanges  # §4.6: the limit `room_post` looks at; None = no limit is looked at
        self.store = store
        self.machine = machine if machine is not None else TaskMachine(store)
        self.hooks = hooks if hooks is not None else Hooks()
        self.studio_dir = Path(studio_dir) if studio_dir is not None else None  # runs/studio: what hire writes
        self.checks = checks  # what the checks of an employee.yaml know of the server (§9.4)

    # --- reads ----------------------------------------------------------------------------------------------
    def _item(self, inbox_id: str) -> dict:
        item = self.store.get_inbox_item(inbox_id)
        if item is None:
            raise InboxError("not_found", f"결재를 찾을 수 없다: {inbox_id}")
        return item

    @staticmethod
    def _closed(item: dict) -> InboxError:
        how = ("취소됐다" if item["status"] == "cancelled"
               else f"'{DECISION_LABEL.get(item['decision'], item['decision'])}'(으)로 결정됐다")
        return InboxError("already_decided", f"이미 처리된 결재다 — {how}", inbox=item)

    def view(self, inbox_id: str) -> dict:
        """`GET /studio/inbox/{id}`: the row, its task and the decisions it takes now. A final review carries
        the key `package` (§5.4) with null here: the request fills it (api.py `get_inbox_item`, studio/review.py)
        — an Inbox knows neither the engine nor the queue."""
        item = self._item(inbox_id)
        out = {**item, "task": self.store.get_task(item["task_id"]) if item["task_id"] else None,
               "allowed_decisions": list(DECISIONS[item["kind"]]) if item["status"] == "pending" else []}
        if item["kind"] == "final_review":
            out["package"] = None
        return out

    def _dm_room(self, employee_id: str) -> dict | None:
        rooms = self.store.list_rooms(kind="dm", employee_id=employee_id, include_archived=True)
        return rooms[0] if rooms else None

    def _hr(self, tx: Tx | None = None) -> dict | None:
        staff = self.store.list_employees(department_id=HR, tx=tx)  # at most one in service (employees_one_hr)
        return staff[0] if staff else None

    def _root(self) -> Path:
        if self.studio_dir is None:
            raise RuntimeError("Inbox needs studio_dir= to write what a hire approval writes (§2.2)")
        return self.studio_dir

    def _check_yaml(self, text: str, department_id: str, department: dict | None) -> None:
        """§9.4, before an `employee.yaml` is written: `precondition_failed` with every code in `failed[]`,
        and nothing is decided. An Inbox made without `checks=` cannot say — a bug, not a refusal."""
        if self.checks is None:
            raise RuntimeError("Inbox needs checks= (employees.context) to check an employee.yaml (§9.4)")
        problems = employees.check(text, department_id=department_id, department=department, checks=self.checks)
        if problems:
            raise InboxError("precondition_failed",
                             "employee.yaml 검사에 걸렸다: " + " / ".join(p["message"] for p in problems),
                             failed=[p["code"] for p in problems])

    # --- filing ---------------------------------------------------------------------------------------------
    def create(self, kind: str, actor: Actor, *, title: str = "", payload: dict | None = None,
               task_id: str | None = None, project_id: str | None = None) -> dict:
        """File an item and return its row. The six blocking kinds are T4 and `final_review` is T6 of
        `task_id`, so the machine's rules decide who may file (TransitionError) and one pending item per task
        holds. `hire` is written here; only the HR employee files it.

        A payload that breaks §5.3 · §5.4 is `missing_fields` with the keys in `fields`; nothing is written.
        The server fills what is its own to say: the options of a system `question`, `exchange_count` of a
        final review, `supersedes_inbox_id` of a draft handed in again.

        ValueError (the caller's bug): `task_proposal` — registering the task files it (C1) —, and a
        `design_approval` · `budget` without a task — the stop of a run that is no task's is filed by
        `file_run` (§5.8 나)."""
        if kind not in KINDS:
            raise ValueError(f"inbox kind must be one of {KINDS}: {kind!r}")
        if kind == "task_proposal":
            raise ValueError("a task_proposal is filed by registering the task: TaskMachine.create_task (C1)")
        payload = copy.deepcopy(payload) if isinstance(payload, dict) else {}
        if kind == "hire":
            return self._create_hire(actor, title, payload, project_id)
        if task_id is None:
            raise ValueError(f"a {kind} item without a task is not filed here: file_run files a run's stop (§5.8 나)")
        task = self.store.get_task(task_id)
        if task is None:
            raise TaskNotFound(f"task {task_id} not found")
        problems: list[str] = []
        if kind == "question":
            if actor.kind == "employee":
                payload.setdefault("reason", "asked")
            reason = payload.get("reason")
            if _one_of(reason, QUESTION_OPTIONS):
                if actor.kind in ("employee", "system") and (reason == "asked") != (actor.kind == "employee"):
                    problems.append("reason")  # `asked` is the employee's reason, the others are the server's
                elif reason != "asked":
                    payload.pop("options", None)
                    if QUESTION_OPTIONS[reason]:
                        payload["options"] = [{"id": o, "label": OPTION_LABEL[o]} for o in QUESTION_OPTIONS[reason]]
        if kind == "final_review":
            payload["exchange_count"] = task["exchange_count"]  # the value at T6 (§5.4)
        problems = validate_payload(kind, payload) + problems
        if problems:
            raise InboxError("missing_fields", f"payload 에 없거나 틀린 값이 있다: {', '.join(problems)}",
                             fields=problems)
        box = {"title": title, "payload": payload}
        if kind != "final_review":
            box["kind"] = kind
        return self.machine.transition(task_id, "review" if kind == "final_review" else "blocked", actor,
                                       inbox=box).inbox

    def use_grant(self, task_id: str, key: str, run_id: str) -> dict | None:
        """§10.5 '찾기 · 쓰기': use the one-time grant of that command (`grant_key`) on that task, for the task run
        `run_id` — the caller has checked that it is one (§6.4 가드의 분기 1). The oldest approved `risky_command`
        of the task whose grant has that key and is neither used nor void gets `used_at` · `used_run_id`; read and
        written in one transaction, so two requests cannot use the same grant. The item after it, or None when no
        grant is left. Nothing else of the decided item changes."""
        with self.store.transaction() as tx:
            rows = tx.rows("SELECT id, decision_json FROM inbox WHERE task_id = ? AND kind = 'risky_command'"
                           " AND status = 'decided' AND decision = 'approve' ORDER BY closed_at, rowid", (task_id,))
            for row in rows:
                data = row["decision_data"] or {}
                grant = data.get("grant")
                if isinstance(grant, dict) and grant.get("key") == key and grant.get("used_at") is None \
                        and not grant.get("void"):
                    used = {**grant, "used_at": now(), "used_run_id": run_id}
                    tx.exec("UPDATE inbox SET decision_json = ? WHERE id = ?",
                            (json.dumps({**data, "grant": used}, ensure_ascii=False), row["id"]))
                    return self.store.get_inbox_item(row["id"], tx=tx)
        return None

    def _create_hire(self, actor: Actor, title: str, payload: dict, project_id: str | None) -> dict:
        requester = self.store.get_employee(actor.id) if actor.kind == "employee" else None
        if requester is None or requester["retired_at"] is not None or requester["department_id"] != HR:
            raise InboxError("actor_not_allowed", "hire 결재는 재직 중인 인사 직원만 올린다")
        problems = validate_payload("hire", payload)
        if not _one_line(title):
            problems.append("title")
        if payload.get("action") == "policy" and not project_id:
            problems.append("project_id")  # the project the policy is for (§5.3)
        if problems:
            raise InboxError("missing_fields", f"payload 에 없거나 틀린 값이 있다: {', '.join(problems)}",
                             fields=problems)
        if project_id and self.store.get_project(project_id) is None:
            raise InboxError("precondition_failed", "프로젝트가 없다")
        with self.store.transaction() as tx:
            # the same draft handed in again: a pending item is replaced, a revised one is what this answers
            prior = [row for row in self.store.list_inbox(kind="hire", tx=tx)
                     if (row["payload"] or {}).get("draft_id") == payload["draft_id"]
                     and (row["status"] == "pending" or row["decision"] == "revise")]
            for row in prior:
                if row["status"] != "pending":
                    continue
                tx.exec("UPDATE inbox SET status = 'cancelled', closed_at = ? WHERE id = ? AND status = 'pending'",
                        (now(), row["id"]))
                closed = self.store.get_inbox_item(row["id"], tx=tx)
                self.store.emit(tx, "inbox.cancelled", {"inbox": closed}, project_id=closed["project_id"])
            if prior:
                payload["supersedes_inbox_id"] = prior[-1]["id"]
            item = self.store.create_inbox_item("hire", _one_line(title), "employee", requester_id=actor.id,
                                                project_id=project_id or None, payload=payload, tx=tx)
            self.store.emit(tx, "inbox.created", {"inbox": item}, project_id=item["project_id"])
        return item

    # --- the stop of a run that is no task's (§5.8 나) ------------------------------------------------------
    def _run_items(self, run_id: str, tx: Tx | None = None) -> list[dict]:
        """The pending items of that run that have no task: at most one, unless a stop went unheard."""
        return [row for row in self.store.list_inbox(status="pending", run_id=run_id, tx=tx)
                if row["task_id"] is None and row["kind"] in RUN_KINDS]

    def _cancel_item(self, tx: Tx, inbox_id: str) -> dict | None:
        """pending -> cancelled with its `inbox.cancelled` (§5.1). None when it was not pending any more."""
        cur = tx.exec("UPDATE inbox SET status = 'cancelled', closed_at = ? WHERE id = ? AND status = 'pending'",
                      (now(), inbox_id))
        if cur.rowcount != 1:
            return None
        closed = self.store.get_inbox_item(inbox_id, tx=tx)
        self.store.emit(tx, "inbox.cancelled", {"inbox": closed}, project_id=closed["project_id"])
        return closed

    def file_run(self, kind: str, run_id: str, *, title: str, payload: dict | None = None,
                 project_id: str | None = None) -> dict:
        """File the item of a run that stopped for approval or at a budget and is no task's: `task_id` NULL,
        requester `system`, `run_id` that run. Nobody is asked whether they may: the engine stopped the run.

        A payload that breaks §5.3 is `missing_fields`. A pending item that run already has comes from a stop
        that was released without the inbox hearing of it (the CLI): it is closed as cancelled and this one
        takes its place — unless it says the same, then that one is returned as it is."""
        if kind not in RUN_KINDS:
            raise ValueError(f"a run's stop is filed as one of {RUN_KINDS}: {kind!r}")
        payload = copy.deepcopy(payload) if isinstance(payload, dict) else {}
        problems = validate_payload(kind, payload)
        if problems:
            raise InboxError("missing_fields", f"payload 에 없거나 틀린 값이 있다: {', '.join(problems)}",
                             fields=problems)
        with self.store.transaction() as tx:
            for row in self._run_items(run_id, tx):
                if (row["kind"], row["payload"]) == (kind, payload):
                    return row
                self._cancel_item(tx, row["id"])
            item = self.store.create_inbox_item(kind, _one_line(title) or KIND_LABEL[kind], "system", run_id=run_id,
                                                project_id=project_id or None, payload=payload, tx=tx)
            self.store.emit(tx, "inbox.created", {"inbox": item}, project_id=item["project_id"])
        return item

    def close_run(self, run_id: str) -> list[dict]:
        """§5.8 나 "밖에서 풀렸을 때": the run was approved, cancelled or otherwise moved on without the inbox,
        so its pending items without a task are closed as cancelled. The rows as closed; [] = none waited.
        The item of a task is never touched here: that one moves with its task (T5 · T11)."""
        with self.store.transaction() as tx:
            closed = [self._cancel_item(tx, row["id"]) for row in self._run_items(run_id, tx)]
        return [row for row in closed if row is not None]

    def sweep_runs(self) -> int:
        """`GET /studio/inbox` (§5.8 나): close the pending items without a task whose run does not wait for
        approval any more — released by the CLI, or gone. How many were closed.

        Nothing is done while a decision of such an item is in flight (`_RUN_LOCK`), nor when no engine is
        wired (`run_status` None)."""
        if not _RUN_LOCK.acquire(blocking=False):
            return 0
        try:
            closed = 0
            waiting = [row["run_id"] for row in self.store.list_inbox(status="pending")
                       if row["task_id"] is None and row["kind"] in RUN_KINDS and row["run_id"]]
            for run_id in dict.fromkeys(waiting):
                try:
                    status = self.hooks.run_status(run_id)
                except Exception:  # noqa: BLE001 - one run that cannot be read does not stop the others
                    log.exception("studio: 런 %s 의 상태를 읽지 못해 그 결재는 그대로 둔다", run_id)
                    continue
                if status is not None and status != "awaiting_approval":
                    closed += len(self.close_run(run_id))
            return closed
        finally:
            _RUN_LOCK.release()

    # --- deciding -------------------------------------------------------------------------------------------
    def decide(self, inbox_id: str, decision: str, *, comment: str | None = "", data: dict | None = None,
               via: str | None = "app") -> dict:
        """The owner's decision (§5.1): `{inbox, task}` after it — `task` is None for `hire`.

        In the order of §5.1: not_found -> already_decided (with the item as it is now) -> invalid_decision
        (with the kind's values in `allowed`) -> the conditions of what follows (TransitionError of §4.5,
        precondition_failed · conflict of a hire approval, engine_error when the engine refused before anything
        was written) -> the guarded write, where the loser of a race gets already_decided.

        What follows a committed decision — the system message, feeding the run, the note to HR — does not
        undo it: a failure there is logged. For a task's item it is done before `Effects.after` (§5.5: the run
        is fed, then resumed), so an exception of `Effects.after` reaches the caller as it is, after those
        were done.

        A `design_approval` · `budget` item without a task is the stop of its run: the decision goes to the
        engine (`_decide_run`, §5.8 나) — `precondition_failed` with the item still pending while no engine is
        wired, `stale_state` with the item closed as cancelled when the run no longer waits for approval."""
        item = self._item(inbox_id)
        if item["status"] != "pending":
            raise self._closed(item)
        comment, via = comment or "", via or "app"
        data = {key: value for key, value in (data or {}).items() if value is not None}
        self._check_decision(item, decision, comment, data, via)
        if item["kind"] == "hire":
            return {"inbox": self._decide_hire(item, decision, comment, data, via), "task": None}
        if item["task_id"] is None:
            return {"inbox": self._decide_run(item, decision, comment, data, via), "task": None}
        return self._decide_task(item, decision, comment, data, via)

    def _check_decision(self, item: dict, decision: str, comment: str, data: dict, via: str) -> None:
        """§5.1 의 3. Everything here is read from the request and the item: nothing is written yet."""
        kind, payload = item["kind"], item["payload"] or {}
        allowed = list(DECISIONS[kind])

        def refuse(detail: str) -> InboxError:
            return InboxError("invalid_decision", detail, allowed=allowed)

        if via not in VIAS:
            raise refuse(f"via 는 {' · '.join(VIAS)} 중 하나다: {via!r}")
        if decision not in allowed:
            raise refuse(f"{kind} 결재의 결정 값은 {' · '.join(allowed)} 다: {decision!r}")
        unknown = sorted(set(data) - set(DATA_KEYS.get((kind, decision), ())))
        if unknown:
            raise refuse(f"{kind} 의 {decision} 에 실을 수 없는 data 다: {', '.join(unknown)}")
        if decision in COMMENT_REQUIRED.get(kind, ()) and not _text(comment):
            raise refuse(f"{decision} 에는 코멘트가 필요하다")
        if "option_id" in data:
            options = payload.get("options") or []
            if data["option_id"] not in [o.get("id") for o in options if isinstance(o, dict)]:
                raise refuse(f"선택지에 없는 option_id 다: {data['option_id']!r}")
        elif (kind, decision) == ("structure_choice", "choose"):
            raise refuse("choose 에는 data.option_id 가 필요하다")
        elif kind == "question" and not _text(comment):
            raise refuse("answer 에는 코멘트나 data.option_id 중 하나가 필요하다")
        if "edits" in data:
            problem = self._edits_problem(kind, payload, data["edits"])
            if problem:
                raise refuse(problem)
        if "judgment_marks" in data:
            calls = payload.get("judgment_calls") or []
            marks = data["judgment_marks"]
            fine = isinstance(marks, list) and all(
                isinstance(m, dict) and set(m) == {"index", "mark"} and isinstance(m["index"], int)
                and not isinstance(m["index"], bool) and 0 <= m["index"] < len(calls) and _one_of(m["mark"], MARKS)
                for m in marks)
            if not fine or len({m["index"] for m in marks}) != len(marks):
                raise refuse(f"judgment_marks 는 [{{index, mark}}] 다. index 는 judgment_calls 의 순번(각 한 번), "
                             f"mark 는 {' · '.join(MARKS)}")
        if via == "notification" and (kind not in NOTIFY_OK or decision != "approve" or _text(comment) or data):
            raise refuse("알림에서는 코멘트와 data 없는 승인만 결정한다. 그 밖의 결정은 앱에서 한다")

    @staticmethod
    def _edits_problem(kind: str, payload: dict, edits: Any) -> str:
        """Why `edits` cannot be taken ("" = fine): only the keys of §5.2, each of its type."""
        if not isinstance(edits, dict):
            return "edits 는 객체다"
        if kind == "task_proposal":
            unknown = sorted(set(edits) - set(PROPOSAL_EDITS))
            if unknown:
                return f"edits 로 고칠 수 없다: {', '.join(unknown)}"
            for key, value in edits.items():
                if key in TEXT_FIELDS:
                    fine = isinstance(value, str)
                elif key == "type":
                    fine = _one_of(value, TASK_TYPES)
                elif key == "is_milestone":
                    fine = isinstance(value, bool)
                elif key == "depends_on":
                    fine = isinstance(value, list) and all(isinstance(v, str) for v in value)
                else:  # assignee_id · campaign_id: null clears
                    fine = value is None or isinstance(value, str)
                if not fine:
                    return f"edits.{key} 의 값이 틀렸다"
            return ""
        action = payload.get("action")
        unknown = sorted(set(edits) - set(HIRE_BODY.get(action, ())))
        if unknown:
            return f"edits 로 고칠 수 없다: {', '.join(unknown)}"
        if action == "hire":
            draft = edits.get("draft", {})
            if not isinstance(draft, dict) or set(draft) - set(DRAFT_KEYS):
                return f"edits.draft 는 {' · '.join(DRAFT_KEYS)} 가운데 고칠 것만 싣는다"
        return ""

    def _decide_task(self, item: dict, decision: str, comment: str, data: dict, via: str) -> dict:
        kind, task_id, payload = item["kind"], item["task_id"], item["payload"] or {}
        edits = dict(data.get("edits") or {})
        depends_on = edits.pop("depends_on", None)  # task_deps rows, not a column: it goes in beside the edits
        stored = {**data, "via": via}
        if (kind, decision) == ("risky_command", "approve"):  # the one-time grant of that command (§10.5)
            stored["grant"] = {"key": grant_key(str(payload.get("tool", "")), str(payload.get("command", ""))),
                               "used_at": None, "used_run_id": None, "void": False}
        empty = [key for key in TEXT_FIELDS if key in edits and not _text(edits[key])]
        if empty:
            raise InboxError("missing_fields", f"고친 값이 비어 있다: {', '.join(empty)}", fields=empty)
        try:
            # The predecessors (§3.11) are written in the transaction of T1: refused -> nothing of it stays.
            # What follows the decision runs as `committed`, before Effects.after resumes the run (§5.5:
            # interject -> approve -> advance) — and so also when Effects.after then raises (§4.5).
            outcome = self.machine.transition(
                task_id, TARGET[(kind, decision)], OWNER, inbox_id=item["id"], decision=decision, comment=comment,
                data=stored, edits=edits or None, depends_on=depends_on,
                committed=lambda out: self._follow(out.inbox))
        except TransitionError as e:
            fresh = self.store.get_inbox_item(item["id"])
            if fresh is not None and fresh["status"] != "pending":
                raise self._closed(fresh) from e  # another decision, or the task's cancel (T11), came first
            raise
        except ValueError as e:
            fresh = self.store.get_inbox_item(item["id"])
            if fresh is not None and fresh["status"] == "pending":  # refused before anything was written
                raise InboxError("engine_error", str(e) or "엔진이 요청을 거부했다") from e
            raise
        return {"inbox": self.store.get_inbox_item(item["id"]), "task": outcome.task}

    def _write_decision(self, tx: Tx, item: dict, decision: str, comment: str, stored: dict) -> dict:
        """§5.1 의 5 for an item without a task: the statement of §3.13. `delivery` stays 'none'."""
        cur = tx.exec(
            "UPDATE inbox SET status = 'decided', decision = ?, decision_comment = ?, decision_json = ?,"
            " closed_at = ? WHERE id = ? AND status = 'pending'",
            (decision, comment, json.dumps(stored, ensure_ascii=False), now(), item["id"]))
        fresh = self.store.get_inbox_item(item["id"], tx=tx)
        if cur.rowcount != 1:
            raise self._closed(fresh)
        self.store.emit(tx, "inbox.decided", {"inbox": fresh}, project_id=fresh["project_id"])
        return fresh

    def _decide_run(self, item: dict, decision: str, comment: str, data: dict, via: str) -> dict:
        """§5.8 나: the decision of a run's stop goes to the engine there and then, so `delivery` stays 'none'.
        `approve` is `engine.approve` and the run goes on; a `revise` first leaves its comment as a note for
        the next stage; a `reject` (budget) is `engine.cancel`.

        The engine is called outside a store transaction (the store lock is not re-entrant, and `engine.cancel`
        may take long) and under `_RUN_LOCK`: decisions of run items go one after the other and `sweep_runs`
        keeps away meanwhile. Nothing wired -> `precondition_failed`, the item stays pending. The run does not
        wait for approval any more -> the item is closed as cancelled and the answer is `stale_state` with it
        in `inbox`. The engine refused a run that still waits -> `engine_error`, the item stays pending."""
        run_id, approved = item["run_id"], False
        if not run_id or item["kind"] not in RUN_KINDS:
            raise InboxError("precondition_failed", f"작업도 런도 없는 {item['kind']} 결재는 결정할 수 없다")
        try:
            with _RUN_LOCK:
                fresh = self._item(item["id"])
                if fresh["status"] != "pending":
                    raise self._closed(fresh)
                try:
                    if decision == "reject":
                        wired = self.hooks.cancel_run(run_id)
                    else:
                        wired = self.hooks.approve_run(run_id, comment if decision == "revise" else "")
                except ValueError as e:
                    if self.hooks.run_status(run_id) == "awaiting_approval":  # refused, and nothing has changed
                        raise InboxError("engine_error", str(e) or "엔진이 요청을 거부했다") from e
                    self.close_run(run_id)
                    raise InboxError("stale_state", f"런이 더는 승인을 기다리지 않아 결재를 닫았다 — {e}",
                                     inbox=self._item(item["id"])) from e
                if not wired:  # nothing was asked of the engine, so this is not engine_error
                    raise InboxError("precondition_failed",
                                     f"런을 잇는 엔진이 연결되지 않아 작업 없는 {item['kind']} 결재를 결정할 수 없다 (§5.8 나)")
                approved = decision != "reject"
                with self.store.transaction() as tx:
                    return self._write_decision(tx, item, decision, comment, {**data, "via": via})
        finally:
            if approved:  # the engine took the approval: the run goes on even when the write then lost a race
                self._safely("런 진행", self.hooks.advance_run, run_id)

    # --- hire (§5.5, provisional until H-2 · H-3 · H-6) -----------------------------------------------------
    def _decide_hire(self, item: dict, decision: str, comment: str, data: dict, via: str) -> dict:
        stored = {**data, "via": via}
        if decision != "approve":  # nothing is written but the decision; HR reads it in its 1:1 room
            with self.store.transaction() as tx:
                fresh = self._write_decision(tx, item, decision, comment, stored)
            self._follow(fresh)
            return fresh
        payload = copy.deepcopy(item["payload"] or {})
        edits = data.get("edits") or {}
        if payload.get("action") == "hire":
            payload["draft"] = {**(payload.get("draft") or {}), **edits.get("draft", {})}
        else:
            payload.update(edits)
        problems = validate_payload("hire", payload)
        if problems:
            raise InboxError("missing_fields", f"고친 뒤의 안에 없거나 틀린 값이 있다: {', '.join(problems)}",
                             fields=problems)
        approve = {"hire": self._hire, "guide_amend": self._amend, "policy": self._policy}[payload["action"]]
        return approve(item, payload, comment, stored)

    def _hire(self, item: dict, payload: dict, comment: str, stored: dict) -> dict:
        """(1) the folder and its four files -> (2) one transaction: the decision, the employee, the 1:1 room,
        both memberships and the first message -> (3) the folder's first commit (hook). A failure of (1)
        decides nothing; a failure of (2) removes the folder and leaves the item pending.
        The conditions are checked before (1) and once more in (2), on what that transaction sees: another
        approval that got in between is refused with the code and the sentence of the first check."""
        draft = payload["draft"]
        name, project_id = draft["name"].strip(), (draft.get("project_id") or "").strip() or None
        department = self._check_hire(draft, name, project_id)
        self._check_yaml(draft["employee_yaml"], department["id"], department)  # before anything is written
        employee_id = "emp_" + uuid.uuid4().hex[:8]
        folder = employees.folder(self._root(), employee_id)
        texts = (draft["persona_md"], draft["guide_md"], "", draft["employee_yaml"])  # MEMORY.md starts empty
        try:
            employees.write_files(folder, dict(zip(EMPLOYEE_FILES, texts)))
        except OSError as e:
            shutil.rmtree(folder, ignore_errors=True)
            raise InboxError("precondition_failed", f"직원 폴더를 쓰지 못했다: {e}") from e
        try:
            with self.store.transaction() as tx:
                fresh = self._write_decision(tx, item, "approve", comment, stored)
                department = self._check_hire(draft, name, project_id, tx)  # the write lock is ours from here
                employee = self.store.create_employee(name, department["id"], id=employee_id, project_id=project_id,
                                                      tx=tx)
                dm = self.store.create_room("dm", title=name, employee_id=employee_id, tx=tx)
                self.store.create_room_member(dm["id"], employee_id, tx=tx)
                self.store.emit(tx, "employee.updated", {"employee": employee, "change": "created"},
                                project_id=project_id)
                self.store.emit(tx, "room.updated",
                                {"room": dm, "members": self.store.list_room_members(room_id=dm["id"], tx=tx)},
                                project_id=project_id, room_id=dm["id"])
                for part in self.store.list_rooms(kind="department", department_id=department["id"], tx=tx)[:1]:
                    self.store.create_room_member(part["id"], employee_id, tx=tx)
                    members = self.store.list_room_members(room_id=part["id"], tx=tx)
                    self.store.emit(tx, "room.updated", {"room": part, "members": members}, room_id=part["id"])
                hello = draft.get("first_message") if _text(draft.get("first_message")) else None
                message = self.store.create_message(
                    dm["id"], "employee", author_id=employee_id, body=hello or GREETING.replace("{name}", name),
                    meta={"event": "greeting", "inbox_id": item["id"]}, tx=tx)
                self.store.emit(tx, "message.created", {"message": message}, project_id=project_id,
                                room_id=dm["id"])
        except sqlite3.IntegrityError as e:
            shutil.rmtree(folder, ignore_errors=True)
            if "employees.name" in str(e):  # another hire took the name after the check above
                raise InboxError("conflict", f"같은 이름의 직원이 이미 있다: {name}", field="name") from e
            raise
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        self._safely("직원 폴더의 첫 커밋", self.hooks.commit, folder, f"hire: {name} ({employee_id})")
        return fresh

    def _check_hire(self, draft: dict, name: str, project_id: str | None, tx: Tx | None = None) -> dict:
        """What a `hire` approval needs (§5.5): the part, no second HR employee in service, the project, a
        free name. Returns the part. `precondition_failed`, or `conflict` for the name."""
        department = self.store.get_department(draft["department_id"], tx=tx)
        problems = []
        if department is None or department["archived_at"] is not None:
            problems.append("파트가 없다")
        elif department["id"] == HR:
            if self._hr(tx) is not None:
                problems.append("인사 직원은 한 명뿐이다")
            if project_id:
                problems.append("인사 직원은 프로젝트에 배치하지 않는다")
        if project_id and self.store.get_project(project_id, tx=tx) is None:
            problems.append("배치할 프로젝트가 없다")
        if problems:
            raise InboxError("precondition_failed", " / ".join(problems))
        if self._name_taken(name, tx):
            raise InboxError("conflict", f"같은 이름의 직원이 이미 있다: {name}", field="name")
        return department

    def _name_taken(self, name: str, tx: Tx | None = None) -> bool:
        """A retired employee's name is not free either (§3.5)."""
        return any(row["name"] == name for row in self.store.list_employees(include_retired=True, tx=tx))

    def _amend(self, item: dict, payload: dict, comment: str, stored: dict) -> dict:
        target = self.store.get_employee(payload["target_employee_id"])
        if target is None:
            raise InboxError("precondition_failed", f"대상 직원이 없다: {payload['target_employee_id']}")
        if payload["file"] == "employee.yaml":  # §9.6: the same checks on every way the file is written
            self._check_yaml(payload["new_text"], target["department_id"],
                             self.store.get_department(target["department_id"]))
        path = self._root() / "employees" / target["id"] / payload["file"]

        def commit(tx: Tx) -> dict:
            fresh = self._write_decision(tx, item, "approve", comment, stored)
            self.store.emit(tx, "employee.updated", {"employee": target, "change": "file"},
                            project_id=target["project_id"])
            return fresh

        fresh = self._replace_file(path, payload, commit)
        self._safely("지침 수정의 커밋", self.hooks.commit, path.parent,
                     f"guide_amend: {payload['file']} ({item['id']})")
        return fresh

    def _policy(self, item: dict, payload: dict, comment: str, stored: dict) -> dict:
        path = self._root() / "projects" / item["project_id"] / "POLICY.md"
        return self._replace_file(path, payload, lambda tx: self._write_decision(tx, item, "approve", comment, stored))

    @staticmethod
    def _restore(path: Path, old: bytes | None) -> None:
        try:
            if old is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(old)
        except OSError:
            log.exception("studio: %s 를 되돌리지 못했다", path)

    def _replace_file(self, path: Path, payload: dict, commit: Callable[[Tx], dict]) -> dict:
        """Write `new_text` over the file if it is still the one the draft was made from (`base_sha256`; "" =
        no file yet), then `commit(tx)` — the decision. If that fails the file goes back to what it was, so
        the item stays pending with nothing changed (§5.5)."""
        try:
            old = path.read_bytes() if path.is_file() else None
        except OSError as e:
            raise InboxError("precondition_failed", f"파일을 읽지 못했다: {e}") from e
        have = hashlib.sha256(old).hexdigest() if old is not None else ""
        if have != payload["base_sha256"].strip().lower():
            raise InboxError("precondition_failed", "제출한 뒤에 파일이 바뀌었다. 지금 파일로 수정안을 다시 올려야 한다")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload["new_text"].encode("utf-8"))
        except OSError as e:
            self._restore(path, old)
            raise InboxError("precondition_failed", f"파일을 쓰지 못했다: {e}") from e
        try:
            with self.store.transaction() as tx:
                return commit(tx)
        except BaseException:
            self._restore(path, old)
            raise

    # --- after a decision -----------------------------------------------------------------------------------
    @staticmethod
    def _safely(what: str, step: Callable[..., Any], *args: Any) -> None:
        try:
            step(*args)
        except Exception:  # noqa: BLE001 - the decision is committed: what follows must not hide or undo it
            log.exception("studio: 결정 뒤의 '%s' 가 실패했다 (결정은 그대로다)", what)

    def _follow(self, item: dict) -> None:
        """§5.5 for a committed decision: the system message, the decision text into the run, the note to HR."""
        self._safely("시스템 글", self._notify, item)
        if item["delivery"] == "pending":
            self._safely("결정문 전달", self.deliver, item["id"])
        self._safely("판단 표시 알림", self._marks, item)

    def deliver(self, inbox_id: str) -> bool:
        """Feed the decision text of a decided item to its run and mark it delivered (§3.13, §4.7). True when
        it went in now; False when there is nothing to deliver or the run cannot take it yet — the item then
        stays `delivery = 'pending'`. S-8 calls this again when the run's progress thread comes back."""
        item = self.store.get_inbox_item(inbox_id)
        if item is None or item["delivery"] != "pending" or not item["run_id"]:
            return False
        try:
            went_in = bool(self.hooks.interject(item["run_id"], decision_text(item)))
        except ValueError as e:  # a done run the engine does not open again (§4.7): its changes are decided
            log.info("studio: 결정문 %s 를 런 %s 에 넣지 못했다 — %s", inbox_id, item["run_id"], e)
            return False
        if not went_in:
            return False
        with self.store.transaction() as tx:
            tx.exec("UPDATE inbox SET delivery = 'delivered', delivered_at = ? WHERE id = ? AND delivery = 'pending'",
                    (now(), inbox_id))
        return True

    def _say(self, room: dict | None, body: str, meta: dict, author_id: str | None = None) -> dict | None:
        """A system message (or an employee's, with `author_id`) and its `message.created`, in one transaction."""
        if room is None:
            return None
        with self.store.transaction() as tx:
            project_id = None
            if room["task_id"]:
                task = self.store.get_task(room["task_id"], tx=tx)
                project_id = task["project_id"] if task else None
            elif room["employee_id"]:
                employee = self.store.get_employee(room["employee_id"], tx=tx)
                project_id = employee["project_id"] if employee else None
            message = self.store.create_message(room["id"], "employee" if author_id else "system",
                                                author_id=author_id, body=body, meta=meta, tx=tx)
            self.store.emit(tx, "message.created", {"message": message}, project_id=project_id,
                            room_id=room["id"], task_id=room["task_id"])
        return message

    @staticmethod
    def _meta(item: dict) -> dict:
        meta = {"event": "inbox.decided", "inbox_id": item["id"], "task_id": item["task_id"]}  # §7.6
        return {key: value for key, value in meta.items() if value is not None}

    def _notify(self, item: dict) -> None:
        kind, decision = item["kind"], item["decision"]
        if kind in BLOCKING_KINDS or (kind, decision) == ("final_review", "rework"):
            rooms = self.store.list_rooms(kind="task", task_id=item["task_id"], include_archived=True)
            self._say(rooms[0] if rooms else None, decision_text(item), self._meta(item))  # the text the run gets
        elif (kind, decision) in NOTES and item["requester_id"]:
            room = self._dm_room(item["requester_id"])
            note = NOTES[(kind, decision)].replace("{title}", item["title"])
            body = _compose(item, note)
            draft_id = (item["payload"] or {}).get("draft_id") if kind == "hire" else None
            if draft_id:  # HR's next conversation may be a new one: the message is its one way back to the draft
                body += f"\n초안 id: {draft_id}"
            self._say(room, body, self._meta(item))
            if (kind, decision) == ("hire", "revise") and room is not None:
                self.hooks.chat_turn(item["requester_id"], room["id"])  # HR reworks the draft; a reject has no turn
        if kind == "final_review" and decision in UPSTREAM_NOTE:
            self._upstream(item)

    def _upstream(self, item: dict) -> None:
        """§5.9 "선행 반려": T8 · T9 marked the tasks that had started behind this one (`TaskMachine._flag_later`);
        each of them is told in its own room, with the `meta` of §7.6. A room that cannot be written to does not
        keep the others from their line."""
        before = self.store.get_task(item["task_id"]) or {}
        note = UPSTREAM_NOTE[item["decision"]].format(title=before.get("title", ""), id=item["task_id"])
        for dep in self.store.list_task_deps(depends_on_id=item["task_id"]):
            later = self.store.get_task(dep["task_id"])
            if later is None or later["status"] not in STARTED or not later["upstream_rejected"]:
                continue
            rooms = self.store.list_rooms(kind="task", task_id=later["id"], include_archived=True)
            self._safely("선행 반려 알림", self._say, rooms[0] if rooms else None, note,
                         {"event": "upstream_rejected", "task_id": later["id"], "upstream_task_id": item["task_id"]})

    def _marks(self, item: dict) -> None:
        """`judgment_marks` with a `should_have_asked`: HR's 1:1 room and its chat turn; with nobody in HR, the
        system room (§5.5)."""
        if item["kind"] != "final_review":
            return
        calls = (item["payload"] or {}).get("judgment_calls") or []
        flagged = [mark for mark in (item["decision_data"] or {}).get("judgment_marks") or []
                   if mark["mark"] == "should_have_asked"]
        if not flagged:
            return
        task = self.store.get_task(item["task_id"]) or {}
        lines = [_compose({**item, "decision_comment": ""}, "묻지 않고 처리한 판단에 '물어봤어야 함' 표시가 붙었다."),
                 f"작업: {task.get('title', '')} ({item['task_id']})"]
        for mark in flagged:
            call = calls[mark["index"]]
            lines.append(f"- {call.get('text', '')} (이유: {call.get('why', '')})")
        hr = self._hr()
        room = self._dm_room(hr["id"]) if hr else None
        if room is None:
            self._say(self.store.get_room(SYSTEM_ROOM), "\n".join(lines), self._meta(item))
            return
        self._say(room, "\n".join(lines), self._meta(item))
        self.hooks.chat_turn(hr["id"], room["id"])
