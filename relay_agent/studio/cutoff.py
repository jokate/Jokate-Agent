"""What the server does once the answer of a T4 has gone out (기다림 = 런 종료): a task's run that is still at
work is cut with `RelayEngine.cancel`.

Contract: design/studio/ARCHITECTURE.md §4.3 T4 '런을 끝내는 방법' · §8.3 '서버의 뒤처리' (the wait), §5.7 (a T4
from a turn that is not the task's run: at once), §4.8 의 1 (the server's start), §6.4 · §10.5 (the guard's T4
ends the same way).

`ask_owner` answers "end the turn now", but only the model can end it: the run goes on until its stage is over,
and through the stages after it. So the server waits — `Config.studio_cut_after` seconds, 60 — and cuts the run
if it is still `running` then. The conversation of the stage that was cut stays in `RunState.stage_sessions`,
and the run goes on in it after T5 (§4.7 '런을 이을 때').

One way in: `Cutoff.after(rule, task_id, caller_run_id=…, inbox_id=…)`, called by whoever has just filed the
item of a T4 or a T6 — api.py for `ask_owner` and `task_request_review`, the guard (S-10) for its own T4.
- `TIMED` is the one list of the transitions that start the wait. T6 is not in it: after a review request the
  run does its remaining stages (§4.3 T6), and `after` does nothing.
- The caller's run is the task's run: that run was told to end its turn — the wait, then `cut`.
- The caller's run is another one (a read turn of a member who is not the assignee, §5.7): the task's run was
  told nothing — `cut` at once. So the two never both happen for one call.
- `cut` is the one judgement, the timer's, the immediate one's and the recovery's (work.py `_stopped_runs`):
  the task is still blocked, its run is still the one that was meant, the item — when one is named — is still
  pending, and the run is `running`. A run that stopped by itself or a decision that came first (T5) leaves
  nothing to do.

The timers live in memory. A server that stops inside the wait loses them; the recovery at its start cuts the
run of a blocked task that is still at work instead (§4.8 의 1). Nothing is written in `task_events` for a cut:
the run ends `cancelled`, the task stays blocked (§4.7 의 표, `cancelled` · `blocked`).

Known gaps, all between the last look of `cut` and its `cancel` (nothing is locked there):
- A run that stops by itself there is written `cancelled` over what it stopped with — the gap `TaskQueue.stop`
  has too.
- A decision that comes there (T5) while the run is still at work: the run ends `cancelled`, but its task is
  `working` and in the line by then, so no T4 follows (§4.7 의 표, the last row) and the queue takes the same
  run up again with `advance(resume=True)` — `cancelled` is not in `queue.NOT_TAKEN_UP`. What is lost is the
  work of the stage that was cut.
- A decision that comes there, the run stopping by itself and the queue taking it up again, all before
  `cancel`: the run of a `working` task out of the line is cut, and the system asks `run_cancelled` (§4.7 의
  표, `cancelled` · `working`). One question too many, which the owner answers with `retry`; closing it would
  take a conditional `cancel` in the engine.

No FastAPI, no api.py, no work.py and no queue.py here: server.py builds the `Cutoff` on its engine and hands it
to api.py (`use_cutoff`).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

TIMED = ("T4",)  # the transitions whose answer starts the wait: the one list. T6 is not among them (§8.3)
NONE, CUT, WAITS = "none", "cut", "timed"  # what `Cutoff.after` did
DEFAULT_WAIT = 60.0  # seconds; the server gives `Config.studio_cut_after`

log = logging.getLogger(__name__)


def _status(engine, run_id: str | None) -> str | None:
    """That run's status on this engine; None when it has no such run."""
    if not run_id:
        return None
    try:
        return engine.load(run_id).status
    except (OSError, ValueError):
        return None


def _timer(delay: float, fn: Callable[[], None]) -> threading.Timer:
    """The clock of the server: a daemon timer, started. Its `cancel()` is what `Cutoff` asks of a handle."""
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()
    return timer


