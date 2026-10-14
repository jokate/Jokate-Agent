"""The disk warning (V-3 — design/studio/ARCHITECTURE.md §5.9 디스크 경고).

When the free space of the drive the runs are written to (`machine.disk`) is below `min_free_gb`, the system room
gets one `disk_low` message (pushed at once: `push.NOTICE_EVENTS`); when it is above again, one `disk_ok` message
(not pushed). Whether it was told is read from the room — the newest of the two events is the state — so a server
that starts again does not tell of the same shortage twice, and a shortage after a recovery is told anew.
The timer has the shape of `stats.Reporter.watch`, kept by itself."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from . import machine
from .inbox import SYSTEM_ROOM
from .store import StudioStore

log = logging.getLogger(__name__)

DISK_LOW, DISK_OK = "disk_low", "disk_ok"  # `meta.event` of the two messages
CHECK_EVERY = 600.0  # seconds between two looks (`DiskWatch.watch`)
GB = 1024 ** 3


def last_state(store: StudioStore, *, page: int = 200) -> str | None:
    """`disk_low` or `disk_ok` — the event of the newest disk message of the system room — or None for none."""
    before = None
    while True:
        rows = store.list_messages(SYSTEM_ROOM, before_id=before, limit=page)
        for row in reversed(rows):
            event = (row["meta"] or {}).get("event")
            if event in (DISK_LOW, DISK_OK):
                return event
        if len(rows) < page:
            return None
        before = rows[0]["id"]


def _timer(delay: float, fn: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()
    return timer


class DiskWatch:
    """Looks at the free space now and then. `store()` gives the studio store (or raises, or None: no look),
    `folder()` the folder whose drive is asked, `min_free_gb()` the threshold — read at every look; 0 or less =
    no warning. Nothing is kept between two looks but the timer."""

    def __init__(self, store: Callable[[], StudioStore | None], *, folder: Callable[[], Any],
                 min_free_gb: Callable[[], float], disk: Callable[[Any], dict] = machine.disk):
        self.store, self.folder, self.min_free_gb, self.disk = store, folder, min_free_gb, disk
        self._lock = threading.Lock()
        self._watching, self._round, self._timer = False, 0, None

    def look(self) -> dict | None:
        """One look: the message when one was written now, else None. Nothing here raises."""
        try:
            minimum = float(self.min_free_gb() or 0)
            if minimum <= 0:
                return None
            try:
                store = self.store()
            except Exception:  # noqa: BLE001 - a studio DB that could not start says so at every request
                return None
            if store is None:
                return None
            seen = self.disk(self.folder())
            free = seen.get("free_bytes") if isinstance(seen, dict) else None
            if free is None:
                return None
            low, told = free < minimum * GB, last_state(store)
            if low and told != DISK_LOW:
                event = DISK_LOW
                body = (f"디스크 여유가 {free / GB:.1f}GB 로 기준 {minimum:g}GB 아래입니다 ({seen.get('path')}). "
                        "런 기록이 쌓이는 드라이브입니다")
            elif not low and told == DISK_LOW:
                event = DISK_OK
                body = f"디스크 여유가 {free / GB:.1f}GB 로 기준 {minimum:g}GB 를 회복했습니다"
            else:
                return None
            meta = {"event": event, "free_bytes": free, "min_free_gb": minimum, "path": seen.get("path")}
            with store.transaction() as tx:
                message = store.create_message(SYSTEM_ROOM, "system", body=body, meta=meta, tx=tx)
                store.emit(tx, "message.created", {"message": message}, room_id=SYSTEM_ROOM)
            return message
        except Exception:  # noqa: BLE001 - a look that failed is tried again at the next
            log.exception("studio: 디스크 여유를 확인하지 못했습니다")
            return None

    def watch(self, every: float = CHECK_EVERY,
              timer: Callable[[float, Callable[[], None]], Any] | None = None) -> None:
        """Look now and every `every` seconds from then on, until `unwatch` (as `stats.Reporter.watch`: the
        first look is on a timer too, delay 0; a second call while one goes on does nothing)."""
        start = timer or _timer
        with self._lock:
            if self._watching:
                return
            self._watching = True
            self._round += 1
            mine = self._round

        def wait(delay: float) -> None:
            handle = start(delay, tick)
            with self._lock:
                if self._watching and self._round == mine:
                    self._timer = handle
                    return
            handle.cancel()

        def tick() -> None:
            with self._lock:
                if not self._watching or self._round != mine:
                    return
            self.look()
            wait(every)

        wait(0)

    def unwatch(self) -> None:
        with self._lock:
            self._watching = False
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
