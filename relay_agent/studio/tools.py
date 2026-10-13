"""The studio MCP tools, on the server's side (S-6): what `POST /studio/mcp/<tool>` does.

Contract: design/studio/ARCHITECTURE.md — §8.3 the result, §8.4 the arguments, §8.5 who may, §5.7 a member
who is not the assignee asking the owner, §1.5 the kinds of `ask_owner`.

A tool is `tool(caller, args, machine, inbox) -> dict`; `call()` runs one and turns every refusal into the
result of §8.3 — a business error is a result here, never an HTTP error. No FastAPI and no api.py: the caller
is handed in (api.py reads it from the actor token, §8.2), and every change goes through TaskMachine and
Inbox, so the rules of §4 and §5 stay theirs. What the tools check themselves is what those two do not know:
the turn (is there a task, is this the task's run), what the caller may see, and the arguments under the
names the model wrote them.

Order inside a tool: the turn (`no_task_context`) -> the target (`not_found`) -> who (`actor_not_allowed`)
-> for the two that end the turn, the state (`illegal_transition`) -> the arguments (`missing_fields`) ->
the machine.

The five that are no task's: `room_post` writes a message as the employee and copies its attachments
(§6.6 '직원의 첨부', studio/files.py); `employee_list` reads; HR's three keep drafts in `runs/studio/drafts/`
(studio/drafts.py) and file the `hire` item through Inbox. The hiring pair (T5): `hire_recommend` (HR only) files
a `hire_recommendation` and `staff_request` (anyone) asks HR to judge (studio/staffing.py).

A mention in a task room asks for a turn (S-9; §4.6 · §8.4 · §9.5): the assignee's is the task's run, which is told
the message; any other member's is a read turn. Both go through `inbox.hooks` — a tool knows the turn, not the
engine. Each turn that went in is one more in `exchange_count`, and at `inbox.max_exchanges` nothing is written:
the system asks the owner (T4, `exchange_limit`) and the turn ends. A mention in a group room or a department room asks for that member's turn in the room's line
(`_group_posted`); in any other room it starts no turn.

Not here yet: the handoff format of `room_post` (H-5), and the two file arguments of an inbox item (`_file_args`,
S-11).

Not here at all: what the server does once the answer of a T4 · T6 has gone out (cutting the run that is still
at work, §8.3 '서버의 뒤처리' · §5.7). That is studio/cutoff.py's, and api.py calls it (`answered`) with the run
the call came from — a tool knows the turn, not the engine.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import drafts, employees, files, staffing
from .inbox import AMEND_FILES, DRAFT_KEYS, DRAFT_REQUIRED, RECOMMEND, Inbox, InboxError, validate_payload
from .tasks import (FORBIDDEN_WHY, STATES, SYSTEM, TASK_TYPES, TEXT_FIELDS, Actor, Outcome, TaskMachine,
                    TaskNotFound, TransitionError, classify)

log = logging.getLogger(__name__)

END_TURN = "이제 턴을 끝내라. 도구를 더 부르지 말고, 지금까지 한 일과 기다리는 것을 한 문단으로 적고 멈춰라."  # §8.3
ASK_KINDS = {"question": "question", "choice": "structure_choice", "approval": "pre_approval"}  # §1.5
HR = "hr"
GROUP_KINDS = ("group", "department")  # the rooms whose turns go into the room's line (api.GROUP_KINDS; no import: api imports this)
CODES = ("no_task_context", "not_found", "actor_not_allowed", "missing_fields", "precondition_failed",
         "exchange_limit")  # Refusal
ADDED = {"illegal_transition": ("from", "to"), "precondition_failed": ("failed",), "missing_fields": ("fields",),
         "exchange_limit": ("inbox_id",)}
ENDING = ("ask_owner", "task_request_review")   # the tools whose success ends the turn (T4 · T6)
WAITING = ("blocked", "review")                 # the task already waits for the owner: end the turn (§8.3)
PROGRESS_STATES = ("working", "blocked", "review")  # §8.4: the run still goes after T4 · T6
FILE_EVIDENCE = ("screenshot", "log", "file")   # evidence[].ref is a file path for these, a URL for `url`
LISTED = ("id", "title", "status", "type", "assignee_id", "is_milestone")
PENDING = ("id", "kind", "title", "created_at")
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
RECENT_EVENTS = 10
ASK_HINT = {
    "question": "question 은 body 가 필수이고 options 는 각 {id, label} 이다",
    "structure_choice": "choice 는 body 와 options(2개 이상, 각 {id, label, detail, figure})가 필수이고 "
                        "recommended 는 options 의 id 중 하나, figure 는 \"\" 또는 {mermaid} 다",
    "pre_approval": "approval 은 what · why · if_not 이 모두 필수다",
}


# --- who calls --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Caller:
    """The employee behind a call and the turn it is in: what the actor token says (§8.2). `task_id` is
    "지금 작업", `room_id` "지금 방" (§8.4). Permissions are not read from here but from the DB (§8.5)."""
    employee: dict            # the employees row, read for this call
    run_id: str
    room_id: str | None
    task_id: str | None
    mode: str                 # work · read · chat
    workdir: Path | None = None  # the folder this run's stages work in (§6.6); filled when a file is handed in

    @property
    def id(self) -> str:
        return self.employee["id"]

    @property
    def actor(self) -> Actor:
        return Actor.employee(self.employee["id"])

    @property
    def is_hr(self) -> bool:
        return self.employee["department_id"] == HR


# --- the result (§8.3) ------------------------------------------------------------------------------------
class Refusal(Exception):
    """A refusal a tool makes itself. `extra`: `failed` (precondition_failed) · `fields` (missing_fields)."""

    def __init__(self, code: str, message: str, **extra: Any):
        if code not in CODES:
            raise ValueError(f"Refusal code must be one of {CODES}: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = dict(extra)


def ok(message: str, *, end_turn: bool = False, **fields: Any) -> dict:
    return {"ok": True, "end_turn": end_turn, "message": f"{message} {END_TURN}" if end_turn else message,
            **fields}


def fail(code: str, message: str, *, end_turn: bool = False, **added: Any) -> dict:
    return {"ok": False, "code": code, "message": f"{message} {END_TURN}" if end_turn else message,
            "end_turn": end_turn, **added}


# --- helpers ----------------------------------------------------------------------------------------------
def _blank(value: Any) -> bool:
    return not (isinstance(value, str) and value.strip())


def _text(value: Any) -> str | None:
    """An optional text argument: "" (the default of §8.4) and blank are None."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _placed(caller: Caller, project_id: str | None) -> bool:
    """§8.5 "배치된 프로젝트의 것. 인사는 전체" — by the employees row of now."""
    return caller.is_hr or (project_id is not None and project_id == caller.employee["project_id"])


