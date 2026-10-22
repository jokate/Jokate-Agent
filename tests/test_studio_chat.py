"""Studio chat turns (S-7, part 2): the owner writes in an employee's 1:1 room and the employee answers there —
the message, the run on the mock runner, the answer stored as the employee's message, the events of the stream;
the second turn resumes the conversation of the first, and a conversation that has grown long is handed over to
a new one (§3.7, §6.6, §7.4, §9.5, §11.2). No AI is called."""
import threading
from pathlib import Path

import pytest

from relay_agent.pipeline import RelayEngine
from relay_agent.projctx import discover
from relay_agent.repos import RepoRegistry
from relay_agent.runners import MockRunner
from relay_agent.studio import api, chat, employees, runs, sat, ws
from test_mcp_attach import setup as engine_setup
from test_studio_inbox import ok
from test_studio_runs import lab  # noqa: F401 - the fixture `desk` stands on
from test_studio_ws import connect, take, until, wait

YAML = "version: 1\ndepartment: program\nchat: {model: opus, effort: low}\n"
FILES = {"PERSONA.md": "나는 민수다. 짧게 답한다.\n", "GUIDE.md": "모르면 묻는다.\n", "MEMORY.md": "",
         "employee.yaml": YAML}


def script(*texts):
    """What the `chat` stage answers, in order. `state` is what a later conversation is handed."""
    return {"chat": [{"summary": "답했다", "state": f"요약: {text}", "open_issues": [], "next_steps": [],
                      "output": text} for text in texts]}


class Held(MockRunner):
    """Answers once the test lets it, so the turn is still running while the stream is read."""

    def __init__(self, texts):
        super().__init__(texts)
        self.go = threading.Event()

    def run(self, call):
        assert self.go.wait(20), "the test never let the runner answer"
        return super().run(call)


class Desk:
    """Lab — the server app, a studio DB and an engine on the mock runner — with the chat turns connected the way
    server.py connects them. A turn runs in place unless `spawn` is given: it has ended when the POST returns."""

    def __init__(self, lab, monkeypatch, spawn=None):  # noqa: F811
        self.lab, self.engine, self.store, self.owner, self.cfg = lab, lab.engine, lab.store, lab.owner, lab.server.cfg
        self.turns = chat.ChatTurns(lab.engine, self.cfg, spawn=spawn or (lambda work: work()))
        monkeypatch.setattr(api, "_hooks", runs.EngineHooks(lab.engine, lab.engine.advance, chat=self.turns.request))
        self.emp, self.dm = lab.world.emp, lab.world.dm  # 민수, placed in the project whose repo is `game`
        self.hire(self.emp)

    def hire(self, employee_id, **files):
        """Write that employee's folder: the four files, with the ones given in place of FILES."""
        folder = employees.folder(self.cfg.studio_dir, employee_id)
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in {**FILES, **files}.items():
            (folder / name).write_bytes(text.encode("utf-8"))
        return folder

    def answers(self, *texts):
        self.lab.runner = MockRunner(script(*texts))

    def say(self, body, room=None):
        r = self.owner.post(f"/studio/rooms/{room or self.dm}/messages", json={"body": body})
        assert r.status_code == 201, r.text
        return r.json()

    def said(self, room=None):
        return [(m["author_kind"], m["body"]) for m in self.store.list_messages(room or self.dm)]

    def last(self, room=None):
        """(author kind, meta.event, body) of the room's newest message."""
        message = self.store.list_messages(room or self.dm)[-1]
        return message["author_kind"], (message["meta"] or {}).get("event"), message["body"]

    def member(self, room=None, emp=None):
        return self.store.get_room_member(room or self.dm, emp or self.emp)

    def runs(self, room=None, emp=None):
        """The runs of that employee's turns in that room, oldest first: the turns of its session."""
        session = self.member(room, emp)["session_id"]
        return [self.engine.load(turn["run_id"]) for turn in self.engine.history.turns(session)] if session else []

    @property
    def calls(self):
        return self.lab.runner.calls


@pytest.fixture
def desk(lab, monkeypatch):  # noqa: F811
    return Desk(lab, monkeypatch)


