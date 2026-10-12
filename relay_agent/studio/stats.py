"""Progress counts and the daily report (S-13; ARCHITECTURE §6.4 `GET /studio/stats` · `progress`, §5.9 일일 보고).

The counts are made at every request from `tasks`, `task_events`, `inbox` and the cost of the runs — nothing here
is stored. `progress` is the one rule of a campaign's `done_ratio`; `bounds` reads `?from` · `?to`; `collect` is
the answer of `GET /studio/stats`. `growth` is the answer of `GET /studio/growth`: levels, experience and
achievements by the rule tables of §6.4 (`grow`, a pure function of the rows) and today's summary (`entries`).

The daily report is three lines — 한 일 / 막힌 것 / 결재 대기 — written once a day in the system room in the name
of the HR employee (`meta.event = daily_report`). `material` gathers what there is to say and `rule_lines` says it
by rule; a light model may word it better (`polish`, one call), and an answer that is not exactly the three lines —
or a call that fails — leaves the rule's lines. The report of a day is known from the room itself (`last_report`),
so a server that starts again does not write a second one. `Reporter` is the timer the server starts and stops.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from .employees import HR_DEPARTMENT
from .inbox import SYSTEM_ROOM
from .store import StudioStore, now as _now
from .tasks import STATES

log = logging.getLogger(__name__)

EVENT = "daily_report"  # `meta.event` of the report: push.py tells it by this (`push.DAILY_REPORT`), not by its author
LABELS = ("한 일", "막힌 것", "결재 대기")  # the three lines, in this order
KEYS = ("done", "blocked", "pending")      # the keys of `material`, one per label
SHOWN = 3        # titles a rule line names; the rest is "외 N개"
TITLE_MAX = 40   # letters of one title in a rule line
LINE_MAX = 200   # letters of one line of the report
CHECK_EVERY = 60.0  # seconds between two looks of the Reporter
MODEL_TIMEOUT_S = 90
SYSTEM = f"""너는 게임 스튜디오의 인사 직원이다. 받은 재료만으로 일일 보고를 한국어로 쓴다.
정확히 세 줄만 쓴다. 머리말 · 맺음말 · 빈 줄 · 마크다운 없이, 이 순서와 머리 그대로:
{LABELS[0]}: …
{LABELS[1]}: …
{LABELS[2]}: …