def _visible_task(caller: Caller, machine: TaskMachine, task_id: str) -> dict:
    task = machine.store.get_task(task_id)
    if task is None or not _placed(caller, task["project_id"]):
        raise Refusal("not_found", f"작업 {task_id} 이 없거나 볼 수 없다. task_list 로 id 를 확인한다")
    return task


def _turn_task(caller: Caller, machine: TaskMachine, what: str) -> dict:
    """The task of this turn, for a tool that works on it."""
    if caller.task_id is None:
        raise Refusal("no_task_context", f"이 턴에는 작업이 없어 {what} 수 없다 (작업방의 턴에서만 한다)")
    task = machine.store.get_task(caller.task_id)
    if task is None:
        raise Refusal("not_found", f"작업 {caller.task_id} 이 없다")
    return task


def _own_task(caller: Caller, machine: TaskMachine, what: str) -> dict:
    """§8.5 `task_progress` · `task_request_review`: the caller is the assignee and this run is the task's."""
    task = _turn_task(caller, machine, what)
    if task["assignee_id"] != caller.id or task["run_id"] != caller.run_id:
        raise Refusal("actor_not_allowed",
                      f"그 작업의 담당이 자기 작업 런에서만 {what} 수 있다. 할 말은 room_post 로 방에 남긴다")
    return task


def _legal(task: dict, to: str) -> None:
    """The machine's first check, ahead of the arguments: a task that already waits for the owner must hear
    "end the turn" whatever else is wrong with the call (§8.3)."""
    frm = task["status"]
    rule = classify(frm, to)
    if rule.startswith("F"):
        raise TransitionError("illegal_transition", f"{frm} → {to} 는 금지된 전이다 ({rule}: {FORBIDDEN_WHY[rule]})",
                              from_status=frm, to_status=to, rule=rule)


def _file_args(tool: str, args: dict) -> None:
    """Where the file arguments of §8.4 are checked and copied (§6.6 '직원의 첨부'): `options[].figure` with
    a `path`, and `evidence[].ref` of a file kind. The files of an item are copied under its id, which the
    machine makes inside the transition — S-11 does that (room_post's attachments are done: `files.check`);
    until then a call that hands in a file is refused whole, so no path is stored unchecked."""
    found: list[str] = []
    if tool == "ask_owner":
        found += [f"options[{i}].figure" for i, option in enumerate(args.get("options") or [])
                  if isinstance(option, dict) and isinstance(option.get("figure"), dict)
                  and "path" in option["figure"]]
    if tool == "task_request_review":
        found += [f"evidence[{i}].ref" for i, item in enumerate(args.get("evidence") or [])
                  if isinstance(item, dict) and item.get("kind") in FILE_EVIDENCE]
    if found:
        raise Refusal("precondition_failed",
                      f"파일을 넘기는 인자는 아직 받지 않는다: {', '.join(found)}. 파일 없이 다시 부른다 — "
                      "그림은 {mermaid} 로, 근거는 kind = url 이나 summary 의 글로 적는다", failed=[])


# --- task_propose · followup_propose (C1) -----------------------------------------------------------------
def _propose(caller: Caller, args: dict, machine: TaskMachine, parent: dict | None = None) -> Outcome:
    store = machine.store
    current = store.get_task(caller.task_id) if caller.task_id else None
    project_id = (_text(args.get("project_id")) or (parent or current or {}).get("project_id")
                  or caller.employee["project_id"])
    if project_id is None:
        if caller.is_hr:
            raise Refusal("missing_fields", "어느 프로젝트의 작업인지 project_id 로 적는다 (인사는 배치된 프로젝트가 없다)",
                          fields=["project_id"])
        raise Refusal("actor_not_allowed", "배치된 프로젝트가 없어 작업을 제안할 수 없다")
    if caller.employee["status"] == "on_leave":
        raise Refusal("actor_not_allowed", "휴직 중에는 작업을 제안할 수 없다")
    if not _placed(caller, project_id):
        raise Refusal("actor_not_allowed", f"프로젝트 {project_id} 에 배치돼 있지 않아 그 프로젝트의 작업을 제안할 수 없다")
    bad = [name for name in TEXT_FIELDS if _blank(args.get(name))]
    if args.get("type") not in TASK_TYPES:
        bad.append("type")
    if bad:
        raise Refusal("missing_fields", f"비어 있거나 틀린 인자: {', '.join(bad)}. title · reason · spec · done_criteria 는 "
                      f"모두 적고 type 은 {' · '.join(TASK_TYPES)} 중 하나다", fields=bad)
    deps = [dep.strip() for dep in args.get("depends_on") or [] if not _blank(dep)]
    extra: dict[str, Any] = {}  # the optional payload keys of §5.3
    if _text(args.get("suggested_assignee_id")):
        extra["suggested_assignee_id"] = _text(args["suggested_assignee_id"])
    if deps:
        extra["depends_on"] = deps
    if caller.task_id:
        extra["source_task_id"] = caller.task_id  # the turn the proposal was made in
    extra["source_run_id"] = caller.run_id
    return machine.create_task(
        caller.actor, "proposed", project_id, title=args["title"], reason=args["reason"], spec=args["spec"],
        done_criteria=args["done_criteria"], type=args["type"], campaign_id=_text(args.get("campaign_id")),
        parent_id=parent["id"] if parent else None, depends_on=deps or None, inbox={"payload": extra})


