"""The FCM sender (N-1; ARCHITECTURE §5.9 · §3.14 · §11.3): the service account's access token, the message a
push is, a dead token, the retries, and the server that runs without a key. No request leaves the test —
`httpx.post` is a fake — and the key is made here: no key file of this PC is read.

The policy (N-2; §5.9 · §3.6 · §3.13 · §3.14 · §6.4): the grade of each trigger, the quiet hours of a device, a
muted room and the reminders of an item that still waits — on a clock the test moves."""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from relay_agent import notify
from relay_agent.studio import api, inbox, push
from relay_agent.studio.store import StudioStore, now

LOAD_KEY = push.load_key  # the real one: conftest.py lets no test read a key file
TOKEN = "shared-Token-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SEND_URL = "https://fcm.googleapis.com/v1/projects/katae-test/messages:send"
STOP = {"stage": "plan", "stop_reason": "승인 대기"}  # the payload of a `design_approval` (§5.3)
DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
NIGHT = {"quiet_hours": {"start": "22:00", "end": "07:00", "utc_offset_min": 540}}  # a phone in Korea


@pytest.fixture(scope="module")
def account():
    """A service account file as Firebase hands it out, with a key made for this run of the tests."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = private.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    data = {"type": "service_account", "project_id": "katae-test", "private_key": pem,
            "client_email": "push@katae-test.iam.gserviceaccount.com", "token_uri": push.TOKEN_URI}
    return SimpleNamespace(data=data, public=public)


@pytest.fixture
def key_file(tmp_path, account):
    path = tmp_path / "fcm-service-account.json"
    path.write_text(json.dumps(account.data), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def key_files_are_read(monkeypatch):
    monkeypatch.setattr(push, "load_key", LOAD_KEY)


class Http:
    """`httpx.post` of a test: it keeps the calls and answers 200 unless a reply was scripted — for the token
    endpoint in `tokens`, for a device's push in `sends[fcm token]`. A reply is a response, an exception to raise,
    or a function that gives one of them when its turn comes."""

    def __init__(self):
        self.calls, self.tokens, self.sends, self.issued = [], [], {}, 0

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if url == push.TOKEN_URI:
            script = self.tokens
        else:
            script = self.sends.get(kw["json"]["message"]["token"], [])
        reply = script.pop(0) if script else None
        if callable(reply):
            reply = reply()
        if isinstance(reply, Exception):
            raise reply
        if reply is not None:
            return reply
        if url == push.TOKEN_URI:
            self.issued += 1
            return httpx.Response(200, json={"access_token": f"access-{self.issued}", "expires_in": 3600,
                                             "token_type": "Bearer"})
        return httpx.Response(200, json={"name": "projects/katae-test/messages/1"})

    def sent(self):
        """The pushes: every call that was not for the access token."""
        return [(url, kw) for url, kw in self.calls if url != push.TOKEN_URI]

    def messages(self):
        return [kw["json"]["message"] for _, kw in self.sent()]


@pytest.fixture
def http(monkeypatch):
    fake = Http()
    monkeypatch.setattr(push.httpx, "post", fake.post)
    return fake


class Clock:
    def __init__(self):
        self.at, self.slept = 1_700_000_000.0, []

    def __call__(self):
        return self.at

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.at += seconds


def sender_of(account):
    clock = Clock()
    return push.Sender(account.data, clock=clock, sleep=clock.sleep), clock


def fcm_error(status, code=None, name="INVALID_ARGUMENT", headers=None):
    """An error as FCM HTTP v1 answers it."""
    details = [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": code}] if code else []
    body = {"error": {"code": status, "message": "…", "status": name, "details": details}}
    return httpx.Response(status, json=body, headers=headers)


def fcm(token, title, body, link=None, grade="urgent", **more):
    """One push as FCM HTTP v1 is asked for it (§5.9): the grade is in `data`, the priority and the channel."""
    data = {"grade": grade, **({"type": link[0], "id": link[1]} if link else {}), **more}
    drawer = {"channel_id": f"katae_{grade}", **({"tag": "katae_digest"} if grade == "digest" else {})}
    return {"token": token, "notification": {"title": title, "body": body}, "data": data,
            "android": {"priority": "HIGH" if grade == "urgent" else "NORMAL", "notification": drawer}}


def at(wall, offset=0):
    """The moment a clock `offset` minutes ahead of UTC shows `wall`, as seconds of the epoch."""
    return datetime.fromisoformat(wall).replace(tzinfo=timezone(timedelta(minutes=offset))).timestamp()


def seconds(stamp):
    """A time of the store (local, no zone) as seconds of the epoch."""
    return datetime.fromisoformat(stamp).timestamp()


def hours_around(moment, minutes=60):
    """Quiet hours, on a UTC clock, from `minutes` before that moment until `minutes` after it."""
    middle = datetime.fromtimestamp(moment, timezone.utc)
    start, end = (middle + timedelta(minutes=sign * minutes) for sign in (-1, 1))
    return {"quiet_hours": {"start": f"{start:%H:%M}", "end": f"{end:%H:%M}", "utc_offset_min": 0}}


class Timers:
    """`timer(delay, fn)` of a test: the waits are kept and none runs by itself."""

    def __init__(self):
        self.waits, self.cancelled = [], 0

    def __call__(self, delay, fn):
        self.waits.append((delay, fn))
        return SimpleNamespace(cancel=self._cancel)

    def _cancel(self):
        self.cancelled += 1

    def fire(self):
        delay, fn = self.waits.pop(0)
        fn()
        return delay


# --- the key file ---------------------------------------------------------------------------------------------
def test_a_key_file_that_cannot_be_used_is_no_key(tmp_path, account, key_file, caplog):
    assert push.load_key("") is None and push.load_key(None) is None and push.load_key("  ") is None
    assert caplog.records == []  # no path is the default: nothing to warn of
    bad = tmp_path / "bad.json"
    cases = {
        "없는 파일": None, "JSON 이 아니다": "{", "객체가 아니다": "[1]",
        "필드가 빠졌다": json.dumps({k: v for k, v in account.data.items() if k != "client_email"}),
        "키가 아니다": json.dumps({**account.data, "private_key": "-----BEGIN PRIVATE KEY-----\nsecret-nope\n"
                                                              "-----END PRIVATE KEY-----\n"}),
    }
    with caplog.at_level(logging.WARNING, logger=push.log.name):
        for why, text in cases.items():
            bad.unlink(missing_ok=True)
            if text is not None:
                bad.write_text(text, encoding="utf-8")
            caplog.clear()
            assert push.load_key(str(bad)) is None, why
            assert len(caplog.records) == 1, why  # said once, and never with the key's text
            assert "secret-nope" not in caplog.text and "PRIVATE KEY" not in caplog.text, why
        caplog.clear()
        assert push.load_key(str(tmp_path)) is None and len(caplog.records) == 1  # a folder

    key = push.load_key(str(key_file))
    assert key == {name: account.data[name] for name in ("project_id", "client_email", "private_key", "token_uri")}
    assert push.key_file("") is None and push.key_file(None) is None and push.key_file(" ") is None
    assert push.key_file(f" {key_file} ") == key_file  # where the guard is told the file is: the same reading
    assert push.key_file("keys/fcm.json") == push.ROOT / "keys" / "fcm.json"
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps({k: v for k, v in account.data.items() if k != "token_uri"}), encoding="utf-8-sig")
    assert push.load_key(bare)["token_uri"] == "https://oauth2.googleapis.com/token"


# --- the access token -----------------------------------------------------------------------------------------
def test_the_access_token_is_asked_with_a_signed_jwt_and_kept(account, http):
    sender, clock = sender_of(account)
    issued_at = int(clock.at)
    assert sender.access_token() == "access-1"
    (url, kw), = http.calls
    assert url == account.data["token_uri"] and kw["timeout"] == 10
    assert kw["data"]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    claims = jwt.decode(kw["data"]["assertion"], account.public, algorithms=["RS256"],
                        audience=account.data["token_uri"], options={"verify_exp": False})
    assert claims == {"iss": account.data["client_email"], "aud": account.data["token_uri"],
                      "scope": "https://www.googleapis.com/auth/firebase.messaging",
                      "iat": issued_at, "exp": issued_at + 3600}

    clock.at += 3600 - 61
    assert sender.access_token() == "access-1" and len(http.calls) == 1  # kept until a minute before its end
    clock.at += 2
    assert sender.access_token() == "access-2" and len(http.calls) == 2


def test_asking_for_the_access_token_is_tried_again_too(account, http):
    sender, clock = sender_of(account)
    http.tokens += [httpx.Response(503), httpx.ConnectError("끊겼다")]
    assert sender.send("fcm-1", "제목", "한 줄") == "sent"
    assert clock.slept == [1.0, 3.0] and len(http.sent()) == 1

    refused, clock = sender_of(account)
    http.tokens.append(httpx.Response(400, json={"error": "invalid_grant"}))
    assert refused.send("fcm-1", "제목", "한 줄") == "failed"  # not a passing failure: asked once
    assert clock.slept == [] and len(http.sent()) == 1          # and no push went out without a token


def test_an_access_token_that_could_not_be_had_is_not_asked_for_again_for_a_minute(account, http):
    sender, clock = sender_of(account)
    http.tokens.append(httpx.Response(400, json={"error": "invalid_grant"}))
    assert sender.send("fcm-1", "제목", "한 줄") == "failed" and len(http.calls) == 1
    assert sender.send("fcm-2", "제목", "한 줄") == "failed" and len(http.calls) == 1  # no token asked, no push
    clock.at += 59
    assert sender.send("fcm-1", "제목", "한 줄") == "failed" and len(http.calls) == 1
    clock.at += 2
    assert sender.send("fcm-1", "제목", "한 줄") == "sent" and len(http.calls) == 3 and clock.slept == []

    down, clock = sender_of(account)  # a failure that may pass: the three tries once, then the same minute
    http.calls.clear()
    http.tokens += [httpx.Response(503), httpx.ConnectError("끊겼다"), httpx.Response(503)]
    assert [down.send(token, "제목", "한 줄") for token in ("fcm-1", "fcm-2")] == ["failed", "failed"]
    assert len(http.calls) == 3 and http.sent() == [] and clock.slept == [1.0, 3.0]
    clock.at += 59  # counted from the last try, not the first
    assert down.access_token() is None and len(http.calls) == 3
    clock.at -= 3600  # this PC's clock was set back: the minute is not stretched into an hour
    assert down.access_token() == "access-2" and len(http.calls) == 4


# --- one push -------------------------------------------------------------------------------------------------
def test_a_push_carries_a_title_one_line_the_deep_link_and_its_grade(account, http):
    sender, _ = sender_of(account)
    assert sender.send("fcm-1", "로그인 화면 설계", "설계 승인", {"type": "inbox", "id": "inb_1"}) == "sent"
    _, (url, kw) = http.calls
    assert url == SEND_URL and kw["headers"] == {"Authorization": "Bearer access-1"} and kw["timeout"] == 10
    assert kw["json"] == {"message": {"token": "fcm-1",
                                      "notification": {"title": "로그인 화면 설계", "body": "설계 승인"},
                                      "data": {"grade": "urgent", "type": "inbox", "id": "inb_1"},
                                      "android": {"priority": "HIGH",
                                                  "notification": {"channel_id": "katae_urgent"}}}}

    assert sender.send("fcm-1", "💥 실패", "로그인 고치기") == "sent"  # nothing to open: the grade alone
    assert http.messages()[-1] == fcm("fcm-1", "💥 실패", "로그인 고치기")
    assert http.messages()[-1]["data"] == {"grade": "urgent"}
    assert len(http.calls) == 3  # the access token was asked for once

    room = {"type": "room", "id": "room_1"}  # N-2: the grade is the priority and the channel too
    assert sender.send("fcm-1", "민수", "다 했습니다", room, "quiet") == "sent"
    assert http.messages()[-1]["data"] == {"grade": "quiet", "type": "room", "id": "room_1"}
    assert http.messages()[-1]["android"] == {"priority": "NORMAL", "notification": {"channel_id": "katae_quiet"}}
    assert sender.send("fcm-1", "일일 보고", "오늘 한 일", room, grade="digest") == "sent"
    assert http.messages()[-1]["android"] == {
        "priority": "NORMAL", "notification": {"channel_id": "katae_digest", "tag": "katae_digest"}}
    assert sender.send("fcm-1", "제목", "한 줄", grade="loud") == "sent"  # a grade nobody knows: not lost
    assert http.messages()[-1] == fcm("fcm-1", "제목", "한 줄")
    assert sender.send("fcm-1", "민수", "다 했습니다", {**room, "action": "reply", "room_id": "room_1"}, "quiet") == "sent"
    assert http.messages()[-1]["data"] == {"grade": "quiet", "type": "room", "id": "room_1", "action": "reply",
                                           "room_id": "room_1"}  # N-3: every key of the link, as a string
    assert sender.send("fcm-1", "설계", "승인", {"type": "inbox", "id": 7, "action": "approve"}) == "sent"
    assert http.messages()[-1]["data"] == {"grade": "urgent", "type": "inbox", "id": "7", "action": "approve"}


def test_a_dead_token_is_told_apart_and_not_tried_again(account, http):
    sender, clock = sender_of(account)
    http.sends = {"gone": [fcm_error(404, "UNREGISTERED", "NOT_FOUND")],
                  "named": [fcm_error(400, "UNREGISTERED")],
                  "bare": [httpx.Response(404, text="not found")],  # a 404 that names no errorCode may be the
                  "unnamed": [fcm_error(404, None, "NOT_FOUND")],   # project's or the address's: the token stays
                  "bad": [fcm_error(400, "INVALID_ARGUMENT")],      # may be this message's fault: the token stays
                  "denied": [fcm_error(403, "SENDER_ID_MISMATCH", "PERMISSION_DENIED")]}
    assert [sender.send(token, "제목", "한 줄") for token in http.sends] == [
        "dead", "dead", "failed", "failed", "failed", "failed"]
    assert len(http.sent()) == 6 and clock.slept == []


def test_a_passing_failure_is_tried_three_times(account, http):
    sender, clock = sender_of(account)
    http.sends["a"] = [fcm_error(503, name="UNAVAILABLE"), httpx.ConnectError("끊겼다")]
    assert sender.send("a", "제목", "한 줄") == "sent"
    assert clock.slept == [1.0, 3.0] and len(http.sent()) == 3

    clock.slept.clear()
    http.sends["b"] = [fcm_error(429, "QUOTA_EXCEEDED", "RESOURCE_EXHAUSTED", headers={"Retry-After": "7"}),
                       fcm_error(500, name="INTERNAL", headers={"Retry-After": "999"})]
    assert sender.send("b", "제목", "한 줄") == "sent" and clock.slept == [7.0, 30.0]  # as told, 30 s at most

    clock.slept.clear()
    http.sends["c"] = [fcm_error(500, name="INTERNAL")] * 3 + [httpx.Response(200, json={})]
    assert sender.send("c", "제목", "한 줄") == "failed"
    assert clock.slept == [1.0, 3.0] and len(http.sends["c"]) == 1  # the fourth answer was never asked for


def test_a_refused_access_token_is_asked_anew(account, http):
    sender, clock = sender_of(account)
    http.sends["a"] = [fcm_error(401, name="UNAUTHENTICATED")]
    assert sender.send("a", "제목", "한 줄") == "sent" and clock.slept == [1.0]
    assert [kw["headers"]["Authorization"] for _, kw in http.sent()] == ["Bearer access-1", "Bearer access-2"]


# --- what is pushed, and to whom ------------------------------------------------------------------------------
class Studio:
    """A studio store of its own and a Pusher that hears its commits, as `api.on_commit` has the server's hear."""

    def __init__(self, path, key_path):
        self.store = StudioStore(path)
        self.key_path = str(key_path)
        self.clock, self.remind_after = Clock(), 7200.0  # what the quiet hours and the reminders are read on
        self.pusher = push.Pusher(lambda: SimpleNamespace(store=self.store), lambda: self.key_path,
                                  clock=self.clock, remind_after=lambda: self.remind_after)
        self.seen = []  # the ids of every commit that wrote stream events
        self.store.commit_listeners += [self.seen.append, self.pusher.committed]

    def employee(self, name="민수"):
        """An employee and the id of the 1:1 room with it."""
        row = self.store.create_employee(name, "program")
        return row, self.store.create_room("dm", title=name, employee_id=row["id"])["id"]

    def say(self, room_id, body, author_kind="employee", author_id=None, meta=None):
        """A message in a room with its `message.created`, and the pushes of it sent."""
        with self.store.transaction() as tx:
            message = self.store.create_message(room_id, author_kind, author_id=author_id, body=body,
                                                meta=meta or {}, tx=tx)
            self.store.emit(tx, "message.created", {"message": message}, room_id=room_id)
        assert self.pusher.drain()
        return message

    def emit(self, type, data):
        """A stream event as the code that will write it does (§7.4), and the pushes of it sent."""
        with self.store.transaction() as tx:
            self.store.emit(tx, type, data)
        assert self.pusher.drain()

    def look(self, moment):
        """The look at the waiting items, at that moment of the clock."""
        self.clock.at = moment
        self.pusher.remind()
        assert self.pusher.drain()

    def reminded(self, item):
        return self.store.get_inbox_item(item["id"])["reminded_at"]

    def device(self, n, token):
        return self.store.create_device(f"폰 {n}", f"hash-{n}", platform="android", fcm_token=token)

    def file(self, title="로그인 화면 설계", kind="design_approval", run_id="run_a"):
        """An inbox item, made the way `Inbox.file_run` makes it, and the pushes of it sent."""
        with self.store.transaction() as tx:
            item = self.store.create_inbox_item(kind, title, "system", run_id=run_id, payload=dict(STOP), tx=tx)
            self.store.emit(tx, "inbox.created", {"inbox": item}, project_id=item["project_id"])
        assert self.pusher.drain()
        return item

    def token(self, device):
        return self.store.get_device(device["id"])["fcm_token"]