# --- message -> answer -> the room -> the stream ------------------------------------------------------------------
def test_an_owners_message_in_a_dm_room_is_answered_there(desk):
    desk.answers("안녕하세요, 민수입니다")
    desk.engine.mcp_registry["studio"] = {"command": "py", "args": ["studio.py"]}
    desk.engine.mcp_env["studio"] = api.mcp_env  # as server.py registers it (§8.2)
    before = desk.store.get_employee(desk.emp)

    sent = desk.say("안녕?")
    assert desk.said() == [("owner", "안녕?"), ("employee", "안녕하세요, 민수입니다")]
    answer = desk.store.list_messages(desk.dm)[-1]
    [run] = desk.runs()
    assert (answer["author_id"], answer["run_id"]) == (desk.emp, run.id)
    assert (run.status, Path(run.relay).name, run.workspace_mode, run.approval) == (
        "done", "employee-chat.yaml", "none", "ai")
    assert run.repo == "game" and Path(run.workdir) == (desk.lab.tmp / "project").resolve()  # §9.5: the project
    assert run.mcp == ["studio"] and run.mcp_only is None and run.mcp_scope == "all"  # no mcp named: every server

    [call] = desk.calls
    assert (call.stage, call.model, call.effort) == ("chat", "opus", "low")  # the `chat` slot of employee.yaml
    assert call.mcp_servers == ["studio"] and not {"Edit", "Write", "Bash"} & set(call.tools)
    claims = sat.verify(call.mcp_overrides["studio"]["env"]["STUDIO_ACTOR_TOKEN"])  # the run was in the ledger
    assert (claims["run"], claims["emp"], claims["room"], claims["mode"]) == (run.id, desk.emp, desk.dm, "chat")
    assert call.resume_session is None and call.fresh_prompt is None
    assert call.prompt.index("owner: 안녕?") < call.prompt.index("나는 민수다") < call.prompt.index("모르면 묻는다")
    assert chat.HANDOFF_TITLE not in call.prompt  # nothing came before this conversation

    events = [e for e in desk.store.list_stream_events(limit=10_000) if e["type"] == "message.created"]
    assert [e["data"]["message"]["id"] for e in events] == [sent["id"], answer["id"]]
    assert (events[-1]["room_id"], events[-1]["project_id"]) == (desk.dm, desk.lab.world.pid)

    member = desk.member()
    assert (member["fed_message_id"], member["conversation_id"]) == (sent["id"], call.session_id)
    assert desk.engine.history.get_session(member["session_id"])["title"] == "민수 · 1:1"
    assert api.run_head(desk.store, run.id) is None  # the ledger lets go of a turn that has ended
    assert desk.store.get_employee(desk.emp) == before  # no status, no slot (§4.6 · §4.9)
    assert ok(desk.owner.get("/studio/inbox"))["items"] == []


def test_the_stream_shows_the_message_the_running_turn_and_the_answer(lab, monkeypatch):  # noqa: F811
    for name, value in (("PING_INTERVAL", 0.05), ("LIVE_INTERVAL", 0.05), ("LIVE_MIN_GAP", 0.01)):
        monkeypatch.setattr(ws, name, value)
    threads = []

    def spawn(work):  # a thread, as on the server: the POST returns while the turn runs
        threads.append(threading.Thread(target=work, daemon=True))
        threads[-1].start()

    desk = Desk(lab, monkeypatch, spawn)
    runner = lab.runner = Held(script("안녕하세요"))
    try:
        with connect(desk.owner) as sock:
            take(sock)
            sent = desk.say("안녕?")
            assert until(sock, lambda m: m["type"] == "message.created")["data"]["message"]["id"] == sent["id"]
            live = until(sock, lambda m: m["type"] == "run.live" and m["data"]["alive"])
            assert (live["data"]["mode"], live["data"]["employee_id"], live["data"]["room_id"]) == (
                "chat", desk.emp, desk.dm)
            assert (live["room_id"], live["project_id"], live["data"]["status"]) == (desk.dm, lab.world.pid, "running")
            assert desk.said() == [("owner", "안녕?")]

            runner.go.set()
            seen = []

            def ended(message):  # the answer and the run's last `run.live`, in whichever order they come
                seen.append(message)
                return (any(m["type"] == "message.created" for m in seen)
                        and any(m["type"] == "run.live" and not m["data"]["alive"] for m in seen))

            until(sock, ended)
            [created] = [m for m in seen if m["type"] == "message.created"]
            answer = created["data"]["message"]
            assert (answer["author_kind"], answer["author_id"], answer["body"]) == ("employee", desk.emp, "안녕하세요")
            assert answer["run_id"] == live["data"]["run_id"] and created["room_id"] == desk.dm
            stopped = [m["data"] for m in seen if m["type"] == "run.live" and not m["data"]["alive"]]
            assert (stopped[-1]["run_id"], stopped[-1]["mode"], stopped[-1]["status"]) == (answer["run_id"], "chat", "done")
    finally:
        runner.go.set()
        for thread in threads:
            thread.join(20)
        wait(lambda: len(ws.hub) == 0 and ws.hub._ticker is None)
        api._tracked.clear()
    assert desk.said() == [("owner", "안녕?"), ("employee", "안녕하세요")]


