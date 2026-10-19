"""Editor control (M-7): studio/editor.py and the four paths under /studio/projects/{id}/editor (§6.4 · §6.6).
No Unreal editor is run: the executable is a file that only has the right name, the processes are a list, and the
editor's MCP is a TCP server on this computer that answers like the plugin."""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay_agent.studio import api, editor, machine

DESIGN = Path(__file__).resolve().parent.parent / "design" / "studio"
TOKEN = "shared-Token-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PNG = b"\x89PNG\r\n\x1a\n" + b"viewport" * 8
STATUS_KEYS = ["running", "pid", "healthy", "checked_at"]
OTHER = {"pid": 50, "name": "UnrealEditor.exe", "cmdline": 'UnrealEditor.exe "E:\\Other\\Other.uproject"'}


def row(pid, uproject):
    return {"pid": pid, "name": "UnrealEditor.exe", "cmdline": f'UnrealEditor.exe "{uproject}" -log'}


class FakeEditor:
    """A TCP server that speaks like the editor's MCP plugin (`Plugins/Unreal-MCP-main`, MCPServerRunnable.cpp):
    one request per connection, framed with a 4-byte big-endian length, and one answer framed the same way —
    `framed=False` answers bare JSON instead (the public unreal-mcp). Its port is bound from the start and nobody
    listens on it until `up()` — an editor that is still loading."""

    def __init__(self, answer=None, framed=True):
        self.requests = []
        self.framed = framed
        self.answer = answer or self.plugin
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = None

    @staticmethod
    def plugin(request):
        if request["type"] == "take_screenshot":
            Path(request["params"]["filepath"]).write_bytes(PNG)
            return {"status": "success", "result": {"filepath": request["params"]["filepath"]}}
        return {"status": "success", "result": {"ok": True}}

    def up(self):
        if self.thread is None:
            self.sock.listen(8)
            self.thread = threading.Thread(target=self._serve, daemon=True)
            self.thread.start()
        return self

    def close(self):
        self.sock.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return  # closed
            with conn:
                try:
                    conn.settimeout(5)
                    head = self._read(conn, 4)
                    if len(head) < 4:
                        continue
                    request = json.loads(self._read(conn, int.from_bytes(head, "big")).decode("utf-8"))
                    self.requests.append(request)
                    reply = self.answer(request)
                    if reply is None:
                        conn.recv(1)  # says nothing: holds the connection until the asker gives up
                        continue
                    data = reply if isinstance(reply, bytes) else json.dumps(reply).encode("utf-8")
                    conn.sendall((len(data).to_bytes(4, "big") if self.framed else b"") + data)
                except (OSError, ValueError):
                    pass

    @staticmethod
    def _read(conn, size):
        got = b""
        while len(got) < size:
            chunk = conn.recv(size - len(got))
            if not chunk:
                break
            got += chunk
        return got