@pytest.fixture
def studio(tmp_path, key_file, http):
    world = Studio(tmp_path / "studio.sqlite", key_file)
    yield world
    assert world.pusher.drain()
    world.pusher.stop()
    world.store.close()


def test_a_new_inbox_item_is_pushed_to_every_device_with_a_token(studio, http):
    studio.device(1, "fcm-1"), studio.device(2, "fcm-2"), studio.device(3, None)
    revoked = studio.device(4, "fcm-4")
    studio.store.update_device(revoked["id"], revoked_at=now())

    item = studio.file("로그인 화면 설계")
    assert sorted(http.messages(), key=lambda m: m["token"]) == [
        fcm(token, "로그인 화면 설계", "설계 승인", ("inbox", item["id"]), action="approve") for token in ("fcm-1", "fcm-2")]
    assert len(http.calls) == 3  # one access token for both
    assert "승인 대기" not in json.dumps(http.messages(), ensure_ascii=False)  # nothing of the payload

    with studio.store.transaction() as tx:  # the item is closed: an event that is no `inbox.created`
        studio.store.update_inbox_item(item["id"], status="cancelled", closed_at=now(), tx=tx)
        studio.store.emit(tx, "inbox.cancelled", {"inbox": studio.store.get_inbox_item(item["id"], tx=tx)})
    assert studio.pusher.drain() and len(http.sent()) == 2
    studio.pusher.committed(studio.seen[0])  # told of its making once more, late: it waits no longer
    assert studio.pusher.drain() and len(http.sent()) == 2

    budget = studio.file("예산을 넘었다", kind="budget", run_id="run_b")
    assert http.messages()[-1]["notification"] == {"title": "예산을 넘었다", "body": "예산"}
    assert http.messages()[-1]["data"] == {"grade": "urgent", "type": "inbox", "id": budget["id"], "action": "approve"}