def cut(engine, store, task_id: str, *, run_id: str | None = None, inbox_id: str | None = None) -> bool:
    """Cut the run of a task that waits for the owner, if there is still something to cut. True when
    `RelayEngine.cancel` was asked.

    All of these, read now: the task is blocked; its run is `run_id` (when given — a new run may have taken
    its place); the item `inbox_id` is still pending (when given — a decision that came first is T5, and the
    run is the owner's to go on with); the run is `running` on this engine. A run that ended or was closed
    meanwhile (`OSError` · `ValueError`, as in `TaskQueue.stop`) is not an error: nothing was cut."""
    task = store.get_task(task_id)
    if task is None or task["status"] != "blocked" or not task["run_id"]:
        return False
    if run_id is not None and task["run_id"] != run_id:
        return False
    if inbox_id is not None:
        item = store.get_inbox_item(inbox_id)
        if item is None or item["status"] != "pending":
            return False
    if _status(engine, task["run_id"]) != "running":
        return False
    try:
        engine.cancel(task["run_id"])
    except (OSError, ValueError):
        return False  # ended or closed already
    log.info("studio: 작업 %s 이 owner 를 기다리는데 런 %s 이 아직 돌아 끊었다", task_id, task["run_id"])
    return True


class Cutoff:
    """The waits that follow a T4, one per task. `inbox` is `api.run_inbox` (None while the store is not
    open: nothing is done then). `wait` is the seconds or a callable that gives them, asked each time a wait
    begins. `timer(delay, fn)` starts a timer and returns a handle with `cancel()` — a test gives its own
    clock. Thread-safe: `after` comes from the request threads, the timers fire on threads of their own."""

    def __init__(self, engine, inbox: Callable[[], Any], *, wait: float | Callable[[], float] = DEFAULT_WAIT,
                 timer: Callable[[float, Callable[[], None]], Any] | None = None):
        self.engine, self.inbox = engine, inbox
        self.wait: Callable[[], float] = wait if callable(wait) else (lambda: wait)
        self.timer = timer or _timer
        self._lock = threading.Lock()
        self._timers: dict[str, Any] = {}  # task id -> the handle of its wait

    def waiting(self) -> list[str]:
        """The ids of the tasks whose run a timer is still to look at."""
        with self._lock:
            return list(self._timers)

    def after(self, rule: str, task_id: str | None, *, caller_run_id: str | None,
              inbox_id: str | None = None) -> str:
        """The one way in, called when the item of `rule` (T4 · T6) has been filed for `task_id` from the run
        `caller_run_id`. Returns what was done: `none` — the rule starts no wait (T6), or there is nothing to
        cut: no such task, not blocked, its run not at work on this engine —, `cut` — the task's run was cut
        now (the call came from another run, §5.7, or the wait is 0) —, `timed` — the wait has begun.
        Never raises: the answer of the tool has been made and goes out whatever happens here."""
        try:
            return self._after(rule, task_id, caller_run_id, inbox_id)
        except Exception:  # noqa: BLE001
            log.exception("studio: 작업 %s 의 %s 뒤처리를 하지 못했다", task_id, rule)
            return NONE

    def _after(self, rule: str, task_id: str | None, caller_run_id: str | None, inbox_id: str | None) -> str:
        if rule not in TIMED or not task_id:
            return NONE
        inbox = self.inbox()
        if inbox is None:
            return NONE
        store = inbox.store
        task = store.get_task(task_id)
        if task is None or task["status"] != "blocked" or not task["run_id"]:
            return NONE
        run_id = task["run_id"]
        self._drop(task_id)  # an older wait of this task was for an item that is decided by now
        if _status(self.engine, run_id) != "running":
            return NONE  # it has stopped already, or it is no run of this engine: no timer for nothing
        wait = float(self.wait())
        if caller_run_id != run_id or wait <= 0:
            return CUT if cut(self.engine, store, task_id, run_id=run_id, inbox_id=inbox_id) else NONE
        mine: dict[str, Any] = {"handle": None, "fired": False}

        def fire() -> None:
            with self._lock:
                mine["fired"] = True
                if mine["handle"] is not None and self._timers.get(task_id) is mine["handle"]:
                    del self._timers[task_id]
            try:
                now = self.inbox()  # the store of now: it may have been closed and opened since
                if now is not None:
                    cut(self.engine, now.store, task_id, run_id=run_id, inbox_id=inbox_id)
            except Exception:  # noqa: BLE001 - a timer's thread has nobody to tell
                log.exception("studio: 작업 %s 의 런 %s 을 대기 뒤에 끊지 못했다", task_id, run_id)

        handle = self.timer(wait, fire)
        with self._lock:
            mine["handle"] = handle
            if not mine["fired"]:  # a clock that fired inside `timer` has nothing left to keep
                self._timers[task_id] = handle
        return WAITS

    def _drop(self, task_id: str) -> None:
        with self._lock:
            handle = self._timers.pop(task_id, None)
        if handle is not None:
            try:
                handle.cancel()
            except Exception:  # noqa: BLE001
                log.exception("studio: 작업 %s 의 옛 대기 타이머를 거두지 못했다", task_id)
