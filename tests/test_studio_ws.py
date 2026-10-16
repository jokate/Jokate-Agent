"""Studio live stream (S-4): WS /studio/ws — authentication (§7.1), hello -> after_id -> live, resync, ping,
retention (§7.2), `run.live` (§7.4) and GET /studio/runs/{run_id}/live (§6.4). No AI is called: the engine's
liveness is a fake.

Reading never blocks for good: the fixture makes the server ping every 50 ms, and the helpers count pings."""
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from relay_agent.studio import Actor, Effects, StudioStore, TaskMachine, api, ws
from relay_agent.studio.tasks import OWNER, SYSTEM

DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
TOKEN = "shared-Token-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TEXT = dict(title="2단 점프", reason="조작감", spec="공중에서 한 번 더", done_criteria="테스트 통과", type="feature")
REVIEW = {"title": "검수 요청", "payload": {"summary": "끝"}}
LIVE = dict(status="running", alive=True, stage="implement", stage_seconds=1.5, quiet_seconds=0.0, doing="tool",
            turn=2, snippet="", tool="Edit", target="a.py", tools=3, tool_seconds=0.5, last=None, precise=True)


class Fx(Effects):
    """Stands in for the runs of S-8: a started task gets run_001, run_002, …"""

    def __init__(self):
        self.runs = 0

    def create_run(self, task):
        self.runs += 1
        return f"run_{self.runs:03d}"


class Engine:
    """Stands in for RelayEngine.liveness. What it says about a run can be changed between two reads, and
    `quiet_seconds` is new at every read — like the real one, also for a run that has stopped."""

    def __init__(self):
        self.runs = {}  # run id -> the fields that differ from LIVE
        self.reads = 0

    def __call__(self, run_id):
        self.reads += 1
        if run_id not in self.runs:
            raise FileNotFoundError(run_id)
        return {**LIVE, **self.runs[run_id], "quiet_seconds": float(self.reads)}


class World:
    """The server app on a studio DB of its own: a project, an employee with a 1:1 room, a task machine."""

    def __init__(self, server):
        self.server, self.cfg = server, server.cfg
        self.owner = TestClient(server.app, headers=AUTH)                    # this PC, with the shared token
        self.local = TestClient(server.app)                                  # this PC, no token
        self.remote = TestClient(server.app, client=("192.168.0.7", 50000))  # another device, no token
        self.store = s = api.get_store()
        self.m = TaskMachine(s, Fx())
        self.pid = s.create_project("게임", "D:/game", "game")["id"]
        self.emp = s.create_employee("민수", "program", project_id=self.pid)["id"]
        self.dm = s.create_room("dm", title="민수", employee_id=self.emp)["id"]
        s.create_room_member(self.dm, self.emp)
        self.hired = 0

    def say(self, body):
        """The owner writes in the 1:1 room, over REST: one `message.created`."""
        r = self.owner.post(f"/studio/rooms/{self.dm}/messages", json={"body": body})
        assert r.status_code == 201, r.text
        return r.json()

    def task(self):
        """A task the owner registered (C2), with an assignee of its own."""
        self.hired += 1
        who = self.store.create_employee(f"직원{self.hired}", "program", project_id=self.pid)["id"]
        return self.m.create_task(OWNER, "backlog", self.pid, assignee_id=who, **TEXT).task

    def start(self, task):
        """The start request and T3: the task is working and holds a run."""
        self.m.request_start(task["id"], OWNER)
        return self.m.transition(task["id"], "working", SYSTEM).task

    def stored(self, after=0):
        """stream_events after a cursor, as the envelopes the stream must send (§7.3)."""
        rows = api.get_store().list_stream_events(after_id=after, limit=10_000)
        return [{key: row[key] for key in ws.ENVELOPE} for row in rows]


def wait(done, seconds=5.0):
    end = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < end, "기다리던 일이 일어나지 않았다"
        time.sleep(0.01)


@pytest.fixture
def world(tmp_path, monkeypatch):
    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    monkeypatch.setattr(ws, "PING_INTERVAL", 0.05)
    monkeypatch.setattr(ws, "LIVE_INTERVAL", 0.05)
    monkeypatch.setattr(ws, "LIVE_MIN_GAP", 0.01)  # below the interval, or a round would wait for the gap
    monkeypatch.setattr(api, "_liveness", None)  # the tests that read liveness put an Engine here
    api.close_store()
    yield World(server)
    wait(lambda: len(ws.hub) == 0 and ws.hub._ticker is None)  # every connection ended, the ticker with them
    api._tracked.clear()
    api.close_store()