def test_a_dead_token_is_cleared_and_the_others_keep_theirs(studio, http):
    one, two, three = studio.device(1, "fcm-1"), studio.device(2, "fcm-2"), studio.device(3, "fcm-3")

    def paired_again():  # the phone got a new token while the push to its old one was on its way
        studio.store.update_device(three["id"], fcm_token="fcm-3b")
        return fcm_error(404, "UNREGISTERED", "NOT_FOUND")

    http.sends = {"fcm-1": [fcm_error(400, "INVALID_ARGUMENT")],
                  "fcm-2": [fcm_error(404, "UNREGISTERED", "NOT_FOUND")], "fcm-3": [paired_again]}
    studio.file()
    assert studio.token(two) is None                                    # dead: cleared
    assert studio.store.get_device(two["id"])["revoked_at"] is None     # the device itself still comes in
    assert studio.token(one) == "fcm-1" and studio.token(three) == "fcm-3b"

    studio.file("둘째", run_id="run_b")
    assert sorted(m["token"] for m in http.messages()[3:]) == ["fcm-1", "fcm-3b"]


def test_the_engines_alerts_that_are_no_stop_are_pushed(studio, http, monkeypatch):
    studio.device(1, "fcm-1")
    monkeypatch.setattr(studio.store, "list_task_runs",  # `run_t` is a task's run, `run_x` nobody's
                        lambda *, run_id=None, **kw: [{"task_id": "task_1"}] if run_id == "run_t" else [])
    for kind in notify.NOTIFY_KINDS:
        studio.pusher("run_t" if kind == "campaign_done" else "run_x", kind, "로그인 고치기\n둘째 줄", {"error": "boom"})
    studio.pusher("run_t", "run_failed", "로그인 고치기", {"error": "boom"})  # a task's run that failed
    assert studio.pusher.drain()
    assert http.messages() == [
        fcm("fcm-1", "💥 실패", "로그인 고치기"),
        fcm("fcm-1", "🏁 캠페인 완료", "로그인 고치기", ("task", "task_1")),
        fcm("fcm-1", "⏸ 캠페인 일시 정지 — 확인 필요", "로그인 고치기"),
    ]  # the stops go with their inbox item and so does a task run's failure (N-2: once for one thing), `run_done`
    # is told by the review item or the employee's words; the error text is not sent