규칙
- 줄마다 {LINE_MAX}자 이내, 한 문장.
- 재료에 없는 것은 쓰지 않는다. 개수는 재료와 같아야 한다.
- 해당하는 것이 없으면 '없음' 이라고 쓴다."""

Writer = Callable[[str], Any]  # facts -> the text, or (text, usage) as handoff.HandoffWriter answers


# --- the counts (§6.4) ----------------------------------------------------------------------------------------
def progress(counts: dict[str, int]) -> dict:
    """`progress` of a campaign: `{by_status, total, done_ratio}`. Every status of §4.1 is a key of `by_status`
    and `total` counts them all. `done_ratio = done / (total − rejected − cancelled)`: a task that was rejected
    or cancelled is no work left to do, so a campaign whose other tasks are done is at 1. 0.0 with nothing to do."""
    by_status = {status: counts.get(status, 0) for status in STATES}
    total = sum(by_status.values())
    live = total - by_status["rejected"] - by_status["cancelled"]
    return {"by_status": by_status, "total": total, "done_ratio": by_status["done"] / live if live else 0.0}


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _moment(text: str | None, *, end: bool) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    if _DAY.fullmatch(text):
        day = date.fromisoformat(text) + timedelta(days=1 if end else 0)
        return datetime.combine(day, time()).isoformat(timespec="seconds")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is not None:  # the store's times are the server's local time, without a zone (store.now)
        moment = moment.astimezone().replace(tzinfo=None)
    return moment.isoformat(timespec="seconds")


def bounds(frm: str | None, to: str | None) -> tuple[str | None, str | None]:
    """`?from` · `?to` as `(since, until)` in the form of `store.now()`: `since <= time < until`, None = open.
    Each is `YYYY-MM-DD` or a time `datetime.fromisoformat` reads. A day as `from` is its 00:00; a day as `to`
    is the 00:00 after it, so that day is in. A time with a zone is turned into the server's local time.
    ValueError for a text that is neither, and for a `from` that is not before `to`."""
    try:
        since, until = _moment(frm, end=False), _moment(to, end=True)
    except (ValueError, OverflowError) as e:
        raise ValueError("from · to 는 YYYY-MM-DD 또는 ISO 시각이어야 합니다") from e
    if since is not None and until is not None and since >= until:
        raise ValueError("from 은 to 보다 앞이어야 합니다")
    return since, until


def _seconds(start: str | None, end: str | None) -> float | None:
    try:
        return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):
        return None


def collect(store: StudioStore, *, project_id: str | None = None, campaign_id: str | None = None,
            since: str | None = None, until: str | None = None,
            cost_of: Callable[[str], float] | None = None) -> dict:
    """`GET /studio/stats`: `{campaigns[{id, progress}], employees[{id, tasks_done, cost_usd}],
    inbox{pending, avg_wait_seconds}}`.

    - campaigns: the ones the filters name, as they are now — the period does not touch `progress`.
    - employees, in the order they were made: `tasks_done` = the tasks that became `done` in the period (the
      `at` of the `to_status = done` row of task_events), given to the task's assignee of now. `cost_usd` = the
      cost of the task runs started in the period (the `detail.run_id` of the T3 rows; `cost_of(run_id)`),
      4 decimals — a run counts whole in the period it started in; 0.0 without `cost_of`. The cost too goes to
      the task's assignee of now (the T3 row keeps no assignee), so the old runs of a task that changed hands are
      the new assignee's; a run of a task without one is not counted. Nothing is cached: `cost_of` is called
      once for each run of the request — a run goes on under the same id (T5 · T8, a reopened `done` run), so
      its cost is not final. Listed: the employees
      at work (with `project_id`: those of that project and those of none), and anyone else with a value.
    - inbox: `pending` = the items waiting now; `avg_wait_seconds` = the mean of `closed_at − created_at` of
      the items decided in the period (1 decimal), None when there is none — 0 would read "decided at once"."""
    scope = {"project_id": project_id, "campaign_id": campaign_id}
    counts = store.count_tasks_by_campaign(**scope)
    campaigns = [{"id": row["id"], "progress": progress(counts.get(row["id"], {}))}
                 for row in store.list_campaigns(project_id=project_id) if campaign_id in (None, row["id"])]

    done: dict[str, int] = {}
    for row in store.list_done_tasks(**scope, since=since, until=until):
        if row["assignee_id"]:
            done[row["assignee_id"]] = done.get(row["assignee_id"], 0) + 1
    cost: dict[str, float] = {}
    seen: set[str] = set()
    for row in store.list_run_starts(**scope, since=since, until=until):
        if cost_of is None or not row["assignee_id"] or row["run_id"] in seen:
            continue
        seen.add(row["run_id"])
        cost[row["assignee_id"]] = cost.get(row["assignee_id"], 0.0) + float(cost_of(row["run_id"]) or 0.0)
    employees = []
    for row in store.list_employees(include_retired=True):
        here = row["retired_at"] is None and (project_id is None or row["project_id"] in (None, project_id))
        if here or done.get(row["id"]) or cost.get(row["id"]):
            employees.append({"id": row["id"], "tasks_done": done.get(row["id"], 0),
                              "cost_usd": round(cost.get(row["id"], 0.0), 4)})

    waits = [w for w in (_seconds(row["created_at"], row["closed_at"])
                         for row in store.list_inbox_waits(**scope, since=since, until=until)) if w is not None]
    inbox = {"pending": store.count_inbox(status="pending", **scope),
             "avg_wait_seconds": round(sum(waits) / len(waits), 1) if waits else None}
    return {"campaigns": campaigns, "employees": employees, "inbox": inbox}


# --- growth and today's summary (§6.4 `GET /studio/growth`) ---------------------------------------------------
# The rule tables of §6.4 in code. Every number comes from a row of task_events or inbox; nothing is stored.
XP_PASS = 10     # a task the owner passed: a T7 row of task_events
XP_CLEAN = 5     # more for a task passed without having been sent back: no T8 · T9 row of that task
XP_DECIDED = 2   # an inbox item decided — the studio's only
LEVEL_STEP = 50  # level n begins at LEVEL_STEP · n · (n − 1)
ACHIEVEMENTS = (  # (id, title, what is counted, how many achieve it), in the order they are answered
    ("first_pass", "첫 통과", "passed", 1),
    ("pass_10", "통과 10건", "passed", 10),
    ("pass_50", "통과 50건", "passed", 50),
    ("clean_5", "재작업 없이 5건", "clean", 5),
    ("clean_20", "재작업 없이 20건", "clean", 20),
    ("decide_10", "결재 10건", "decided", 10),
    ("decide_100", "결재 100건", "decided", 100),
)


def level_floor(level: int) -> int:
    """The experience level `level` begins at: 0, 100, 300, 600, 1000, …"""
    return LEVEL_STEP * level * (level - 1)


def level_of(xp: int) -> int:
    """The highest level whose floor is not over `xp`; 1 with nothing. Whole numbers only."""
    level = 1
    while level_floor(level + 1) <= xp:
        level += 1
    return level


def _grade(xp: int) -> dict:
    level = level_of(xp)
    return {"level": level, "xp": xp, "level_xp": level_floor(level), "next_level_xp": level_floor(level + 1)}


def grow(passes: list[dict], decided: list[str]) -> dict:
    """The growth those rows make: `{studio, employees{id: …}, achievements[]}`. `passes` are the rows of
    `store.list_passes` (earliest first), `decided` the `closed_at` of the decided inbox items (earliest first).

    - studio: `XP_PASS` a pass, `XP_CLEAN` more for a clean one, `XP_DECIDED` a decided item; with `counts`.
    - employees: the same without the inbox, given to the task's assignee of now — a pass of a task without
      one is the studio's only.
    - achievements: achieved once the count reaches the threshold, at the time of the row that reached it
      (the n-th row), so the same rows give the same answer and a later row takes nothing back."""
    times = {"passed": [row["at"] for row in passes],
             "clean": [row["at"] for row in passes if not row["reworked"]],
             "decided": list(decided)}
    counts = {what: len(found) for what, found in times.items()}
    people: dict[str, dict[str, int]] = {}
    for row in passes:
        if row["assignee_id"]:
            mine = people.setdefault(row["assignee_id"], {"passed": 0, "clean": 0})
            mine["passed"] += 1
            mine["clean"] += 0 if row["reworked"] else 1
    xp = XP_PASS * counts["passed"] + XP_CLEAN * counts["clean"] + XP_DECIDED * counts["decided"]
    return {"studio": {**_grade(xp), "counts": counts},
            "employees": {key: {**_grade(XP_PASS * mine["passed"] + XP_CLEAN * mine["clean"]), "counts": mine}
                          for key, mine in people.items()},
            "achievements": [{"id": key, "title": title, "achieved": counts[what] >= need,
                              "achieved_at": times[what][need - 1] if counts[what] >= need else None}
                             for key, title, what, need in ACHIEVEMENTS]}


def growth(store: StudioStore, *, moment: str | None = None) -> dict:
    """`GET /studio/growth`: `{studio, employees[], achievements[], today}`, counted now — nothing is stored.

    - studio · achievements: `grow` of every pass and every decided inbox item there is.
    - employees, in the order they were made: `{id, level, xp, level_xp, next_level_xp, counts{passed, clean}}`
      of the employees at work and of anyone else with experience.
    - today: `{date, from, to, done, blocked, pending}`, each of the three `{count, items[{id, title}]}` — the
      daily report's material (`entries`) of the server's local day of `moment` (`store.now()` when not
      given): `from <= time < to` is that day's 00:00 to the next. `blocked` · `pending` are as they are now."""
    day = (moment or _now())[:10]
    since, until = bounds(day, day)
    grown = grow(store.list_passes(), [row["closed_at"] for row in store.list_inbox_waits()])
    employees = []
    for row in store.list_employees(include_retired=True):
        mine = grown["employees"].get(row["id"])
        if row["retired_at"] is None or (mine is not None and mine["xp"] > 0):
            employees.append({"id": row["id"], **(mine or {**_grade(0), "counts": {"passed": 0, "clean": 0}})})
    found = entries(store, since, until)
    today = {"date": day, "from": since, "to": until,
             **{key: {"count": len(found[key]), "items": found[key]} for key in KEYS}}
    return {"studio": grown["studio"], "employees": employees, "achievements": grown["achievements"],
            "today": today}