# --- the conversation: resumed, and handed over when it is long ----------------------------------------------------
def test_the_second_turn_resumes_the_conversation_of_the_first(desk):
    desk.answers("1.2미터가 좋겠습니다", "공중에서 한 번 더 뛰게 하죠")
    desk.say("점프 높이는 얼마가 좋을까?")
    second = desk.say("그럼 2단 점프는?")
    one, two = desk.calls
    assert one.resume_session is None and one.session_id
    assert (two.resume_session, two.session_id) == (one.session_id, None)  # what the runner gives --resume
    assert two.prompt.startswith("[재개]") and "owner: 그럼 2단 점프는?" in two.prompt
    assert "점프 높이는 얼마가" not in two.prompt and "나는 민수다" not in two.prompt  # only what is new
    # the whole goal waits in case the conversation cannot be resumed: the new message, the hand-over, the files
    marks = ["owner: 그럼 2단 점프는?", chat.HANDOFF_TITLE, "요약: 1.2미터가 좋겠습니다", "나는 민수다"]
    assert [two.fresh_prompt.index(mark) for mark in marks] == sorted(two.fresh_prompt.index(mark) for mark in marks)

    first_run, second_run = desk.runs()  # one session, a turn each
    assert "stage_resumed" in desk.lab.events(second_run) and "stage_resumed" not in desk.lab.events(first_run)
    assert (first_run.status, second_run.status) == ("done", "done")
    member = desk.member()
    assert (member["conversation_id"], member["fed_message_id"]) == (one.session_id, second["id"])
    assert desk.said() == [("owner", "점프 높이는 얼마가 좋을까?"), ("employee", "1.2미터가 좋겠습니다"),
                           ("owner", "그럼 2단 점프는?"), ("employee", "공중에서 한 번 더 뛰게 하죠")]


def test_a_conversation_that_has_grown_long_is_handed_over_to_a_new_one(desk, monkeypatch):
    desk.answers("첫 답", "둘째 답", "셋째 답")
    desk.say("하나")
    session = desk.member()["session_id"]
    assert desk.member()["conversation_id"] == desk.calls[0].session_id  # under the limit: kept

    monkeypatch.setattr(chat, "HANDOFF_TOKENS", 1)
    desk.say("둘")  # resumes once more, and ends over the limit
    assert desk.calls[1].resume_session == desk.calls[0].session_id
    member = desk.member()
    assert (member["conversation_id"], member["session_id"]) == (None, session)  # §3.7: emptied; the session stays

    monkeypatch.setattr(chat, "HANDOFF_TOKENS", 10 ** 9)
    third = desk.say("셋")
    three = desk.calls[2]
    assert three.resume_session is None and three.fresh_prompt is None
    assert three.session_id and three.session_id != desk.calls[0].session_id
    marks = ["owner: 셋", chat.HANDOFF_TITLE, "지금까지의 대화: 요약: 둘째 답", "PERSONA.md", "나는 민수다"]
    assert [three.prompt.index(mark) for mark in marks] == sorted(three.prompt.index(mark) for mark in marks)
    member = desk.member()
    assert (member["conversation_id"], member["session_id"], member["fed_message_id"]) == (
        three.session_id, session, third["id"])
    assert len(desk.runs()) == 3 and desk.said()[-1] == ("employee", "셋째 답")