def connect(client, query="", **kw):
    return client.websocket_connect("/studio/ws" + query, **kw)


def take(sock, n=1):
    """The next n messages that are not pings."""
    out, pings = [], 0
    while len(out) < n:
        message = sock.receive_json()
        if message["type"] == "ping":
            pings += 1
            assert pings < 100, f"{n}개를 기다렸는데 {out} 만 왔다"
        else:
            out.append(message)
    return out


def idle(sock, pings=3):
    """What comes besides pings while `pings` pings pass."""
    seen, n = [], 0
    while n < pings:
        message = sock.receive_json()
        if message["type"] == "ping":
            n += 1
        else:
            seen.append(message)
    return seen


def until(sock, wanted):
    """Read up to the first message `wanted` accepts."""
    for _ in range(2000):
        message = sock.receive_json()
        if wanted(message):
            return message
    raise AssertionError("기다리던 메시지가 오지 않았다")


def lives(sock, n):
    """Read until n `run.live` have come. Returns them."""
    return [until(sock, lambda m: m["type"] == "run.live") for _ in range(n)]


def closed(sock):
    """The code the server closes the connection with."""
    with pytest.raises(WebSocketDisconnect) as end:
        for _ in range(2000):
            sock.receive_json()
    return end.value.code


def refused(r, status, code):
    assert (r.status_code, r.json().get("code")) == (status, code), r.text
    return r.json()


def doc_events():
    """§7.4: event type -> is it stored."""
    text = DOC.read_text(encoding="utf-8")
    out = {}
    for line in text[text.index("### 7.4 이벤트"):text.index("### 7.5")].splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and re.fullmatch(r"`[a-z]+\.[a-z]+`", cells[0]):
            out[cells[0].strip("`")] = cells[1] == "●"
    return out


# --- the document's numbers -------------------------------------------------------------------------------
def test_intervals_codes_and_envelope_are_the_documents():
    assert (ws.AUTH_TIMEOUT, ws.PING_INTERVAL, ws.LIVE_INTERVAL, ws.LIVE_MIN_GAP) == (10, 30, 2, 0.5)  # §7.1 · §7.4
    assert (ws.UNAUTHORIZED, ws.FORBIDDEN) == (4401, 4403)
    text = DOC.read_text(encoding="utf-8")
    live = text[text.index("**런 liveness (`run.live`).**"):text.index("### 7.5")]
    assert "2초마다" in live and "0.5초 이상" in live
    # What the later tasks must call is written where they read it (M-2, S-7 · S-9), by the names of the code.
    assert "`ws.hub.drop_device(device_id)`" in text and callable(ws.hub.drop_device)
    assert "`api.track_run`" in live and "`api.untrack_run`" in live
    assert callable(api.track_run) and callable(api.untrack_run)
    example = text[text.index("### 7.3 봉투"):text.index("### 7.4")].split("```json")[1].split("```")[0]
    assert list(json.loads(example)) == list(ws.ENVELOPE)
    events = doc_events()
    assert len(events) == 18 and [t for t, kept in events.items() if not kept] == ["job.log", "run.live"]


def test_ws_route_is_on_the_server_and_the_live_alias_is_the_s4_row_of_the_document(world):
    assert any(getattr(r, "path", None) == "/studio/ws" for r in world.server.app.routes)
    assert "/studio/ws" not in api.openapi_schema()["paths"]  # the contract file lists REST only
    row = next(line for line in DOC.read_text(encoding="utf-8").splitlines()
               if line.startswith("| `GET /studio/runs/{run_id}/live` |"))
    assert row.rstrip().endswith("| S-4 |")
    assert ("GET", "/studio/runs/{run_id}/live") in {(m, r.path) for r in api.router.routes for m in r.methods}


