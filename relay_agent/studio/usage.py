"""The usage window's safety net (V-4 — design/studio/ARCHITECTURE.md §4.6 · §5.9 사용량 한도).

`providers.py` knows how much of a Claude window is used (`status(...)["limits"]`) and benches the provider at
`switch_at_utilization` (95%) — that is not touched here. This watch looks at the same rows once a minute:

- A gating window, not stale, whose `utilization` has reached `warn_at()` (`Config.studio_usage_warn_at`, below the
  95%) gets one `usage_high` message in the system room — pushed at once (`push.NOTICE_EVENTS`). A window is told of
  once: its key is (provider, window, `resets_at`), and the room is the record, so a restart does not tell it twice
  and the window after the reset is told anew.
- With `hold_on()` (`Config.studio_usage_hold`, off by default) `held()` is True while such a window has no approval:
  the queue then starts no new task (T3). `ask()` files the owner's question for that — a `budget` item without a
  task and without a run (`payload.stop_reason = 'usage_hold'`), once per window. `approve` starts the line again,
  `reject` keeps it held until that window is reset. A task that works already is not held.
The timer has the shape of `disk.DiskWatch`."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .disk import _timer
from .inbox import SYSTEM_ROOM, _one_line
from .store import StudioStore

log = logging.getLogger(__name__)

USAGE_HIGH = "usage_high"  # `meta.event` of the message
HOLD_REASON = "usage_hold"  # `payload.stop_reason` of the budget item
HOLD_TITLE = "사용량 임계 — 새 작업 시작 보류"
CHECK_EVERY = 60.0  # seconds between two looks (`UsageWatch.watch`)


def is_hold(item: dict) -> bool:
    """Whether that inbox item is the question of a usage hold."""
    return (item.get("kind") == "budget" and item.get("task_id") is None
            and (item.get("payload") or {}).get("stop_reason") == HOLD_REASON)


def _key(provider: Any, window: Any, resets_at: Any) -> str:
    return f"{provider}|{window}|{resets_at}"


class UsageWatch:
    """`store()` gives the studio store (or raises, or None: nothing is looked at), `status()` the providers' status
    rows (`[providers.status('claude')]`), `warn_at()` the threshold (0 or less = off), `hold_on()` whether the line
    is held, `kick()` looks at the line again. All of them are read at every use."""

    def __init__(self, store: Callable[[], StudioStore | None], *, status: Callable[[], list[dict]],
                 warn_at: Callable[[], float], hold_on: Callable[[], bool] = lambda: False,
                 kick: Callable[[], Any] = lambda: None):
        self.store, self.status, self.warn_at, self.hold_on, self.kick = store, status, warn_at, hold_on, kick
        self._told: set[str] | None = None  # the keys the room has told; None = not read from the room yet
        self._lock = threading.Lock()
        self._asking = threading.local()  # `ask` is not entered again by the kick its own item may cause
        self._watching, self._round, self._timer = False, 0, None

    # --- what is high ---------------------------------------------------------------------------------------
    def high(self) -> list[dict]:
        """The windows at or over the threshold now: `{provider, window, utilization, resets_at, key}`."""
        try:
            warn = float(self.warn_at() or 0)
        except (TypeError, ValueError):
            return []
        if warn <= 0:
            return []
        rows = []
        for state in self.status() or []:
            for row in state.get("limits") or []:
                use = row.get("utilization")
                if not row.get("gating") or row.get("stale") or not isinstance(use, (int, float)) or use < warn:
                    continue
                rows.append({"provider": state.get("name"), "window": row.get("window"), "utilization": use,
                             "resets_at": row.get("resets_at"),
                             "key": _key(state.get("name"), row.get("window"), row.get("resets_at"))})
        return rows

    def _open(self) -> StudioStore | None:
        try:
            return self.store()
        except Exception:  # noqa: BLE001 - a studio DB that could not start says so at every request
            return None

    def _approved(self, store: StudioStore) -> set[str]:
        return {(row["payload"] or {}).get("key") for row in store.list_inbox(kind="budget")
                if is_hold(row) and row["decision"] == "approve"}

    def held(self) -> bool:
        """Whether the queue starts no new task now: the hold is on and a window at the threshold has no
        approval. Never raises (the queue treats it as False then)."""
        try:
            if not self.hold_on():
                return False
            rows = self.high()
            if not rows:
                return False
            store = self._open()
            if store is None:
                return False
            approved = self._approved(store)
            return any(row["key"] not in approved for row in rows)
        except Exception:  # noqa: BLE001
            log.exception("studio: 사용량 보류를 확인하지 못했다")
            return False

    def ask(self) -> dict | None:
        """File the owner's question of the hold for each window that has none yet (pending or decided): the item
        of the first, or None when nothing was filed. Never raises."""
        if getattr(self._asking, "on", False):
            return None
        self._asking.on = True
        try:
            if not self.hold_on():
                return None
            rows = self.high()
            store = self._open()
            if not rows or store is None:
                return None
            made = None
            for row in rows:
                with store.transaction() as tx:
                    if any(is_hold(item) and item["payload"].get("key") == row["key"]
                           for item in store.list_inbox(kind="budget", tx=tx)):
                        continue
                    payload = {"scope": "run", "stage": "", "spent_usd": 0, "limit_usd": 0,
                               "stop_reason": HOLD_REASON, "key": row["key"], "provider": row["provider"],
                               "window": row["window"], "utilization": row["utilization"],
                               "resets_at": row["resets_at"]}
                    item = store.create_inbox_item("budget", _one_line(HOLD_TITLE), "system", payload=payload, tx=tx)
                    store.emit(tx, "inbox.created", {"inbox": item}, project_id=item["project_id"])
                made = made or item
            return made
        except Exception:  # noqa: BLE001
            log.exception("studio: 사용량 보류 결재를 올리지 못했다")
            return None
        finally:
            self._asking.on = False

    # --- the message ----------------------------------------------------------------------------------------
    def _told_keys(self, store: StudioStore, *, page: int = 200) -> set[str]:
        keys: set[str] = set()
        before = None
        while True:
            rows = store.list_messages(SYSTEM_ROOM, before_id=before, limit=page)
            for row in rows:
                meta = row["meta"] or {}
                if meta.get("event") == USAGE_HIGH:
                    keys.add(_key(meta.get("provider"), meta.get("window"), meta.get("resets_at")))
            if len(rows) < page:
                return keys
            before = rows[0]["id"]

    def look(self) -> list[dict]:
        """One look: the messages written now (one per window not told yet). With the hold on, the line is looked
        at again at every look — a window that was reset frees what it held. Nothing here raises."""
        written: list[dict] = []
        try:
            rows = self.high()
            store = self._open() if rows else None
            if rows and store is not None:
                with self._lock:
                    if self._told is None:
                        self._told = self._told_keys(store)
                    fresh = [row for row in rows if row["key"] not in self._told]
                    self._told.update(row["key"] for row in fresh)
                warn, held = float(self.warn_at()), bool(self.hold_on())
                for row in fresh:
                    body = (f"{row['provider']} 의 {row['window']} 사용 창이 {row['utilization']:.0%} 에 닿았습니다 "
                            f"(알림 기준 {warn:.0%})."
                            + (" 새 작업 시작을 미루고 결재로 묻습니다" if held else " 작업은 그대로 시작합니다"))
                    meta = {"event": USAGE_HIGH, "provider": row["provider"], "window": row["window"],
                            "utilization": row["utilization"], "resets_at": row["resets_at"], "warn_at": warn,
                            "held": held}
                    with store.transaction() as tx:
                        message = store.create_message(SYSTEM_ROOM, "system", body=body, meta=meta, tx=tx)
                        store.emit(tx, "message.created", {"message": message}, room_id=SYSTEM_ROOM)
                    written.append(message)
        except Exception:  # noqa: BLE001 - a look that failed is tried again at the next
            log.exception("studio: 사용 창을 확인하지 못했습니다")
        try:
            if self.hold_on():
                self.kick()
        except Exception:  # noqa: BLE001
            log.exception("studio: 사용 창을 본 뒤 줄을 다시 보지 못했습니다")
        return written

    def watch(self, every: float = CHECK_EVERY,
              timer: Callable[[float, Callable[[], None]], Any] | None = None) -> None:
        """Look now and every `every` seconds from then on, until `unwatch` (as `DiskWatch.watch`)."""
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
