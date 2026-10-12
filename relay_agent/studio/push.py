"""Push to the owner's phone through FCM HTTP v1 (N-1 · N-2; design/studio/ARCHITECTURE.md §5.9 · §3.14 · §11.3).

What is pushed, each once:

- an inbox item that was made (`inbox.created`, all nine kinds) — heard as a commit listener (`api.on_commit`),
  for the push carries the item's id and the engine's alert has none: a run's stop becomes an item only after the
  run is let go (studio/runs.py);
- the engine's alerts that are no stop (`run_failed`, `campaign_done`, `campaign_paused`) — heard beside the toast,
  as one more receiver of `notify.Fanout`. The three stops (`runs.STOP_EVENTS`) are left to the item they become,
  a task run's failure to the item or the new place in the line it becomes (studio/work.py), the failure of a
  chat · read turn to the `run_error` notice written in its room (studio/chat.py; `Pusher.is_turn`), and
  `run_done` to the review item or the employee's words the run ends in;
- what else the table of §5.9 names, all of it stream events of the same commits (`classify`): the notices
  written in a room (`NOTICE_EVENTS`), an employee's words unless the room is muted, a task's start (T3) and its
  `progress` notes, the daily report and a project job that ended — not one the owner had cleaned away.

Each goes out with a grade (N-2) — urgent, quiet or digest: `data.grade`, the Android priority and the channel id
(`CHANNELS`; the app makes the channels). A device inside its quiet hours (`settings.quiet_hours`, `quiet_now`)
gets an urgent push as a quiet one: lowered, never dropped. An item that still waits `remind_after` seconds after
it was made or last told of is pushed again (`Pusher.remind`, looked at by `watch` at the start and every
minute): the time is `inbox.reminded_at`, so a restart goes on from it. A reminder passes over a device in its
quiet hours, and when it reached none nothing is stamped — it goes out at the first look after them.

A push carries a title, one line, the deep link `{type, id}` and its grade, no more: the app reads the rest from this
server. It goes to every device that is not revoked and has an `fcm_token`. A token FCM calls dead
(`UNREGISTERED`, nothing less) is cleared, a failure that may pass is tried again, and what could not be sent is
dropped — the item still waits in the inbox. An access token that could not be had is not asked for again for a
minute (`ISSUE_HOLD`): the pushes of that minute are dropped without a request.

The service account key is the file `Config.studio_fcm_key` names. It is read at the first push, never at import:
without a usable key, or without `httpx` · `jwt` (both come with `mcp`, see uv.lock), nothing is sent and the
server runs as before. Nothing here raises into a commit or a run. The HTTP is done by one daemon thread, so the
request that committed does not wait for Google.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import ROOT
from ..notify import NOTIFY_KINDS, Fanout
from .inbox import KIND_LABEL

try:  # not named in pyproject.toml: they come with `mcp`. Without them only this sender is off (load_key)
    import httpx
    import jwt
except ImportError:  # pragma: no cover - the locked environment has both
    httpx = jwt = None

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_URI = "https://oauth2.googleapis.com/token"  # when the key file names none
GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
KEY_FIELDS = ("project_id", "client_email", "private_key")
TOKEN_LIFE = 3600     # seconds the signed request asks for, and what is assumed when the answer names none
TOKEN_MARGIN = 60.0   # an access token is asked anew this long before it ends
ISSUE_HOLD = 60.0     # after an access token could not be had, none is asked for this long: the one thread is not held
TIMEOUT = 10.0
WAITS = (1.0, 3.0)    # before the second and the third try; short, for one thread sends to every device in turn
TRIES = len(WAITS) + 1
MAX_WAIT = 30.0       # the most a `Retry-After` is followed
QUEUE_MAX = 1000
LINE_MAX = 80         # §5.1: a title is one line of 80 characters
RUN_KINDS = ("run_failed", "campaign_done", "campaign_paused")  # the engine's alerts pushed here; () = none

# N-2 (§5.9): how loud a push is. The grade goes out as `data.grade`, the Android priority and the channel.
URGENT, QUIET, DIGEST = "urgent", "quiet", "digest"  # 즉시 · 조용히 · 묶음
CHANNELS = {URGENT: "katae_urgent", QUIET: "katae_quiet", DIGEST: "katae_digest"}  # the app makes them (A-11)
DIGEST_TAG = "katae_digest"  # one entry in the drawer: a later digest takes the place of the one before
NOTICE_EVENTS = {  # `meta.event` of the notices of §5.9 that are pushed at once -> the title of the push
    "needs_assignment": "배치 필요", "upstream_rejected": "선행 반려 · 거절", "run_error": "실행하지 못함",
    "invariant_broken": "불변식 깨짐", "group_exchange_limit": "직원 대화 멈춤",  # the second: a group room held (§4.6)
}
DAILY_REPORT = "daily_report"  # `meta.event` of the daily report (S-13): the one digest
DAILY_TITLE, EMPLOYEE_TITLE, TASK_TITLE = "일일 보고", "직원", "작업"
ATTACHED, STARTED, PROGRESS = "첨부", "작업 시작", "진행 상황"
JOB_DONE, JOB_FAILED = "프로젝트 생성 완료", "프로젝트 생성 실패"
REMIND_AFTER = 7200.0  # seconds a waiting item is left alone; the server gives `Config.studio_remind_after`
REMIND_EVERY = 60.0    # seconds between two looks at the waiting items (`Pusher.watch`)
REMIND_NOTE = "다시 알림"

SENT, DEAD, FAILED = "sent", "dead", "failed"  # what Sender.send answers


def key_file(path: str | Path | None) -> Path | None:
    """Where the key file of that setting is, None for no path: `~` is the user's folder and a relative path is
    taken from this repository's folder. Nothing is opened and nothing is raised — the guard asks too, to keep the
    file from an employee wherever the owner put it (studio/guard.py `write_ctx`)."""
    text = str(path or "").strip()
    if not text:
        return None
    file = Path(text)
    try:
        file = file.expanduser()
    except RuntimeError:  # `~name` of nobody: left as written, and reading it fails with the warning of load_key
        pass
    return file if file.is_absolute() else ROOT / file


def load_key(path: str | Path | None) -> dict | None:
    """The service account of that key file — `project_id`, `client_email`, `private_key`, `token_uri` — or None:
    no path, no such file, not readable, not JSON, a field missing, or a key that does not sign. One warning says
    which (never the key's text) and nothing is raised: without a key the push is off, that is all. A relative
    path is taken from this repository's folder (`key_file`). No other place is looked in."""
    file = key_file(path)
    if file is None:
        return None
    if httpx is None or jwt is None:
        log.warning("studio: httpx · pyjwt 가 없어 푸시를 끕니다")
        return None
    try:
        data = json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        log.warning("studio: FCM 키 파일을 읽지 못해 푸시를 끕니다 — %s (%s)", file, type(e).__name__)
        return None
    if not isinstance(data, dict):
        data = {}
    missing = [name for name in KEY_FIELDS if not isinstance(data.get(name), str) or not data[name].strip()]
    if missing:
        log.warning("studio: FCM 키 파일에 %s 가 없어 푸시를 끕니다 — %s", " · ".join(missing), file)
        return None
    key = {name: data[name] for name in KEY_FIELDS}
    uri = data.get("token_uri")
    key["token_uri"] = uri.strip() if isinstance(uri, str) and uri.strip() else TOKEN_URI
    try:
        jwt.encode({"iss": key["client_email"]}, key["private_key"], algorithm="RS256")
    except Exception as e:  # noqa: BLE001 - whatever the crypto library says of a key it cannot use
        log.warning("studio: FCM 키로 서명할 수 없어 푸시를 끕니다 — %s (%s)", file, type(e).__name__)
        return None
    return key


def _json(response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_code(response) -> str:
    """`errorCode` of FCM's error body (`error.details[]`, the FcmError entry), "" when it has none."""
    error = _json(response).get("error")
    details = error.get("details") if isinstance(error, dict) else None
    for detail in details if isinstance(details, list) else ():
        if isinstance(detail, dict) and isinstance(detail.get("errorCode"), str):
            return detail["errorCode"]
    return ""


def _passing(code: int) -> bool:
    """A failure that may be gone at the next try."""
    return code == 429 or code >= 500


def _retry_after(response, default: float) -> float:
    try:
        seconds = float(response.headers.get("retry-after"))
    except (TypeError, ValueError):
        return default
    return min(max(seconds, 0.0), MAX_WAIT)


def _line(text: Any) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0].strip()[:LINE_MAX] if lines else ""


_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def _minutes(text: Any) -> int | None:
    """`HH:MM` as minutes of the day, None for anything else."""
    found = _HHMM.fullmatch(text) if isinstance(text, str) else None
    return int(found.group(1)) * 60 + int(found.group(2)) if found else None


def quiet_now(settings: Any, ts: float) -> bool:
    """Is a device with those settings inside its quiet hours at `ts` (seconds of the epoch)?

    `settings.quiet_hours` is `{start, end, utc_offset_min}`: `start` and `end` are `HH:MM` on the device's own
    clock, `utc_offset_min` the minutes that clock is ahead of UTC — without it this PC's local time is read.
    From `start` until just before `end`; a `start` later than `end` goes over midnight, the same two are no
    quiet hours. Anything that is not of that shape is no quiet hours either: nothing is raised."""
    hours = settings.get("quiet_hours") if isinstance(settings, dict) else None
    if not isinstance(hours, dict):
        return False
    start, end = _minutes(hours.get("start")), _minutes(hours.get("end"))
    if start is None or end is None or start == end:
        return False
    offset = hours.get("utc_offset_min")
    try:
        if isinstance(offset, (int, float)) and not isinstance(offset, bool):
            at = datetime.fromtimestamp(ts, timezone(timedelta(minutes=offset)))
        else:
            at = datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return False
    minute = at.hour * 60 + at.minute
    return (start <= minute < end) if start < end else (minute >= start or minute < end)


def _seconds(stamp: Any) -> float | None:
    """A time of the store (`store.now()`: local, to the second, no zone) as seconds of the epoch."""
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _stamp(ts: float) -> str:
    """Seconds of the epoch as the store writes a time (`store.now()`)."""
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _timer(delay: float, fn: Callable[[], None]) -> threading.Timer:
    """A daemon timer, started (as studio/cutoff.py has its own). `cancel()` is all `Pusher.unwatch` asks of it."""
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()
    return timer


# --- what a stream event is pushed as (§5.9) ------------------------------------------------------------------
Told = tuple[str, str, str, "dict | None"]  # (grade, title, one line, deep link)


def classify(event: dict, store) -> Told | None:
    """What a stream event is pushed as, or None for one that is not pushed: the table of §5.9. Read when the
    push's turn comes, so an item decided meanwhile and a room muted meanwhile are not told of.

    - `inbox.created`: urgent, while the item still waits. Title = the item's, the line = its kind.
    - `message.created`: the daily report is the digest; a system message whose `meta.event` is one of
      `NOTICE_EVENTS` is urgent and opens its task (`meta.task_id`) or else its room; an employee's words are
      quiet — title = the employee's name — and not pushed at all for a muted room. The owner's own words and
      every other system message (the line of an item or a decision: the item's push tells) are not pushed.
    - a task's start (`task.transitioned`, T3) and a `progress` note (`task.updated`): quiet.
    - `job.finished`: urgent; it opens the project that was made, or the job that failed. One that was
      `cleaned` is not pushed."""
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    kind = event.get("type")
    if kind == "inbox.created":
        return _of_inbox(data, store)
    if kind == "message.created":
        return _of_message(data, store)
    if kind == "task.transitioned" and data.get("rule") == "T3":
        return _of_task(data, STARTED)
    if kind == "task.updated" and data.get("change") == "progress":
        return _of_task(data, _note(data, store) or PROGRESS)
    if kind == "job.finished":
        return _of_job(data)
    return None


def _of_inbox(data: dict, store) -> Told | None:
    inbox = data.get("inbox")
    inbox_id = inbox.get("id") if isinstance(inbox, dict) else None
    item = store.get_inbox_item(inbox_id) if inbox_id else None
    if item is None or item["status"] != "pending":
        return None  # decided or cancelled before its turn came: nothing to ask any more
    label = KIND_LABEL.get(item["kind"], "")
    return URGENT, _line(item["title"]) or label, label, {"type": "inbox", "id": item["id"]}


def _of_message(data: dict, store) -> Told | None:
    message = data.get("message")
    if not isinstance(message, dict) or not message.get("room_id"):
        return None
    meta = message.get("meta") if isinstance(message.get("meta"), dict) else {}
    name = meta.get("event") if isinstance(meta.get("event"), str) else ""
    room_link = {"type": "room", "id": message["room_id"]}
    line = _line(message.get("body"))
    if name == DAILY_REPORT:  # written in the name of the HR employee (§5.9): told by its event, not its author
        return DIGEST, DAILY_TITLE, line, room_link
    author = message.get("author_kind")
    if author == "system":
        if name not in NOTICE_EVENTS:
            return None
        link = {"type": "task", "id": meta["task_id"]} if meta.get("task_id") else room_link
        return URGENT, NOTICE_EVENTS[name], line, link
    if author != "employee":
        return None  # the owner's own words
    room = store.get_room(message["room_id"])
    if room is None or room["muted"]:
        return None
    employee = store.get_employee(message["author_id"]) if message.get("author_id") else None
    title = _line((employee or {}).get("name")) or _line(room["title"]) or EMPLOYEE_TITLE
    return QUIET, title, line or ATTACHED, room_link


def _of_task(data: dict, line: str) -> Told | None:
    task = data.get("task")
    if not isinstance(task, dict) or not task.get("id"):
        return None
    return QUIET, _line(task.get("title")) or TASK_TITLE, line, {"type": "task", "id": task["id"]}


def _note(data: dict, store) -> str:
    """The first line of the `progress` note a `task.updated` tells of (the reason of its task_events row)."""
    task, event_id = data.get("task"), data.get("event_id")
    if not isinstance(task, dict) or not task.get("id") or not isinstance(event_id, int):
        return ""
    rows = store.list_task_events(task["id"], after_id=event_id - 1, limit=1)
    return _line(rows[0].get("reason")) if rows and rows[0].get("id") == event_id else ""


def _of_job(data: dict) -> Told | None:
    """§7.4 `job.finished` = `{job_id, kind, status, project_id, error}` (M-8 writes it; the shape is its to
    settle). `cleaned` is not pushed: the owner had what was made cleared away, and knows. Every other status
    that is not `done` is told as a failure, so a new name M-8 gives one does not go unheard."""
    status = data.get("status")
    if status == "cleaned":
        return None
    done = status == "done"
    if done and data.get("project_id"):
        link = {"type": "project", "id": data["project_id"]}
    else:
        link = {"type": "job", "id": data["job_id"]} if data.get("job_id") else None
    return URGENT, JOB_DONE if done else JOB_FAILED, "" if done else _line(data.get("error")), link


class Sender:
    """One service account: its access token, kept until shortly before it ends, and the sending of one message.
    `clock` and `sleep` are given in tests. `httpx.post` is the only way out."""

    def __init__(self, key: dict, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] = time.sleep):
        self.key, self.clock, self.sleep = key, clock, sleep
        self._lock = threading.Lock()
        self._token: str | None = None
        self._until = 0.0
        self._failed_until = 0.0  # no access token is asked for before this (ISSUE_HOLD)

    def access_token(self) -> str | None:
        """The OAuth access token, asked for with a JWT this key signed. None when it could not be had — and then
        for `ISSUE_HOLD` seconds more without asking: every device of every job would wait through the tries
        again, on the one thread that sends."""
        with self._lock:
            now = self.clock()
            if self._token is not None and now < self._until:
                return self._token
            self._token = None
            if now < self._failed_until <= now + ISSUE_HOLD:  # the upper bound: a clock set back does not stretch it
                return None
            token, life = self._issue(now)
            if token is None:
                self._failed_until = self.clock() + ISSUE_HOLD  # from the last try, not the first
            else:
                self._token, self._until = token, now + max(life - TOKEN_MARGIN, 0.0)
            return token

    def _issue(self, now: float) -> tuple[str | None, float]:
        key = self.key
        claims = {"iss": key["client_email"], "scope": SCOPE, "aud": key["token_uri"],
                  "iat": int(now), "exp": int(now) + TOKEN_LIFE}
        try:
            assertion = jwt.encode(claims, key["private_key"], algorithm="RS256")
        except Exception as e:  # noqa: BLE001 - load_key signed with it once; say so and send nothing
            log.warning("studio: FCM 키로 서명하지 못했습니다 (%s). %d초 동안 다시 묻지 않습니다",
                        type(e).__name__, ISSUE_HOLD)
            return None, 0.0
        wait, said = 0.0, "응답 없음"
        for attempt in range(TRIES):
            if attempt:
                self.sleep(wait)
            wait = WAITS[min(attempt, len(WAITS) - 1)]
            try:
                response = httpx.post(key["token_uri"], data={"grant_type": GRANT, "assertion": assertion},
                                      timeout=TIMEOUT)
            except httpx.HTTPError as e:
                said = type(e).__name__
                continue
            code = response.status_code
            if code == 200:
                body = _json(response)
                token, life = body.get("access_token"), body.get("expires_in")
                if isinstance(token, str) and token:
                    known = isinstance(life, (int, float)) and not isinstance(life, bool)
                    return token, float(life if known else TOKEN_LIFE)
                said = "access_token 이 없는 응답"
                break
            said = f"HTTP {code}"
            if not _passing(code):
                break
            wait = _retry_after(response, wait)
        log.warning("studio: FCM 액세스 토큰을 받지 못했습니다 — %s. %d초 동안 다시 묻지 않습니다", said, ISSUE_HOLD)
        return None, 0.0

    def _drop(self, token: str) -> None:
        with self._lock:
            if self._token == token:
                self._token = None

    def send(self, fcm_token: str, title: str, line: str = "", link: dict | None = None,
             grade: str = URGENT) -> str:
        """Send one push to one device: `sent`, `dead` (FCM says that token is gone: the errorCode `UNREGISTERED`,
        which comes with a 404 — not tried again) or `failed`. 429 · 5xx · a network error are tried again, and a
        401 after the access token was dropped: three tries in all. Any other 4xx is `failed` at once and says
        nothing about the token — a 400 may be this message's fault, and a 404 without that errorCode may be the
        project's or the address's: a token cleared by mistake is a phone to pair again, one kept by mistake only
        a request in vain.

        `grade` is how loud the push is (§5.9): `data.grade` for the app in the foreground, the Android priority
        (HIGH for an urgent one only) and the channel for the system. A digest has one tag, so the drawer holds
        one. A grade nobody knows is sent as urgent."""
        grade = grade if grade in CHANNELS else URGENT
        data = {"grade": grade}
        if link:
            data.update(type=str(link["type"]), id=str(link["id"]))
        drawer = {"channel_id": CHANNELS[grade]}
        if grade == DIGEST:
            drawer["tag"] = DIGEST_TAG
        message: dict[str, Any] = {
            "token": fcm_token, "notification": {"title": title, "body": line}, "data": data,
            "android": {"priority": "HIGH" if grade == URGENT else "NORMAL", "notification": drawer}}
        url = SEND_URL.format(project_id=self.key["project_id"])
        wait, said = 0.0, "응답 없음"
        for attempt in range(TRIES):
            if attempt:
                self.sleep(wait)
            wait = WAITS[min(attempt, len(WAITS) - 1)]
            access = self.access_token()
            if access is None:
                return FAILED  # asked by the same rule already, and said why
            try:
                response = httpx.post(url, headers={"Authorization": f"Bearer {access}"},
                                      json={"message": message}, timeout=TIMEOUT)
            except httpx.HTTPError as e:
                said = type(e).__name__
                continue
            code = response.status_code
            if code == 200:
                return SENT
            reason = _error_code(response)
            if reason == "UNREGISTERED":
                return DEAD
            said = f"HTTP {code} {reason}".strip()
            if code == 401:
                self._drop(access)
            elif _passing(code):
                wait = _retry_after(response, wait)
            else:
                break
        log.warning("studio: 푸시를 보내지 못했습니다 — %s", said)
        return FAILED


