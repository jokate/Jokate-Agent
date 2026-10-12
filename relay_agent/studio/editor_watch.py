"""The editor of a task's run, looked at while the run is at work (M-7): one that stops answering ends the run.

Contract: design/studio/ARCHITECTURE.md §4.5 '에디터 무응답을 감지', §4.3 T3 · T4, §4.7.

The check before a run goes on is work.py's (`WorkEffects.editor_ready`). A run that passed it is handed here
(`EditorWatch.begin`, which server.py puts in `WorkEffects.watch`): every `every` seconds the project's port is
asked `health_check` once (`WorkEffects.editor_alive`), and `misses` looks in a row without an answer are an
editor that is gone — `work.editor_lost` then asks the owner when the task is still working, cuts the run with
`RelayEngine.cancel` and writes the `run` row. One look is not enough: an editor whose game thread is busy with
a long MCP command does not answer for a while and is not gone.

A watch is one timer per task, in memory. It ends by itself at the first look that finds nothing to watch: the
task is gone or has another run, it is no longer working · blocked · review, no thread holds its slot (the run
has stopped), or its project has no port any more. A server that stops loses the watches, and nothing takes
their place: the check before the run is taken up again is what looks then. A dead editor and one that only does
not answer are not told apart — both end the run, and starting it again is that check's.

No FastAPI, no api.py and no queue.py here: server.py builds the `EditorWatch` on its engine, with the queue's
`holds`.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from . import work

EVERY = 30.0   # seconds between two looks
MISSES = 3     # looks in a row without an answer that make an editor one that is gone
WATCHED = ("working", "blocked", "review")  # the states in which a task's run may be at work
AT_WORK = ("pending", "running")            # without a queue to ask: the run has not stopped

log = logging.getLogger(__name__)


def _timer(delay: float, fn: Callable[[], None]) -> threading.Timer:
    """The clock of the server: a daemon timer, started. Its `cancel()` is what `EditorWatch` asks of a handle."""
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()
    return timer


class EditorWatch:
    """The watches, one per task. `effects` is the `WorkEffects` (its `editor_alive`), `inbox` is
    `api.run_inbox` (None while the store is not open: the watch ends), `holds` the queue's "does that task
    hold a slot" (None: the run's own status is read instead). `every` <= 0 watches nothing. `timer(delay, fn)`
    starts a timer and returns a handle with `cancel()` — a test gives its own clock. Thread-safe: `begin` comes
    from the threads of the slots, the timers fire on threads of their own."""

    def __init__(self, engine, effects, inbox: Callable[[], Any], holds: Callable[[str], bool] | None = None, *,
                 every: float = EVERY, misses: int = MISSES,
                 timer: Callable[[float, Callable[[], None]], Any] | None = None):
        self.engine, self.effects, self.inbox, self.holds = engine, effects, inbox, holds
        self.every, self.misses = float(every), max(1, int(misses))
        self.timer = timer or _timer
        self._lock = threading.Lock()
        self._watches: dict[str, dict] = {}  # task id -> {run_id, missed, handle}

    def watching(self) -> list[str]:
        """The ids of the tasks whose editor a timer is still to look at."""
        with self._lock:
            return list(self._watches)

    def begin(self, task_id: str, run_id: str) -> bool:
        """That run passed the editor check and goes on: watch its editor from now. An older watch of the task
        is dropped. True when a timer was started. Never raises: the run goes on whatever happens here."""
        try:
            self._drop(task_id)
            if self.every <= 0:
                return False
            mine = {"run_id": run_id, "missed": 0, "handle": None}
            with self._lock:
                self._watches[task_id] = mine
            self._arm(task_id, mine)
            return True
        except Exception:  # noqa: BLE001
            log.exception("studio: 작업 %s 의 에디터 감시를 시작하지 못했다", task_id)
            self._end(task_id, None)
            return False

    def _arm(self, task_id: str, mine: dict) -> None:
        handle = self.timer(self.every, lambda: self._tick(task_id, mine))
        with self._lock:
            if self._watches.get(task_id) is mine:
                mine["handle"] = handle
                return
        _cancel(handle, task_id)  # a new watch took its place while the timer was being started

    def _tick(self, task_id: str, mine: dict) -> None:
        """One look. Never raises: a timer's thread has nobody to tell."""
        with self._lock:
            if self._watches.get(task_id) is not mine:
                return  # dropped, or a newer watch of the task is the one that counts
        try:
            again = self._look(task_id, mine)
        except Exception:  # noqa: BLE001
            log.exception("studio: 작업 %s 의 에디터를 보지 못했다", task_id)
            again = False
        try:
            if again:
                self._arm(task_id, mine)
            else:
                self._end(task_id, mine)
        except Exception:  # noqa: BLE001
            log.exception("studio: 작업 %s 의 에디터 감시를 잇지 못했다", task_id)
            self._end(task_id, mine)

    def _look(self, task_id: str, mine: dict) -> bool:
        """True: look again after `every` seconds."""
        inbox = self.inbox()
        if inbox is None:
            return False
        task = inbox.store.get_task(task_id)
        if task is None or task["run_id"] != mine["run_id"] or task["status"] not in WATCHED:
            return False
        if not self._at_work(task_id, mine["run_id"]):
            return False
        alive = self.effects.editor_alive(inbox.store, task)
        if alive is None:
            return False
        if alive:
            mine["missed"] = 0
            return True
        mine["missed"] += 1
        if mine["missed"] < self.misses:
            return True
        log.warning("studio: 작업 %s 의 에디터가 %d번 연속 응답하지 않았다 (런 %s)", task_id, mine["missed"],
                    mine["run_id"])
        work.editor_lost(self.engine, inbox, task_id, mine["run_id"])
        return False

    def _at_work(self, task_id: str, run_id: str) -> bool:
        if self.holds is not None:
            return bool(self.holds(task_id))
        try:
            return self.engine.load(run_id).status in AT_WORK
        except (OSError, ValueError):
            return False

    def _end(self, task_id: str, mine: dict | None) -> None:
        with self._lock:
            if mine is None or self._watches.get(task_id) is mine:
                self._watches.pop(task_id, None)

    def _drop(self, task_id: str) -> None:
        with self._lock:
            old = self._watches.pop(task_id, None)
        if old is not None and old["handle"] is not None:
            _cancel(old["handle"], task_id)


def _cancel(handle: Any, task_id: str) -> None:
    try:
        handle.cancel()
    except Exception:  # noqa: BLE001
        log.exception("studio: 작업 %s 의 옛 에디터 감시 타이머를 거두지 못했다", task_id)