def task_propose(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    """C1: a proposed task and its task_proposal item. The project is the argument's, then this turn's
    task's, then the one the caller is placed in."""
    made = _propose(caller, args, machine)
    return ok(f"작업 {made.task['id']} 를 제안했다 (결재 {made.inbox['id']}). 승인되면 backlog 에 들어간다. 하던 일을 계속한다",
              task_id=made.task["id"], inbox_id=made.inbox["id"])


def followup_propose(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    """C1 with a parent (S-12): `parent_task_id`, by default the task of this turn."""
    parent_id = _text(args.get("parent_task_id")) or caller.task_id
    if parent_id is None:
        raise Refusal("no_task_context", "이 턴에는 작업이 없다. 부모 작업을 parent_task_id 로 적는다")
    parent = _visible_task(caller, machine, parent_id)
    made = _propose(caller, args, machine, parent)
    return ok(f"후속 작업 {made.task['id']} 를 제안했다 (부모 {parent['id']}, 결재 {made.inbox['id']}). 하던 일을 계속한다",
              task_id=made.task["id"], inbox_id=made.inbox["id"], parent_id=parent["id"])


# --- task_get · task_list (reads) -------------------------------------------------------------------------
def task_get(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    task_id = _text(args.get("task_id")) or caller.task_id
    if task_id is None:
        raise Refusal("no_task_context", "이 턴에는 작업이 없다. 볼 작업을 task_id 로 적는다")
    task = _visible_task(caller, machine, task_id)
    store = machine.store
    pending = store.list_inbox(task_id=task_id, status="pending")  # at most one (inbox_one_pending_per_task)
    return ok(f"작업 {task_id} — {task['title']} ({task['status']})", task=task,
              depends_on=[dep["depends_on_id"] for dep in store.list_task_deps(task_id=task_id)],
              dependents=[dep["task_id"] for dep in store.list_task_deps(depends_on_id=task_id)],
              pending_inbox={key: pending[0][key] for key in PENDING} if pending else None,
              recent_events=store.list_task_events(task_id)[-RECENT_EVENTS:])


def task_list(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    status = _text(args.get("status"))
    if status is not None and status not in STATES:
        raise Refusal("missing_fields", f"status 는 {' · '.join(STATES)} 중 하나다 (비우면 전체)", fields=["status"])
    assignee = _text(args.get("assignee"))
    if assignee == "me":
        assignee = caller.id
    limit = args.get("limit")
    limit = DEFAULT_LIMIT if isinstance(limit, bool) or not isinstance(limit, int) else min(max(limit, 1), MAX_LIMIT)
    project_id = None if caller.is_hr else caller.employee["project_id"]
    rows: list[dict] = []
    if caller.is_hr or project_id is not None:  # not placed anywhere: nothing to see
        rows = machine.store.list_tasks(project_id=project_id, campaign_id=_text(args.get("campaign_id")),
                                        status=status, assignee_id=assignee)
    items = [{key: row[key] for key in LISTED} for row in rows[:limit]]
    more = f" ({len(rows)}개 중 앞의 {limit}개다. status · assignee 로 좁힌다)" if len(rows) > limit else ""
    return ok(f"작업 {len(items)}개{more}", items=items)


# --- task_progress ----------------------------------------------------------------------------------------
def task_progress(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    task = _own_task(caller, machine, "진행 상황을 남길")
    percent = args.get("percent")
    bad = ["note"] if _blank(args.get("note")) else []
    if percent is not None and (isinstance(percent, bool) or not isinstance(percent, (int, float))
                                or not 0 <= percent <= 100):
        bad.append("percent")
    if bad:
        raise Refusal("missing_fields", f"비어 있거나 틀린 인자: {', '.join(bad)}. note 는 한 일을 적은 글이고 "
                      "percent 는 0–100 (생략 가능)이다", fields=bad)
    if task["status"] not in PROGRESS_STATES:
        raise Refusal("precondition_failed", f"진행 상황은 작업이 {' · '.join(PROGRESS_STATES)} 일 때만 남긴다 "
                      f"(지금 {task['status']})", failed=[])
    detail: dict[str, Any] = {"run_id": caller.run_id}
    if percent is not None:
        detail["percent"] = int(percent) if float(percent).is_integer() else percent
    event = machine.record(task["id"], caller.actor, "progress", args["note"].strip(), detail).event
    return ok("진행 상황을 남겼다. 하던 일을 계속한다", event_id=event["id"])


# --- task_request_review (T6) -----------------------------------------------------------------------------
def task_request_review(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    task = _own_task(caller, machine, "검수를 요청할")
    _legal(task, "review")
    payload: dict[str, Any] = {"summary": args.get("summary"), "checklist": args.get("checklist")}
    for key in ("evidence", "judgment_calls"):
        if args.get(key):
            payload[key] = args[key]
    bad = validate_payload("final_review", {**payload, "exchange_count": task["exchange_count"]})
    if "evidence" not in bad and any(_blank(item.get("ref")) for item in payload.get("evidence") or []):
        bad.append("evidence")
    if bad:
        raise Refusal("missing_fields", f"비어 있거나 틀린 인자: {', '.join(bad)}. summary 와 checklist(완료 기준마다 "
                      "{criterion, status, note}, status 는 verified_auto · verified_manual · unverified)가 필수이고 "
                      "evidence 는 각 {kind, ref, caption}, judgment_calls 는 각 {text, why} 다", fields=bad)
    _file_args("task_request_review", args)
    item = inbox.create("final_review", caller.actor, payload=payload, task_id=task["id"])
    return ok(f"검수를 요청했다 (결재 {item['id']}). 결과는 이 작업방에 온다.", end_turn=True, inbox_id=item["id"])


# --- ask_owner (T4, §5.7) ---------------------------------------------------------------------------------
def _option(option: Any, *, detail: bool) -> Any:
    """An option as it is stored (§5.3): {id, label}, and for a choice detail and figure (null = no figure)."""
    if not isinstance(option, dict):
        return option  # left as it is: the validation names `options`
    out: dict[str, Any] = {"id": option.get("id"), "label": option.get("label")}
    if detail:
        figure = option.get("figure")
        if isinstance(figure, dict) and set(figure) == {"mermaid"}:
            figure = {"mermaid": figure["mermaid"]}
        out["detail"] = option.get("detail")
        out["figure"] = None if figure in ("", None) else figure
    return out


def _figure_ok(option: Any) -> bool:
    figure = option.get("figure") if isinstance(option, dict) else None
    if figure is None:
        return True
    return isinstance(figure, dict) and (not _blank(figure.get("mermaid")) or not _blank(figure.get("path")))


def _ask_payload(kind: str, args: dict) -> dict:
    options = args.get("options") or []
    if kind == "question":
        payload: dict[str, Any] = {"reason": "asked", "body": args.get("body")}
        if options:
            payload["options"] = [_option(option, detail=False) for option in options]
    elif kind == "structure_choice":
        payload = {"question": args.get("body"), "options": [_option(option, detail=True) for option in options]}
        for key in ("recommended", "fix_points"):
            if _text(args.get(key)):
                payload[key] = _text(args[key])
    else:
        payload = {key: args.get(key) for key in ("what", "why", "if_not")}
        if _text(args.get("policy_ref")):
            payload["policy_ref"] = _text(args["policy_ref"])
    return payload


def ask_owner(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    """T4 by a member of the task room — the assignee in its task run, or another member in a read turn
    (§5.7: the item's run is the task run either way; the machine writes that)."""
    if caller.task_id is None:
        raise Refusal("no_task_context", "이 턴에는 작업이 없어 결재를 올릴 수 없다. 방에 글로 그대로 묻는다")
    task = _turn_task(caller, machine, "결재를 올릴")
    store = machine.store
    rooms = store.list_rooms(kind="task", task_id=task["id"], include_archived=True)
    member = store.get_room_member(rooms[0]["id"], caller.id) if rooms else None
    if member is None or member["left_at"] is not None:
        raise Refusal("actor_not_allowed", "그 작업방의 구성원만 결재를 올린다")
    _legal(task, "blocked")
    kind = ASK_KINDS.get(args.get("kind")) if isinstance(args.get("kind"), str) else None
    bad = ([] if kind else ["kind"]) + (["title"] if _blank(args.get("title")) else [])
    if kind is None:
        raise Refusal("missing_fields", f"비어 있거나 틀린 인자: {', '.join(bad)}. kind 는 {' · '.join(ASK_KINDS)} 중 하나다",
                      fields=bad)
    payload = _ask_payload(kind, args)
    names = {"question": "body"} if kind == "structure_choice" else {}  # payload key -> the argument's name
    bad += [names.get(key, key) for key in validate_payload(kind, payload)]
    if kind == "structure_choice" and "options" not in bad and not all(map(_figure_ok, payload["options"])):
        bad.append("options")
    if bad:
        raise Refusal("missing_fields", f"비어 있거나 틀린 인자: {', '.join(bad)}. {ASK_HINT[kind]}", fields=bad)
    _file_args("ask_owner", args)
    item = inbox.create(kind, caller.actor, title=args["title"], payload=payload, task_id=task["id"])
    return ok(f"결재를 올렸다 ({item['id']}). 결정은 이 작업방에 온다.", end_turn=True, inbox_id=item["id"])


# --- room_post (§8.4 · §8.5; the attachments are §6.6 '직원의 첨부') -------------------------------------------
EMPLOYEE_LISTED = ("id", "name", "department_id", "project_id", "status")
AMEND_ARG = {"PERSONA.md": "persona_md", "GUIDE.md": "guide_md", "employee.yaml": "employee_yaml"}  # file -> its text
TRIMMED = ("name", "department_id", "project_id", "part_name", "part_relay")  # the draft keys that are a name or an id, not a text


def _root(inbox: Inbox) -> Path:
    """runs/studio: where attachments and drafts are written. A server without it is a bug, not a refusal."""
    if inbox.studio_dir is None:
        raise RuntimeError("the studio tools that write files need an Inbox made with studio_dir= (§2.2)")
    return inbox.studio_dir


def _checks(inbox: Inbox) -> employees.Checks:
    """What the checks of an employee.yaml know of the server (§9.4). Without it no check is skipped: a bug."""
    if inbox.checks is None:
        raise RuntimeError("HR's draft tools need an Inbox made with checks= (employees.context, §9.4)")
    return inbox.checks


def _may_post(caller: Caller, store: Any, room: dict) -> bool:
    """§8.5, by the DB of now: a task, a group and a department room = its members (a row not left), a dm =
    one's own, the system room = HR (the daily report, S-13)."""
    if room["kind"] in ("task", "group", "department"):
        member = store.get_room_member(room["id"], caller.id)
        return member is not None and member["left_at"] is None
    if room["kind"] == "dm":
        return room["employee_id"] == caller.id
    return caller.is_hr


def _attachment_roots(caller: Caller, store: Any) -> list[Path]:
    """§6.6: the folder the run works in, and the project's folder — this turn's task's project, without a
    task the one the caller is placed in. HR and an employee placed nowhere have the first only."""
    task = store.get_task(caller.task_id) if caller.task_id else None
    project_id = task["project_id"] if task else caller.employee["project_id"]
    project = store.get_project(project_id) if project_id else None
    roots = [caller.workdir] if caller.workdir else []
    if project and (project["path"] or "").strip():
        roots.append(Path(project["path"]))
    return roots


def _before_mentions(caller: Caller, task: dict, room: dict, mentions: list[str], machine: TaskMachine,
                     inbox: Inbox) -> None:
    """What the mentions of a task room are refused with before the message is made (S-9). First the read turns
    that cannot be made (§9.5 의 5 가): `precondition_failed` with the codes in `failed`. Then the limit (§4.6) —
    last of all the checks, and once for the message: one that would have been refused anyway asks the owner
    nothing."""
    stuck = {m: codes for m in mentions if m != task["assignee_id"]
             and (codes := inbox.hooks.turn_failed(m, room["id"]))}
    if stuck:
        raise Refusal("precondition_failed", "멘션한 직원의 턴을 넣을 수 없어 글을 쓰지 않았다: "
                      + ", ".join(f"{m} ({', '.join(codes)})" for m, codes in stuck.items())
                      + ". 그 직원의 채팅 모델이 Claude Code 가 아니다 — 멘션을 빼고 쓰거나 owner 에게 알린다",
                      failed=list(dict.fromkeys(code for codes in stuck.values() for code in codes)))
    if inbox.max_exchanges is not None and task["exchange_count"] >= inbox.max_exchanges:
        raise _at_limit(caller, task, mentions, machine, inbox)


def _at_limit(caller: Caller, task: dict, mentions: list[str], machine: TaskMachine, inbox: Inbox) -> Refusal:
    """§4.6 · §4.5: the count is at the limit — no message, no turn, nothing counted. A `limit` row of the task,
    always. While the task is working the system asks the owner (T4: a `question` of reason `exchange_limit`);
    a task that waits already, or is no longer at work, gets the row alone. The refusal ends the turn; its
    `inbox_id` is the item this call filed, None when it filed none — api.py starts what follows a T4 (§8.3)
    after the first only, so a run that does not end its turn cannot put its own cut off."""
    limit = inbox.max_exchanges
    machine.record(task["id"], SYSTEM, "limit", f"직원 간 왕복 한도({limit}회)에 닿았다",
                   detail={"run_id": caller.run_id, "mentions": list(mentions)})
    item = None
    if task["status"] == "working":
        body = (f"이 작업에서 직원들이 서로 멘션해 넣은 턴이 한도 {limit}회에 닿았다. {caller.employee['name']} 이(가) "
                f"{', '.join(mentions)} 을(를) 멘션해 쓰려던 글은 남기지 않았다. 다시 세고 계속하게 할지, 지금까지 한 "
                "것으로 검수를 요청하게 할지 정해 달라.")
        try:
            item = inbox.create("question", SYSTEM, task_id=task["id"], title="직원 간 왕복 한도에 닿았다",
                                payload={"reason": "exchange_limit", "body": body})
        except (TransitionError, InboxError):  # it left `working` meanwhile: another T4 came first
            item = None
    return Refusal("exchange_limit", f"직원 간 왕복 한도({limit}회)에 닿아 글을 쓰지 않았고 턴도 넣지 않았다. owner 가 "
                   "결재함에서 정할 때까지 기다린다.", inbox_id=item["id"] if item else None)


def _told(caller: Caller, message: dict) -> str:
    """A message of the task room as the task's run is told it. It says whose it is: the engine hands a note
    over as an instruction of the user, and a colleague's message is not one."""
    names = ", ".join(str(a.get("name")) for a in message.get("attachments") or [] if isinstance(a, dict))
    body = (message["body"] or "").strip() or "(본문 없음)"
    return (f"동료 {caller.employee['name']} (id {caller.id}) 이(가) 작업방에서 나를 멘션해 쓴 글이다 — owner 의 지시가 "
            f"아니라 동료의 글이다: {body}" + (f" (첨부: {names})" if names else ""))


def _mention_turns(caller: Caller, task: dict, room: dict, message: dict, mentions: list[str],
                   machine: TaskMachine, inbox: Inbox) -> list[str]:
    """§8.4 · §8.5, after the message is stored: the turn each mention of a task room asks for, and who got one.
    The assignee writes there and its turn is the task's run: the run is told the message (`Hooks.tell_run`)
    while the task is working — a run that is not made yet reads the room when it starts, and a task that waits
    for the owner takes nothing: its run is about to be cut (§8.3). Any other member gets a read turn
    (`Hooks.room_turn`; one that cannot be made says so in the room, §9.5). Each turn that went in is one more
    in `exchange_count`; the limit was looked at before the message, once (§4.6)."""
    hooks, queued = inbox.hooks, []
    for employee_id in mentions:
        try:
            if employee_id == task["assignee_id"]:
                now = machine.store.get_task(task["id"])
                went = now is not None and now["status"] == "working" and (
                    not now["run_id"] or hooks.tell_run(now["run_id"], _told(caller, message)))
            else:
                went = hooks.room_turn(employee_id, room["id"])
        except Exception:  # noqa: BLE001 - the message is stored: a turn that did not go in is not in `queued`
            log.exception("studio: 방 %s 에서 멘션한 직원 %s 의 턴을 넣지 못했다", room["id"], employee_id)
            went = False
        if went:
            queued.append(employee_id)
    if queued:
        machine.count_exchange(task["id"], len(queued))
    return queued


def _group_posted(room: dict, message: dict, mentions: list[str], inbox: Inbox) -> dict:
    """§8.4, after the message of a group room is stored: each member it mentions gets a turn at the end of the
    room's line (`Hooks.group_turns`) — `queued` is who waits there for it. Nothing is refused or counted here:
    the limit is looked at when that turn's time comes, and the turn is counted when it left a message (§4.6;
    studio/chat.py). The message stays in the room either way."""
    queued: list[str] = []
    if mentions:
        try:
            queued = list(inbox.hooks.group_turns(room["id"], list(mentions), True))
        except Exception:  # noqa: BLE001 - the message is stored: a turn that did not go in is not in `queued`
            log.exception("studio: 단체방 %s 에서 멘션한 직원의 턴을 넣지 못했다", room["id"])
    missed = [m for m in mentions if m not in queued]
    note = "방에 글을 남겼다."
    if queued:
        note += (f" 멘션한 직원의 턴이 차례에 들어갔다: {', '.join(queued)} — 이 턴이 끝난 뒤에 돌고, 직원 간 왕복 한도에 "
                 "닿아 있으면 owner 가 이어 가게 할 때까지 선다. 답을 기다리지 않는다.")
    if missed:
        note += (f" 턴이 들어가지 않은 직원: {', '.join(missed)} — 퇴사한 직원이거나 턴을 넣을 수 없었다. 글은 방에 남아 "
                 "있다.")
    return ok(f"{note} 하던 일을 계속한다", message_id=message["id"], queued=queued)


def room_post(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    """A message of the employee in a room it may write in, with `message.created`, in one transaction.
    Every check — the attachments' too — ends before the message is made (§6.6). A dict in `handoff` is stored
    in `meta.handoff` as it is.

    A mention in a task room asks for that employee's turn (S-9): `queued` is who got one, and each is one more
    in `exchange_count` (§4.6). `_before_mentions` refuses before the message — a read turn that cannot be made,
    the limit —, `_mention_turns` puts the turns in after it. A mention in a group room or a department room asks
    for that member's turn too (`_group_posted`): it goes into the room's line, and the limit of such a room is looked at when
    its time comes, not here (§4.6). In any other room `queued` is []."""
    store = machine.store
    room_id = _text(args.get("room")) or caller.room_id
    room = store.get_room(room_id) if room_id else None
    if room is None:
        raise Refusal("not_found", f"방 {room_id or ''} 이 없다. room 을 비우면 지금 방에 쓴다")
    if not _may_post(caller, store, room):
        raise Refusal("actor_not_allowed", "이 방에는 글을 쓸 수 없다 — 작업방 · 단체방 · 파트 방은 구성원, "
                      "1:1 방은 자기 방, 시스템 방은 인사만 쓴다")
    if room["archived_at"] is not None:
        raise Refusal("precondition_failed", "보관된 방에는 글을 쓸 수 없다", failed=[])
    body = args["text"] if isinstance(args.get("text"), str) else ""
    specs = args.get("attachments") or []
    bad = ["text"] if not body.strip() and not specs else []
    if not all(isinstance(spec, dict) and not _blank(spec.get("path")) and isinstance(spec.get("name") or "", str)
               for spec in specs):
        bad.append("attachments")
    if bad:
        raise Refusal("missing_fields", f"비어 있거나 틀린 인자: {', '.join(bad)}. text 나 attachments 중 하나는 있어야 "
                      "하고 attachments 는 각 {path, name=\"\"} 다", fields=bad)
    mentions = list(dict.fromkeys(m.strip() for m in args.get("mentions") or [] if not _blank(m)))
    if caller.id in mentions:
        raise Refusal("precondition_failed", "자기 자신은 멘션할 수 없다", failed=[])
    members = {member["employee_id"] for member in store.list_room_members(room_id=room["id"])}
    strangers = [m for m in mentions if m not in members]
    if strangers:
        raise Refusal("precondition_failed", f"이 방의 구성원이 아닌 직원을 멘션했다: {', '.join(strangers)}",
                      failed=[])
    found, refused = files.check(specs, _attachment_roots(caller, store), caller.workdir)
    if refused:
        raise Refusal("precondition_failed", "붙일 수 없는 파일이 있어 글을 쓰지 않았다: "
                      + ", ".join(f"{path} ({code})" for code, path in refused)
                      + f". 이 런의 작업 폴더나 프로젝트 폴더 안에 있는 {files.MAX_ATTACHMENT_MB}MB 이하 파일만 붙인다",
                      failed=list(dict.fromkeys(code for code, _ in refused)))
    task = store.get_task(room["task_id"]) if room["kind"] == "task" and room["task_id"] and mentions else None
    if task is not None:
        _before_mentions(caller, task, room, mentions, machine, inbox)
    meta = {"handoff": args["handoff"]} if isinstance(args.get("handoff"), dict) else {}
    folder: Path | None = None
    try:
        with store.transaction() as tx:
            message = store.create_message(room["id"], "employee", tx=tx, author_id=caller.id, body=body,
                                           mentions=mentions, meta=meta, run_id=caller.run_id)
            if found:  # the folder is named after the message id, known only now
                folder = _root(inbox) / "attachments" / str(message["id"])
                message = store.set_message_attachments(tx, message["id"], files.copy(found, folder))
            store.emit(tx, "message.created", {"message": message}, room_id=room["id"], task_id=room["task_id"],
                       project_id=files.room_project(store, room, tx))
    except BaseException as e:
        if folder is not None:  # rolled back: the id may be handed out again, so its folder must not stay
            shutil.rmtree(folder, ignore_errors=True)
        if isinstance(e, OSError):  # a file that was there at the check and could not be copied
            raise Refusal("precondition_failed", f"첨부를 복사하지 못해 글을 쓰지 않았다: {e}",
                          failed=[files.NOT_FOUND]) from e
        raise
    if room["kind"] in GROUP_KINDS:
        return _group_posted(room, message, mentions, inbox)
    if task is None:
        return ok("방에 글을 남겼다. 하던 일을 계속한다", message_id=message["id"], queued=[])
    queued = _mention_turns(caller, task, room, message, mentions, machine, inbox)
    missed = [m for m in mentions if m not in queued]
    note = "방에 글을 남겼다."
    if queued:
        note += f" 멘션한 직원의 턴이 들어갔다: {', '.join(queued)}."
    if missed:
        note += (f" 턴이 들어가지 않은 직원: {', '.join(missed)} — 턴을 만들지 못했거나(이유는 방의 시스템 글에 있다) "
                 "담당인데 작업이 진행 중이 아니다. 글은 방에 남아 있다.")
    return ok(f"{note} 하던 일을 계속한다", message_id=message["id"], queued=queued)


# --- employee_list (read) ---------------------------------------------------------------------------------
def employee_list(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    """The employees in service of the caller's project; HR sees all. `status` is the stored column."""
    project_id, department_id = _text(args.get("project_id")), _text(args.get("department_id"))
    own = caller.employee["project_id"]
    rows: list[dict] = []
    if caller.is_hr:
        rows = machine.store.list_employees(department_id=department_id, project_id=project_id)
    elif own is not None and project_id in (None, own):  # not placed anywhere: nothing to see
        rows = machine.store.list_employees(department_id=department_id, project_id=own)
    return ok(f"직원 {len(rows)}명", items=[{key: row[key] for key in EMPLOYEE_LISTED} for row in rows])


# --- HR's three: employee_draft · employee_submit · policy_draft (§8.4 인사 전용) ----------------------------
def _hr_only(caller: Caller, what: str) -> None:
    if not caller.is_hr:
        raise Refusal("actor_not_allowed", f"인사 직원만 {what} 수 있다")


def _base(path: Path) -> tuple[str, str]:
    try:
        return drafts.file_base(path)
    except OSError as e:
        raise Refusal("precondition_failed", f"지금 파일을 읽지 못했다: {e}", failed=[]) from e


def _draft(root: Path, draft_id: str) -> dict:
    draft = drafts.load(root, draft_id)
    if draft is None:
        raise Refusal("not_found", f"초안 {draft_id} 이 없다. employee_draft 가 돌려준 draft_id 를 적는다")
    return draft


def _hire_draft(store: Any, args: dict, old: dict | None, checks: employees.Checks) -> dict:
    body = {key: "" for key in DRAFT_KEYS}
    body.update({key: value for key, value in ((old or {}).get("draft") or {}).items() if key in DRAFT_KEYS})
    was_new = employees.proposes_part(body)  # the saved draft already proposed a part: its id stays
    for key in DRAFT_KEYS:  # an empty argument keeps what the draft has
        if not _blank(args.get(key)):
            body[key] = args[key].strip() if key in TRIMMED else args[key]
    if employees.proposes_part(body):  # a new part: its id is the server's, and the yaml names it by the token
        if not _blank(args.get("department_id")):
            hint = (" 이 초안은 이미 새 파트를 제안했다 — 기존 파트로 채용하려면 draft_id 를 비워 새 초안을 만든다"
                    if was_new else "")
            raise Refusal("precondition_failed", "새 파트를 제안할 때는 department_id 를 적지 않는다 — 서버가 "
                          "새 파트의 id 를 정해 돌려준다. employee_yaml 의 department 에는 {department_id} 를 적는다."
                          + hint, failed=[])
        body["department_id"] = body["department_id"] if was_new else "dep_" + uuid.uuid4().hex[:8]
        body["employee_yaml"] = body["employee_yaml"].replace("{department_id}", body["department_id"])
    missing = [key for key in DRAFT_REQUIRED if _blank(body[key])]
    if missing:
        raise Refusal("missing_fields", f"비어 있는 인자: {', '.join(missing)}. 채용 초안은 "
                      f"{' · '.join(DRAFT_REQUIRED)} 가 모두 있어야 한다", fields=missing)
    return {"action": "hire", "draft": body, "problems": drafts.hire_problems(store, body, checks)}


def _amend_draft(root: Path, store: Any, args: dict, old: dict | None, target_id: str,
                 checks: employees.Checks) -> dict:
    target = store.get_employee(target_id)
    if target is None or target["retired_at"] is not None:
        raise Refusal("not_found", f"직원 {target_id} 이 없거나 퇴사했다. employee_list 로 id 를 확인한다")
    file = _text(args.get("file")) or (old or {}).get("file")
    if file not in AMEND_FILES:
        raise Refusal("missing_fields", f"file 은 {' · '.join(AMEND_FILES)} 중 하나다", fields=["file"])
    key = AMEND_ARG[file]
    new_text = args.get(key)
    if _blank(new_text):
        new_text = old["new_text"] if old and old.get("file") == file else None
    if new_text is None:
        raise Refusal("missing_fields", f"{file} 의 새 본문을 {key} 에 적는다", fields=[key])
    base_text, sha = _base(root / "employees" / target["id"] / file)
    return {"action": "guide_amend", "target_employee_id": target["id"], "file": file, "new_text": new_text,
            "base_sha256": sha, "diff": drafts.diff(base_text, new_text, file),
            "problems": drafts.amend_problems(store, target, file, new_text, checks)}


def employee_draft(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    """Make or change a draft: of a hire, or with `target_employee_id` of an amendment of one file. It is
    saved with its problems; `employee_submit` is what refuses them."""
    _hr_only(caller, "직원 초안을 만들")
    root, store = _root(inbox), machine.store
    draft_id, target_id = _text(args.get("draft_id")), _text(args.get("target_employee_id"))
    old: dict | None = None
    if draft_id:
        old = _draft(root, draft_id)
        if old.get("kind") != "employee":
            raise Refusal("precondition_failed", "그것은 정책 초안이다. 정책은 policy_draft 로 고친다", failed=[])
        if target_id and old.get("target_employee_id") != target_id:
            raise Refusal("precondition_failed", "그 초안은 다른 것(채용 초안이거나 다른 직원의 수정안)이다. "
                          "draft_id 를 비우면 새 초안을 만든다", failed=[])
        target_id = target_id or old.get("target_employee_id")
    checks = _checks(inbox)
    made = (_amend_draft(root, store, args, old, target_id, checks) if target_id
            else _hire_draft(store, args, old, checks))
    saved = drafts.save(root, {"id": draft_id or drafts.new_id(root), "kind": "employee", "author_id": caller.id,
                               **made}, old)
    problems = saved["problems"]
    state = ("문제가 있다: " + " / ".join(p["message"] for p in problems) + ". 고쳐서 같은 draft_id 로 다시 부른다"
             if problems else "employee_submit 으로 올릴 수 있다")
    extra = ({"department_id": saved["draft"]["department_id"]}
             if saved["action"] == "hire" and employees.proposes_part(saved["draft"]) else {})
    return ok(f"초안 {saved['id']} 을 저장했다. {state}", draft_id=saved["id"], action=saved["action"],
              problems=problems, **extra)


def _source(args: dict, payload: dict) -> dict:
    if _text(args.get("source_inbox_id")):
        payload["source_inbox_id"] = _text(args["source_inbox_id"])  # the review that called for it (§5.3)
    return payload


def employee_submit(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    """File the `hire` item of a draft (`action` hire · guide_amend), with a copy of the draft's body. The
    draft is checked against the DB and the file of now; a pending item of the same draft is replaced by
    Inbox (§5.5)."""
    _hr_only(caller, "초안을 결재로 올릴")
    root, store = _root(inbox), machine.store
    bad = [key for key in ("draft_id", "reason") if _blank(args.get(key))]
    if bad:
        raise Refusal("missing_fields", f"비어 있는 인자: {', '.join(bad)}", fields=bad)
    draft = _draft(root, args["draft_id"].strip())
    if draft.get("kind") != "employee":
        raise Refusal("precondition_failed", "정책 초안은 policy_draft(submit=True) 로 올린다", failed=[])
    payload: dict[str, Any] = {"action": draft["action"], "draft_id": draft["id"], "reason": args["reason"].strip()}
    if draft["action"] == "hire":
        body = draft["draft"]
        problems = drafts.hire_problems(store, body, _checks(inbox))
        payload["draft"] = {key: body.get(key, "") for key in DRAFT_KEYS}
        title, project_id = f"채용: {body['name']}", _text(body.get("project_id"))
        if employees.proposes_part(body):  # the owner sees the new part in the title (§5.5)
            title += f" — 새 파트 {body['part_name'].strip()} 를 만든다"
    else:
        target = store.get_employee(draft["target_employee_id"])
        problems = drafts.amend_problems(store, target, draft["file"], draft["new_text"], _checks(inbox))
        if target is None or target["retired_at"] is not None:
            problems.append(drafts.problem("target_not_found", "대상 직원이 없거나 퇴사했다"))
        elif _base(root / "employees" / target["id"] / draft["file"])[1] != draft["base_sha256"]:
            problems.append(drafts.problem("file_changed", f"초안을 만든 뒤에 {draft['file']} 이 바뀌었다 — "
                                           "employee_draft 를 같은 draft_id 로 다시 불러 지금 파일에 맞춘다"))
        payload.update({key: draft[key] for key in ("target_employee_id", "file", "new_text", "diff", "base_sha256")})
        title = f"지침 수정: {target['name'] if target else draft['target_employee_id']} · {draft['file']}"
        project_id = target["project_id"] if target else None
    if problems:
        raise Refusal("precondition_failed", "초안에 문제가 있어 올리지 않았다: "
                      + " / ".join(p["message"] for p in problems) + ". employee_draft 로 고친 뒤 다시 올린다",
                      failed=[p["code"] for p in problems])
    item = inbox.create("hire", caller.actor, title=title, payload=_source(args, payload), project_id=project_id)
    return ok(f"결재를 올렸다 ({item['id']}). 결정은 내 1:1 방에 온다", inbox_id=item["id"])


def policy_draft(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    """Save the draft of a project's POLICY.md — one per project — and return its diff against the file of
    now; with `submit` also file the `hire` item (`action` policy)."""
    _hr_only(caller, "정책 초안을 만들")
    root, store = _root(inbox), machine.store
    bad = [key for key in ("project_id", "policy_md", "reason") if _blank(args.get(key))]
    if bad:
        raise Refusal("missing_fields", f"비어 있는 인자: {', '.join(bad)}", fields=bad)
    project = store.get_project(args["project_id"].strip())
    if project is None:
        raise Refusal("not_found", f"프로젝트 {args['project_id'].strip()} 이 없다")
    base_text, sha = _base(root / "projects" / project["id"] / "POLICY.md")
    draft_id, reason, new_text = drafts.policy_id(project["id"]), args["reason"].strip(), args["policy_md"]
    saved = drafts.save(root, {
        "id": draft_id, "kind": "policy", "action": "policy", "author_id": caller.id, "project_id": project["id"],
        "reason": reason, "new_text": new_text, "base_sha256": sha,
        "diff": drafts.diff(base_text, new_text, "POLICY.md"), "problems": []}, drafts.load(root, draft_id))
    inbox_id = None
    if args.get("submit") is True:
        payload = {"action": "policy", "draft_id": draft_id, "reason": reason, "new_text": new_text,
                   "diff": saved["diff"], "base_sha256": sha}
        inbox_id = inbox.create("hire", caller.actor, title=f"정책 수정: {project['name']}",
                                payload=_source(args, payload), project_id=project["id"])["id"]
    state = f"결재를 올렸다 ({inbox_id})" if inbox_id else "올리려면 submit=True 로 다시 부른다"
    return ok(f"정책 초안 {draft_id} 을 저장했다. {state}", draft_id=draft_id, diff=saved["diff"], inbox_id=inbox_id)


# --- the hiring pair (T5): hire_recommend (HR) · staff_request (any employee) ------------------------------
RECOMMEND_TEXT = ("reason", "role", "model", "effort", "department_id", "part_name", "part_job", "part_relay",
                  "part_models", "project_id", "signal_key")
REQUEST_WHY = {  # staffing.Staffing.request -> `why`: what the employee is told
    "asked": "인사에게 채용이 필요한지 판단해 달라고 요청했다.",
    "open": "같은 파트·프로젝트의 채용 권장이 이미 owner 의 결재를 기다린다.",
    "skipped": "같은 파트·프로젝트의 채용 권장을 얼마 전에 넘겼다. 한동안 다시 올리지 않는다.",
    "recently": "같은 파트·프로젝트로는 얼마 전에 인사에게 이미 물었다.",
    "unheard": "물을 인사 직원이 없어 요청이 전해지지 않았다.",
}
REQUEST_NOTE = " 인사가 판단한다 — 채용 권장은 보장이 아니다."


def hire_recommend(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    """HR raises a recommendation to hire (§5.3 채용 권장) -> an inbox item `hire_recommendation` for the owner. The
    evidence `numbers` is the server's (the staff of the part now, those placed in the project), the title too.
    A trigger's key goes in `signal_key`. The filing is Inbox's (`conflict` · `precondition_failed` on a key that
    waits or was skipped); on success one line stays in HR's 1:1 room."""
    _hr_only(caller, "채용 권장을 올릴")
    store = machine.store
    payload: dict[str, Any] = {key: _text(args.get(key)) for key in RECOMMEND_TEXT if _text(args.get(key))}
    ids = args.get("task_ids")
    if isinstance(ids, list) and ids:
        payload["task_ids"] = [i.strip() for i in ids if isinstance(i, str) and i.strip()]
    project_id = payload.get("project_id") or caller.employee["project_id"]
    if project_id:
        payload["project_id"] = project_id
    department_id = payload.get("department_id")
    staff = store.list_employees(department_id=department_id) if department_id else []
    placed = sum(1 for e in staff if project_id and e["project_id"] == project_id)
    payload["numbers"] = {"staff": len(staff), "placed": placed}
    department = store.get_department(department_id) if department_id else None
    part = department["name"] if department else payload.get("part_name", "")
    title = f"채용 권장: {part} · {payload.get('role', '')}".strip(" ·:")
    item = inbox.create(RECOMMEND, caller.actor, title=title, payload=payload, project_id=project_id)
    inbox.note_recommendation(item)
    return ok("채용 권장을 올렸다. owner 가 면담을 열면 이 방에서 채용 면담이 이어진다. 더 할 일은 없다 — output 은 비운다.",
              inbox_id=item["id"])


def staff_request(caller: Caller, args: dict, machine: TaskMachine, inbox: Inbox) -> dict:
    """An employee tells HR that help is needed (§5.3 판단 요청 · 계기 다): `staffing.Staffing.request`. It asks HR to
    judge; it files nothing for the owner, and HR may recommend nothing."""
    reason = _text(args.get("reason"))
    if reason is None:
        raise Refusal("missing_fields", "reason(왜 사람이 더 필요한지)을 적는다", fields=["reason"])
    task_id = _text(args.get("task_id")) or caller.task_id
    if task_id and machine.store.get_task(task_id) is None:
        raise Refusal("not_found", f"작업 {task_id} 이 없다. task_list 로 id 를 확인한다")
    staffing_ = staffing.Staffing(machine.store, ask=inbox.hooks.staffing_ask, cooldown_days=inbox.hire_cooldown_days)
    try:
        result = staffing_.request(caller.id, reason=reason, role=_text(args.get("role")) or "", task_id=task_id,
                                   department_id=_text(args.get("department_id")),
                                   project_id=_text(args.get("project_id")))
    except Exception as e:  # noqa: BLE001 - the question to HR failed; the employee goes on with the work
        log.exception("studio: 직원의 채용 요청을 인사에게 전하지 못했다")
        raise Refusal("precondition_failed", f"요청을 전하지 못했다 ({type(e).__name__}). 하던 일을 계속한다", failed=[]) from e
    if result["why"] == "invalid":
        raise Refusal("precondition_failed", "요청을 받을 수 없다 — 파트나 프로젝트를 정할 수 없다. department_id · "
                      "project_id · task_id 중 하나로 알려 준다", failed=[])
    return ok(REQUEST_WHY.get(result["why"], result["why"]) + REQUEST_NOTE, asked=result["asked"], why=result["why"])


# --- the one way in ---------------------------------------------------------------------------------------
TOOLS: dict[str, Callable[[Caller, dict, TaskMachine, Inbox], dict]] = {
    "task_propose": task_propose, "followup_propose": followup_propose, "task_get": task_get,
    "task_list": task_list, "task_progress": task_progress, "task_request_review": task_request_review,
    "ask_owner": ask_owner, "room_post": room_post, "employee_list": employee_list,
    "employee_draft": employee_draft, "employee_submit": employee_submit, "policy_draft": policy_draft,
    "hire_recommend": hire_recommend, "staff_request": staff_request,
}


def call(name: str, caller: Caller, args: dict | None, *, machine: TaskMachine, inbox: Inbox) -> dict:
    """Run one tool and return the dict of §8.3. A refusal — the tool's own, the machine's (TransitionError),
    the inbox's (InboxError), a task that is gone — is `{ok: false, code, message, end_turn}` plus `from` ·
    `to`, `failed` or `fields` for the three codes that carry them (§6.3 names), and `inbox_id` for
    `exchange_limit`; nothing else rides along.

    `end_turn` of a failure is true in two cases here: `ask_owner` · `task_request_review` on a task that
    already waits for the owner (blocked · review), and a `room_post` at the limit of exchanges
    (`exchange_limit`, §4.6). Anything that is not a refusal — a ValueError of the
    machine, a DB error, an exception of Effects.after — is not caught: that is a server error."""
    tool = TOOLS.get(name)
    if tool is None:
        raise ValueError(f"not a studio tool: {name!r} (one of {tuple(TOOLS)})")
    try:
        return tool(caller, dict(args or {}), machine, inbox)
    except TaskNotFound:
        body: dict[str, Any] = {"code": "not_found", "detail": "그 작업이 없다. task_list 로 id 를 확인한다"}
    except Refusal as e:
        body = {"code": e.code, "detail": e.message, **e.extra}
    except (TransitionError, InboxError) as e:
        body = e.as_dict()
    code, message = body["code"], body["detail"]
    added = {key: body.get(key, [] if key in ("failed", "fields") else None) for key in ADDED.get(code, ())}
    waits = name in ENDING and code == "illegal_transition" and body.get("from") in WAITING
    if waits:
        message = f"이 작업은 이미 내 결정을 기다리고 있다 ({body['from']}). {message}."
    return fail(code, message, end_turn=waits or code == "exchange_limit", **added)
