"""Runs and the inbox (S-5, part 2): a run of the engine that stops for approval or at a budget files an inbox
item, and the decision of an item without a task goes back to the engine.

Contract: design/studio/ARCHITECTURE.md §5.8 (가)(나), §4.7 (the two `awaiting_approval` · `working` rows of its
table), §5.3 (the payload of `design_approval` · `budget`), §11.3 (where this meets the existing code).

Two connection points in the engine, both set by attach():
- `engine.notifier` — where the toast hears `awaiting_approval` · `budget_exceeded` · `stage_budget_exceeded`
  (`RelayEngine._event`). The sink only remembers the event there: the engine calls it before it writes the
  StopNote and saves, so on disk the run has not stopped yet.
- `engine.after_advance` — called once `advance()` has let go of the run. Only then is the item filed: the
  StopNote is on disk, and an approval that follows cannot be overwritten by the run thread's last save.

Only the engine of the server process is attached. The CLI advances runs in a process of its own, which does
not open the studio DB (§3.1): a run the CLI drives files nothing while the server runs (provisional), and what
the CLI releases is noticed later — `Inbox.sweep_runs`, and `stale_state` at the decision (§5.8 나). At the
server's start `file_waiting` files the stops no sink heard.

No FastAPI here: server.py connects it, and tests use it with an engine of their own.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from ..notify import Fanout
from . import employee_git
from .inbox import RUN_KINDS, Hooks, Inbox
from .tasks import SYSTEM

log = logging.getLogger(__name__)

WAITING = "awaiting_approval"
STOP_EVENTS = ("awaiting_approval", "budget_exceeded", "stage_budget_exceeded")  # the NOTIFY_KINDS that are a stop
STOP_KINDS = {  # StopNote.kind -> (inbox kind, scope): the table of §5.8 가
    "budget": ("budget", "run"),
    "stage_budget": ("budget", "stage"),
    "awaiting_approval": ("design_approval", None),
}
EVENT_OF = {  # StopNote.kind -> the event the engine told that stop with (pipeline.py 1586 · 1728 · 1786)
    "budget": "budget_exceeded",
    "stage_budget": "stage_budget_exceeded",
    "awaiting_approval": "awaiting_approval",
}


def _usd(value: Any, fallback: float) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else fallback


def workdir(engine, run_id: str) -> Path | None:
    """The folder a run's stages work in — `cwd` of RelayEngine._advance: the workspace's path, or
    RunState.workdir for a run without one. What an employee's attachment path is checked against (§6.6);
    server.py hands it to api.install. None for a run the engine does not have, and on any error."""
    try:
        run = engine.load(run_id)
        ws = engine.workspace(run)
        return Path(ws.path if ws is not None else run.workdir)
    except Exception:  # noqa: BLE001 - no folder is an answer: only paths in the project folder pass then
        return None


class RunSink:
    """Hears the engine's stop events and, once the run is let go, files the inbox item of the stop.

    `inbox_factory()` gives the Inbox to file with, or None while the studio store is not open — then nothing
    is filed. `__call__` and `idle` never raise: an alert and a hook must not break a run.

    A stop it did not hear — the run stopped in another process, or while the store was not open — is filed
    at the server's start by `file_waiting`."""

    def __init__(self, engine, inbox_factory: Callable[[], Inbox | None]):
        self.engine = engine
        self.inbox_factory = inbox_factory
        self._lock = threading.Lock()
        self._heard: dict[str, tuple[str, dict]] = {}  # run id -> (title, detail) of the stop it is going into

    def __call__(self, run_id: str, kind: str, title: str, detail: dict) -> None:
        """The notifier's call. Nothing is read or written here (see the module text)."""
        if kind in STOP_EVENTS:
            with self._lock:
                self._heard[run_id] = (title, dict(detail or {}))

    def idle(self, run_id: str) -> None:
        """`engine.after_advance`: the run is let go. File the stop that was heard while it advanced, if any."""
        with self._lock:
            heard = self._heard.pop(run_id, None)
        if heard is None:
            return
        try:
            self.file(run_id, *heard)
        except Exception:  # noqa: BLE001 - the run has its result; the stop still shows in the dashboard
            log.exception("studio: 런 %s 의 정지를 결재로 올리지 못했다", run_id)

    def file(self, run_id: str, title: str = "", detail: dict | None = None) -> dict | None:
        """The item of the stop that run is in now. None when there is nothing to file: the run does not wait
        for approval, the store is not open, or it is a task's run and §4.7 files nothing for the state that
        task is in (blocked · review · done, or working and in line)."""
        run = self.engine.load(run_id)
        stop = run.baton.stop
        if run.status != WAITING or stop is None or stop.kind not in STOP_KINDS:
            return None
        inbox = self.inbox_factory()
        if inbox is None:
            return None
        kind, scope = STOP_KINDS[stop.kind]
        payload = self._payload(run, stop, kind, scope, detail or {})
        store = inbox.store
        owner = store.list_task_runs(run_id=run_id)
        if owner:  # §5.8 가: T4 by the system. It stays here (S-8): step 3 of §4.7 and §4.8 ask it again (`file_stop`)
            task = store.get_task(owner[0]["task_id"])
            if task is None or task["status"] != "working" or task["queued_at"] is not None:
                return None
            return inbox.create(kind, SYSTEM, task_id=task["id"], payload=payload)  # no title: the task's
        session = self.engine.history.get_session(run.session_id) or {}
        project = next((p["id"] for p in store.list_projects(include_archived=True)
                        if run.repo and p["repo"] == run.repo), None)
        return inbox.file_run(kind, run_id, title=session.get("title") or title, payload=payload,
                              project_id=project)

    def _payload(self, run, stop, kind: str, scope: str | None, detail: dict) -> dict:
        """§5.3: `stage` · `stop_reason` · `done_stages` · `remaining_stages` from the StopNote; `next_stage` ·
        `spent_usd` · `limit_usd` from the detail of the event that stopped the run; `design_summary` from the
        `stage_finished` event of the stage that stopped at its gate."""
        payload: dict[str, Any] = {"stage": stop.stage, "stop_reason": stop.reason.strip() or "승인이 필요하다"}
        next_stage = detail.get("next") or (stop.remaining_stages[0] if stop.remaining_stages else "")
        if next_stage:
            payload["next_stage"] = next_stage
        if kind == "budget":  # a stage that could not say what it spent: what the run has spent so far
            payload.update(scope=scope, spent_usd=_usd(detail.get("spent_usd"), round(run.cost_usd, 4)),
                           limit_usd=_usd(detail.get("limit_usd"), 0.0))
            return payload
        payload.update(done_stages=list(stop.done_stages), remaining_stages=list(stop.remaining_stages))
        summary = self._stage_summary(run.id, stop.stage)
        if summary:
            payload["design_summary"] = summary
        return payload

    def _stage_summary(self, run_id: str, stage: str) -> str:
        for event in reversed(self.engine.history.events(run_id)):
            if event["kind"] == "stage_finished" and event["stage"] == stage:
                return str(event["detail"].get("summary") or "").strip()
        return ""