def test_a_conversation_that_could_not_be_resumed_is_not_resumed_again(desk):
    class Lost(MockRunner):
        """The CLI does not have the conversation any more: the runner starts the stage anew and says so."""

        def run(self, call):
            if call.resume_session:
                call.emit("resume_fallback", {"reason": "이전 대화가 저장되지 않았음"})
            return super().run(call)

    desk.lab.runner = Lost(script("하나", "둘", "셋"))
    desk.say("첫 글")
    assert desk.member()["conversation_id"] == desk.calls[0].session_id
    desk.say("둘째 글")
    assert desk.calls[1].resume_session == desk.calls[0].session_id and desk.member()["conversation_id"] is None
    desk.say("셋째 글")
    assert desk.calls[2].resume_session is None and desk.member()["conversation_id"] == desk.calls[2].session_id


# --- what a turn is made with (§9.5) -----------------------------------------------------------------------------
def test_an_employee_placed_nowhere_works_in_its_own_folder_under_its_cap(desk):
    hr, dm = desk.lab.world.hr()
    folder = desk.hire(hr, **{"employee.yaml": "version: 1\ndepartment: hr\nmcp: [unreal, digest]\n"
                                               "chat_mcp: [unreal]\nchat: {model: opus, effort: high}\n"
                                               "cap: {model: sonnet, effort: medium}\n"})
    note = desk.store.create_message(dm, "system", body="채용 초안을 고쳐 달라")  # what a decision leaves in HR's room
    desk.answers("고쳐서 다시 올리겠습니다")
    api._hooks.chat_turn(hr, dm)  # ... before it asks for the turn (studio/inbox.py)

    [run] = desk.runs(dm, hr)
    assert run.repo is None and Path(run.workdir) == folder.resolve()
    assert run.mcp == ["unreal", "studio"] and run.mcp_only == ["unreal", "studio"]  # chat_mcp + studio
    assert run.mcp_scope == "only"
    assert (run.model_cap, run.effort_cap) == ("sonnet", "medium")
    [call] = desk.calls
    assert (call.model, call.effort, Path(call.cwd)) == ("sonnet", "medium", folder.resolve())  # lowered to the cap
    assert "시스템: 채용 초안을 고쳐 달라" in call.prompt
    assert desk.said(dm)[-1] == ("employee", "고쳐서 다시 올리겠습니다")
    assert desk.member(dm, hr)["fed_message_id"] == note["id"]

    api._hooks.chat_turn(hr, dm)  # nothing new but its own answer: no run
    assert len(desk.calls) == 1 and len(desk.runs(dm, hr)) == 1


def studio_on(desk):
    """The studio server in the engine's registry and its env callback, as server.py registers them (§8.2)."""
    desk.engine.mcp_registry["studio"] = {"command": "py", "args": ["studio.py"]}
    desk.engine.mcp_env["studio"] = api.mcp_env


def test_a_turn_in_a_folder_that_is_no_project_still_has_studio(desk, monkeypatch):
    home = desk.lab.tmp / "home"  # a PC whose user has no MCP server: nothing makes that folder a project
    home.mkdir()
    monkeypatch.setattr("relay_agent.projctx.Path.home", staticmethod(lambda: home))
    studio_on(desk)
    hr, dm = desk.lab.world.hr()
    folder = desk.hire(hr, **{"employee.yaml": YAML.replace("program", "hr")})
    assert discover(folder) is None  # no CLAUDE.md, `.claude/` or `.mcp.json` in it or above it (§9.5)
    desk.store.create_message(dm, "system", body="채용 초안을 고쳐 달라")
    desk.answers("고쳐서 다시 올리겠습니다")
    api._hooks.chat_turn(hr, dm)

    [run] = desk.runs(dm, hr)
    [call] = desk.calls
    assert run.repo is None and Path(run.workdir) == folder.resolve() and run.status == "done"
    assert desk.engine.project_context(run) is None and "project_context" not in desk.lab.events(run)
    assert call.mcp_servers == run.mcp == ["studio"] and "ToolSearch" in call.tools  # what the run keeps is attached
    claims = sat.verify(call.mcp_overrides["studio"]["env"]["STUDIO_ACTOR_TOKEN"])
    assert (claims["run"], claims["emp"], claims["room"], claims["mode"]) == (run.id, hr, dm, "chat")