# --- the daily report: what there is to say (§5.9) ------------------------------------------------------------
def last_report(store: StudioStore, *, tx: Any = None, page: int = 200) -> dict | None:
    """The newest `daily_report` message of the system room, or None. Read from the newest back, a page at a
    time — the room is the record of which days were reported, so nothing else has to be kept."""
    before = None
    while True:
        rows = store.list_messages(SYSTEM_ROOM, before_id=before, limit=page, tx=tx)
        for row in reversed(rows):
            if (row["meta"] or {}).get("event") == EVENT:
                return row
        if len(rows) < page:
            return None
        before = rows[0]["id"]


def _day(row: dict | None) -> str | None:
    """The day (server's local date) a report is of: its `meta.date`, or the day it was written on."""
    if row is None:
        return None
    day = (row["meta"] or {}).get("date")
    return day if isinstance(day, str) and day else (row.get("created_at") or "")[:10] or None


def entries(store: StudioStore, since: str | None, until: str | None) -> dict[str, list[dict]]:
    """What the report and today's summary are made of, each entry `{id, title}`: `done` = the tasks that became
    done in `since <= time < until`, `blocked` = the tasks blocked now, `pending` = the inbox items waiting now
    (the longest waiting first). The ids are the tasks' (`done` · `blocked`) and the inbox items' (`pending`)."""
    rows = {"done": store.list_done_tasks(since=since, until=until),
            "blocked": store.list_tasks(status="blocked"),
            "pending": store.list_inbox(status="pending")}
    return {key: [{"id": row["id"], "title": row["title"]} for row in rows[key]] for key in KEYS}