def test_a_turn_that_failed_is_pushed_once_as_the_notice_of_its_room(studio, http, caplog):
    studio.device(1, "fcm-1")
    _, dm = studio.employee()
    studio.pusher.is_turn = lambda run_id: run_id == "run_c"  # `run_c` is a chat turn, tracked while it runs
    studio.pusher("run_c", "run_failed", "안녕?", {"error": "boom"})  # the engine's alert, from inside the turn
    assert studio.pusher.drain() and http.sent() == []
    studio.say(dm, "민수 이(가) 답하지 못했다 — boom", "system", meta={"event": "run_error", "run_id": "run_c"})
    assert http.messages() == [fcm("fcm-1", "실행하지 못함", "민수 이(가) 답하지 못했다 — boom", ("room", dm))]

    studio.pusher("run_c", "campaign_paused", "안녕?", {})     # only its failure is the notice's to tell
    studio.pusher("run_x", "run_failed", "로그인 고치기", {})  # a run that is no turn's is told as before
    assert studio.pusher.drain()
    assert [m["notification"]["title"] for m in http.messages()[1:]] == ["⏸ 캠페인 일시 정지 — 확인 필요", "💥 실패"]

    def broken(run_id):
        raise RuntimeError("no ledger")

    studio.pusher.is_turn = broken  # nothing raises into the run that tells
    with caplog.at_level(logging.ERROR, logger=push.log.name):
        studio.pusher("run_x", "run_failed", "로그인 고치기", {})
    assert studio.pusher.drain() and "run_x" in caplog.text


@pytest.mark.parametrize("key", ["", "missing.json", "broken.json"])
def test_without_a_usable_key_nothing_is_sent(tmp_path, key_file, http, monkeypatch, key):
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    loads = []
    monkeypatch.setattr(push, "load_key", lambda path: loads.append(path) or LOAD_KEY(path))
    studio = Studio(tmp_path / "studio.sqlite", tmp_path / key if key else "")
    try:
        device = studio.device(1, "fcm-1")
        studio.file()
        studio.file("둘째", run_id="run_b")
        studio.pusher("run_x", "run_failed", "로그인 고치기", {})
        assert studio.pusher.drain() and http.calls == [] and studio.token(device) == "fcm-1"
        assert studio.pusher._worker is None      # not even a thread
        assert len(loads) == (1 if key else 0)    # the file is looked at once, not at every commit

        studio.key_path = str(key_file)           # the path changes: it is read at the next push
        item = studio.file("셋째", run_id="run_c")
        assert [m["data"]["id"] for m in http.messages()] == [item["id"]]
    finally:
        studio.pusher.stop()
        studio.store.close()


def test_attach_puts_the_sender_beside_the_toast(studio, http, monkeypatch):
    heard = []
    engine = SimpleNamespace(notifier=lambda run_id, kind, title, detail: heard.append((run_id, kind)))
    monkeypatch.setattr(api, "_commit_listeners", [])
    pusher = push.attach(engine, studio.pusher.inbox_factory, lambda: studio.key_path)
    try:
        assert isinstance(engine.notifier, notify.Fanout) and engine.notifier.receivers[1] is pusher
        assert api._commit_listeners == [pusher.committed]
        studio.device(1, "fcm-1")
        engine.notifier("run_x", "run_failed", "로그인 고치기", {})
        engine.notifier("run_x", "awaiting_approval", "로그인 고치기", {})
        assert pusher.drain() and heard == [("run_x", "run_failed"), ("run_x", "awaiting_approval")]
        assert [m["notification"]["title"] for m in http.messages()] == ["💥 실패"]
    finally:
        pusher.stop()