def test_a_repo_without_project_context_gives_studio_to_its_employees_turn_alone(desk):
    desk.engine.repos = RepoRegistry({"game": {"path": str(desk.lab.tmp / "project"), "project_context": False}})
    studio_on(desk)
    desk.answers("안녕하세요")
    desk.say("안녕?")
    [run] = desk.runs()
    [call] = desk.calls
    assert run.repo == "game" and run.status == "done"
    assert desk.engine.project_context(run) is None and "project_context" not in desk.lab.events(run)
    assert call.mcp_servers == run.mcp == ["studio"] and "ToolSearch" in call.tools
    claims = sat.verify(call.mcp_overrides["studio"]["env"]["STUDIO_ACTOR_TOKEN"])
    assert (claims["run"], claims["emp"], claims["room"], claims["mode"]) == (run.id, desk.emp, desk.dm, "chat")

    desk.lab.runner = MockRunner()  # a run of that repo that is no employee's (dashboard · CLI): no MCP, as before
    other = desk.lab.run(repo="game")
    assert desk.calls and desk.engine.project_context(other) is None
    assert all(c.mcp_servers == [] and "studio" not in c.mcp_overrides for c in desk.calls)


def test_what_the_owner_attached_goes_with_the_turn(desk):
    with desk.store.transaction() as tx:
        message = desk.store.create_message(desk.dm, "owner", tx=tx, body="")
        desk.store.set_message_attachments(tx, message["id"], [
            {"name": "shot.txt", "path": f"{message['id']}/shot.txt", "mime": "text/plain", "size": 2}])
    file = Path(desk.cfg.studio_dir) / "attachments" / str(message["id"]) / "shot.txt"
    file.parent.mkdir(parents=True)
    file.write_text("hi", encoding="utf-8")
    api._hooks.chat_turn(desk.emp, desk.dm)
    [call], [run] = desk.calls, desk.runs()
    assert "owner: (첨부: shot.txt)" in call.prompt
    [copied] = run.baton.attachments
    assert Path(copied).read_text(encoding="utf-8") == "hi" and Path(call.add_dirs[0]) == Path(copied).parent


def test_a_turn_that_cannot_be_made_says_why_in_the_room(desk, monkeypatch):
    world, store = desk.lab.world, desk.store
    emp, dm = world.staff("지수")  # no folder yet: its employee.yaml cannot be read (§9.6)
    desk.say("안녕?", dm)
    assert desk.said(dm)[0] == ("owner", "안녕?")  # the message stays, and the POST was a 201
    assert desk.last(dm)[:2] == ("system", "run_error") and "yaml_invalid" in desk.last(dm)[2]

    desk.hire(emp, **{"employee.yaml": "version: 1\ndepartment: program\nchat: {provider: nobody}\n"})
    desk.say("다시", dm)  # §9.5 의 5 (가): no Claude Code runner
    assert desk.last(dm)[:2] == ("system", "run_error") and "provider_unguarded" in desk.last(dm)[2]
    member = store.get_room_member(dm, emp)
    assert (member["fed_message_id"], member["session_id"]) == (0, None)
    assert desk.calls == [] and desk.engine.list_runs() == []
    assert ok(desk.owner.get("/studio/inbox"))["items"] == []  # no inbox item for it

    ghost = store.create_project("다른 게임", "D:/other", "ghost")["id"]  # a repository the engine does not know
    lost = store.create_employee("유령", "program", project_id=ghost)["id"]
    lost_dm = store.create_room("dm", title="유령", employee_id=lost)["id"]
    store.create_room_member(lost_dm, lost)
    desk.hire(lost)
    desk.say("거기 있나?", lost_dm)
    assert desk.last(lost_dm)[:2] == ("system", "run_error") and "ghost" in desk.last(lost_dm)[2]

    desk.hire(emp)  # the file is right now, and the engine refuses the run
    real, refusing = desk.engine.create, [True]

    def create(*args, **kw):
        if refusing:
            raise ValueError("sonnet 는 effort 'max' 를 지원하지 않습니다")
        return real(*args, **kw)

    monkeypatch.setattr(desk.engine, "create", create)
    desk.say("또", dm)
    assert desk.last(dm)[:2] == ("system", "run_error") and "effort 'max'" in desk.last(dm)[2]
    assert desk.engine.list_runs() == [] and store.get_room_member(dm, emp)["fed_message_id"] == 0

    refusing.clear()
    desk.answers("이제 답합니다")
    desk.say("이제 되나?", dm)
    assert desk.last(dm) == ("employee", None, "이제 답합니다")
    [call] = desk.calls  # everything that went unanswered, without what the turns said about themselves
    assert call.prompt.index("owner: 안녕?\nowner: 다시\nowner: 또\nowner: 이제 되나?") < call.prompt.index("PERSONA.md")