def material(store: StudioStore, since: str | None, until: str | None) -> dict[str, list[str]]:
    """The titles the report is made of: those of `entries`, key by key."""
    return {key: [entry["title"] for entry in found] for key, found in entries(store, since, until).items()}


def _title(text: Any) -> str:
    """A title on one line (a line break would break the three-line form), cut to TITLE_MAX."""
    one = " ".join(str(text or "").split())
    return one if len(one) <= TITLE_MAX else one[:TITLE_MAX - 1] + "…"


def rule_lines(found: dict[str, list[str]]) -> list[str]:
    """The three lines by rule, exactly: `<label>: N개 — a, b, c 외 M개`, or `<label>: 없음`."""
    lines = []
    for label, key in zip(LABELS, KEYS):
        titles = list(found.get(key) or [])
        if not titles:
            lines.append(f"{label}: 없음")
            continue
        more = len(titles) - SHOWN
        text = f"{label}: {len(titles)}개 — " + ", ".join(_title(t) for t in titles[:SHOWN])
        lines.append((text + (f" 외 {more}개" if more > 0 else ""))[:LINE_MAX])
    return lines


def well_formed(text: Any) -> list[str] | None:
    """The three lines of a report, or None for a text that is not one: exactly three lines once the blanks
    around it are gone, the i-th beginning with `LABELS[i]:` and saying something after it, none over LINE_MAX."""
    if not isinstance(text, str):
        return None
    lines = [line.strip() for line in text.strip().splitlines()]
    if len(lines) != len(LABELS):
        return None
    for label, line in zip(LABELS, lines):
        if not line.startswith(label + ":") or not line[len(label) + 1:].strip() or len(line) > LINE_MAX:
            return None
    return lines


def polish(found: dict[str, list[str]], lines: list[str], writer: Writer | None) -> list[str]:
    """`lines` worded by the model — one call. `lines` as they are when there is no writer, when the call
    fails and when the answer is not the three lines (`well_formed`): the form is the contract, not the model."""
    if writer is None:
        return list(lines)
    facts = (json.dumps({label: found.get(key) or [] for label, key in zip(LABELS, KEYS)}, ensure_ascii=False)
             + "\n\n규칙으로 만든 세 줄 (형식의 본):\n" + "\n".join(lines))
    try:
        answer = writer(facts)
    except Exception as e:  # noqa: BLE001 - no claude, a timeout, an error answer: the rule's lines stand
        log.warning("studio: 일일 보고를 모델이 다듬지 못해 규칙으로 만든 문장을 씁니다: %s", e)
        return list(lines)
    text = answer[0] if isinstance(answer, tuple) and answer else answer
    worded = well_formed(text)
    if worded is None:
        log.warning("studio: 일일 보고의 모델 답이 세 줄 형식이 아니어서 규칙으로 만든 문장을 씁니다")
    return worded or list(lines)


def model_writer(model: str | None) -> Writer | None:
    """The light model that words the report: one call without tools from a neutral folder, as the hand-over
    is written (handoff.HandoffWriter). None — the rule's lines are written — for a blank model or a PC
    without `claude`."""
    name = (model or "").strip()
    exe = shutil.which("claude") if name else None
    if not exe:
        return None
    from ..handoff import HandoffWriter  # at the call: the counts need neither the runners nor a model

    return HandoffWriter(name, exe=exe, timeout_s=MODEL_TIMEOUT_S, system=SYSTEM)