class Clock:
    """Time that only passes when somebody sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept = []
        self.on_sleep = None

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(len(self.slept))


@pytest.fixture
def fake():
    server = FakeEditor()
    yield server
    server.close()


def refused(r, reason):
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "precondition_failed" and body["failed"] == [reason], body
    return body


# --- the MCP exchange (machine.py) ----------------------------------------------------------------------------
def test_mcp_send_sends_one_json_command_and_reads_the_answer(fake):
    fake.up()
    answer = machine.mcp_send(fake.port, "health_check", timeout=5)
    assert answer == {"status": "success", "result": {"ok": True}}
    machine.mcp_send(fake.port, "take_screenshot", {"filepath": os.devnull, "이름": "뷰"}, timeout=5)
    assert fake.requests == [{"type": "health_check", "params": {}},
                             {"type": "take_screenshot", "params": {"filepath": os.devnull, "이름": "뷰"}}]


def test_mcp_send_reads_a_bare_json_answer_too():
    """An editor whose plugin does not frame its answer (the public unreal-mcp) is still read."""
    bare = FakeEditor(framed=False).up()
    try:
        assert machine.mcp_send(bare.port, "health_check", timeout=5) == {"status": "success", "result": {"ok": True}}
        assert bare.requests == [{"type": "health_check", "params": {}}]
    finally:
        bare.close()


def test_mcp_send_refuses_what_is_not_a_json_object_and_gives_up_in_time(fake):
    replies = iter([b"hello", b"[1, 2]", None])
    fake.answer = lambda request: next(replies)
    fake.up()
    with pytest.raises(ValueError):
        machine.mcp_send(fake.port, "health_check", timeout=5)  # not JSON
    with pytest.raises(ValueError):
        machine.mcp_send(fake.port, "health_check", timeout=5)  # JSON, not an object
    with pytest.raises(OSError):
        machine.mcp_send(fake.port, "health_check", timeout=0.3)  # no answer in time


def test_health_check_is_true_only_when_the_editor_answers(fake):
    assert machine.HEALTH_COMMAND == "health_check" and machine.HEALTH_TIMEOUT == 3.0
    assert machine.health_check(fake.port, timeout=0.5) is False  # nobody listens yet
    replies = iter([{"status": "success", "result": {}}, {"status": "error", "error": "unknown command"},
                    b"not json", None])
    fake.answer = lambda request: next(replies)
    fake.up()
    assert machine.health_check(fake.port) is True
    assert machine.health_check(fake.port) is True   # an error is an answer too: the editor is not stuck
    assert machine.health_check(fake.port) is False  # something else listens on the port
    assert machine.health_check(fake.port, timeout=0.3) is False  # a modal window: connected, no answer
    assert [r["type"] for r in fake.requests] == ["health_check"] * 4
    assert machine.health_check(None) is False and machine.health_check("x") is False


def test_the_machine_report_asks_the_same_health_check():
    assert machine.Sources().healthy is machine.health_check
    assert machine.editors.__defaults__ == (machine.health_check,)


# --- the command line -------------------------------------------------------------------------------------------
@pytest.fixture
def disk(tmp_path):
    """An engine folder with a file named like the editor, and a project folder with a .uproject."""
    engine = tmp_path / "UE_5.7"
    exe = engine.joinpath(*editor.EDITOR_EXE)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")  # never run: only where it is matters
    folder = tmp_path / "Game"
    folder.mkdir()
    (folder / "Game.uproject").write_text("{}", encoding="utf-8")
    engines = [{"version": "5.6", "path": str(tmp_path / "gone"), "exists": False},
               {"version": "5.7", "path": str(engine), "exists": True}]
    project = {"id": "prj_a", "name": "게임", "path": str(folder), "engine_version": "5.7", "mcp_port": 8101}
    return engines, project, exe, folder


def test_command_line_is_the_engines_editor_and_the_uproject(disk, tmp_path):
    engines, project, exe, folder = disk
    assert editor.EDITOR_EXE == ("Engine", "Binaries", "Win64", "UnrealEditor.exe")
    assert editor.command_line(project, engines) == ([str(exe), str(folder / "Game.uproject")], folder)

    def reason(**change):
        with pytest.raises(editor.Refused) as e:
            editor.command_line({**project, **change}, engines)
        assert e.value.code == "precondition_failed" and e.value.message
        return e.value.failed

    assert reason(engine_version="5.1") == ["engine_unknown"] == reason(engine_version=None)
    assert reason(engine_version="5.6") == ["engine_missing"]
    empty = tmp_path / "Empty"
    empty.mkdir()
    assert reason(path=str(empty)) == ["uproject_missing"] == reason(path="")
    exe.unlink()
    assert reason() == ["editor_missing"]


def test_launch_starts_the_command_in_the_folder_and_end_process_ends_it(tmp_path):
    out = tmp_path / "ran.txt"
    code = "import os, sys, pathlib; pathlib.Path(sys.argv[1]).write_text(os.getcwd(), encoding='utf-8')"
    process = editor.launch([sys.executable, "-c", code, str(out)], tmp_path)
    assert process.wait(timeout=60) == 0 and os.path.samefile(out.read_text(encoding="utf-8"), tmp_path)
    sleeper = editor.launch([sys.executable, "-c", "import time; time.sleep(120)"], tmp_path)
    editor.end_process(sleeper.pid, True)
    assert sleeper.wait(timeout=30) != 0
    editor.end_process(sleeper.pid, True)  # already gone: never raises


# --- status · start · stop ------------------------------------------------------------------------------------
def test_status_is_running_pid_healthy_checked_at(disk):
    _, project, _, folder = disk
    mine = folder / "Game.uproject"
    projects = [project, {"id": "prj_b", "path": "E:/Other", "mcp_port": 8102}]
    asked = []

    def healthy(port):
        asked.append(port)
        return True

    off = editor.status(project, projects, processes=lambda: [OTHER], healthy=healthy)
    assert list(off) == STATUS_KEYS and (off["running"], off["pid"], off["healthy"]) == (False, None, False)
    assert asked == [] and datetime.fromisoformat(off["checked_at"])  # an editor that is off is not asked

    two = [row(30, mine), OTHER, row(10, str(mine).replace("\\", "/")), row(40, "D:/nobody/N.uproject")]
    on = editor.status(project, projects, processes=lambda: two, healthy=healthy)
    assert (on["running"], on["pid"], on["healthy"]) == (True, 10, True) and asked == [8101]  # asked once

    stuck = editor.status(project, projects, processes=lambda: two, healthy=lambda port: False)
    assert (stuck["running"], stuck["pid"], stuck["healthy"]) == (True, 10, False)
    portless = editor.status({**project, "mcp_port": None}, [], processes=lambda: two, healthy=healthy)
    assert (portless["running"], portless["healthy"]) == (True, False) and asked == [8101]  # no port to ask

    def broken(port):
        raise RuntimeError("검사 실패")

    assert editor.status(project, projects, processes=lambda: two, healthy=broken)["healthy"] is False


def test_start_runs_the_command_line_once_and_does_not_wait(disk):
    engines, project, exe, folder = disk
    running, spawned, clock = [OTHER], [], Clock()

    def spawn(argv, cwd):
        spawned.append((list(argv), cwd))
        running.append(row(777, argv[1]))

    def unexpected(port):
        raise AssertionError("an editor that was just started is not asked yet")

    tools = editor.Tools(spawn=spawn, sleep=clock.sleep, clock=clock.clock)
    first = editor.start(project, [project], engines, processes=lambda: list(running), healthy=unexpected,
                         tools=tools)
    assert spawned == [([str(exe), str(folder / "Game.uproject")], folder)] and clock.slept == []
    assert list(first) == STATUS_KEYS and (first["running"], first["pid"], first["healthy"]) == (True, 777, False)
    again = editor.start(project, [project], engines, processes=lambda: list(running), healthy=lambda port: True,
                         tools=tools)
    assert len(spawned) == 1 and (again["running"], again["pid"], again["healthy"]) == (True, 777, True)

    def no_exe(argv, cwd):
        raise FileNotFoundError(argv[0])

    with pytest.raises(editor.Refused) as e:
        editor.start(project, [project], engines, processes=lambda: [], tools=editor.Tools(spawn=no_exe))
    assert e.value.failed == ["editor_start_failed"]
    with pytest.raises(editor.Refused) as e:
        editor.start({**project, "engine_version": "4.27"}, [project], engines, processes=lambda: [], tools=tools)
    assert e.value.failed == ["engine_unknown"] and len(spawned) == 1


def test_stop_ends_only_this_projects_editors_gently_then_by_force(disk):
    _, project, _, folder = disk
    mine = folder / "Game.uproject"
    running, ended, clock = [row(10, mine), OTHER, row(30, mine)], [], Clock()

    def end(pid, force):
        ended.append((pid, force))
        if force or pid == 10:  # 30 does not close when asked: it waits at "save changes?"
            running[:] = [p for p in running if p["pid"] != pid]

    tools = editor.Tools(end=end, sleep=clock.sleep, clock=clock.clock, poll=2.0, stop_grace=6.0)
    state = editor.stop(project, [project], processes=lambda: list(running), healthy=lambda port: True, tools=tools)
    assert ended == [(10, False), (30, False), (30, True)] and clock.slept == [2.0, 2.0, 2.0]
    assert running == [OTHER] and (state["running"], state["pid"], state["healthy"]) == (False, None, False)

    ended.clear(), clock.slept.clear()
    state = editor.stop(project, [project], processes=lambda: list(running), tools=tools)  # none of this project
    assert ended == [] and clock.slept == [] and state["running"] is False and running == [OTHER]

    running.append(row(10, mine))
    ended.clear()
    editor.stop(project, [project], processes=lambda: list(running), tools=tools)  # closes when asked: no force
    assert ended == [(10, False)] and clock.slept == [] and running == [OTHER]


def test_stop_without_force_asks_waits_and_leaves_the_editor(disk):
    _, project, _, folder = disk
    running, ended, clock = [row(30, folder / "Game.uproject"), OTHER], [], Clock()
    tools = editor.Tools(end=lambda pid, force: ended.append((pid, force)), sleep=clock.sleep, clock=clock.clock,
                         poll=2.0, stop_grace=6.0)  # 30 never closes: it waits at "save changes?"
    state = editor.stop(project, [project], processes=lambda: list(running), healthy=lambda port: True, tools=tools,
                        force=False)
    assert ended == [(30, False)] and clock.slept == [2.0, 2.0, 2.0]  # asked once, waited stop_grace, not ended
    assert (state["running"], state["pid"], state["healthy"]) == (True, 30, True)
    assert editor.stop.__kwdefaults__["force"] is True  # left out: ended by force, as before


# --- screenshot (the function) ----------------------------------------------------------------------------------
def test_screenshot_refuses_and_leaves_no_folder(disk, tmp_path):
    engines, project, _, folder = disk
    root = tmp_path / "attachments"
    running = [row(10, folder / "Game.uproject")]

    def shoot(send, **change):
        clock = Clock()  # the waits are counted, not slept
        tools = editor.Tools(send=send, spawn=lambda *a: 1 / 0, sleep=clock.sleep, clock=clock.clock,
                             file_wait=1.0, file_poll=0.25)
        with pytest.raises(editor.Refused) as e:
            editor.screenshot({**project, **change}, [project], engines, root, processes=lambda: list(running),
                              healthy=lambda port: True, tools=tools)
        assert e.value.code == "precondition_failed" and (not root.exists() or list(root.iterdir()) == [])
        e.value.slept = clock.slept
        return e.value

    def unreachable(port, command, params, *, timeout):
        raise ConnectionRefusedError("닫힘")

    assert shoot(unreachable).failed == ["screenshot_failed"]
    said = shoot(lambda *a, **k: {"status": "error", "error": "뷰포트가 없습니다"})
    assert said.failed == ["screenshot_failed"] and "뷰포트가 없습니다" in said.message
    assert said.slept == []  # the editor was already on and answering: asked once, told at once
    assert shoot(lambda *a, **k: {"status": "success", "result": {"success": False, "message": "x"}}).failed == [
        "screenshot_failed"]
    none = shoot(lambda *a, **k: {"status": "success", "result": {}})  # no file
    assert none.failed == ["screenshot_missing"] and none.slept == [0.25] * 4  # looked for during file_wait
    assert shoot(lambda port, command, params, *, timeout: Path(params["filepath"]).write_bytes(b"") and {}
                 or {"status": "success"}).failed == ["screenshot_missing"]  # an empty one
    assert shoot(lambda *a, **k: 1 / 0, mcp_port=None).failed == ["mcp_port_missing"]

    asked = []

    def plugin(port, command, params, *, timeout):
        asked.append((port, command, list(params), timeout))
        Path(params["filepath"]).write_bytes(PNG)
        return {"status": "success", "result": {}}

    shot = editor.screenshot(project, [project], engines, root, processes=lambda: list(running),
                             healthy=lambda port: True, tools=editor.Tools(send=plugin, spawn=lambda *a: 1 / 0))
    assert asked == [(8101, "take_screenshot", ["filepath"], editor.SHOT_TIMEOUT)] and shot.read_bytes() == PNG
    assert shot.parent.parent == root and re.fullmatch(r"shot_[0-9a-f]{12}", shot.parent.name)
    assert re.fullmatch(r"viewport-\d{8}-\d{6}\.png", shot.name)


def test_screenshot_asks_again_only_an_editor_that_just_came_up(disk, tmp_path):
    engines, project, _, folder = disk
    root = tmp_path / "attachments"
    no_viewport = {"status": "error", "error": "뷰포트가 아직 없습니다"}
    taken = {"status": "success", "result": {}}

    def shoot(running, healthy, replies, ready_timeout=30.0):
        """-> (the file or the refusal, the paths the editor was given, the waits). The last reply repeats."""
        clock, asked = Clock(), []

        def send(port, command, params, *, timeout):
            asked.append(params["filepath"])
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
            if isinstance(reply, Exception):
                raise reply
            if reply is taken:
                Path(params["filepath"]).write_bytes(PNG)
            return reply

        tools = editor.Tools(spawn=lambda argv, cwd: running.append(row(777, argv[1])), send=send,
                             sleep=clock.sleep, clock=clock.clock, poll=2.0, ready_timeout=ready_timeout)
        try:
            got = editor.screenshot(project, [project], engines, root, processes=lambda: list(running),
                                    healthy=healthy, tools=tools)
        except editor.Refused as e:
            got = e
        return got, asked, clock.slept

    # started by this call: the port answers, the viewport comes two looks later
    shot, asked, slept = shoot([], lambda port: True, [no_viewport, ConnectionResetError("끊김"), taken])
    assert shot.read_bytes() == PNG and slept == [2.0, 2.0]
    assert len(asked) == 3 and set(asked) == {str(shot)}  # the same file every time

    # started by somebody else a moment ago: this call had to wait for health_check, so it is as new
    answers = iter([False, True])
    shot, asked, slept = shoot([row(10, folder / "Game.uproject")], lambda port: next(answers), [no_viewport, taken])
    assert shot.read_bytes() == PNG and len(asked) == 2 and slept == [2.0, 2.0]  # one wait for each

    # already on and answering: asked once — its error is the answer, told at once
    refusal, asked, slept = shoot([row(10, folder / "Game.uproject")], lambda port: True, [no_viewport, taken])
    assert refusal.failed == ["screenshot_failed"] and "뷰포트가 아직 없습니다" in refusal.message
    assert len(asked) == 1 and slept == []

    # started here and never ready: given up after ready_timeout, with what it said last
    refusal, asked, slept = shoot([], lambda port: True, [ConnectionResetError("끊김"), no_viewport],
                                  ready_timeout=4.0)
    assert refusal.failed == ["screenshot_failed"] and "뷰포트가 아직 없습니다" in refusal.message
    assert len(asked) == 3 and slept == [2.0, 2.0]
    assert len(list(root.iterdir())) == 2  # the two pictures: a refusal left no folder
    assert (editor.READY_TIMEOUT, editor.Tools().ready_timeout, editor.Tools().poll) == (30.0, 30.0, 2.0)


def test_screenshot_waits_for_a_file_written_after_the_answer(disk, tmp_path):
    engines, project, _, folder = disk
    root, running, asked = tmp_path / "attachments", [row(10, folder / "Game.uproject")], []

    def send(port, command, params, *, timeout):
        asked.append(Path(params["filepath"]))
        return {"status": "success", "result": {}}  # said before the file is there

    def shoot(on_sleep):
        clock = Clock()
        clock.on_sleep = on_sleep
        tools = editor.Tools(send=send, spawn=lambda *a: 1 / 0, sleep=clock.sleep, clock=clock.clock,
                             file_wait=1.0, file_poll=0.25)
        try:
            got = editor.screenshot(project, [project], engines, root, processes=lambda: list(running),
                                    healthy=lambda port: True, tools=tools)
        except editor.Refused as e:
            got = e
        return got, clock.slept

    # the plugin opens the file a frame later and fills it the frame after: an empty file is not the picture
    shot, slept = shoot(lambda n: asked[-1].write_bytes(b"" if n == 1 else PNG))
    assert shot == asked[0] and shot.read_bytes() == PNG and slept == [0.25, 0.25] and len(asked) == 1

    refusal, slept = shoot(None)  # never written
    assert refusal.failed == ["screenshot_missing"] and slept == [0.25] * 4 and len(asked) == 2  # not asked again
    assert [p.name for p in root.iterdir()] == [shot.parent.name]
    assert (editor.FILE_WAIT, editor.FILE_POLL) == (5.0, 0.2)
    assert (editor.Tools().file_wait, editor.Tools().file_poll) == (5.0, 0.2)


def test_old_shot_folders_are_removed_by_the_next_screenshot_and_nothing_else_is(disk, tmp_path):
    engines, project, _, folder = disk
    root = tmp_path / "attachments"
    assert editor.SHOT_KEEP == 24 * 3600 and editor.prune_shots(root) == [] and not root.exists()  # nothing made

    def aged(name, old=True, a_file=False):
        path = root / name
        if a_file:
            path.write_bytes(PNG)
        else:
            path.mkdir(parents=True)
            (path / "viewport-20260101-000000.png").write_bytes(PNG)
        if old:
            then = time.time() - editor.SHOT_KEEP - 60
            os.utime(path, (then, then))
        return name

    old_shot = aged("shot_0123456789ab")
    kept = [aged("shot_ba9876543210", old=False),  # a picture of today
            aged("12"), aged("inb_0123abcd"), aged("_staged"),  # a message's, an approval's, files on their way
            aged("shot_0123"), aged("shot_0123456789abc"), aged("shot_0123456789xy"),  # only named like one
            aged("shot_aaaaaaaaaaaa", a_file=True)]
    assert editor.prune_shots(root, keep=editor.SHOT_KEEP + 3600) == []  # not that old yet
    assert editor.prune_shots(root) == [old_shot]
    assert sorted(p.name for p in root.iterdir()) == sorted(kept)

    def plugin(port, command, params, *, timeout):
        Path(params["filepath"]).write_bytes(PNG)
        return {"status": "success", "result": {}}

    def shoot(**change):
        return editor.screenshot({**project, **change}, [project], engines, root,
                                 processes=lambda: [row(10, folder / "Game.uproject")], healthy=lambda port: True,
                                 tools=editor.Tools(send=plugin, spawn=lambda *a: 1 / 0))

    old_shot = aged("shot_cccccccccccc")
    with pytest.raises(editor.Refused):
        shoot(mcp_port=None)
    assert (root / old_shot).is_dir()  # refused before the editor was asked: the disk is not touched
    shot = shoot()
    assert sorted(p.name for p in root.iterdir()) == sorted([*kept, shot.parent.name])  # the old one went


# --- the four paths ---------------------------------------------------------------------------------------------
class Studio:
    """The server app on a studio DB of its own, one project on disk, and an editor that is off."""

    def __init__(self, server, disk, fake):
        engines, _, self.exe, self.folder = disk
        self.uproject = self.folder / "Game.uproject"
        self.fake = fake
        self.owner = TestClient(server.app, headers=AUTH)
        self.anyone = TestClient(server.app)
        self.store = api.get_store()
        self.project = self.store.create_project("게임", str(self.folder), "game", engine_version="5.7",
                                                 mcp_port=fake.port)
        self.pid = self.project["id"]
        self.url = f"/studio/projects/{self.pid}/editor"
        emp = self.store.create_employee("민수", "program", project_id=self.pid)
        self.room = self.store.create_room("dm", title="민수", employee_id=emp["id"])
        self.attachments = server.cfg.studio_dir / "attachments"
        self.running, self.spawned, self.ended = [OTHER], [], []
        self.stubborn = set()  # the pids that do not close when asked: they wait at "save changes?"
        self.clock = Clock()
        self.clock.on_sleep = lambda n: n == self.answers_after and self.fake.up()
        self.answers_after = 0  # the wait after which the started editor answers; 0: at once, None: never
        self.sources = machine.Sources(engines=lambda: {"5.7": engines[1]["path"]},
                                       processes=lambda: list(self.running),
                                       healthy=lambda port: machine.health_check(port, timeout=0.5))
        self.tools = editor.Tools(spawn=self.spawn, end=self.end, sleep=self.clock.sleep, clock=self.clock.clock,
                                  start_timeout=4.0, poll=2.0, stop_grace=6.0, shot_timeout=5.0,
                                  ready_timeout=4.0, file_wait=1.0, file_poll=0.25)

    def spawn(self, argv, cwd):
        self.spawned.append((list(argv), Path(cwd)))
        self.running.append({"pid": 777, "name": "UnrealEditor.exe", "cmdline": subprocess.list2cmdline(argv)})
        if self.answers_after == 0:
            self.fake.up()

    def end(self, pid, force):
        self.ended.append((pid, force))
        if force or pid not in self.stubborn:
            self.running[:] = [p for p in self.running if p["pid"] != pid]

    def shots(self):
        return sorted(p.name for p in self.attachments.iterdir()) if self.attachments.exists() else []

    def events(self, type):
        return [e for e in self.store.list_stream_events() if e["type"] == type]


@pytest.fixture
def studio(tmp_path, monkeypatch, disk, fake):
    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    api.close_store()
    world = Studio(server, disk, fake)
    monkeypatch.setattr(api, "_machine", world.sources)
    monkeypatch.setattr(api, "_editor", world.tools)
    yield world
    api.close_store()


def test_status_start_and_stop_by_path(studio):
    off = studio.owner.get(studio.url)
    assert off.status_code == 200 and list(off.json()) == STATUS_KEYS
    assert (off.json()["running"], off.json()["pid"], off.json()["healthy"]) == (False, None, False)

    started = studio.owner.post(studio.url + "/start")
    assert started.status_code == 200, started.text
    assert studio.spawned == [([str(studio.exe), str(studio.uproject)], studio.folder)]  # the real command line
    assert (started.json()["running"], started.json()["pid"], started.json()["healthy"]) == (True, 777, False)
    assert studio.clock.slept == []  # start does not wait for the editor
    on = studio.owner.get(studio.url).json()
    assert (on["running"], on["pid"], on["healthy"]) == (True, 777, True)  # the MCP port answered health_check
    assert studio.fake.requests == [{"type": "health_check", "params": {}}]
    assert studio.owner.post(studio.url + "/start").json()["healthy"] is True and len(studio.spawned) == 1
    editors = studio.owner.get("/studio/machine").json()["editors"]  # the diagnosis says the same
    assert [(e["pid"], e["project_id"], e["healthy"]) for e in editors] == [(50, None, False), (777, studio.pid, True)]

    stopped = studio.owner.post(studio.url + "/stop")
    assert stopped.status_code == 200 and stopped.json()["running"] is False and stopped.json()["pid"] is None
    assert studio.ended == [(777, False)] and studio.running == [OTHER]  # the other project's editor stays
    assert studio.owner.post(studio.url + "/stop").json()["running"] is False and len(studio.ended) == 1

    for call in (studio.owner.get, studio.owner.post):
        missing = call("/studio/projects/prj_none/editor" + ("" if call == studio.owner.get else "/start"))
        assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    assert studio.anyone.get(studio.url).status_code == 401
    assert studio.anyone.post(studio.url + "/stop").status_code == 401 and len(studio.ended) == 1


def test_stop_by_path_takes_force(studio):
    studio.running.append(row(777, studio.uproject))
    studio.stubborn.add(777)
    asked = studio.owner.post(studio.url + "/stop", json={"force": False})
    assert asked.status_code == 200, asked.text
    assert list(asked.json()) == STATUS_KEYS and (asked.json()["running"], asked.json()["pid"]) == (True, 777)
    assert studio.ended == [(777, False)] and studio.clock.slept == [2.0, 2.0, 2.0]  # waited stop_grace, no force
    for bad in ({"force": "maybe"}, {"hard": True}):
        assert studio.owner.post(studio.url + "/stop", json=bad).status_code == 422
    assert len(studio.ended) == 1 and len(studio.running) == 2

    forced = studio.owner.post(studio.url + "/stop", json={})  # `force` left out is true
    assert forced.status_code == 200 and forced.json()["running"] is False
    assert studio.ended[1:] == [(777, False), (777, True)] and studio.running == [OTHER]

    studio.running.append(row(777, studio.uproject))
    studio.ended.clear()
    gone = studio.owner.post(studio.url + "/stop")  # no body at all: as it was before `force`
    assert gone.status_code == 200 and gone.json()["running"] is False
    assert studio.ended == [(777, False), (777, True)] and studio.running == [OTHER]


def test_start_is_refused_when_the_engine_is_not_installed(studio):
    assert studio.owner.patch(f"/studio/projects/{studio.pid}", json={"engine_version": "5.1"}).status_code == 200
    refused(studio.owner.post(studio.url + "/start"), "engine_unknown")
    assert studio.spawned == []


def test_screenshot_with_the_editor_off_starts_it_waits_and_posts_the_picture(studio):
    studio.answers_after = 1  # loading: the port answers after one wait
    r = studio.owner.post(studio.url + "/screenshot", json={"room_id": studio.room["id"]})
    assert r.status_code == 201, r.text
    assert studio.spawned == [([str(studio.exe), str(studio.uproject)], studio.folder)]  # off -> started
    assert studio.clock.slept == [2.0]                                                    # -> waited for
    assert [q["type"] for q in studio.fake.requests] == ["health_check", "take_screenshot"]
    asked = Path(studio.fake.requests[1]["params"]["filepath"])  # where the server told the editor to write
    assert list(studio.fake.requests[1]["params"]) == ["filepath"] and asked.parent.parent == studio.attachments
    assert re.fullmatch(r"shot_[0-9a-f]{12}", asked.parent.name)

    body = r.json()
    assert list(body) == ["attachment", "message"]
    message, attachment = body["message"], body["attachment"]
    assert message["room_id"] == studio.room["id"] and message["author_kind"] == "system"
    assert message["meta"] == {"event": "editor.screenshot", "project_id": studio.pid} and "게임" in message["body"]
    assert message["attachments"] == [attachment]
    assert attachment == {"name": asked.name, "path": f"{message['id']}/{asked.name}", "mime": "image/png",
                          "size": len(PNG)}
    assert studio.shots() == [str(message["id"])]  # the `shot_` folder is gone: the message's folder has the file
    got = studio.owner.get(f"/studio/attachments/{attachment['path']}")
    assert got.status_code == 200 and got.content == PNG and got.headers["content-type"] == "image/png"
    assert [m["id"] for m in studio.store.list_messages(studio.room["id"])] == [message["id"]]
    assert len(studio.events("message.created")) == 1
    assert studio.ended == [] and len(studio.running) == 2  # the editor stays on


def test_screenshot_without_a_room_is_an_attachment_alone(studio):
    studio.running.append(row(777, studio.uproject))  # already on and answering: nothing is started
    studio.fake.up()
    r = studio.owner.post(studio.url + "/screenshot", json={})
    assert r.status_code == 201, r.text
    attachment = r.json()["attachment"]
    assert r.json()["message"] is None and studio.spawned == [] and studio.clock.slept == []
    assert re.fullmatch(r"shot_[0-9a-f]{12}/viewport-\d{8}-\d{6}\.png", attachment["path"])
    assert (attachment["name"], attachment["mime"], attachment["size"]) == (
        attachment["path"].split("/")[1], "image/png", len(PNG))
    got = studio.owner.get(f"/studio/attachments/{attachment['path']}")
    assert got.status_code == 200 and got.content == PNG
    assert studio.events("message.created") == [] and studio.store.list_messages(studio.room["id"]) == []

    assert studio.owner.post(studio.url + "/screenshot").status_code == 422           # the body is required
    assert studio.owner.post(studio.url + "/screenshot", json={"room": "x"}).status_code == 422
    assert studio.anyone.post(studio.url + "/screenshot", json={}).status_code == 401
    assert len(studio.shots()) == 1


def test_screenshot_of_a_project_without_mcp_port_is_precondition_failed(studio):
    assert studio.owner.patch(f"/studio/projects/{studio.pid}", json={"mcp_port": None}).status_code == 200
    body = refused(studio.owner.post(studio.url + "/screenshot", json={"room_id": studio.room["id"]}),
                   "mcp_port_missing")
    assert "mcp_port" in body["detail"]
    assert studio.spawned == [] and studio.fake.requests == [] and studio.shots() == []  # nothing was started
    assert studio.store.list_messages(studio.room["id"]) == []


def test_screenshot_gives_up_when_the_editor_does_not_answer_in_time(studio):
    studio.answers_after = None  # a modal window, a crash: the port never answers
    body = refused(studio.owner.post(studio.url + "/screenshot", json={"room_id": studio.room["id"]}),
                   "editor_timeout")
    assert "4초" in body["detail"]
    assert len(studio.spawned) == 1 and studio.clock.slept == [2.0, 2.0]  # started once, waited start_timeout
    assert studio.ended == [] and len(studio.running) == 2                # the editor is left as it is
    assert studio.fake.requests == [] and studio.shots() == []
    assert studio.store.list_messages(studio.room["id"]) == [] and studio.events("message.created") == []


def test_screenshot_for_a_room_that_is_not_there_starts_nothing(studio):
    r = studio.owner.post(studio.url + "/screenshot", json={"room_id": "room_none"})
    assert r.status_code == 404 and r.json()["code"] == "not_found" and studio.spawned == []
    r = studio.owner.post("/studio/projects/prj_none/editor/screenshot", json={})
    assert r.status_code == 404 and studio.spawned == [] and studio.shots() == []


def test_a_screenshot_the_editor_refuses_posts_nothing(studio):
    studio.fake.answer = lambda request: ({"status": "error", "error": "no active viewport"}
                                          if request["type"] == "take_screenshot" else {"status": "success"})
    body = refused(studio.owner.post(studio.url + "/screenshot", json={"room_id": studio.room["id"]}),
                   "screenshot_failed")
    assert "no active viewport" in body["detail"] and studio.shots() == []
    assert studio.store.list_messages(studio.room["id"]) == []
    # this request started the editor, so it was asked again until ready_timeout (4s, every 2s) ...
    assert [q["type"] for q in studio.fake.requests] == ["health_check"] + ["take_screenshot"] * 3
    assert studio.clock.slept == [2.0, 2.0]
    # ... and now that it is on and answering, the same refusal comes at once
    refused(studio.owner.post(studio.url + "/screenshot", json={}), "screenshot_failed")
    assert [q["type"] for q in studio.fake.requests].count("take_screenshot") == 4
    assert studio.clock.slept == [2.0, 2.0] and len(studio.spawned) == 1 and studio.shots() == []


# --- the documents ----------------------------------------------------------------------------------------------
def test_the_documents_say_what_was_settled():
    text = (DESIGN / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "| `relay_agent/studio/editor.py` |" in text  # §2.1
    assert "| `GET /studio/projects/{id}/editor` | — | `{running, pid, healthy, checked_at}` |" in text
    assert "확인 필요 (M-7)" not in text and "shot_<hex12>/viewport-" in text  # §6.6: where a screenshot goes
    assert "잠정 — `health_check` 는 M-7" not in text                          # §6.4: what `healthy` means
    for reason in editor.REASONS:
        assert f"`{reason}`" in text, reason
    assert "M-7 꺼진 상태에서 스크린샷 요청 → 이미지" in (DESIGN / "VERIFICATION.md").read_text(encoding="utf-8")
    paths = api.openapi_schema()["paths"]
    assert "get" in paths["/studio/projects/{id}/editor"]
    assert all("post" in paths[f"/studio/projects/{{id}}/editor/{what}"] for what in ("start", "stop", "screenshot"))
    schemas = api.openapi_schema()["components"]["schemas"]
    assert schemas["EditorShot"]["properties"].keys() == {"room_id"}
    assert schemas["EditorStop"]["properties"] == {"force": {"default": True, "title": "Force", "type": "boolean"}}
    stop = paths["/studio/projects/{id}/editor/stop"]["post"]
    assert not stop["requestBody"].get("required")  # `stop` without a body is what it was
    assert "| `stop`: `{force}` (선택, 기본 `true`) |" in text and "`force: false` 면 강제 종료를 하지 않는다" in text
    assert "지우는 규칙은 아직 없다" not in text and "24시간이 지난 것은 다음 스크린샷을 찍을 때 지우고" in text
    assert "첫 전송부터 30초 동안 2초 간격으로" in text and "0.2초 간격으로 5초까지" in text
    manual = (DESIGN / "VERIFICATION.md").read_text(encoding="utf-8")
    assert all(word in manual for word in ("READY_TIMEOUT", "FILE_WAIT", '{"force": false}'))
    assert api.ATTACHED_ID.fullmatch("shot_0123456789ab") and not api.ATTACHED_ID.fullmatch("shot_../x")