def test_a_run_that_does_not_end_done_leaves_the_message_for_the_next_turn(desk, monkeypatch):
    desk.lab.runner = MockRunner({"chat": [{"summary": "못 했다", "state": "", "open_issues": [], "next_steps": [],
                                            "output": "", "verdict": "fail"}, *script("이제 답합니다")["chat"]]})
    heard, before = [], desk.engine.notifier  # (kind, was that run a tracked turn when the engine told of it)

    def hear(run_id, kind, title, detail):
        heard.append((kind, api.tracked_run(run_id) is not None))
        if before is not None:
            before(run_id, kind, title, detail)

    monkeypatch.setattr(desk.engine, "notifier", hear)
    first = desk.say("첫 글")
    [failed] = desk.runs()
    assert failed.status == "failed" and desk.last()[:2] == ("system", "run_error")
    # the engine tells of the failure while the turn is still tracked: what push.py goes by to leave that alert
    # out and have the notice of the room pushed alone (§5.9)
    assert ("run_failed", True) in heard and ("run_failed", False) not in heard
    assert api.tracked_run(failed.id) is None
    assert desk.store.list_messages(desk.dm)[-1]["meta"] == {"event": "run_error", "run_id": failed.id}
    assert (desk.member()["fed_message_id"], desk.member()["conversation_id"]) == (0, None)
    assert first["id"] > 0 and api.run_head(desk.store, failed.id) is None

    desk.say("둘째 글")
    assert desk.last() == ("employee", None, "이제 답합니다")
    assert "owner: 첫 글\nowner: 둘째 글" in desk.calls[1].prompt and desk.calls[1].resume_session is None


def test_a_done_turn_with_no_answer_says_so_and_is_not_read_again(desk):
    empty = {"summary": "끝났다", "state": "", "open_issues": [], "next_steps": [], "output": ""}
    desk.lab.runner = MockRunner({"chat": [empty, *script("이제 답합니다")["chat"]]})
    first = desk.say("첫 글")
    [run] = desk.runs()
    assert run.status == "done" and desk.last()[:2] == ("system", "run_error")
    assert desk.store.list_messages(desk.dm)[-1]["meta"] == {"event": "run_error", "run_id": run.id}
    assert [kind for kind, _ in desk.said()] == ["owner", "system"]  # no message of the employee
    member = desk.member()  # §3.7: read all the same — the message is in the conversation the next turn resumes
    assert (member["fed_message_id"], member["conversation_id"]) == (first["id"], desk.calls[0].session_id)

    desk.say("둘째 글")
    assert desk.last() == ("employee", None, "이제 답합니다")
    two = desk.calls[1]
    assert two.resume_session == desk.calls[0].session_id
    assert "owner: 둘째 글" in two.prompt and "첫 글" not in two.prompt  # not a second time
    assert "답이 비어 있다" not in two.prompt  # nor what the turn said about itself


def test_what_the_run_wrote_in_the_room_is_the_answer_of_a_turn_with_no_output(desk):
    desk.engine.mcp_registry["studio"] = {"command": "py", "args": ["studio.py"]}
    desk.engine.mcp_env["studio"] = api.mcp_env  # as server.py registers it (§8.2)

    class Posting(MockRunner):
        """Answers in the room itself, the way `room_post` stores it (§8.4), and leaves its output empty."""

        def run(self, call):
            run_id = sat.verify(call.mcp_overrides["studio"]["env"]["STUDIO_ACTOR_TOKEN"])["run"]
            desk.store.create_message(desk.dm, "employee", author_id=desk.emp, body="방에 직접 씁니다", run_id=run_id)
            return super().run(call)

    desk.lab.runner = Posting({"chat": [{"summary": "방에 썼다", "state": "", "open_issues": [], "next_steps": [],
                                         "output": ""}]})
    sent = desk.say("안녕?")
    [run] = desk.runs()
    assert run.status == "done"
    assert desk.said() == [("owner", "안녕?"), ("employee", "방에 직접 씁니다")]  # no `run_error`, no second answer
    assert desk.store.list_messages(desk.dm)[-1]["run_id"] == run.id
    member = desk.member()
    assert (member["fed_message_id"], member["conversation_id"]) == (sent["id"], desk.calls[0].session_id)