class EngineHooks(Hooks):
    """`Hooks` on a RelayEngine, for the items of runs that are no task's (§5.8 나). `advance(run_id)` has an
    approved run go on — server.py gives its `_advance_bg`, a thread of its own. None = nobody advances it.

    `chat(employee_id, room_id)` starts that employee's chat turn in that room — server.py gives
    `chat.ChatTurns.request` (S-7). None = no turn is started.

    `turns` is what a mention asks its turn of — server.py gives the `chat.ChatTurns` itself (S-9): `room_turn`
    is its `put`, `turn_failed` its `failed`, `staffing_ask` its `judge` (HR's turn on a trigger, T5). None = a
    mention starts no turn and nobody is asked. `tell_run` needs neither: it is
    `engine.interject`, for a run that may be at work.

    `interject` feeds a decision text to a run that has stopped (§4.7 '런을 이을 때'); a run that is at work takes
    it when the thread that advances it is back (`work.deliver`). A run that is `done` is opened again first
    (`RelayEngine.reopen`, S-12): the one connection point of a rework of a finished run, of an answer to
    `no_review_request`, of the progress thread coming back and of the restart recovery — all of them reach the
    run through `Inbox.deliver`.

    `learn(item)` is the memory of H-4: server.py gives `api.learn_later`, a thread of its own that asks a light
    model for the lessons of a decided final review. None = nothing is learned.

    `commit` is the commit of an employee's folder after a `hire` · `guide_amend` approval (H-3; §5.5 · §9.6):
    the folder's own git, `studio/employee_git.py`. It needs no engine."""

    def __init__(self, engine, advance: Callable[[str], Any] | None = None,
                 chat: Callable[[str, str], Any] | None = None, turns: Any = None,
                 learn: Callable[[dict], Any] | None = None):
        self.engine = engine
        self._learn = learn
        self._advance = advance
        self._chat = chat
        self._turns = turns

    def interject(self, run_id: str, text: str) -> bool:
        """True = the text is in the run's notes now. False = not now: the engine has no such run, or the run is
        at work — a note given then would go into a conversation the server is about to cut (§4.7).

        A run that is `done` is opened again for the text (§4.7 의 `done` 행): the same run, `pending`, at the
        stage work is sent back to and in the conversation that stage ended in — the queue then takes it up as
        any run that stopped (`advance(resume=True)`). The ValueError of a `done` run the engine does not open
        again (its changes are applied or reverted, or a thread still holds it) goes up as it is:
        `Inbox.deliver` reads it as False and the text stays owed."""
        try:
            status = self.engine.load(run_id).status
        except (OSError, ValueError):
            return False
        if status == "running":
            return False
        if status == "done" and text.strip():  # an empty text is the engine's to refuse: nothing is opened for it
            self.engine.reopen(run_id)
        self.engine.interject(run_id, text)
        return True

    def commit(self, folder: Path, message: str) -> None:
        """One commit of that employee's folder in its own git; the first one makes the repository. A GitError
        goes up as it is: `Inbox._safely` logs it and the decision stays."""
        employee_git.commit(folder, message)

    def learn(self, item: dict) -> None:
        if self._learn is not None:
            self._learn(item)

    def chat_turn(self, employee_id: str, room_id: str) -> None:
        if self._chat is not None:
            self._chat(employee_id, room_id)

    def staffing_ask(self, signal: dict) -> bool | None:
        """T5: HR's judging turn — `ChatTurns.judge`. False when there are no turns to ask through."""
        return bool(self._turns.judge(signal)) if self._turns is not None else False

    def turn_failed(self, employee_id: str, room_id: str) -> list[str]:
        return list(self._turns.failed(employee_id, room_id)) if self._turns is not None else []

    def room_turn(self, employee_id: str, room_id: str) -> bool:
        return bool(self._turns.put(employee_id, room_id)) if self._turns is not None else False

    def group_turns(self, room_id: str, employee_ids: list[str], mentioned: bool = False) -> list[str]:
        """The turns of a group room (§6.6): `ChatTurns.group` — the room's line, one turn at a time."""
        return list(self._turns.group(room_id, employee_ids, mentioned)) if self._turns is not None else []

    def tell_run(self, run_id: str, text: str) -> bool:
        """Unlike `interject` above, a run at work is what this is for: the engine puts the text into the
        conversation that is going on, or hands it to the next stage. A run the engine does not have (OSError)
        or that is `done` (ValueError, as an empty text) took nothing."""
        try:
            self.engine.interject(run_id, text)
        except (OSError, ValueError):
            return False
        return True

    def run_status(self, run_id: str) -> str | None:
        try:
            return self.engine.load(run_id).status
        except FileNotFoundError:
            return "gone"

    def _waiting(self, run_id: str) -> None:
        status = self.run_status(run_id)
        if status != WAITING:
            raise ValueError(f"run {run_id} is {status}, not {WAITING}")

    def approve_run(self, run_id: str, note: str = "") -> bool:
        self._waiting(run_id)  # first: a note must not be left in a run that has already gone on
        if note.strip():
            self.engine.interject(run_id, note)
        self.engine.approve(run_id)
        return True

    def cancel_run(self, run_id: str) -> bool:
        self._waiting(run_id)  # engine.cancel would also end a run another process is advancing
        self.engine.cancel(run_id)
        return True

    def advance_run(self, run_id: str) -> None:
        if self._advance is not None:
            self._advance(run_id)