def test_attach_leaves_the_failure_of_a_tracked_turn_to_the_notice_of_its_room(studio, http, monkeypatch):
    engine = SimpleNamespace(notifier=None)
    monkeypatch.setattr(api, "_commit_listeners", [])
    pusher = push.attach(engine, studio.pusher.inbox_factory, lambda: studio.key_path)
    api.track_run("run_c", mode="chat", employee_id="emp_1", room_id="room_1")  # as chat.py does around `advance`
    try:
        assert api.tracked_run("run_c")["mode"] == "chat" and api.tracked_run("run_x") is None
        studio.device(1, "fcm-1")
        engine.notifier("run_c", "run_failed", "안녕?", {})
        assert pusher.drain() and http.sent() == []
        api.untrack_run("run_c")  # nothing tracks it any more — so it is after the server came up again
        assert api.tracked_run("run_c") is None
        engine.notifier("run_c", "run_failed", "안녕?", {})
        assert pusher.drain() and [m["notification"]["title"] for m in http.messages()] == ["💥 실패"]
    finally:
        api.untrack_run("run_c")
        pusher.stop()


# --- the policy (N-2): grades · quiet hours · a muted room · reminders ----------------------------------------
def test_each_trigger_goes_out_with_the_grade_of_the_table(studio, http, monkeypatch):
    studio.device(1, "fcm-1")
    minsu, dm = studio.employee()
    task = {"id": "task_9", "title": "로그인 고치기"}
    note = {"id": 7, "reason": "절반 끝났다\n나머지는 내일"}  # the task_events row of a `progress` note
    monkeypatch.setattr(studio.store, "list_task_events",
                        lambda task_id, **kw: [note] if (task_id, kw.get("after_id")) == ("task_9", 6) else [])

    studio.say(dm, "로그인 화면을 고쳤습니다\n둘째 줄", author_id=minsu["id"])
    studio.say(dm, "수고했어요", "owner")                                              # the owner's own words
    studio.say("room_system", "담당이 없는 작업이 있다", "system", meta={"event": "needs_assignment", "task_id": "task_9"})
    studio.say(dm, "턴을 넣지 못했다", "system", meta={"event": "run_error"})
    studio.say(dm, "결재를 올렸다", "system", meta={"event": "inbox.created", "inbox_id": "inb_1"})  # the item tells
    studio.say("room_system", "오늘 한 일\n작업 3개 완료", author_id=minsu["id"], meta={"event": "daily_report"})
    studio.emit("task.transitioned", {"task": task, "rule": "T3", "from": "backlog", "to": "working"})
    studio.emit("task.transitioned", {"task": task, "rule": "T6", "from": "working", "to": "review"})  # its item
    studio.emit("task.updated", {"task": task, "change": "progress", "event_id": 7})
    studio.emit("task.updated", {"task": task, "change": "progress", "event_id": 9})   # a note that is not there
    studio.emit("task.updated", {"task": task, "change": "edited", "event_id": 8})
    studio.emit("job.finished", {"job_id": "job_1", "kind": "project_create", "status": "done",
                                 "project_id": "prj_1", "error": None})
    studio.emit("job.finished", {"job_id": "job_2", "kind": "project_create", "status": "failed",
                                 "project_id": None, "error": "빌드 실패\n로그 300줄"})
    studio.emit("job.finished", {"job_id": "job_3", "kind": "project_create", "status": "cleaned",
                                 "project_id": None, "error": None})                   # the owner had it cleaned away
    assert http.messages() == [
        fcm("fcm-1", "민수", "로그인 화면을 고쳤습니다", ("room", dm), "quiet", action="reply", room_id=dm),
        fcm("fcm-1", "배치 필요", "담당이 없는 작업이 있다", ("task", "task_9")),
        fcm("fcm-1", "실행하지 못함", "턴을 넣지 못했다", ("room", dm)),
        fcm("fcm-1", "일일 보고", "오늘 한 일", ("room", "room_system"), "digest"),
        fcm("fcm-1", "로그인 고치기", "작업 시작", ("task", "task_9"), "quiet"),
        fcm("fcm-1", "로그인 고치기", "절반 끝났다", ("task", "task_9"), "quiet"),
        fcm("fcm-1", "로그인 고치기", "진행 상황", ("task", "task_9"), "quiet"),
        fcm("fcm-1", "프로젝트 생성 완료", "", ("project", "prj_1")),
        fcm("fcm-1", "프로젝트 생성 실패", "빌드 실패", ("job", "job_2")),
    ]


def test_a_job_is_pushed_as_it_ends_and_not_when_it_is_cleaned(studio, http, tmp_path, monkeypatch):
    """The `job.finished` that jobs.py itself writes (M-8), as far as the line that is sent: the steps are
    stand-ins, the job's file, its events and the store's commit are the real ones."""
    from relay_agent.repos import RepoRegistry
    from relay_agent.studio import harness, jobs
    from relay_agent.studio.creator import Spec, StepResult

    studio.device(1, "fcm-1")
    ends = {"build": "빌드 실패\n로그 300줄"}

    def step(self, name, store, job, admitted, hear):
        if name == "register":
            row = store.create_project(admitted.spec.name, str(admitted.spec.root), admitted.spec.name)
            return harness.RegisterResult(name, repo=row["repo"], project=row)
        return StepResult(name, error=ends.get(name, ""))

    monkeypatch.setattr(jobs.Jobs, "_step", step)
    runner = jobs.Jobs(tmp_path / "studio", local_file=tmp_path / "relay.config.local.yaml",
                       reload=lambda: RepoRegistry({}), harness_root=lambda: "")
    request = {"name": "Sample", "engine_version": "5.7", "parent_dir": str(tmp_path / "Projects"), "plugins": [],
               "harness": False}
    admitted = jobs.Admitted(Spec("Sample", "5.7", tmp_path / "UE_5.7", tmp_path / "Projects"), [], False, request)

    def ended(job):
        assert runner.wait(job["id"], 30.0) and studio.pusher.drain()
        return runner.get(job["id"])

    failed = ended(runner.create(studio.store, admitted))
    assert (failed["status"], failed["error"]) == ("failed", "build: 빌드 실패\n로그 300줄")
    cleaned = runner.cleanup(studio.store, failed["id"])
    assert cleaned["status"] == "cleaned" and studio.pusher.drain()
    ends.clear()
    done = ended(runner.create(studio.store, admitted))
    assert done["status"] == "done" and studio.store.get_project(done["project_id"])["name"] == "Sample"

    told = [(row["data"]["job_id"], row["data"]["status"]) for row in studio.store.list_stream_events(limit=1000)
            if row["type"] == "job.finished"]
    assert told == [(failed["id"], "failed"), (failed["id"], "cleaned"), (done["id"], "done")]
    assert http.messages() == [
        fcm("fcm-1", "프로젝트 생성 실패", "build: 빌드 실패", ("job", failed["id"])),
        fcm("fcm-1", "프로젝트 생성 완료", "", ("project", done["project_id"])),
    ]  # the cleanup is not told of: the owner asked for it