# --- authentication (§7.1) --------------------------------------------------------------------------------
def test_the_http_guard_never_sees_a_websocket(world, monkeypatch):
    """The '확인 필요 (S-4)' of §7.1 · §11.3: `@app.middleware("http")` is not run for a WebSocket."""
    seen, real = [], api.authenticate

    async def spy(request, cfg):
        seen.append(request.url.path)
        return await real(request, cfg)

    monkeypatch.setattr(api, "authenticate", spy)  # the guard's /studio branch calls it by this name
    with connect(world.owner) as sock:
        assert take(sock)[0]["type"] == "hello"
    assert seen == []
    assert world.owner.get("/studio/projects").status_code == 200 and seen == ["/studio/projects"]
    # A network the guard refuses: HTTP gets the guard's 403 before any handler. The WebSocket is accepted
    # and then closed with a code — by the handler, which asks the same function.
    monkeypatch.setattr(world.server, "LOOPBACK", set())
    monkeypatch.setattr(world.cfg, "remote_networks", ["100.64.0.0/10"])
    assert world.remote.get("/studio/projects", headers=AUTH).status_code == 403
    with connect(world.remote, headers=AUTH) as sock:
        assert closed(sock) == 4403


def test_no_token_is_closed_with_4401_on_this_pc_too(world, monkeypatch):
    monkeypatch.setattr(ws, "AUTH_TIMEOUT", 0.3)
    for client in (world.local, world.remote):
        with connect(client) as sock:  # says nothing: closed when the time is up
            assert closed(sock) == 4401
        with connect(client) as sock:  # the first message is not `auth`
            sock.send_json({"type": "pong"})
            assert closed(sock) == 4401
        with connect(client) as sock:
            sock.send_text("not json")
            assert closed(sock) == 4401
        with connect(client) as sock:
            sock.send_json({"type": "auth"})
            assert closed(sock) == 4401
        with connect(client) as sock:
            sock.send_json({"type": "auth", "token": "wrong"})
            assert closed(sock) == 4401
        with connect(client, headers={"Authorization": "Bearer wrong"}) as sock:
            assert closed(sock) == 4401
        with connect(client, headers={"Authorization": f"Basic {TOKEN}"}) as sock:  # a header decides alone
            sock.send_json({"type": "auth", "token": TOKEN})
            assert closed(sock) == 4401
    with connect(world.local, f"?token={TOKEN}") as sock:  # never from the address
        assert closed(sock) == 4401
    assert len(ws.hub) == 0


def test_header_or_first_message_opens_the_stream(world):
    with connect(world.owner) as sock:
        hello = take(sock)[0]
        assert len(ws.hub) == 1
    assert list(hello) == ["type", "last_id", "now", "machine"] and hello["type"] == "hello"
    assert hello["last_id"] == 0 and datetime.fromisoformat(hello["now"])
    assert list(hello["machine"]) == ["name", "server_version", "schema_version", "utc_offset_minutes"]  # §7.1
    assert hello["machine"] == api.machine_brief() and isinstance(hello["machine"]["name"], str)
    for client in (world.local, world.remote):  # a browser: the token as the first message
        with connect(client) as sock:
            sock.send_json({"type": "auth", "token": TOKEN})
            assert take(sock)[0]["type"] == "hello"
    token = "sdt_" + "k" * 43  # a paired device's token
    world.store.create_device("폰", hashlib.sha256(token.encode()).hexdigest())
    with connect(world.remote, headers={"Authorization": f"Bearer {token}"}) as sock:
        assert take(sock)[0]["type"] == "hello"
    with connect(world.remote) as sock:
        sock.send_json({"type": "auth", "token": token})
        assert take(sock)[0]["type"] == "hello"


def test_another_origin_is_closed_with_4403_before_the_token_is_asked(world):
    with connect(world.owner, headers={"Origin": "http://testserver"}) as sock:
        assert take(sock)[0]["type"] == "hello"
    with connect(world.owner, headers={"Origin": "http://evil.example"}) as sock:
        assert closed(sock) == 4403
    with connect(world.local, headers={"Origin": "http://evil.example"}) as sock:  # 4403, not 4401
        assert closed(sock) == 4403


def test_a_revoked_devices_connections_are_closed_with_4401(world):
    token = "sdt_" + "k" * 43
    device = world.store.create_device("폰", hashlib.sha256(token.encode()).hexdigest())
    with connect(world.remote, headers={"Authorization": f"Bearer {token}"}) as phone, connect(world.owner) as pc:
        take(phone), take(pc)
        assert ws.hub.drop_device("dev_none") == 0
        assert ws.hub.drop_device(device["id"]) == 1
        assert closed(phone) == 4401
        world.say("남은 연결")
        assert take(pc)[0]["type"] == "message.created"  # the shared token's connection stays


