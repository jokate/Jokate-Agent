"""Task runs (S-8, part 2): the line and its slots.

Contract: design/studio/ARCHITECTURE.md §4.6 (the two limits, the slot, the line), §4.7 (what the thread that
advanced a run does when it is back, how a run goes on), §6.4 (`start` · `stop`).

- A slot is a task run's from the moment the queue gives it until the thread that advanced the run is back from
  `engine.advance`. The book of slots is in memory and kept by task id — a backlog task gets its slot before it
  has a run (T3 makes it) — and it is empty after a restart (§4.8).
- The line is every task with `queued_at`, the oldest request first (`store.list_queued_tasks`). `kick()` gives
  a slot to each task both limits have room for: `limit()` slots in all (`Config.studio_max_runs`, 3), one per
  project. A task is passed over while it holds a slot already (T5 · T8 came while its run was at work), while
  a run from outside studio holds its folder (`RelayEngine.folder_holder`; looked at again after
  `retry_after` seconds), and when its run cannot be taken up (gone, `done` or `running` — it stays in line).
- With its slot a backlog task does T3 and its new run is advanced; a working task (T5 · T8) leaves the line
  and its run goes on — `approve` + `advance` from `awaiting_approval`, else `advance(resume=True)`.
- `kick()` is called when a task joins the line (`WorkEffects.after`), when a slot is let go (the end of
  `_work`) and when the server starts (server.py). It never raises.
- `stop()` is the owner's (§6.4): a task leaves the line; a working one ends in blocked — its run is cancelled
  and the system files a `question` (T4), at once when no thread holds the run, else when that thread is back.
  The slot carries the run's cancel event and gives it to `advance`, so a stop that comes after the thread's
  last look and before `advance` is in runs no stage either.
- A thread that comes back with an exception while its run is `running` found the run at work somewhere else
  (the dashboard, the CLI, another thread: 'already advancing'). Nothing is filed and the task does not join
  the line — there the queue would go on with the run however it ended —: the task is remembered (`waits()`),
  looked at with every `kick` and after `retry_after` seconds, and gets its row of the table once that run has
  stopped. The memory is gone after a restart; step 1 of §4.8 does the same then.

Chat and read turns do not come here and are not counted (§4.6): chat.py advances them itself.

What follows when the run of a slot has stopped — step 2 of §4.7 (the decision texts), the `stage_skipped` rows
and the table — is studio/work.py's (`stopped`), and so is the recovery at the server's start (`recover`, §4.8),
whose step 3 is this queue's `kick`. A run that stops for approval is filed by `runs.RunSink` before `advance`
is back; the table files it only when the sink did not. The editor check (M-7) is `editor_ready`: the effects'
own (`WorkEffects.editor_ready` — it starts the editor, waits for it and files the T4 itself); effects that have
none let every run go on.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Any, Callable

from .tasks import OWNER, SYSTEM, TaskNotFound, TransitionError
from .work import CANCELLED, STOPPED, ask as file_question, stopped as run_stopped  # noqa: F401 - both bodies

log = logging.getLogger(__name__)

LINED = ("backlog", "working")      # the states a task stands in the line in (§4.6)
NOT_TAKEN_UP = ("done", "running")  # a working task's run the queue cannot go on with (§4.7 '런을 이을 때')


@dataclasses.dataclass
class Slot:
    project_id: str
    run_id: str | None = None  # None until T3 has made the run
    stopped: bool = False      # the owner's `stop` came while the slot was held
    seen: int | None = None    # the engine's last event id just before this slot's `advance` (None: not read)
    # the cancel event `engine.advance` is given: `stop` sets it, and so does a cancel from outside studio
    cancel: threading.Event = dataclasses.field(default_factory=threading.Event, repr=False, compare=False)


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True, name="katae-task-run").start()


class TaskQueue:
    """The line of one server, on one engine.

    `effects` is the `WorkEffects` of that engine (its `folder(task)` says where a task's run works), `inbox`
    gives an `Inbox` outside a request (`api.run_inbox`; None while the store is not open — then nothing is
    looked at), `limit` the number of slots, asked each time. `spawn(work)` starts the thread of a slot — a
    daemon thread unless a test gives its own. `retry_after`: seconds until the line is looked at again when a
    folder was held from outside; None = not by itself."""

    def __init__(self, engine, effects, inbox: Callable[[], Any], *, limit: Callable[[], int] | int = 3,
                 spawn: Callable[[Callable[[], None]], Any] | None = None, retry_after: float | None = 5.0):
        self.engine, self.effects, self.inbox = engine, effects, inbox
        self.limit = limit if callable(limit) else (lambda: limit)
        self.spawn = spawn or _thread
        self.retry_after = retry_after
        self._lock = threading.Lock()
        self._slots: dict[str, Slot] = {}
        self._stuck: dict[str, str] = {}  # task id -> the queued_at it could not be started or taken out with
        # task id -> its run that was at work somewhere else when the thread of its slot came back with an
        # exception: no row of the table yet — that comes when the run has stopped (`_stopped_elsewhere`)
        self._elsewhere: dict[str, str] = {}
        # the threads that have let their slot go and are in the steps that follow (§4.7 의 2–4), and the
        # timer's thread while it looks at the line
        self._after = 0
        self._timer: threading.Timer | None = None

    # --- the book -------------------------------------------------------------------------------------------
    def slots(self) -> dict[str, dict]:
        """A copy of the book: task id -> `{project_id, run_id, stopped}`."""
        with self._lock:
            return {task_id: {"project_id": slot.project_id, "run_id": slot.run_id, "stopped": slot.stopped}
                    for task_id, slot in self._slots.items()}

    def waits(self) -> dict[str, str]:
        """A copy: task id -> the id of its run that is at work somewhere else. The queue waits for that run
        to stop and does the table then (`_stopped_elsewhere`)."""
        with self._lock:
            return dict(self._elsewhere)

    def holds(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._slots

    def idle(self) -> bool:
        """No slot is held and no thread of a slot is still in what follows its run (the decision texts, the
        table, the look at the line), nor is the timer's thread looking at the line: nothing of this queue is
        writing any more. A task that waits for a run at work somewhere else (`waits`) does not count."""
        with self._lock:
            return not self._slots and not self._after

    # --- the line -------------------------------------------------------------------------------------------
    def kick(self) -> list[str]:
        """Look at the line and start what both limits have room for. Returns the ids of the tasks that got a
        slot. Never raises: what goes wrong is logged, and the line is looked at again at the next occasion."""
        try:
            waiting = self._stopped_elsewhere()  # first: a run that stopped elsewhere may put its task in line
            picked, busy = self._pick()
        except Exception:  # noqa: BLE001 - a commit, a thread's end or the server's start called this
            log.exception("studio: 줄을 보지 못했다")
            return []
        if busy or waiting:
            self._later()
        for task in picked:
            try:
                self.spawn(lambda task_id=task["id"]: self._work(task_id))
            except Exception:  # noqa: BLE001 - no thread: the slot goes back, the task stays in line
                log.exception("studio: 작업 %s 의 진행 스레드를 띄우지 못했다", task["id"])
                with self._lock:
                    self._slots.pop(task["id"], None)
        return [task["id"] for task in picked]

    def _pick(self) -> tuple[list[dict], bool]:
        inbox = self.inbox()
        if inbox is None:
            return [], False
        picked, busy = [], False
        with self._lock:
            line = inbox.store.list_queued_tasks()
            self._stuck = {task["id"]: self._stuck[task["id"]] for task in line if task["id"] in self._stuck}
            room = max(int(self.limit()), 0)
            for task in line:
                if len(self._slots) >= room:
                    break
                if task["id"] in self._slots or task["status"] not in LINED:
                    continue  # its run has not stopped yet (§4.6 '건너뛴다')
                if self._stuck.get(task["id"]) == task["queued_at"]:
                    continue
                if any(slot.project_id == task["project_id"] for slot in self._slots.values()):
                    continue  # one writer per project
                run_id = task["run_id"] if task["status"] == "working" else None
                if task["status"] == "working" and self._status(run_id) in (None, *NOT_TAKEN_UP):
                    continue
                if self._held_outside(task, run_id):
                    busy = True
                    continue
                self._slots[task["id"]] = Slot(task["project_id"], run_id)
                picked.append(task)
        return picked, busy

    def _status(self, run_id: str | None) -> str | None:
        """That run's status on this engine; None when it has no such run."""
        if not run_id:
            return None
        try:
            return self.engine.load(run_id).status
        except (OSError, ValueError):
            return None

    def _held_outside(self, task: dict, run_id: str | None) -> bool:
        """§4.6: a run that is not studio's (CLI · dashboard) may be editing the folder. A folder that cannot
        be told is not held — T3 says why no run is made there."""
        find = getattr(self.effects, "folder", None)
        folder = find(task) if find is not None else None
        if folder is None:
            return False
        try:
            return self.engine.folder_holder(str(folder), exclude=run_id) is not None
        except OSError:
            return False

    def _later(self) -> None:
        """One timer at a time: nobody tells the queue that a run from outside let its folder go, or that a run
        at work somewhere else has stopped."""
        if not self.retry_after or self.retry_after <= 0:
            return
        with self._lock:
            if self._timer is not None:
                return
            timer = self._timer = threading.Timer(self.retry_after, self._again)
            timer.daemon = True
        timer.start()

    def _again(self) -> None:
        with self._lock:
            self._timer = None
            self._after += 1  # this look may do the table for a run that stopped elsewhere: `idle` waits for it
        try:
            self.kick()
        finally:
            with self._lock:
                self._after -= 1

    # --- a run at work somewhere else (§4.7) ----------------------------------------------------------------
    def _watch(self, inbox, task_id: str, known: bool = False) -> bool:
        """Remember a task until its run, at work somewhere else, has stopped: only one that is working, out
        of the line and has a run that is `running` — or, when it was waited for already (`known`), a run that
        cannot be read just now. True when it is waited for."""
        task = inbox.store.get_task(task_id)
        if task is None or task["status"] != "working" or task["queued_at"] is not None or not task["run_id"]:
            return False
        status = self._status(task["run_id"])
        if status != "running" and not (known and status is None):
            return False
        with self._lock:
            self._elsewhere[task_id] = task["run_id"]
        return True

    def _stopped_elsewhere(self) -> bool:
        """The tasks that wait for a run at work somewhere else (`_watch`): each whose run has stopped since —
        and that holds no slot — is taken out and gets its row of the table (`work.stopped`), with no error:
        the run ended as it says. A task the table did nothing for is waited for again if its run is at work
        once more. What goes wrong with one task is logged. True while any is waited for still — the line is
        then looked at again after `retry_after`."""
        with self._lock:
            ended = [(task_id, run_id) for task_id, run_id in self._elsewhere.items()
                     if task_id not in self._slots and self._status(run_id) != "running"]
            for task_id, _ in ended:
                del self._elsewhere[task_id]
        inbox = self.inbox() if ended else None
        if ended and inbox is None:  # the store is not open: they wait on
            with self._lock:
                for task_id, run_id in ended:
                    self._elsewhere.setdefault(task_id, run_id)
            return True
        for task_id, _ in ended:
            try:
                if run_stopped(self.engine, inbox, task_id) is None:
                    self._watch(inbox, task_id, known=True)
            except Exception:  # noqa: BLE001 - one task does not keep the others
                log.exception("studio: 다른 곳에서 멈춘 작업 %s 의 런을 처리하지 못했다", task_id)
        with self._lock:
            return bool(self._elsewhere)

    def _mark(self) -> int | None:
        """The id of the engine's last event, of any run: what a slot has seen before its `advance`. None when
        it cannot be read."""
        try:
            return int(self.engine.history.last_event_id())
        except Exception:  # noqa: BLE001 - only the text of a T4 depends on it
            return None

    def _failed_since(self, slot: Slot | None) -> bool:
        """Whether the engine has recorded since `slot.seen` that the slot's run failed (its `run_failed`
        event): the run ended failed before the exception came, and its own error is the one to tell
        (`work.table`, `advanced`). False when that cannot be told."""
        if slot is None or slot.seen is None or not slot.run_id:
            return False
        try:
            return any(event["kind"] == "run_failed" for event in self.engine.history.events(slot.run_id, slot.seen))
        except Exception:  # noqa: BLE001
            return False

    # --- the thread of a slot (§4.7) ------------------------------------------------------------------------
    def editor_ready(self, task: dict, run_id: str) -> bool:
        """The editor check before a run that uses MCP goes on (§4.3 T3 · §4.7), after T3 and before a run is
        taken up again: the effects' `editor_ready(inbox, task, run_id)` (work.py), which has filed the T4 when
        it says no. Effects without one, or no store: every run goes on. False: the run is not advanced and the
        slot is let go."""
        check = getattr(self.effects, "editor_ready", None)
        inbox = self.inbox()
        if check is None or inbox is None:
            return True
        return bool(check(inbox, task, run_id))

    def _work(self, task_id: str) -> None:
        """What a slot is for: T3 or leaving the line, the run until it stops, and then the steps of §4.7 —
        1 the slot is let go, 2 · 3 the decision texts and the table (`_settle`), 4 the line again."""
        error = None  # what `_go` raised that the task machine did not: the run was not advanced (`work.table`)
        try:
            self._go(task_id)
        except TransitionError as e:  # T3 could not be done, or the task left the line meanwhile (`stop`)
            log.info("studio: 작업 %s 을 시작하지 못했다: %s", task_id, e.detail)
            self._leave_line(task_id, e.detail)
        except Exception as e:  # noqa: BLE001 - the slot comes back whatever happened
            log.exception("studio: 작업 %s 의 런을 진행하지 못했다", task_id)
            error = str(e) or e.__class__.__name__
            self._leave_line(task_id, error)
        finally:
            with self._lock:
                slot = self._slots.pop(task_id, None)
                self._after += 1
            try:
                advanced = error is not None and self._failed_since(slot)
                self._settle(task_id, bool(slot and slot.stopped), error, advanced)
            except Exception:  # noqa: BLE001
                log.exception("studio: 작업 %s 의 런이 멈춘 뒤를 처리하지 못했다", task_id)
            try:
                self.kick()
            finally:
                with self._lock:
                    self._after -= 1

    def _go(self, task_id: str) -> None:
        inbox = self.inbox()
        if inbox is None:
            return
        task = inbox.store.get_task(task_id)
        if task is None or task["queued_at"] is None:
            return  # it left the line between the look and this thread (`stop`, a T4)
        if task["status"] == "backlog":
            row = inbox.machine.transition(task_id, "working", SYSTEM).task  # T3: the run is made, pending
            resume = False
        else:
            row = inbox.machine.unqueue(task_id, SYSTEM, reason="슬롯을 얻어 런을 잇는다").task
            resume = True
        run_id = row["run_id"]
        with self._lock:
            slot = self._slots.get(task_id)
            if slot is not None:
                slot.run_id = run_id
        if not run_id or not self.editor_ready(row, run_id):
            return
        with self._lock:
            if slot is None or slot.stopped:
                return  # `stop` came first: the run is not advanced, and _settle files the T4
        if resume and self._status(run_id) == "awaiting_approval":
            self.engine.approve(run_id)  # back to pending; the decision text is in the run already (§5.5)
            resume = False
        slot.seen = self._mark()  # what the engine writes about the run from here on is this `advance`'s
        self.engine.advance(run_id, resume=resume, cancel=slot.cancel)  # a `stop` since that look: no stage runs

    def _leave_line(self, task_id: str, why: str) -> None:
        """A task that could not be started is taken out of the line, so the next look does not pick it again
        (T3 that makes no run has done so itself). If even that fails it is passed over until it asks anew."""
        queued_at = None
        try:
            inbox = self.inbox()
            task = inbox.store.get_task(task_id) if inbox is not None else None
            if task is None or task["queued_at"] is None:
                return
            queued_at = task["queued_at"]
            inbox.machine.unqueue(task_id, SYSTEM, reason=f"시작하지 못해 줄에서 뺐다: {why}"[:300])
        except Exception:  # noqa: BLE001
            log.exception("studio: 작업 %s 을 줄에서 빼지 못했다", task_id)
            if queued_at is not None:
                with self._lock:
                    self._stuck[task_id] = queued_at

    def _settle(self, task_id: str, stopped: bool, error: str | None = None, advanced: bool = False) -> None:
        """Steps 2 and 3 of §4.7, which studio/work.py does (`stopped`): the decision texts still owed go into
        the run, each skipped stage gets its `run` row, and the table — the owner's `stop` or a cancel from
        outside, a run that ended without a review request or failed: the system files its item (T4); a run that
        goes on by itself joins the line again. A run that waits for approval has been filed by `runs.RunSink`
        before `advance` returned. `error`: `_go` raised it — the run was not advanced, and one that would go on
        by itself is asked about instead of being put back in the line, where it would end up here again.
        `advanced`: the engine recorded meanwhile that the run failed — the item tells the run's own error.
        With an `error` and a run that is `running` the table does nothing: the run is at work somewhere else,
        and the task is remembered until it has stopped (`_watch`). If it stopped between the table's look and
        that one, the table is done once more, with no error."""
        inbox = self.inbox()
        if inbox is None:
            return
        done = run_stopped(self.engine, inbox, task_id, owner_stop=stopped, error=error, advanced=advanced)
        if done is None and error is not None and not stopped and not self._watch(inbox, task_id):
            run_stopped(self.engine, inbox, task_id)

    # --- stop (§6.4) ----------------------------------------------------------------------------------------
    def stop(self, task_id: str, inbox=None) -> dict:
        """The owner stops a task: the row afterwards.

        backlog, in the line: it leaves the line. working: it leaves the line if it stands there, its run is
        cancelled, and the task ends in blocked with a system `question` (`run_cancelled`) — filed here when no
        thread holds the run, else by that thread when `advance` is back (the row is still `working` then).
        The slot's cancel event is set under the lock and `advance` is given it, so a thread that is past its
        last look at `stopped` and not in `advance` yet runs no stage.
        Anything else — and a backlog task out of the line — is `precondition_failed`."""
        inbox = inbox or self.inbox()
        if inbox is None:
            raise RuntimeError("the studio store is not open")
        store, machine = inbox.store, inbox.machine
        task = store.get_task(task_id)
        if task is None:
            raise TaskNotFound(task_id)
        if task["status"] == "backlog":
            machine.unqueue(task_id, OWNER, reason="owner 가 시작 요청을 거뒀다")
        elif task["status"] == "working":
            with self._lock:
                slot = self._slots.get(task_id)
                if slot is not None:
                    slot.stopped = True
                    slot.cancel.set()  # its thread may be between its last look and `advance`
            if task["queued_at"] is not None:
                machine.unqueue(task_id, OWNER, reason="owner 가 작업을 중지했다")
            if task["run_id"] and (slot is not None or self._status(task["run_id"]) == "running"):
                try:
                    self.engine.cancel(task["run_id"])
                except (OSError, ValueError):
                    pass  # ended or closed already
            if slot is None:
                file_question(inbox, task_id, "run_cancelled", STOPPED)
        else:
            raise TransitionError("precondition_failed",
                                  f"{task['status']} 작업에는 멈출 것이 없다 (줄 선 backlog 와 working 만 중지한다)",
                                  from_status=task["status"])
        return store.get_task(task_id)