def report(store: StudioStore, *, now: datetime | None = None, writer: Writer | None = None) -> dict | None:
    """Write the daily report and return its message, or None when the day of `now` has one already.

    The period is from the last report's `meta.to` (24 hours back for the first one) until `now`, so what was
    finished after a report is in the next one. The day (`meta.date`) is that of `now`, the moment the report
    began: a model call that runs past midnight still writes the report of the day it began on, and the next
    day gets its own. The model is called outside the transaction; the day is looked
    at again inside it, so two calls at once write one report. The author is the HR employee at work, or the
    system when there is none — the push tells the report by `meta.event` either way."""
    moment = (now or datetime.now()).replace(microsecond=0)
    today, until = moment.date().isoformat(), moment.isoformat(timespec="seconds")
    last = last_report(store)
    if _day(last) == today:
        return None
    since = (last["meta"] or {}).get("to") if last is not None else None
    if not isinstance(since, str) or not since or since > until:
        since = (moment - timedelta(hours=24)).isoformat(timespec="seconds")
    found = material(store, since, until)
    lines = polish(found, rule_lines(found), writer)
    meta = {"event": EVENT, "date": today, "from": since, "to": until}
    with store.transaction() as tx:
        if _day(last_report(store, tx=tx)) == today:
            return None
        hr = store.list_employees(department_id=HR_DEPARTMENT, tx=tx)
        author = hr[0]["id"] if hr else None
        message = store.create_message(SYSTEM_ROOM, "employee" if author else "system", author_id=author,
                                       body="\n".join(lines), meta=meta, tx=tx)
        store.emit(tx, "message.created", {"message": message}, room_id=SYSTEM_ROOM)
    return message


# --- the timer (server.py starts and stops it) ----------------------------------------------------------------
def due(at: str | None, moment: datetime) -> bool:
    """Has the time of day `at` ("HH:MM") come at `moment`? False for a blank or unreadable `at`: no report."""
    found = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", at or "")
    if not found:
        return False
    hour, minute = int(found[1]), int(found[2])
    return hour < 24 and minute < 60 and (moment.hour, moment.minute) >= (hour, minute)


def _timer(delay: float, fn: Callable[[], None]) -> threading.Timer:
    """A daemon timer, started (as studio/push.py has its own)."""
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()
    return timer


class Reporter:
    """Writes the daily report once the time of day has come. `store()` gives the studio store (or raises, or
    None: no look), `at()` the time of day and `model()` the model's name — all three read at every look, so
    a setting changed while the server runs is followed. Nothing is kept between two looks but the timer."""

    def __init__(self, store: Callable[[], StudioStore | None], *, at: Callable[[], str],
                 model: Callable[[], str] = lambda: "haiku", clock: Callable[[], datetime] = datetime.now,
                 writer: Callable[[str], Writer | None] = model_writer):
        self.store, self.at, self.model, self.clock, self.writer = store, at, model, clock, writer
        self._lock = threading.Lock()
        self._watching, self._round, self._timer = False, 0, None

    def look(self) -> dict | None:
        """One look: the report's message when it was written now, else None. Nothing here raises."""
        try:
            moment = self.clock()
            if not due(self.at(), moment):
                return None
            try:
                store = self.store()
            except Exception:  # noqa: BLE001 - a studio DB that could not start says so at every request
                return None
            if store is None or _day(last_report(store)) == moment.date().isoformat():
                return None  # looked at before the writer is made: no model is asked for a day that has its report
            return report(store, now=moment, writer=self.writer(self.model()))
        except Exception:  # noqa: BLE001 - a look that failed is tried again at the next
            log.exception("studio: 일일 보고를 쓰지 못했습니다")
            return None

    def watch(self, every: float = CHECK_EVERY,
              timer: Callable[[float, Callable[[], None]], Any] | None = None) -> None:
        """Look now and every `every` seconds from then on, until `unwatch`: the server's start and stop. The
        first look is on a timer too (delay 0) — the model may take over a minute and the start does not wait
        for it. A second call while one goes on does nothing. `timer(delay, fn)` starts a timer and returns a
        handle with `cancel()` — a test gives its own."""
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
            handle.cancel()  # stopped while this look was on its way

        def tick() -> None:
            with self._lock:
                if not self._watching or self._round != mine:
                    return
            self.look()
            wait(every)

        wait(0)

    def unwatch(self) -> None:
        """End `watch`: the timer that waits is cancelled, one that fires all the same does nothing."""
        with self._lock:
            self._watching = False
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