def attach(engine, inbox_factory: Callable[[], Inbox | None], *,
           advance: Callable[[str], Any] | None = None,
           chat: Callable[[str, str], Any] | None = None, turns: Any = None,
           learn: Callable[[dict], Any] | None = None) -> EngineHooks:
    """Connect an engine to the inbox. Its stops are filed: the sink is put beside whatever `engine.notifier`
    already is, so the toast keeps working. The hooks that come back carry the decision of an item without a
    task to that engine — give them to the Inbox (`api.use_hooks`). `chat` is what starts a chat turn
    (`EngineHooks`); without it no turn is started, whoever asks. `turns` is what a mention asks its turn of
    (`chat.ChatTurns`, S-9); without it a mention starts none. `learn` is what picks the lessons out of a final
    review's reason (`api.learn_later`, H-4); without it nothing is learned."""
    sink = RunSink(engine, inbox_factory)
    engine.notifier = Fanout(engine.notifier, sink)
    engine.after_advance = sink.idle
    return EngineHooks(engine, advance, chat, turns, learn)


def _told(engine, run) -> tuple[str, dict]:
    """(title, detail) as the notifier was told them when that run stopped, read back from the event record:
    the first line of the goal, and the detail of the last event of the stop's kind at the stage the StopNote
    names. No such event (the record is gone) -> an empty detail, and `RunSink._payload` falls back on the
    run itself: what it has spent, a limit of 0.0, the first of the remaining stages."""
    lines = (run.baton.goal or "").strip().splitlines()
    title = lines[0][:80] if lines else ""
    stop = run.baton.stop
    wanted = EVENT_OF.get(stop.kind) if stop is not None else None
    if wanted is not None:
        for event in reversed(engine.history.events(run.id)):
            if event["kind"] == wanted and event["stage"] == stop.stage:
                return title, dict(event["detail"] or {})
    return title, {}


