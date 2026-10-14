"""Keep this PC awake while the server works (V-3 — design/studio/ARCHITECTURE.md §4.8 · §11).

A task run that holds a slot (`queue.TaskQueue._work`) and a chat or read turn (`chat.ChatTurns._work` ·
`_group_work`) `hold()` the PC awake for as long as they go on; when none is left it may sleep again. The count is
kept here and the call to the system is `setter(on)`: it is called on 0 -> 1 with True and on 1 -> 0 with False,
nothing in between. The system call is given in, so a test counts calls and no real power API is touched.

Nothing happens before `enable()` (the server's start): a test that builds a queue never asks the system. The
power settings, the start-up programs and `supervise.py` are not touched here."""
from __future__ import annotations

import contextlib
import logging
import queue
import sys
import threading
from typing import Callable, Iterator

log = logging.getLogger(__name__)

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


class _WindowsKeeper:
    """The default `setter`. `SetThreadExecutionState` holds for the thread that called it only, and the thread
    that starts a run is not the one that ends it: one thread of its own makes every call. Anything but Windows,
    or a call that fails, is logged and nothing more."""

    def __init__(self) -> None:
        self._asks: queue.Queue[bool] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def __call__(self, on: bool) -> None:
        if sys.platform != "win32":
            log.info("studio: 이 PC 는 Windows 가 아니라 절전 방지를 쓰지 않는다")
            return
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="studio-awake", daemon=True)
                self._thread.start()
        self._asks.put(bool(on))

    def _loop(self) -> None:
        try:
            import ctypes

            call = ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            log.exception("studio: 절전 방지 API 를 찾지 못했다")
            return
        while True:
            on = self._asks.get()
            try:
                if not call(ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS):
                    log.warning("studio: SetThreadExecutionState 가 실패했다")
            except Exception:  # noqa: BLE001
                log.exception("studio: 절전 방지를 바꾸지 못했다")


class Awake:
    """The count of what is at work and the switch. `setter(on)` is called when the count leaves or reaches 0 —
    once `enable` has been called, and never raises out of here."""

    def __init__(self, setter: Callable[[bool], None] | None = None):
        self._setter = setter
        self._lock = threading.Lock()
        self._count = 0
        self._enabled = False
        self._on = False

    @property
    def count(self) -> int:
        return self._count

    @property
    def on(self) -> bool:
        return self._on

    def _set(self, on: bool) -> None:
        """Call the setter for a change of state (under the lock: calls follow the order of the changes)."""
        if self._on == on:
            return
        self._on = on
        try:
            (self._setter or _default)(on)
        except Exception:  # noqa: BLE001 - a PC that cannot be held awake must not stop a run
            log.exception("studio: 절전 방지를 %s 하지 못했다", "켜" if on else "꺼")

    def acquire(self) -> None:
        with self._lock:
            self._count += 1
            if self._enabled and self._count > 0:
                self._set(True)

    def release(self) -> None:
        """One less at work; never below 0 (a release the matching acquire did not come before is ignored)."""
        with self._lock:
            if self._count > 0:
                self._count -= 1
            if self._enabled and self._count == 0:
                self._set(False)

    @contextlib.contextmanager
    def hold(self) -> Iterator[None]:
        self.acquire()
        try:
            yield
        finally:
            self.release()

    def enable(self, setter: Callable[[bool], None] | None = None) -> None:
        """Start asking the system (the server's start): what is at work already holds the PC awake at once."""
        with self._lock:
            if setter is not None:
                self._setter = setter
            self._enabled = True
            if self._count > 0:
                self._set(True)

    def disable(self) -> None:
        """Stop asking (the server's stop): the PC may sleep again, the count goes on."""
        with self._lock:
            self._enabled = False
            self._set(False)


_default = _WindowsKeeper()
KEEPER = Awake()  # the server's one: queue.py and chat.py hold it, server.py enables it