# --- hello -> after_id -> live (§7.1 · §7.2) --------------------------------------------------------------
def test_reconnecting_with_after_id_misses_nothing_and_keeps_the_order(world):
    """The done criterion of S-4."""
    with connect(world.owner) as sock:
        start = take(sock)[0]["last_id"]
        world.say("하나")
        task = world.task()  # C2: task.created · room.updated
        written = world.stored(after=start)
        assert take(sock, len(written)) == written and idle(sock) == []
    handled = written[:2]  # the app got as far as the second event when the connection broke
    cursor = handled[-1]["id"]
    world.say("둘")  # written while nobody is connected
    world.start(task)  # task.updated (queued), then T3: task.transitioned · employee.status
    with connect(world.owner, f"?after_id={cursor}") as sock:
        missed = world.stored(after=cursor)
        assert take(sock)[0]["last_id"] == missed[-1]["id"]
        replayed = take(sock, len(missed))
        assert replayed == missed and len(missed) > len(written) - 2
        world.say("셋")  # and the stream goes on from there, without a gap or a repeat
        world.m.transition(task["id"], "review", Actor.employee(task["assignee_id"]), inbox=dict(REVIEW))
        live = take(sock, len(world.stored(after=missed[-1]["id"])))
        assert idle(sock) == []
    everything = world.stored(after=start)
    assert handled + replayed + live == everything
    ids = [e["id"] for e in everything]
    assert ids == list(range(start + 1, start + 1 + len(ids)))
    kinds = {e["type"] for e in everything}
    assert {"message.created", "task.created", "room.updated", "task.updated", "task.transitioned",
            "employee.status", "inbox.created"} <= kinds  # the change points of S-2 · S-3 (§7.5)
    assert all(doc_events()[kind] for kind in kinds) and all(list(e) == list(ws.ENVELOPE) for e in everything)


def test_without_after_id_the_stream_starts_now_and_zero_replays_everything(world):
    world.say("전에")
    with connect(world.owner) as sock:
        assert take(sock)[0]["last_id"] == 1 and idle(sock) == []
        world.say("뒤에")
        assert [e["data"]["message"]["body"] for e in take(sock)] == ["뒤에"]
    with connect(world.owner, "?after_id=0") as sock:
        assert take(sock)[0]["last_id"] == 2
        assert take(sock, 2) == world.stored() and idle(sock) == []
    with connect(world.owner, "?after_id=2") as sock:  # up to date: nothing to replay, and no resync
        assert take(sock)[0]["last_id"] == 2 and idle(sock) == []


def test_every_connection_gets_every_event(world):
    with connect(world.owner) as pc, connect(world.remote, headers=AUTH) as phone:
        take(pc), take(phone)
        assert len(ws.hub) == 2
        world.say("모두에게")
        assert take(pc) == take(phone) == world.stored()


def test_the_stream_survives_a_restart(world):
    world.say("재시작 전")
    api.close_store()  # the server stops; the next call opens the same file again
    with connect(world.owner, "?after_id=0") as sock:
        assert take(sock)[0]["last_id"] == 1
        assert [e["data"]["message"]["body"] for e in take(sock)] == ["재시작 전"]
        world.say("재시작 후")
        assert take(sock)[0]["id"] == 2


def test_a_cursor_out_of_range_gets_resync(world):
    for n in range(5):
        world.say(f"글 {n}")
    assert world.store.prune_stream_events(max_rows=2) == 3
    assert world.store.stream_bounds() == {"last_id": 5, "oldest_id": 4}

    def opening(query):
        with connect(world.owner, query) as sock:
            assert take(sock)[0]["last_id"] == world.store.stream_bounds()["last_id"]
            return idle(sock)

    # pruned past the cursor, ahead of this DB, not a number: resync, then only what is new
    for query in ("?after_id=1", "?after_id=2", "?after_id=6", "?after_id=-1", "?after_id=x", "?after_id=1.5",
                  "?after_id="):
        assert opening(query) == [{"type": "resync", "oldest_id": 4}], query
    assert [e["id"] for e in opening("?after_id=3")] == [4, 5]  # the row after it is still there
    assert opening("?after_id=5") == []
    with connect(world.owner, "?after_id=1") as sock:
        assert [m["type"] for m in take(sock, 2)] == ["hello", "resync"]
        world.say("새 글")
        assert take(sock)[0]["id"] == 6
    assert world.store.prune_stream_events(max_rows=0) == 3  # nothing is stored any more
    assert opening("?after_id=6") == []
    assert opening("?after_id=5") == [{"type": "resync", "oldest_id": 7}]