def test_a_request_while_the_turn_runs_is_one_more_turn_not_a_second_run(lab, monkeypatch):  # noqa: F811
    waiting = []
    desk = Desk(lab, monkeypatch, waiting.append)  # the turn runs when the test says so
    desk.answers("둘 다 읽었습니다", "셋째도 읽었습니다")
    desk.say("첫 글")
    desk.say("둘째 글")  # the turn of the first has not ended: no second thread
    assert len(waiting) == 1 and desk.calls == []
    waiting.pop()()  # one run reads both; the turn asked for meanwhile finds nothing new
    [call] = desk.calls
    assert "owner: 첫 글\nowner: 둘째 글" in call.prompt and len(desk.runs()) == 1
    desk.say("셋째 글")  # the room is free again
    assert len(waiting) == 1
    waiting.pop()()
    assert desk.said()[-1] == ("employee", "셋째도 읽었습니다") and desk.calls[1].resume_session == call.session_id


def test_hooks_without_a_chat_start_no_turn(lab):  # noqa: F811
    lab.world.store.create_message(lab.world.dm, "owner", body="안녕?")
    lab.hooks.chat_turn(lab.world.emp, lab.world.dm)  # Lab attaches without `chat`
    assert lab.runner.calls == [] and lab.engine.list_runs() == []
    turns = lab.server.chat_turns  # what server.py gives attach(): on its own engine, on a thread of its own
    assert isinstance(turns, chat.ChatTurns) and turns.spawn is None and turns.relay.is_file()


# --- the engine: `mcp_only` (§9.5 · §11.2) -----------------------------------------------------------------------
def test_mcp_only_keeps_the_servers_it_names_and_drops_the_projects_others(tmp_path, monkeypatch):
    engine, relay, runner, _ = engine_setup(tmp_path, monkeypatch)
    plain = engine.create(relay, "g", repo="game")
    engine.advance(plain.id)
    assert runner.seen[0].mcp_servers == ["proj"] and plain.mcp_only is None  # `mcp=[]` alone would not drop it

    none = engine.create(relay, "g", repo="game", mcp=[], mcp_only=[])
    engine.advance(none.id)
    assert runner.seen[1].mcp_servers == [] and "proj" not in runner.seen[1].mcp_overrides
    assert engine.load(none.id).mcp_only == []  # kept with the run: a resume narrows the same way

    kept = engine.create(relay, "g", repo="game", mcp_only=["proj"])
    engine.advance(kept.id)
    assert runner.seen[2].mcp_servers == ["proj"]

    folder = tmp_path / "plain"
    folder.mkdir()
    assert RelayEngine._run_mcp_names(folder, None, ["unreal", "studio"], ["studio"]) == ["studio"]
    assert RelayEngine._run_mcp_names(folder, None, ["unreal", "studio"])[:2] == ["unreal", "studio"]


# --- the harness section of a conversation's first prompt (§9.1) ---------------------------------------------------
def test_the_first_turn_carries_the_harness_section_and_a_resumed_one_does_not(desk, monkeypatch):
    from test_studio_harness_root import fake_harness
    monkeypatch.setattr(desk.cfg, "studio_harness_root", str(fake_harness(desk.lab.tmp)))
    desk.answers("네", "알겠습니다", "좋습니다")
    desk.say("안녕?")
    desk.say("그럼?")
    one, two = desk.calls
    assert "===== 하네스 =====\n행동하기 전에 읽는다." in one.prompt and "- a — a 을 할 때" in one.prompt
    assert "하네스" not in two.prompt  # the session holds it already
    monkeypatch.setattr(desk.cfg, "studio_harness_root", "")
    desk.say("다시?")
    three = desk.calls[2]
    assert "하네스" not in three.prompt  # a resumed turn sends no goal, so clearing the setting changes nothing
    assert three.resume_session == one.session_id
    assert desk.said()[-1] == ("employee", "좋습니다")


def test_an_empty_setting_without_a_junction_gives_a_first_turn_without_the_section(desk, monkeypatch):
    monkeypatch.setattr(desk.cfg, "studio_harness_root", "")  # conftest's default_home is empty: no junction either
    desk.answers("네")
    desk.say("안녕?")
    [call] = desk.calls
    assert "===== 하네스 =====" not in call.prompt
    assert "나는 민수다" in call.prompt
