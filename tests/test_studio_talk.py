"""Studio: employees talking in a task room (S-9; ARCHITECTURE.md §4.6 · §8.4 · §8.5 · §9.5 · §6.6). A mention in
`room_post` puts a turn in — the assignee's is the task's run, which is told the message; any other member's is a
read turn —, each is one more in `exchange_count`, and at the limit the system asks the owner and the run is cut.
The demo relay and the chat relay on the mock runner: the stages of the task's run wait at a gate the test opens,
the `chat` stage of a turn answers at once, doing what an employee would do there. No AI is called."""
import shutil
import sys
import threading
import traceback
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay_agent.runners import MockRunner
from relay_agent.studio import api, chat, runs, sat, tools
from test_studio_cutoff import bench  # noqa: F401 - the fixture `talk` stands on
from test_studio_inbox import ok
from test_studio_queue import Gate, line  # noqa: F401 - the fixture `bench` stands on
from test_studio_runs import DEMO, lab  # noqa: F401 - the fixture `line` stands on
from test_studio_tools import REVIEW, bearer
from test_studio_work import PLAIN
from test_studio_ws import wait

CHAT = PLAIN + "chat: {model: sonnet, effort: low}\n"
READS, WRITES = {"Read", "Grep", "Glob"}, {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"}


class Chatty(Gate):
    """Gate for the stages of a task's run. The `chat` stage — a read or chat turn — does not wait: it does what
    the test gave it (`act(call)`: what an employee does in its turn, returning what it answers) and ends."""

    def __init__(self):
        super().__init__()
        self.act = None

    def run(self, call):
        if call.stage != chat.STAGE:
            return super().run(call)
        answer = self.act(call) if self.act is not None else "알겠다"
        self.script.setdefault(chat.STAGE, []).append(
            {"summary": "답했다", "state": "동료와 이야기했다", "open_issues": [], "next_steps": [], "output": answer})
        return MockRunner.run(self, call)


class Talk:
    """`bench` — the line, a Cutoff on a clock the test moves, the MCP tools over HTTP — with the turns connected
    the way server.py connects them (`EngineHooks(turns=)`), on threads of their own, and the runner above.

    `replies` is what the turns to come write with `room_post`, `(text, mentions)` each, from their own token;
    a turn that finds none answers in its output. `seen` is what each turn was: the head of its run, the claims
    of its token, its tools and workspace. `posted` is what its `room_post` answered."""

    def __init__(self, line, bench, monkeypatch):  # noqa: F811
        self.line, self.bench, self.store, self.engine = line, bench, line.store, line.engine
        shutil.copy(DEMO.parent / chat.RELAY, line.relays / chat.RELAY)  # the relay folder of Shop has the demos only
        self.runner = line.gate = line.lab.runner = Chatty()
        self.runner.act = self.act
        # conftest.py turns `chat._thread` off: a turn of these tests runs on a thread this file starts, as on the server
        self.turns = chat.ChatTurns(line.engine, line.cfg, spawn=lambda work: threading.Thread(
            target=work, daemon=True, name="studio-chat").start())
        monkeypatch.setattr(api, "_hooks", runs.EngineHooks(line.engine, line.engine.advance,
                                                           chat=self.turns.request, turns=self.turns))
        self.side = TestClient(line.owner.app)  # the turn's own calls come from its thread
        self.replies, self.seen, self.posted, self.errors, self.probe, self.stuck = [], [], [], [], None, False

    def act(self, call):
        try:
            [(run_id, head)] = [(run_id, head) for run_id, head in api.live_runs(self.store).items()
                                if head["mode"] != "work" and run_id not in self.bench.turns]
            token = api.actor_token(run_id)
            self.seen.append({**head, "claims": sat.verify(token), "tools": set(call.tools or []),
                              "workspace": self.engine.load(run_id).workspace_mode, "prompt": call.prompt})
            if self.probe is not None:
                self.posted.append(self.side_call(self.probe[0], token, **self.probe[1]))
            if not self.replies:
                return "알겠다"
            text, mentions = self.replies.pop(0)
            self.posted.append(self.side_call("room_post", token, text=text, mentions=mentions))
            return ""
        except Exception as e:  # noqa: BLE001 - the engine would only fail the run: the test must see it
            self.errors.append(e)
            raise

    def side_call(self, tool, token, **args):
        r = self.side.post(f"/studio/mcp/{tool}", json=args, headers=bearer(token))
        assert r.status_code == 200, r.text
        return r.json()

    def call(self, tool, token, **args):
        return self.bench.call(tool, token, **args)

    def at_work(self):
        """A task whose run waits at the gate: (the task, its run, its assignee, its room)."""
        task, run_id = self.bench.at_work()
        return task, run_id, task["assignee_id"], self.store.list_rooms(kind="task", task_id=task["id"])[0]["id"]

    def token(self, task):
        return api.actor_token(self.line.row(task)["run_id"])

    def mate(self, room, text=CHAT):
        """Another employee of the project, with a folder, seated in that room the way the app seats one (§6.4)."""
        emp = self.line.hire(text)
        r = self.line.owner.post(f"/studio/rooms/{room}/members/{emp}")
        assert r.status_code == 200 and emp in [m["employee_id"] for m in r.json()["members"]], r.text
        return emp

    def rested(self):
        """No turn is running or owed any more, and none of them failed in what the test had it do. A turn that
        does not end fails the test with where its thread stands."""
        try:
            wait(lambda: not self.turns._busy and not self.turns._rooms, 30)  # the lines of rooms too (group turns)
        except AssertionError:
            self.stuck = True
            names = {thread.ident: thread.name for thread in threading.enumerate()}
            where = ["".join(traceback.format_stack(frame)[-14:]) for ident, frame in sys._current_frames().items()
                     if names.get(ident) == "studio-chat"]
            raise AssertionError(f"a turn did not end ({self.errors!r}):\n" + "\n---\n".join(where)) from None
        assert self.errors == []

    def told(self, run_id):
        """What that run was told while it went (`engine.interject`): the `user_interject` events."""
        return [event["detail"] for event in self.engine.history.events(run_id) if event["kind"] == "user_interject"]

    def said(self, room):
        return [(m["author_kind"], m["author_id"], m["body"]) for m in self.store.list_messages(room)]

    def chats(self):
        return [call for call in self.runner.calls if call.stage == chat.STAGE]


@pytest.fixture
def talk(line, bench, monkeypatch):  # noqa: F811
    made = Talk(line, bench, monkeypatch)
    yield made
    if not made.stuck:
        wait(lambda: not made.turns._busy, 30)  # a turn still at work ends before the store is closed


# --- the scenario of S-9: three rounds, then the review ---------------------------------------------------------
def test_two_employees_talk_three_rounds_and_the_task_goes_to_review(line, talk):  # noqa: F811
    task, run_id, me, room = talk.at_work()
    mate = talk.mate(room)
    for n in (1, 2, 3):
        talk.replies.append((f"답 {n}", [me]))
        out = talk.call("room_post", talk.token(task), text=f"질문 {n}", mentions=[mate])
        assert out["ok"] and out["queued"] == [mate] and not out["end_turn"], out
        talk.rested()
        back = talk.posted[-1]  # the mate's turn wrote back, and that went into the run that is at work
        assert back["ok"] and back["queued"] == [me] and not back["end_turn"], back
        assert line.row(task)["exchange_count"] == 2 * n  # the read turn, and what the run was told
    assert [said for said in talk.said(room) if said[0] == "employee"] == [
        ("employee", who, body) for n in (1, 2, 3) for who, body in ((me, f"질문 {n}"), (mate, f"답 {n}"))]
    told = talk.told(run_id)
    assert len(told) == 3 and all(f"답 {n}" in told[n - 1]["text"] for n in (1, 2, 3))
    assert all("owner 의 지시가 아니라" in note["text"] for note in told)  # the engine calls a note the user's
    assert len(talk.chats()) == 3 and {seen["employee_id"] for seen in talk.seen} == {mate}  # none for the assignee
    assert line.run_of(task).status == "running" and line.gate.now == 1  # the run went on all the while

    out = talk.call("task_request_review", talk.token(task), **REVIEW)  # T6, from the run itself
    assert out["ok"] and out["end_turn"], out
    line.gate.go.set()
    line.settled(task)
    row = line.row(task)
    assert (row["status"], row["exchange_count"], row["run_id"]) == ("review", 6, run_id)
    assert line.run_of(task).status == "done"
    [item] = line.store.list_inbox(task_id=task["id"], status="pending")
    assert (item["kind"], item["payload"]["exchange_count"]) == ("final_review", 6)
    plan = next(call for call in talk.runner.calls if call.stage == "plan")
    assert "답 3" in plan.prompt + plan.system  # what the mate said was handed to the stages that followed


# --- the limit (§4.6) ---------------------------------------------------------------------------------------------
def test_at_the_limit_the_system_asks_the_owner_the_run_is_cut_and_t5_counts_anew(line, talk, monkeypatch):  # noqa: F811
    monkeypatch.setattr(line.cfg, "studio_max_exchanges", 2)
    task, run_id, me, room = talk.at_work()
    mate = talk.mate(room)
    talk.replies.append(("답 1", [me]))
    assert talk.call("room_post", talk.token(task), text="질문 1", mentions=[mate])["queued"] == [mate]
    talk.rested()
    assert line.row(task)["exchange_count"] == 2
    before = talk.said(room)

    out = talk.call("room_post", talk.token(task), text="질문 2", mentions=[mate])  # the count is at the limit
    assert (out["ok"], out["code"], out["end_turn"]) == (False, "exchange_limit", True), out
    assert out["message"].endswith(tools.END_TURN) and "message_id" not in out
    item = line.store.get_inbox_item(out["inbox_id"])
    assert (item["kind"], item["status"], item["task_id"], item["run_id"]) == ("question", "pending", task["id"], run_id)
    assert item["payload"]["reason"] == "exchange_limit" and item["requester_kind"] == "system"
    assert [option["id"] for option in item["payload"]["options"]] == ["continue", "wrap_up"]
    row = line.row(task)
    assert (row["status"], row["exchange_count"]) == ("blocked", 2)  # T4 by the system; nothing was counted
    assert talk.said(room) == before and len(talk.chats()) == 1  # no message, no turn
    assert talk.bench.cutoff.waiting() == [task["id"]] and talk.bench.cancelled == []  # 기다림 = 런 종료: the wait

    again = talk.call("room_post", talk.token(task), text="질문 3", mentions=[mate])  # the run did not end its turn
    assert (again["code"], again["end_turn"], again["inbox_id"]) == ("exchange_limit", True, None)
    assert [handle.at for handle in talk.bench.clock.due] == [60.0]  # no second item, and the wait did not begin again
    limits = [event for event in line.events(task) if event["kind"] == "limit"]
    assert len(limits) == 2 and all(e["detail"] == {"run_id": run_id, "mentions": [mate]} for e in limits)
    assert [i["id"] for i in line.store.list_inbox(task_id=task["id"], status="pending")] == [item["id"]]

    talk.bench.clock.advance(60)
    assert talk.bench.cancelled == [run_id]  # the run was still at work: it is cut
    wait(lambda: line.q.idle(), 30)
    assert line.run_of(task).status == "cancelled" and line.row(task)["status"] == "blocked"

    done = ok(line.lab.decide(item, "answer", data={"option_id": "continue"}))  # T5
    assert (done["task"]["status"], done["task"]["exchange_count"]) == ("working", 0)
    wait(lambda: line.gate.now == 1)  # the same run goes on
    out = talk.call("room_post", talk.token(task), text="질문 4", mentions=[mate])
    assert out["ok"] and out["queued"] == [mate], out  # and the mentions count from 0 again
    talk.rested()
    assert line.row(task)["exchange_count"] == 1 and line.row(task)["run_id"] == run_id


def test_a_read_turn_that_reaches_the_limit_has_the_tasks_run_cut_at_once(line, talk, monkeypatch):  # noqa: F811
    monkeypatch.setattr(line.cfg, "studio_max_exchanges", 2)
    task, run_id, me, room = talk.at_work()
    mate, token = talk.bench.reader(task)  # a read turn of another member of the room
    line.store.update_task(task["id"], exchange_count=2)
    out = talk.call("room_post", token, text="이건 네가 봐야 한다", mentions=[me])
    assert (out["code"], out["end_turn"]) == ("exchange_limit", True) and out["inbox_id"], out
    assert talk.bench.cancelled == [run_id] and talk.bench.cutoff.waiting() == []  # not the task's run: no wait (§5.7)
    wait(lambda: line.q.idle(), 30)
    assert line.run_of(task).status == "cancelled" and line.row(task)["status"] == "blocked"
    assert talk.told(run_id) == [] and line.row(task)["exchange_count"] == 2


def test_counting_says_so_on_the_stream_and_leaves_no_task_event(line):  # noqa: F811
    task = line.backlog()
    before = len(line.store.list_stream_events(limit=10_000))
    row = line.m.count_exchange(task["id"], 2)
    assert row["exchange_count"] == 2 and line.row(task)["exchange_count"] == 2
    assert not [event for event in line.events(task) if event["kind"] == "limit"]  # a column, not a task_events row
    [event] = line.store.list_stream_events(limit=10_000)[before:]
    assert (event["type"], event["task_id"]) == ("task.updated", task["id"])
    with pytest.raises(ValueError):
        line.m.count_exchange(task["id"], 0)


def test_no_limit_is_looked_at_without_one(line, talk, monkeypatch):  # noqa: F811
    monkeypatch.setattr(line.cfg, "studio_max_exchanges", None)  # None = the count is kept and never looked at
    task, run_id, me, room = talk.at_work()
    mate = talk.mate(room)
    line.store.update_task(task["id"], exchange_count=999)  # far past any limit there could be
    out = talk.call("room_post", talk.token(task), text="아직 물어도 되나?", mentions=[mate])
    assert out["ok"] and out["queued"] == [mate], out
    talk.rested()
    row = line.row(task)
    assert (row["status"], row["exchange_count"], row["run_id"]) == ("working", 1000, run_id)  # counted all the same
    assert not [event for event in line.events(task) if event["kind"] == "limit"]
    assert line.store.list_inbox(task_id=task["id"], status="pending") == []  # the owner was asked nothing
    assert talk.bench.cutoff.waiting() == [] and talk.bench.cancelled == []
    assert talk.said(room)[-1] == ("employee", mate, "알겠다")


# --- who writes: the assignee's run alone (§8.5) -------------------------------------------------------------------
def test_the_turn_of_a_member_who_is_not_the_assignee_is_read_only(line, talk):  # noqa: F811
    task, run_id, me, room = talk.at_work()
    mate = talk.mate(room)
    talk.probe = ("task_progress", {"note": "내가 고쳤다"})  # what the turn tries besides answering
    out = talk.call("room_post", talk.token(task), text="이 함수는 어디서 쓰나?", mentions=[mate])
    assert out["ok"] and out["queued"] == [mate], out
    talk.rested()
    [seen] = talk.seen
    assert (seen["mode"], seen["employee_id"], seen["room_id"], seen["task_id"]) == ("read", mate, room, task["id"])
    assert seen["run_id"] != run_id and seen["project_id"] == task["project_id"]  # a run of its own, in the project
    assert (seen["claims"]["mode"], seen["claims"]["task"], seen["claims"]["emp"]) == ("read", task["id"], mate)
    assert seen["workspace"] == "none"  # no workspace: nothing it does is applied to the project
    assert READS <= seen["tools"] and not WRITES & seen["tools"]
    assert "읽기 전용 턴" in seen["prompt"] and me in seen["prompt"]  # told so, and whom to mention
    [tried] = talk.posted
    assert (tried["ok"], tried["code"]) == (False, "actor_not_allowed")  # the task's tools are the assignee's run's
    assert talk.said(room)[-1] == ("employee", mate, "알겠다")  # its answer stays in the room
    assert line.row(task)["exchange_count"] == 1 and talk.told(run_id) == []

    assert talk.turns.put(me, room) is False and len(talk.chats()) == 1  # the assignee's turn there is the run
    member = line.store.get_room_member(room, me)
    assert member["session_id"] == line.run_of(task).session_id  # its row is the task run's, untouched
    assert line.store.get_room_member(room, mate)["session_id"] not in (None, member["session_id"])


# --- who is in the room: the owner seats and takes out (§6.4) -------------------------------------------------------
def test_a_member_taken_out_of_the_room_is_not_mentioned_until_it_is_seated_again(line, talk):  # noqa: F811
    task, run_id, me, room = talk.at_work()
    mate = talk.mate(room)
    url = f"/studio/rooms/{room}/members/{mate}"
    assert [m["employee_id"] for m in ok(line.owner.delete(url))["members"]] == [me]
    before = talk.said(room)
    out = talk.call("room_post", talk.token(task), text="거기 있나?", mentions=[mate])
    assert (out["ok"], out["code"], out["failed"]) == (False, "precondition_failed", []), out  # not a member now
    assert talk.said(room) == before and line.row(task)["exchange_count"] == 0 and talk.chats() == []

    r = line.owner.delete(f"/studio/rooms/{room}/members/{me}")  # the assignee is not taken out: its run writes there
    assert (r.status_code, r.json()["code"]) == (409, "precondition_failed"), r.text
    assert {m["employee_id"] for m in ok(line.owner.post(url))["members"]} == {me, mate}
    out = talk.call("room_post", talk.token(task), text="다시 묻는다", mentions=[mate])
    assert out["ok"] and out["queued"] == [mate], out
    talk.rested()
    [seen] = talk.seen
    assert (seen["mode"], seen["employee_id"], seen["room_id"]) == ("read", mate, room)
    assert line.row(task)["exchange_count"] == 1 and line.row(task)["run_id"] == run_id


# --- the owner writes (§6.6) ----------------------------------------------------------------------------------------
def test_the_owners_message_in_a_task_room_goes_into_the_run_and_is_not_counted(line, talk):  # noqa: F811
    task, run_id, me, room = talk.at_work()
    mate = talk.mate(room)
    r = line.owner.post(f"/studio/rooms/{room}/messages", json={"body": "점프 높이는 300 으로 해라",
                                                                 "mentions": [mate, me]})
    assert r.status_code == 201, r.text
    talk.rested()
    [note] = talk.told(run_id)  # the run that is at work was told, as `/runs/{id}/interject` does
    assert "점프 높이는 300 으로 해라" in note["text"] and note["applied"] in ("live", "next_stage")
    [seen] = talk.seen  # the mate's read turn; the assignee's turn is the run
    assert (seen["mode"], seen["employee_id"]) == ("read", mate) and "owner: 점프 높이는 300 으로 해라" in seen["prompt"]
    assert talk.said(room)[-1] == ("employee", mate, "알겠다")
    assert line.row(task)["exchange_count"] == 0  # the owner's messages are no exchange (§4.6)


def test_the_owners_message_in_a_department_room_gives_the_members_a_chat_turn_as_in_a_group_room(line, talk):  # noqa: F811
    mate = line.hire(CHAT)
    rooms = line.store.list_rooms(kind="department", department_id="program")
    room = rooms[0]["id"] if rooms else line.store.create_room("department", title="프로그램",
                                                                department_id="program")["id"]
    if line.store.get_room_member(room, mate) is None:
        line.store.create_room_member(room, mate)

    def say(body, mentions):
        r = line.owner.post(f"/studio/rooms/{room}/messages", json={"body": body, "mentions": mentions})
        assert r.status_code == 201, r.text
        talk.rested()

    members = [m["employee_id"] for m in line.store.list_room_members(room_id=room)]
    say("다들 수고했다", [])
    assert sorted(s["employee_id"] for s in talk.seen) == sorted(members)  # nobody was mentioned: every member has a turn
    talk.seen.clear()
    say("이건 어떻게 보나?", [mate])
    [seen] = talk.seen
    assert (seen["mode"], seen["employee_id"], seen["room_id"], seen["task_id"]) == ("chat", mate, room, None)
    assert talk.said(room)[-1] == ("employee", mate, "알겠다")


# --- a turn that cannot be made (§9.5) ------------------------------------------------------------------------------
def test_a_mention_whose_turn_cannot_be_made_is_refused_or_said_in_the_room(line, talk, monkeypatch):  # noqa: F811
    task, run_id, me, room = talk.at_work()
    odd = talk.mate(room, PLAIN + "chat: {provider: nobody}\n")  # §9.5 의 5 (가): no Claude Code runner
    before = talk.said(room)
    out = talk.call("room_post", talk.token(task), text="봐 달라", mentions=[odd])
    assert (out["ok"], out["code"], out["failed"]) == (False, "precondition_failed", ["provider_unguarded"]), out
    assert talk.said(room) == before and line.row(task)["exchange_count"] == 0  # no message, nothing counted

    mate, create = talk.mate(room), line.engine.create

    def refusing(relay, *args, **kwargs):
        if Path(relay).name == chat.RELAY:
            raise ValueError("지금은 런을 만들 수 없다")
        return create(relay, *args, **kwargs)

    monkeypatch.setattr(line.engine, "create", refusing)  # the engine's refusal comes after the message
    out = talk.call("room_post", talk.token(task), text="봐 달라", mentions=[mate])
    assert out["ok"] and out["queued"] == [] and mate in out["message"], out
    talk.rested()
    last = [(m["author_kind"], (m["meta"] or {}).get("event")) for m in line.store.list_messages(room)][-2:]
    assert last == [("employee", None), ("system", "run_error")]  # the message stays, and the room is told why
    assert line.row(task)["exchange_count"] == 0 and talk.seen == []  # a turn that did not go in is not counted