def test_a_muted_room_keeps_its_employees_words_and_nothing_else(studio, http):
    studio.device(1, "fcm-1")
    minsu, dm = studio.employee()
    studio.store.update_room(dm, muted=True)
    studio.say(dm, "조용한 글", author_id=minsu["id"])
    assert http.sent() == []
    studio.say(dm, "턴을 넣지 못했다", "system", meta={"event": "run_error"})  # a notice is not what is muted,
    studio.file()                                                             # and an inbox item never is
    assert [(m["data"]["type"], m["data"]["grade"]) for m in http.messages()] == [("room", "urgent"),
                                                                                 ("inbox", "urgent")]
    studio.store.update_room(dm, muted=False)
    studio.say(dm, "다시 들리는 글", author_id=minsu["id"])
    assert http.messages()[-1] == fcm("fcm-1", "민수", "다시 들리는 글", ("room", dm), "quiet", action="reply", room_id=dm)


def test_quiet_hours_are_read_on_the_devices_own_clock():
    def quiet(settings, offset, *walls):
        return [push.quiet_now(settings, at(f"2026-10-12T{wall}", offset)) for wall in walls]

    assert quiet(NIGHT, 540, "21:59", "22:00", "23:59", "00:00", "06:59", "07:00", "12:00") == [
        False, True, True, True, True, False, False]  # from `start` until just before `end`, over midnight
    lunch = {"quiet_hours": {"start": "12:00", "end": "13:30", "utc_offset_min": -300}}
    assert quiet(lunch, -300, "11:59", "12:00", "13:29", "13:30") == [False, True, True, False]
    assert quiet(lunch, 540, "12:30") == [False]  # the same wall clock in Korea is another moment
    local = {"quiet_hours": {"start": "22:00", "end": "07:00"}}  # no offset: this PC's clock
    assert push.quiet_now(local, datetime(2026, 10, 12, 23, 0).timestamp())
    assert not push.quiet_now(local, datetime(2026, 10, 12, 12, 0).timestamp())
    for none in (None, {}, [], {"quiet_hours": None}, {"quiet_hours": {"start": "22:00"}},
                 {"quiet_hours": {"start": "22:00", "end": "22:00", "utc_offset_min": 0}},
                 {"quiet_hours": {"start": "25:00", "end": "07:00", "utc_offset_min": 0}},
                 {"quiet_hours": {"start": 22, "end": 7, "utc_offset_min": 0}},
                 {"quiet_hours": {"start": "22:00", "end": "07:00", "utc_offset_min": 99999}}):
        assert push.quiet_now(none, at("2026-10-12T23:00")) is False, none  # no quiet hours, and nothing raised


def test_inside_its_quiet_hours_a_device_hears_an_urgent_push_quietly(studio, http):
    asleep, _ = studio.device(1, "fcm-1"), studio.device(2, "fcm-2")
    studio.store.update_device(asleep["id"], settings=NIGHT)
    studio.clock.at = at("2026-10-12T23:30", 540)
    item = studio.file()
    by = {m["token"]: m for m in http.messages()}
    assert by["fcm-1"] == fcm("fcm-1", "로그인 화면 설계", "설계 승인", ("inbox", item["id"]), "quiet", action="approve")  # lowered,
    assert by["fcm-2"] == fcm("fcm-2", "로그인 화면 설계", "설계 승인", ("inbox", item["id"]), action="approve")           # not dropped

    studio.clock.at = at("2026-10-13T07:00", 540)  # the night is over
    studio.file("둘째", run_id="run_b")
    assert [m["data"]["grade"] for m in http.messages()[2:]] == ["urgent", "urgent"]


def test_an_item_that_still_waits_is_told_of_again_after_two_hours(studio, http):
    studio.device(1, "fcm-1")
    item = studio.file()
    made = seconds(item["created_at"])

    studio.look(made + 7199)
    assert studio.reminded(item) is None and len(http.sent()) == 1
    studio.look(made + 7200)
    assert http.messages()[-1] == fcm("fcm-1", "로그인 화면 설계", "설계 승인 · 다시 알림", ("inbox", item["id"]), action="approve")
    first = studio.reminded(item)
    assert first == datetime.fromtimestamp(made + 7200).isoformat(timespec="seconds") and len(http.sent()) == 2
    assert len(studio.seen) == 1  # the stamp is no stream event: nothing for the app to draw anew

    studio.look(made + 7200 + 7199)  # counted from the reminder now
    assert studio.reminded(item) == first and len(http.sent()) == 2
    http.sends["fcm-1"] = [fcm_error(400, "INVALID_ARGUMENT")]  # one that could not be sent is stamped all the same
    studio.look(made + 7200 + 7200)
    second = studio.reminded(item)
    assert second != first and len(http.sent()) == 3

    studio.remind_after = 0  # read at every look: 0 = no reminder
    studio.look(made + 900_000)
    assert studio.reminded(item) == second and len(http.sent()) == 3
    studio.remind_after = 600
    with studio.store.transaction() as tx:  # closed: nothing to ask any more
        studio.store.update_inbox_item(item["id"], status="cancelled", closed_at=now(), tx=tx)
    studio.look(made + 900_000)
    assert studio.reminded(item) == second and len(http.sent()) == 3


def test_a_reminder_passes_over_a_device_in_its_quiet_hours(studio, http):
    asleep, awake = studio.device(1, "fcm-1"), studio.device(2, "fcm-2")
    item = studio.file()
    due = seconds(item["created_at"]) + 7200
    studio.store.update_device(asleep["id"], settings=hours_around(due))
    studio.look(due)
    assert http.messages()[2:] == [fcm("fcm-2", "로그인 화면 설계", "설계 승인 · 다시 알림", ("inbox", item["id"]), action="approve")]
    first = studio.reminded(item)
    assert first is not None  # one device heard it: the two hours begin anew

    again = due + 7200
    for device in (asleep, awake):  # this time both sleep
        studio.store.update_device(device["id"], settings=hours_around(again))
    studio.look(again)
    studio.look(again + 59 * 60)
    assert studio.reminded(item) == first and len(http.sent()) == 3  # nobody was told: nothing is stamped
    studio.look(again + 60 * 60)  # the first look after the quiet hours
    late = http.messages()[3:]
    assert sorted(m["token"] for m in late) == ["fcm-1", "fcm-2"] and {m["data"]["grade"] for m in late} == {"urgent"}
    assert studio.reminded(item) not in (None, first)