class Pusher:
    """What the server hears with: `committed` is the commit listener, the object itself a receiver of the
    engine's alerts. Both only put a job in the line; one daemon thread, started with the first job, sends.

    `inbox_factory()` gives the Inbox whose store is read, or None while the studio store is not open — then
    nothing is sent. The store is taken on the thread that tells, not by the worker: that one would open the
    store anew after a test closed it (see ws.py `_read_live`). `key_path()` gives the path of the key file,
    "" for none; the file is read again only when that path changes.

    `clock` gives the seconds of the epoch the quiet hours and the reminders are read on (a test gives its own).
    `remind_after` is the seconds a waiting item is left alone, or a callable that gives them, asked at every
    look; 0 or less = no reminder.

    `is_turn(run_id)` says whether that run is a chat · read turn at work now (`attach` gives
    `api.tracked_run`). The `run_failed` of such a run is not pushed: the `run_error` notice chat.py writes in
    its room is, and that one opens the room. It is asked in `__call__`, on the notifier's thread — the turn
    is tracked only while `engine.advance` runs, and the worker would ask too late. None = no run is a turn."""

    def __init__(self, inbox_factory: Callable[[], Any], key_path: Callable[[], str | None], *,
                 sender: Callable[[dict], Sender] = Sender, clock: Callable[[], float] = time.time,
                 remind_after: float | Callable[[], float] = REMIND_AFTER,
                 is_turn: Callable[[str], bool] | None = None):
        self.inbox_factory = inbox_factory
        self.key_path = key_path
        self.make_sender = sender
        self.clock = clock
        self.is_turn = is_turn
        self.remind_after: Callable[[], float] = remind_after if callable(remind_after) else (lambda: remind_after)
        self._looking = False                # a look at the waiting items is in the line or at work (`remind`)
        self._watching = False               # `watch` goes on
        self._round = 0                      # which `watch` a timer is of: one that fires after `unwatch` ends
        self._timer: Any = None              # the handle of the next look
        self._lock = threading.Lock()
        self._path: str | None = None        # the path `_sender` was made from; None = not looked at yet
        self._sender: Sender | None = None
        self._jobs: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._worker: threading.Thread | None = None

    def ready(self) -> Sender | None:
        """The sender, or None while the push is off (no path, or a key file that cannot be used)."""
        try:
            path = str(self.key_path() or "").strip()
        except Exception:  # noqa: BLE001 - a config that cannot be read is no key
            path = ""
        with self._lock:
            if path != self._path:
                key = load_key(path) if path else None
                self._path, self._sender = path, (self.make_sender(key) if key is not None else None)
            return self._sender

    def _store(self):
        return getattr(self.inbox_factory(), "store", None)

    def _put(self, *job) -> bool:
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            log.warning("studio: 보낼 푸시가 %d개 밀려 하나를 버립니다", QUEUE_MAX)
            return False
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="studio-push", daemon=True)
                self._worker.start()
        return True

    def committed(self, ids: tuple[int, ...]) -> None:
        """`api.on_commit`: stream events were committed. On the committing thread, so nothing is read here."""
        try:
            sender = self.ready() if ids else None
            store = self._store() if sender is not None else None
            if store is not None:
                self._put(self._events, sender, store, tuple(ids))
        except Exception:  # noqa: BLE001 - a push must not undo or hide a committed change
            log.exception("studio: 푸시를 줄에 넣지 못했습니다 (stream events %s)", ids)

    def __call__(self, run_id: str, kind: str, title: str, detail: dict) -> None:
        """The notifier's call (`notify.Fanout`). The stops are not pushed from here: their item is. Nor is the
        failure of a chat · read turn (`is_turn`): the notice written in its room is — once for one thing."""
        if kind not in RUN_KINDS:
            return
        try:
            if kind == "run_failed" and self.is_turn is not None and self.is_turn(run_id):
                return
            sender = self.ready()
            store = self._store() if sender is not None else None
            if store is not None:
                self._put(self._run, sender, store, run_id, kind, str(title or ""))
        except Exception:  # noqa: BLE001 - an alert never breaks a run
            log.exception("studio: 런 %s 의 푸시를 줄에 넣지 못했습니다", run_id)

    def drain(self, timeout: float = 5.0) -> bool:
        """Wait until every job that was put in is done (tests). False when that took longer than `timeout`."""
        end = time.monotonic() + timeout
        with self._jobs.all_tasks_done:
            while self._jobs.unfinished_tasks:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._jobs.all_tasks_done.wait(left)
        return True

    def stop(self) -> None:
        """End the worker thread after the jobs already in the line (tests). A later job starts a new one."""
        with self._lock:
            worker, self._worker = self._worker, None
        if worker is not None and worker.is_alive():
            self._jobs.put(None)

    def remind(self) -> None:
        """Have the waiting inbox items looked at once (§5.9 재알림): each that still waits `remind_after`
        seconds after it was made or last told of is pushed again. Only the key and the store are taken on the
        calling thread; the look is one job of the line, and no second one is put in while it waits there.
        Never raises: `watch` calls it from a timer."""
        try:
            after = float(self.remind_after())
            if after <= 0:
                return
            sender = self.ready()
            store = self._store() if sender is not None else None
            if store is None:
                return
            with self._lock:
                if self._looking:
                    return
                self._looking = True
            if not self._put(self._remind, sender, store, after):
                with self._lock:
                    self._looking = False
        except Exception:  # noqa: BLE001 - a look that could not begin is tried again at the next
            log.exception("studio: 재알림을 줄에 넣지 못했습니다")

    def watch(self, every: float = REMIND_EVERY,
              timer: Callable[[float, Callable[[], None]], Any] | None = None) -> None:
        """Look at the waiting items now and every `every` seconds from then on, until `unwatch`: the server's
        start and stop. A second call while one goes on does nothing. `timer(delay, fn)` starts a timer and
        returns a handle with `cancel()` — a test gives its own. Nothing is kept between two looks but the
        timer: what is due is read from the DB each time, so a restart loses no reminder."""
        start = timer or _timer
        with self._lock:
            if self._watching:
                return
            self._watching = True
            self._round += 1
            mine = self._round

        def look() -> None:
            with self._lock:
                if not self._watching or self._round != mine:
                    return
            self.remind()
            handle = start(every, look)
            with self._lock:
                if self._watching and self._round == mine:
                    self._timer = handle
                    return
            handle.cancel()  # stopped while this look was on its way

        look()

    def unwatch(self) -> None:
        """End `watch`: the timer that waits is cancelled, one that fires all the same does nothing."""
        with self._lock:
            self._watching = False
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    # --- the worker thread ------------------------------------------------------------------------------------
    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                work, *args = job
                work(*args)
            except Exception:  # noqa: BLE001 - one push that failed does not end the thread
                log.exception("studio: 푸시를 보내지 못했습니다")
            finally:
                self._jobs.task_done()

    def _events(self, sender: Sender, store, ids: tuple[int, ...]) -> None:
        """Those stream events, each pushed as `classify` says (§5.9) — most of them are nothing to push."""
        low, high, wanted = min(ids), max(ids), set(ids)
        for event in store.list_stream_events(after_id=low - 1, limit=high - low + 1):
            if event["id"] not in wanted:
                continue
            try:
                told = classify(event, store)
            except Exception:  # noqa: BLE001 - an event that cannot be read does not keep the next from its push
                log.exception("studio: stream event %s 의 푸시를 정하지 못했습니다", event["id"])
                continue
            if told is not None:
                self._deliver(sender, store, *told)

    def _run(self, sender: Sender, store, run_id: str, kind: str, title: str) -> None:
        """An alert of the engine: title = its icon and label, the line = the first line of what the toast shows,
        the link = the task whose run that is — none for a run that is no task's."""
        icon, label = NOTIFY_KINDS[kind]
        rows = store.list_task_runs(run_id=run_id) if run_id else []
        if rows and kind == "run_failed":
            return  # a task run's failure: the item it becomes is pushed, or the run stands in the line again (N-2)
        link = {"type": "task", "id": rows[0]["task_id"]} if rows else None
        self._deliver(sender, store, URGENT, f"{icon} {label}", _line(title), link)

    def _deliver(self, sender: Sender, store, grade: str, title: str, line: str, link: dict | None, *,
                 hold: bool = False) -> int:
        """Send to every device that can be reached; the answer is how many a send was tried for. A device
        inside its quiet hours hears an urgent push as a quiet one — or, with `hold` (a reminder), is passed
        over: it is told when its quiet hours are over."""
        at, tried = self.clock(), 0
        for device in store.list_devices():  # the revoked ones are left out
            token = device.get("fcm_token")
            if not token:
                continue
            as_grade = grade
            if grade == URGENT and quiet_now(device.get("settings"), at):
                if hold:
                    continue
                as_grade = QUIET
            tried += 1
            try:
                if sender.send(token, title, line, link, as_grade) == DEAD:
                    self._clear(store, device["id"], token)
            except Exception:  # noqa: BLE001 - one device must not keep the next from hearing
                log.exception("studio: 기기 %s 에 푸시를 보내지 못했습니다", device["id"])
        return tried

    def _remind(self, sender: Sender, store, after: float) -> None:
        """The look of `remind`: every pending item whose `reminded_at` — `created_at` while it has none — is
        `after` seconds back or more is pushed again, urgent, with the same title and link. `reminded_at` is
        set when a send was tried for one device at least, whatever FCM said: a failure that stays would be
        tried every minute. With every device in its quiet hours nothing is set, so the first look after them
        tells."""
        try:
            at = self.clock()
            for item in store.list_inbox(status="pending"):
                since = _seconds(item["reminded_at"] or item["created_at"])
                if since is None or at - since < after:
                    continue
                label = KIND_LABEL.get(item["kind"], "")
                line = f"{label} · {REMIND_NOTE}" if label else REMIND_NOTE
                link = {"type": "inbox", "id": item["id"]}
                if self._deliver(sender, store, URGENT, _line(item["title"]) or label, line, link, hold=True):
                    self._reminded(store, item["id"], at)
        finally:
            with self._lock:
                self._looking = False

    @staticmethod
    def _reminded(store, inbox_id: str, at: float) -> None:
        """Write `reminded_at` — unless the item was decided meanwhile. No stream event: nothing to draw anew."""
        with store.transaction() as tx:
            row = store.get_inbox_item(inbox_id, tx=tx)
            if row is not None and row["status"] == "pending":
                store.update_inbox_item(inbox_id, reminded_at=_stamp(at), tx=tx)

    @staticmethod
    def _clear(store, device_id: str, token: str) -> None:
        """Forget a dead push token — unless the device was paired again meanwhile and holds another by now.
        The device is not revoked: it still comes in with its own token (§3.14)."""
        with store.transaction() as tx:
            row = store.get_device(device_id, tx=tx)
            if row is None or row["fcm_token"] != token:
                return
            store.update_device(device_id, fcm_token=None, tx=tx)
        log.info("studio: 죽은 푸시 토큰을 비웠습니다 — 기기 %s", device_id)


def attach(engine, inbox_factory: Callable[[], Any], key_path: Callable[[], str | None], *,
           remind_after: float | Callable[[], float] = REMIND_AFTER) -> Pusher:
    """Connect the sender: it is put beside whatever `engine.notifier` already is — the toast keeps working —
    and hears every commit that wrote stream events (§11.3). The reminders are not started here: the server
    calls `watch` at its start and `unwatch` at its stop. A run `api.track_run` holds at the moment of its
    alert is a turn: its failure is left to the notice of its room."""
    from . import api  # at the call: this module is read without the REST layer (tests/conftest.py)

    pusher = Pusher(inbox_factory, key_path, remind_after=remind_after,
                    is_turn=lambda run_id: api.tracked_run(run_id) is not None)
    engine.notifier = Fanout(engine.notifier, pusher)
    api.on_commit(pusher.committed)
    return pusher