def test_ping_comes_on_its_own_and_what_the_client_sends_is_ignored(world):
    with connect(world.owner) as sock:
        take(sock)
        assert sock.receive_json() == {"type": "ping"}
        sock.send_json({"type": "pong"})
        sock.send_json({"type": "auth", "token": "wrong"})  # after the first message: not looked at
        sock.send_text("not json")
        sock.send_bytes(b"\x00")
        sock.send_json({"type": "subscribe", "room_id": world.dm})
        world.say("그래도 온다")
        assert take(sock)[0]["type"] == "message.created" and len(ws.hub) == 1


def test_a_store_that_closes_under_a_connection_ends_it_with_1013(world):
    with connect(world.owner) as sock:
        take(sock)
        api.close_store()
        assert closed(sock) == 1013


# --- run.live (§7.4) and its REST alias (§6.4) ------------------------------------------------------------
def test_run_live_while_the_run_works_and_once_more_when_it_stops(world, monkeypatch):
    engine = Engine()
    monkeypatch.setattr(api, "_liveness", engine)
    task = world.task()
    with connect(world.owner) as sock:
        take(sock)
        assert idle(sock) == [] and engine.reads == 0  # no studio run: the engine is not asked
        engine.runs["run_001"] = {}
        assert world.start(task)["run_id"] == "run_001"
        room = world.store.list_rooms(task_id=task["id"])[0]["id"]
        first = lives(sock, 3)
        for event in first:
            assert list(event) == list(ws.ENVELOPE) and event["id"] is None  # not stored: no cursor moves
            assert (event["project_id"], event["room_id"], event["task_id"]) == (world.pid, room, task["id"])
            assert event["data"] == {"run_id": "run_001", "employee_id": task["assignee_id"], "room_id": room,
                                     "task_id": task["id"], "mode": "work", **LIVE,
                                     "quiet_seconds": event["data"]["quiet_seconds"]}
            assert list(event["data"])[:5] == list(api.LIVE_HEAD)
        quiet = [event["data"]["quiet_seconds"] for event in first]
        assert quiet == sorted(set(quiet))  # each one a new reading

        engine.runs["run_001"] = {"status": "awaiting_approval", "alive": False}  # it stopped at a gate
        stopped = until(sock, lambda m: m["type"] == "run.live" and not m["data"]["alive"])
        assert stopped["data"]["status"] == "awaiting_approval"
        reads = engine.reads
        assert [m for m in idle(sock, 5) if m["type"] == "run.live"] == []  # said once, though the seconds count on
        assert engine.reads > reads  # it is still a working task's run: read, to see it resume
        engine.runs["run_001"] = {}
        assert lives(sock, 1)[0]["data"]["alive"] is True

        # The task leaves `working` while its run still winds down: watched until its end has been said.
        world.m.transition(task["id"], "review", Actor.employee(task["assignee_id"]), inbox=dict(REVIEW))
        assert lives(sock, 1)[0]["data"]["alive"] is True
        engine.runs["run_001"] = {"status": "done", "alive": False}
        assert until(sock, lambda m: m["type"] == "run.live" and not m["data"]["alive"])["data"]["status"] == "done"
        idle(sock)
        reads = engine.reads
        assert [m for m in idle(sock, 5) if m["type"] == "run.live"] == [] and engine.reads == reads
    assert world.stored() == [e for e in world.stored() if e["type"] != "run.live"]  # never in stream_events
    wait(lambda: ws.hub._ticker is None)
    reads = engine.reads
    time.sleep(0.2)
    assert engine.reads == reads  # nobody is connected: liveness is not read