def test_a_device_a_reminder_passed_over_waits_for_the_next_one(studio, http):
    asleep, _ = studio.device(1, "fcm-1"), studio.device(2, "fcm-2")
    item = studio.file()
    due = seconds(item["created_at"]) + 7200
    studio.store.update_device(asleep["id"], settings=hours_around(due))
    studio.look(due)
    assert [m["token"] for m in http.messages()[2:]] == ["fcm-2"]
    first = studio.reminded(item)
    assert first == datetime.fromtimestamp(due).isoformat(timespec="seconds")  # one device heard it: stamped

    studio.look(due + 60 * 60 + 60)  # its quiet hours are over: the stamp stands, so nothing is told on the spot
    studio.look(due + 7199)
    assert studio.reminded(item) == first and len(http.sent()) == 3
    studio.look(due + 7200)  # the next reminder: both, no time is kept for one device
    late = http.messages()[3:]
    assert sorted(m["token"] for m in late) == ["fcm-1", "fcm-2"] and {m["data"]["grade"] for m in late} == {"urgent"}
    assert studio.reminded(item) == datetime.fromtimestamp(due + 7200).isoformat(timespec="seconds")


def test_an_item_made_inside_the_quiet_hours_is_told_of_again_two_hours_after_it_was_made(studio, http):
    asleep = studio.device(1, "fcm-1")
    moment = time.time()  # `created_at` is read on the store's own clock: the test's stands where that one does
    studio.store.update_device(asleep["id"], settings=hours_around(moment))
    studio.clock.at = moment
    item = studio.file()
    assert http.messages() == [fcm("fcm-1", "로그인 화면 설계", "설계 승인", ("inbox", item["id"]), "quiet", action="approve")]
    made = seconds(item["created_at"])

    studio.look(made + 60 * 60 + 60)  # the quiet hours are over: its making was told, quietly — their end is not
    studio.look(made + 7199)
    assert studio.reminded(item) is None and len(http.sent()) == 1
    studio.look(made + 7200)  # counted from its making, not from the end of the quiet hours
    assert http.messages()[1:] == [fcm("fcm-1", "로그인 화면 설계", "설계 승인 · 다시 알림", ("inbox", item["id"]), action="approve")]
    assert studio.reminded(item) == datetime.fromtimestamp(made + 7200).isoformat(timespec="seconds")


def test_the_reminders_go_on_from_the_database_after_a_restart(studio, http):
    studio.device(1, "fcm-1")
    item = studio.file()
    made = seconds(item["created_at"])
    studio.look(made + 7200)
    assert len(http.sent()) == 2

    # the server starts again: a sender that remembers nothing, and the clock of its start
    again = push.Pusher(studio.pusher.inbox_factory, lambda: studio.key_path, clock=studio.clock, remind_after=7200)
    timers = Timers()
    try:
        studio.clock.at = made + 7200 + 3600
        again.watch(timer=timers)  # the look at the start: an hour after the last reminder is too early
        assert again.drain() and len(http.sent()) == 2
        again.watch(timer=timers)  # a second start is none
        assert [delay for delay, _ in timers.waits] == [60.0]

        studio.clock.at = made + 7200 + 7200
        assert timers.fire() == 60.0 and again.drain()  # the minute's look: two hours after the reminder
        assert http.messages()[-1] == fcm("fcm-1", "로그인 화면 설계", "설계 승인 · 다시 알림", ("inbox", item["id"]), action="approve")
        assert len(http.sent()) == 3 and len(timers.waits) == 1  # and the next look is set

        again.unwatch()
        assert timers.cancelled == 1
        studio.clock.at += 7200
        timers.fire()  # a timer that fires all the same: no look, and no next one
        assert again.drain() and len(http.sent()) == 3 and timers.waits == []
    finally:
        again.unwatch()
        again.stop()


def test_the_document_holds_the_policy_as_settled():
    text = DOC.read_text(encoding="utf-8")
    policy = text[text.index("### 5.9 알림"):text.index("### 5.10")]
    for left in ("잠정, N-2", "확인 필요 (N-2)", "N-2 가 한다", "N-2 가 정리한다", "둘 다 푸시가 될 수 있다"):
        assert left not in policy, left
    for word in ("`data.grade`", "`katae_urgent`", "`quiet_hours", "`Config.studio_remind_after`", "`reminded_at`",
                 "`push.classify`", "`api.tracked_run`", "`cleaned`", "다음 간격 뒤의 재알림"):
        assert word in policy, word
    devices = text[text.index("### 3.14 devices"):text.index("### 3.15")]
    assert "잠정, N-2" not in devices and "utc_offset_min" in devices