def file_stop(engine, inbox: Inbox, run) -> dict | None:
    """Step 3 of §4.7 for a task's run that waits for approval, and step 1 of §4.8: the item of that stop, by
    `RunSink.file` — the same code `after_advance` asks, told what the notifier was told. None when there is
    nothing to file: the sink has filed it (the task is blocked by now), or the task is not working and out of
    the line."""
    return RunSink(engine, lambda: inbox).file(run.id, *_told(engine, run))


def file_waiting(engine, inbox_factory: Callable[[], Inbox | None]) -> list[dict]:
    """The server's start (§5.8 나 "시작할 때"): file the stops no sink heard — the runs that wait for approval,
    are no task's and have no pending item. The CLI stopped them in a process of its own, or this server did
    while the studio store was not open.

    A run that already has a pending item is left alone whatever that item says: filing it again would close
    the item and put another in its place at every start. A task's run is left to the restart recovery
    (`work.recover`, §4.8 step 1), which does the whole table of §4.7 for it. A run without a StopNote files
    nothing (`RunSink.file`).

    Returns the items it filed. Never raises, and a run that cannot be filed does not keep the others: the
    server must start."""
    filed: list[dict] = []
    try:
        inbox = inbox_factory()
        if inbox is None:
            return filed
        store = inbox.store
        skip = {row["run_id"] for row in store.list_inbox(status="pending")
                if row["task_id"] is None and row["kind"] in RUN_KINDS}
        skip |= {row["run_id"] for row in store.list_task_runs()}
        sink = RunSink(engine, inbox_factory)
        for run in engine.list_runs():
            if run.status != WAITING or run.id in skip:
                continue
            try:
                item = sink.file(run.id, *_told(engine, run))
            except Exception:  # noqa: BLE001 - the stop still shows in the dashboard
                log.exception("studio: 시작할 때 런 %s 의 승인 대기를 결재로 올리지 못했다", run.id)
                continue
            if item is not None:
                filed.append(item)
    except Exception:  # noqa: BLE001
        log.exception("studio: 시작할 때 승인 대기 런을 결재로 채우지 못했다")
    return filed