def test_a_tracked_chat_turn_is_reported_as_run_live(world, monkeypatch):
    engine = Engine()
    monkeypatch.setattr(api, "_liveness", engine)
    engine.runs["run_chat"] = {}
    with connect(world.owner) as sock:
        take(sock)
        api.track_run("run_chat", mode="chat", employee_id=world.emp, room_id=world.dm, project_id=world.pid)
        event = lives(sock, 1)[0]
        assert (event["project_id"], event["room_id"], event["task_id"]) == (world.pid, world.dm, None)
        assert {k: event["data"][k] for k in api.LIVE_HEAD} == {
            "run_id": "run_chat", "employee_id": world.emp, "room_id": world.dm, "task_id": None, "mode": "chat"}
        api.untrack_run("run_chat")
        engine.runs["run_chat"] = {"status": "done", "alive": False}
        assert until(sock, lambda m: m["type"] == "run.live" and not m["data"]["alive"])["data"]["mode"] == "chat"
        assert [m for m in idle(sock, 5) if m["type"] == "run.live"] == []


def test_a_burst_of_commits_reads_liveness_no_more_often_than_the_gap(world, monkeypatch):
    """Every commit wakes the ticker — a run may have started or stopped with it — but two reads are
    LIVE_MIN_GAP apart: ten messages in a row cost a read or two, not ten (§7.4)."""
    engine, gap = Engine(), 0.2
    monkeypatch.setattr(api, "_liveness", engine)
    monkeypatch.setattr(ws, "LIVE_INTERVAL", 30.0)  # so only a commit makes the ticker read again
    monkeypatch.setattr(ws, "LIVE_MIN_GAP", gap)
    engine.runs["run_chat"] = {}
    api.track_run("run_chat", mode="chat", employee_id=world.emp, room_id=world.dm, project_id=world.pid)
    with connect(world.owner) as sock:
        hello = take(sock)[0]
        lives(sock, 1)  # the read at the start
        began, reads = time.monotonic(), engine.reads
        for n in range(10):
            world.say(f"{n}번째 글")
        got = [until(sock, lambda m: m["type"] == "message.created") for _ in range(10)]
        wait(lambda: engine.reads > reads)  # what the commits announced is read without waiting for the interval
        more = engine.reads - reads
        # The bound grows with the time that passed, so a slow machine does not fail it; ten reads would.
        assert more <= (time.monotonic() - began) / gap + 1
        assert got == world.stored(hello["last_id"])  # and the events themselves all came, in id order


def test_the_ticker_ends_at_once_when_the_last_connection_leaves_however_far_the_next_read(world, monkeypatch):
    engine = Engine()
    monkeypatch.setattr(api, "_liveness", engine)
    monkeypatch.setattr(ws, "LIVE_MIN_GAP", 30.0)
    engine.runs["run_chat"] = {}
    api.track_run("run_chat", mode="chat", employee_id=world.emp, room_id=world.dm, project_id=world.pid)
    with connect(world.owner) as sock:
        take(sock)
        lives(sock, 1)
        world.say("읽을 때를 기다리게 한다")  # the commit wakes the ticker; it now waits for the gap to pass
        until(sock, lambda m: m["type"] == "message.created")
    wait(lambda: ws.hub._ticker is None, 2)  # or a connection that comes back would get no report of this run
    assert engine.reads == 1


def test_get_run_live_is_the_data_of_run_live(world, monkeypatch):
    task = world.task()
    world.start(task)
    url = "/studio/runs/run_001/live"
    refused(world.local.get(url), 401, "unauthorized")
    refused(world.owner.get(url), 503, "unavailable")  # no engine connected
    engine = Engine()
    monkeypatch.setattr(api, "_liveness", engine)
    refused(world.owner.get(url), 404, "not_found")  # the engine has no such run
    engine.runs.update(run_001={}, run_plain={}, run_chat={})
    room = world.store.list_rooms(task_id=task["id"])[0]["id"]
    body = world.owner.get(url).json()
    assert body == {"run_id": "run_001", "employee_id": task["assignee_id"], "room_id": room, "task_id": task["id"],
                    "mode": "work", **LIVE, "quiet_seconds": body["quiet_seconds"]}
    refused(world.owner.get("/studio/runs/run_plain/live"), 404, "not_found")  # a relay run that is not studio's
    world.m.transition(task["id"], "review", Actor.employee(task["assignee_id"]), inbox=dict(REVIEW))
    engine.runs["run_001"] = {"status": "done", "alive": False}
    assert world.owner.get(url).json()["alive"] is False  # a stopped task run still answers
    api.track_run("run_chat", mode="chat", employee_id=world.emp, room_id=world.dm)
    assert world.owner.get("/studio/runs/run_chat/live").json()["mode"] == "chat"
    api.untrack_run("run_chat")
    refused(world.owner.get("/studio/runs/run_chat/live"), 404, "not_found")