# --- the server -----------------------------------------------------------------------------------------------
def receivers(notifier):
    if isinstance(notifier, notify.Fanout):
        return [receiver for part in notifier.receivers for receiver in receivers(part)]
    return [notifier]


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The server app on a studio DB of its own, with no key file named."""
    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    monkeypatch.setattr(server.cfg, "studio_fcm_key", "")
    monkeypatch.setattr(api, "_liveness", None)
    api.close_store()
    yield server
    assert server.pusher.drain()
    api.close_store()


@pytest.mark.parametrize("key", ["", "missing.json", "broken.json"])
def test_the_server_runs_and_files_an_inbox_item_without_a_key(server, tmp_path, http, monkeypatch, key):
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(server.cfg, "studio_fcm_key", str(tmp_path / key) if key else "")
    heard = receivers(server.engine.notifier)
    assert server.pusher in heard and any(isinstance(r, notify.Notifier) for r in heard)  # the toast stays

    owner = TestClient(server.app, headers=AUTH)
    assert owner.get("/studio/devices").status_code == 200
    device = api.get_store().create_device("내 폰", "hash-1", platform="android", fcm_token="fcm-1")
    item = api.run_inbox().file_run("design_approval", "run_nokey", title="설계 확인", payload=dict(STOP))
    server.pusher("run_nokey", "run_failed", "설계 확인", {})
    assert server.pusher.drain() and http.calls == []

    # The item is read from the store, before the list: `GET /studio/inbox` shows no item of `run_nokey`, a run
    # the engine does not have.
    assert api.get_store().get_inbox_item(item["id"])["status"] == "pending"
    assert owner.get("/studio/inbox", params={"status": "pending"}).status_code == 200
    assert api.get_store().get_device(device["id"])["fcm_token"] == "fcm-1"


def test_the_server_pushes_a_new_inbox_item_once_a_key_is_named(server, key_file, http, monkeypatch):
    monkeypatch.setattr(server.cfg, "studio_fcm_key", str(key_file))
    api.get_store().create_device("내 폰", "hash-1", platform="android", fcm_token="fcm-1")
    item = api.run_inbox().file_run("design_approval", "run_push", title="설계 확인", payload=dict(STOP))
    assert server.pusher.drain()
    (url, kw), = http.sent()
    assert url == SEND_URL and kw["json"] == {"message": fcm("fcm-1", "설계 확인", "설계 승인", ("inbox", item["id"]), action="approve")}


def test_the_server_looks_at_the_waiting_items_from_its_start_to_its_stop(server, monkeypatch):
    monkeypatch.setattr(server.cfg, "studio_remind_after", 123.0)
    assert server.pusher.remind_after() == 123.0  # the setting, read at every look
    assert server.start_push_reminders in server.app.router.on_startup
    assert server.stop_push_reminders in server.app.router.on_shutdown
    calls = []
    monkeypatch.setattr(server.pusher, "watch", lambda: calls.append("watch"))
    monkeypatch.setattr(server.pusher, "unwatch", lambda: calls.append("unwatch"))
    server.start_push_reminders()
    server.stop_push_reminders()
    assert calls == ["watch", "unwatch"]


def test_a_room_is_muted_over_rest_and_its_employees_words_are_not_pushed(server, key_file, http, monkeypatch):
    monkeypatch.setattr(server.cfg, "studio_fcm_key", str(key_file))
    owner, store = TestClient(server.app, headers=AUTH), api.get_store()
    store.create_device("내 폰", "hash-1", platform="android", fcm_token="fcm-1")
    minsu = store.create_employee("민수", "program")
    dm = store.create_room("dm", title="민수", employee_id=minsu["id"])["id"]

    def say(body):
        with store.transaction() as tx:
            message = store.create_message(dm, "employee", author_id=minsu["id"], body=body, tx=tx)
            store.emit(tx, "message.created", {"message": message}, room_id=dm)
        assert server.pusher.drain()

    last = store.stream_bounds()["last_id"]
    r = owner.patch(f"/studio/rooms/{dm}", json={"muted": True})
    assert r.status_code == 200 and (r.json()["id"], r.json()["muted"]) == (dm, True), r.text
    assert owner.patch(f"/studio/rooms/{dm}", json={"muted": True}).json()["muted"] is True  # the same once more
    told = store.list_stream_events(after_id=last)
    assert [(e["type"], e["data"]["room"]["muted"]) for e in told] == [("room.updated", True)]  # it changed once
    assert owner.get(f"/studio/rooms/{dm}").json()["muted"] is True

    say("조용한 글")
    item = api.run_inbox().file_run("design_approval", "run_muted", title="설계 확인", payload=dict(STOP))
    assert server.pusher.drain()
    assert http.messages() == [fcm("fcm-1", "설계 확인", "설계 승인", ("inbox", item["id"]), action="approve")]  # an item is not muted

    assert owner.patch(f"/studio/rooms/{dm}", json={"muted": False}).json()["muted"] is False
    say("들리는 글")
    assert http.messages()[-1] == fcm("fcm-1", "민수", "들리는 글", ("room", dm), "quiet", action="reply", room_id=dm)

    assert owner.patch("/studio/rooms/room_nope", json={"muted": True}).status_code == 404
    for bad in ({}, {"muted": None}, {"muted": True, "title": "새 이름"}):
        assert owner.patch(f"/studio/rooms/{dm}", json=bad).status_code == 422, bad
    assert store.get_room(dm)["muted"] is False


def test_a_hiring_recommendation_is_pushed_quietly_and_is_not_reminded_of(studio, http):
    studio.device(1, "fcm-1")
    item = studio.file("아트 파트 채용 권장", kind="hire_recommendation", run_id=None)
    assert [m["data"]["grade"] for m in http.messages()] == [push.QUIET] and len(http.sent()) == 1
    studio.look(seconds(item["created_at"]) + 7200 * 3)  # long past the two hours: the reminder is urgent
    assert len(http.sent()) == 1 and studio.reminded(item) is None


# --- the notification action (N-3) ---------------------------------------------------------------------------
NOT_FROM_A_NOTIFICATION = {"risky_command", "final_review", "structure_choice", "question", "hire_recommendation"}


def test_each_inbox_kind_is_pushed_with_what_its_notification_can_do_and_again_the_same(studio, http):
    assert set(push.KIND_LABEL) == set(inbox.NOTIFY_OK) | NOT_FROM_A_NOTIFICATION  # a new kind is decided here
    studio.device(1, "fcm-1")
    first, items = {}, {}
    for kind in push.KIND_LABEL:
        item = studio.file(f"{kind} 결재", kind=kind, run_id=None if kind == push.QUIET_KIND else "run_a")
        data = http.messages()[-1]["data"]
        grade = push.QUIET if kind == push.QUIET_KIND else push.URGENT
        action = "approve" if kind in inbox.NOTIFY_OK else "open"
        assert data == {"grade": grade, "type": "inbox", "id": item["id"], "action": action}, kind
        assert all(isinstance(v, str) for v in data.values())  # FCM data holds strings only
        first[item["id"]], items[kind] = data, item
    assert len(http.sent()) == len(push.KIND_LABEL)

    studio.look(max(seconds(item["created_at"]) for item in items.values()) + 7200)
    again = http.messages()[len(push.KIND_LABEL):]
    assert len(again) == len(push.KIND_LABEL) - 1  # a hiring recommendation is never reminded of
    for message in again:
        assert message["data"] == {**first[message["data"]["id"]], "grade": "urgent"}


def test_an_employees_words_are_pushed_with_a_reply_and_the_room_and_nothing_else_is(studio, http):
    studio.device(1, "fcm-1")
    minsu, dm = studio.employee()
    studio.say(dm, "로그인 화면을 고쳤습니다", author_id=minsu["id"])
    assert http.messages()[-1]["data"] == {"grade": "quiet", "type": "room", "id": dm, "action": "reply", "room_id": dm}
    studio.say(dm, "턴을 넣지 못했다", "system", meta={"event": "run_error"})
    assert http.messages()[-1]["data"] == {"grade": "urgent", "type": "room", "id": dm}
    studio.say("room_system", "오늘 한 일", author_id=minsu["id"], meta={"event": "daily_report"})
    assert http.messages()[-1]["data"] == {"grade": "digest", "type": "room", "id": "room_system"}
    assert len(http.sent()) == 3