# --- the store's side (§7.2) ------------------------------------------------------------------------------
def test_commit_listeners_hear_committed_stream_events_only(tmp_path):
    store = StudioStore(tmp_path / "studio.sqlite")
    heard = []
    store.commit_listeners.append(lambda ids: heard.append((ids, store.stream_bounds()["last_id"])))
    with store.transaction() as tx:
        a, b = store.emit(tx, "x"), store.emit(tx, "y")
        assert heard == []  # not before the commit
    assert heard == [((a, b), b)]  # ... and the listener could read: the lock was free
    with pytest.raises(RuntimeError):
        with store.transaction() as tx:
            store.emit(tx, "x")
            raise RuntimeError("rolled back")
    with store.transaction() as tx:  # a commit without events: nothing to announce
        store.create_project("게임", "D:/game", "game", tx=tx)
    assert len(heard) == 1
    store.commit_listeners.insert(0, lambda ids: 1 / 0)  # a broken listener: the commit and the others stand
    with store.transaction() as tx:
        c = store.emit(tx, "z")
    assert heard[-1][0] == (c,) and [e["id"] for e in store.list_stream_events()] == [a, b, c]
    store.close()


def test_stream_bounds_and_pruning(tmp_path):
    store = StudioStore(tmp_path / "studio.sqlite")
    assert store.stream_bounds() == {"last_id": 0, "oldest_id": 1}
    with store.transaction() as tx:
        for n in range(6):
            store.emit(tx, "x", {"n": n})
    assert store.stream_bounds() == {"last_id": 6, "oldest_id": 1}
    assert store.prune_stream_events() == 0  # inside 7 days and 50,000 rows
    old = (datetime.now() - timedelta(days=7, minutes=1)).isoformat(timespec="seconds")
    store._exec("UPDATE stream_events SET at = ? WHERE id IN (1, 3)", (old,))
    assert store.prune_stream_events() == 3  # a prefix: 2 goes with 1 and 3, so the ids left have no hole
    assert store.stream_bounds() == {"last_id": 6, "oldest_id": 4}
    assert store.prune_stream_events(max_rows=2) == 1
    assert [e["id"] for e in store.list_stream_events()] == [5, 6]
    assert store.prune_stream_events(max_rows=0) == 2
    assert store.stream_bounds() == {"last_id": 6, "oldest_id": 7}  # the sequence does not start over
    with store.transaction() as tx:
        assert store.emit(tx, "x") == 7
    store.close()


def test_retention_is_looked_at_when_the_store_opens_and_once_a_day(world, monkeypatch):
    old = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
    world.say("오래된 글")
    world.store._exec("UPDATE stream_events SET at = ?", (old,))
    world.say("새 글")  # a day has not passed since the store opened: nothing is deleted
    assert [e["id"] for e in world.stored()] == [1, 2]
    monkeypatch.setattr(api, "_pruned_at", time.monotonic() - api.PRUNE_EVERY - 1)
    world.say("하루 뒤")  # the commit that finds a day gone prunes after itself
    assert [e["id"] for e in world.stored()] == [2, 3] and time.monotonic() - api._pruned_at < 60
    world.store._exec("UPDATE stream_events SET at = ?", (old,))
    api.close_store()
    assert api.get_store().stream_bounds() == {"last_id": 3, "oldest_id": 4}  # at the start

    def broken(self, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(StudioStore, "prune_stream_events", broken)  # pruning that fails stops nothing
    api.close_store()
    assert api.open_store() is True
    monkeypatch.setattr(api, "_pruned_at", time.monotonic() - api.PRUNE_EVERY - 1)
    world.say("정리가 실패해도 글은 써진다")
    assert [e["id"] for e in world.stored()] == [4]
