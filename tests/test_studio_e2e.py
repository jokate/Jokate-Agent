"""The first half of the mock end-to-end scenario (V-1): pairing -> importing a UE project -> an HR interview that
ends in a hire -> the chat with the employee -> a task the employee proposes -> its approval -> the assignment and
the start request -> the run of the task (a slot of the queue, the mock runner held at a gate) -> a group room
held at its exchange limit -> the review request -> a rework (a second round of the run) -> the pass -> a follow-up
task with the finished one as its parent. Every request is the paired phone's (a device token); the engine is the
mock runner, the pushes go to a fake FCM (`push.httpx.post`). No AI, no editor, no port, nothing of this PC's
runs/studio/.

At the end of each step the same things are checked, each with a message that names the step: the inbox (count and
kinds), the task states, the messages of the room, the order of the WS events against the stored stream, the
trail the server keeps (stream events, task events) and, from the run on, the pushes the fake FCM got."""
import hashlib
import threading

import pytest
from fastapi.testclient import TestClient

from relay_agent.repos import RepoRegistry
from relay_agent.runners import MockRunner
from relay_agent.studio import api, employees, machine, push, runs, ws
from relay_agent.studio.inbox import GREETING
from test_studio_chat import script
from test_studio_hr import YAML, Acting, Office, draft, office  # noqa: F401, F811
from test_studio_importer import fake_project
from test_studio_inbox import TEXT, ok, refused
from test_studio_push import account, http, key_file, key_files_are_read  # noqa: F401 - fixtures (the last: autouse)
from test_studio_queue import Line, line  # noqa: F401 - the fixture of the small test at the end
from test_studio_runs import lab  # noqa: F401 - the fixture `office` stands on
from test_studio_tools import bearer
from test_studio_ws import connect, take, wait

PHONE = ("192.168.0.7", 50000)  # another device on the network, not this PC
HIRE_YAML = YAML + "relay: demo\n"  # the hired employee's file names the relay; the task proposal does not


class Phone:
    """The paired phone: REST with its device token, and the stream it keeps open."""

    def __init__(self, office, token, sock):
        self.office, self.sock, self.store = office, sock, office.store
        self.http = TestClient(office.lab.owner.app, client=PHONE, headers={"Authorization": f"Bearer {token}"})
        self.cursor = 0

    def say(self, room, body, step):
        r = self.http.post(f"/studio/rooms/{room}/messages", json={"body": body})
        assert r.status_code == 201, f"[{step}] 메시지 전송: {r.status_code} {r.text}"
        return r.json()

    def inbox(self, step):
        r = self.http.get("/studio/inbox")
        assert r.status_code == 200, f"[{step}] 결재함: {r.text}"
        return r.json()

    def decide(self, item, decision, step, **body):
        r = self.http.post(f"/studio/inbox/{item['id']}/decide", json={"decision": decision, **body})
        assert r.status_code == 200, f"[{step}] 결정 {decision}: {r.status_code} {r.text}"
        return r.json()

    def stored(self):
        return self.store.list_stream_events(after_id=self.cursor, limit=10_000)

    def sync(self, step, expect):
        """What was stored since the last sync reaches the socket in the same order (§7.3); `expect` are the
        types that have to be among them, in that order. Moves the cursor."""
        rows = self.stored()
        types = [row["type"] for row in rows]
        it = iter(types)
        assert all(any(t == want for t in it) for want in expect), f"[{step}] 저장된 이벤트 {types} 에 {expect} 순서가 없다"
        got = []
        if rows:
            last = rows[-1]["id"]
            for _ in range(5000):
                message = self.sock.receive_json()
                if message["type"] in ("ping", "run.live"):
                    continue
                got.append(message)
                if message["id"] >= last:
                    break
            else:
                raise AssertionError(f"[{step}] WS 가 id {last} 까지 오지 않았다: {[m['type'] for m in got]}")
        assert [m["id"] for m in got] == [row["id"] for row in rows], f"[{step}] WS 이벤트 id 순서가 저장된 순서와 다르다"
        assert [m["type"] for m in got] == types, f"[{step}] WS 이벤트 type 순서가 저장된 순서와 다르다"
        if rows:
            self.cursor = rows[-1]["id"]
        return types


def play(texts, *acts):
    """The runner of the next turn. The engine keeps the runner it built for a name, so a new one is only taken
    when that is forgotten."""
    OFFICE.play(texts, *acts)
    OFFICE.lab.engine._runners.clear()


class Crew(Acting):
    """The one runner of the second half: the `chat` stage is a turn that does what `say` was given (as Acting
    does); any other stage is a stage of a task's run — it waits at `gate` (or until its run is cancelled), then
    does the hook of `on_stage` for it, and answers by the mock. `now` is how many stages are at work, `stages`
    the stages that were run, in order. What a hook raises is kept in `errors`, with what a turn's act raised."""

    def __init__(self):
        super().__init__([])
        self.gate = threading.Event()
        self.gate.set()
        self._count = threading.Lock()
        self.now, self.stages, self.on_stage = 0, [], {}

    def say(self, texts, *acts):
        self.script["chat"] = script(*texts)["chat"]
        self.acts = list(acts)

    def run(self, call):
        if call.stage == "chat":
            return super().run(call)
        with self._count:
            self.now += 1
            self.stages.append(call.stage)
        try:
            for _ in range(3000):
                if self.gate.wait(0.01) or (call.cancel_event is not None and call.cancel_event.is_set()):
                    break
            else:
                raise AssertionError("the test never opened the gate")
            hook = self.on_stage.get(call.stage)
            if hook is not None and not (call.cancel_event is not None and call.cancel_event.is_set()):
                try:
                    hook(call)
                except Exception as e:  # noqa: BLE001 - an assert of the test: the engine must not take it for the run's
                    self.errors.append(e)
            return MockRunner.run(self, call)
        finally:
            with self._count:
                self.now -= 1


OFFICE = None


@pytest.fixture
def world(office, monkeypatch, tmp_path):  # noqa: F811
    lab = office.lab
    monkeypatch.setattr(ws, "PING_INTERVAL", 0.05)
    monkeypatch.setattr(ws, "LIVE_INTERVAL", 0.05)
    monkeypatch.setattr(ws, "LIVE_MIN_GAP", 0.01)
    monkeypatch.setattr(api, "_machine", machine.Sources(engines=lambda: {}))  # not this PC's engine registry
    monkeypatch.setattr(api, "_pairings", api.devices.Pairings())
    monkeypatch.setattr(api, "_hooks", runs.EngineHooks(lab.engine, lab.engine.advance, chat=office.desk.turns.request,
                                                       turns=office.desk.turns))  # the group rooms' turns (server.py)
    (tmp_path / "ue").mkdir()
    lab.engine.repos = RepoRegistry({"game": {"path": str(tmp_path / "project")}, "ue": {"path": str(tmp_path / "ue")}})
    global OFFICE
    OFFICE = office
    yield office
    OFFICE = None
    crew, line, pusher = (getattr(office, name, None) for name in ("crew", "line", "pusher"))
    if crew is not None:
        crew.gate.set()  # whatever is still at work ends before the store is closed
    if line is not None:
        wait(lambda: line.q.idle(), 30)
    if pusher is not None:
        pusher.drain()
        pusher.stop()  # the one thread this test starts besides the slots of the queue
    wait(lambda: len(ws.hub) == 0 and ws.hub._ticker is None)
    api._tracked.clear()


def test_pairing_to_the_follow_up_task(world, tmp_path, monkeypatch, key_file, http):  # noqa: F811
    office, lab, store = world, world.lab, world.store

    # --- 1. pairing ---------------------------------------------------------------------------------------
    step = "단계1 페어링"
    r = lab.owner.post("/studio/devices/pairing")
    assert r.status_code == 200, f"[{step}] 코드 발급: {r.text}"
    pairing = r.json()
    assert pairing["code"] and pairing["expires_at"] and pairing["qr"]["code"] == pairing["code"], f"[{step}] 코드·QR"
    anonymous = TestClient(lab.owner.app, client=PHONE)
    body = {"code": pairing["code"], "name": "폰", "platform": "android"}
    r = anonymous.post("/studio/devices/pair", json=body)
    assert r.status_code == 201, f"[{step}] 코드 교환: {r.status_code} {r.text}"
    token = r.json()["token"]
    assert token.startswith("sdt_"), f"[{step}] 기기 토큰 형식"
    assert r.json()["device"]["name"] == "폰", f"[{step}] 기기 이름"
    refused(anonymous.post("/studio/devices/pair", json=body), 400, "pairing_invalid")  # one use only
    devices = store.list_devices()
    assert [d["name"] for d in devices] == ["폰"], f"[{step}] 기기 행"
    assert devices[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest(), f"[{step}] 토큰은 해시로만 저장"
    headers = {"Authorization": f"Bearer {token}"}
    with connect(TestClient(lab.owner.app, client=PHONE), headers=headers) as sock:
        hello = take(sock)[0]
        assert hello["type"] == "hello", f"[{step}] WS hello"
        phone = Phone(office, token, sock)
        phone.cursor = hello["last_id"]
        assert [d["name"] for d in phone.http.get("/studio/devices").json()["items"]] == ["폰"], f"[{step}] 기기 목록"
        assert phone.inbox(step)["pending_count"] == 0, f"[{step}] 결재함은 비어 있다"
        phone.sync(step, [])
        session = [m["id"] for m in store.list_messages(office.room)]

        # --- 2. importing a UE project --------------------------------------------------------------------
        step = "단계2 프로젝트 가져오기"
        folder = fake_project(tmp_path / "ue", name="Fake", ini=None)
        r = phone.http.post("/studio/projects", json={"path": str(folder)})
        assert r.status_code == 201, f"[{step}] 가져오기: {r.status_code} {r.text}"
        project = r.json()
        assert project["detected"]["harness"] == {"claude_md": True, "docs": True}, f"[{step}] harness"
        assert project["detected"]["uproject"] == "Fake.uproject" and project["repo"] == "ue", f"[{step}] uproject·repo"
        pid = project["id"]
        assert store.get_project(pid)["path"] == str(folder.resolve()), f"[{step}] 프로젝트 행"
        assert pid in [p["id"] for p in phone.http.get("/studio/projects").json()["items"]], f"[{step}] 프로젝트 목록"
        assert len(office.waiting) == 1, f"[{step}] 정책 면담 턴이 하나 대기한다"
        notes = [m for m in store.list_messages(office.room) if m["id"] not in session]
        assert [(m["author_kind"], (m["meta"] or {}).get("event")) for m in notes] == [("system", "policy_interview")], \
            f"[{step}] HR 방의 정책 면담 시작 글"
        play(["정책 확인했습니다"])
        office.turn()
        assert office.note()["author_kind"] == "employee" and office.note()["body"] == "정책 확인했습니다", \
            f"[{step}] HR 의 답이 방에 올라온다"
        assert phone.inbox(step)["pending_count"] == 0, f"[{step}] 결재함 수"
        types = phone.sync(step, ["project.updated", "message.created", "message.created"])
        assert types[0] == "project.updated", f"[{step}] 첫 이벤트는 project.updated: {types}"

        # --- 3. the interview that ends in a hire ----------------------------------------------------------
        step = "단계3 채용"
        filed = {}

        def interview(call):
            made = office.tool(call, "employee_draft", **draft(project_id=pid, employee_yaml=HIRE_YAML))
            filed.update(made, **office.tool(call, "employee_submit", draft_id=made["draft_id"], reason="프로그래머 충원"))

        play(["채용 결재를 올렸습니다"], interview)
        phone.say(office.room, "프로그래머 한 명 뽑자", step)
        office.turn()
        assert filed.get("ok"),f"[{step}] 도구 결과: {filed}"
        listed = phone.inbox(step)
        assert listed["pending_count"] == 1 and [i["kind"] for i in listed["items"]] == ["hire"], \
            f"[{step}] 결재함: {listed['pending_count']}건 {[i['kind'] for i in listed['items']]}"
        [item] = listed["items"]
        assert item["id"] == filed["inbox_id"] and item["requester_id"] == office.hr, f"[{step}] 결재 요청자"
        assert office.desk.said(office.room)[-2:] == [("owner", "프로그래머 한 명 뽑자"), ("employee", "채용 결재를 올렸습니다")], \
            f"[{step}] HR 방의 글"
        assert [e["name"] for e in store.list_employees() if e["id"] != office.hr] == ["민수"], f"[{step}] 승인 전엔 직원이 늘지 않는다"
        types = phone.sync(step, ["message.created", "inbox.created", "message.created"])
        assert "inbox.created" in types, f"[{step}] inbox.created 가 이벤트에 있다: {types}"

        phone.decide(item, "approve", step)
        [hired] = [e for e in store.list_employees() if e["name"] == "하늘"]
        assert (hired["project_id"], hired["department_id"]) == (pid, "program"), f"[{step}] 직원이 프로젝트에 배치된다"
        assert phone.inbox(step)["pending_count"] == 0, f"[{step}] 승인 뒤 결재함은 비었다"
        decided = store.get_inbox_item(item["id"])
        assert (decided["status"], decided["decision"]) == ("decided", "approve"), f"[{step}] 결재 상태"
        [dm] = store.list_rooms(kind="dm", employee_id=hired["id"])
        assert office.desk.said(dm["id"]) == [("employee", GREETING.replace("{name}", "하늘"))], f"[{step}] 첫 인사"
        types = phone.sync(step, ["inbox.decided", "employee.updated"])
        assert "inbox.decided" in types, f"[{step}] inbox.decided: {types}"

        # --- 4. chatting with the employee -----------------------------------------------------------------
        step = "단계4 직원 채팅"
        emp, room = hired["id"], dm["id"]
        office.desk.hire(emp)
        play(["안녕하세요, 하늘입니다"])
        phone.say(room, "안녕 하늘", step)
        office.turn()
        assert office.desk.said(room)[-2:] == [("owner", "안녕 하늘"), ("employee", "안녕하세요, 하늘입니다")], \
            f"[{step}] 방의 글"
        [run] = office.desk.runs(room, emp)
        assert run.status == "done", f"[{step}] 직원 턴의 런 상태: {run.status}"
        assert phone.inbox(step)["pending_count"] == 0, f"[{step}] 결재함 수"
        types = phone.sync(step, ["message.created", "message.created"])
        assert types.count("message.created") >= 2, f"[{step}] message.created 두 번: {types}"

        # --- 5. a proposed task -> approved -> assigned -> queued -------------------------------------------
        step = "단계5 작업 제안"
        proposed = {}
        play(["작업을 제안했습니다"], lambda call: proposed.update(office.tool(call, "task_propose", **TEXT)))
        phone.say(room, "할 일 하나 제안해 봐", step)
        office.turn()
        assert proposed.get("ok"), f"[{step}] 도구 결과: {proposed}"
        task_id = proposed["task_id"]
        assert store.get_task(task_id)["status"] == "proposed", f"[{step}] 제안된 작업은 proposed"
        listed = phone.inbox(step)
        assert [i["kind"] for i in listed["items"]] == ["task_proposal"], f"[{step}] 결재함 종류: {listed['items']}"
        [item] = listed["items"]
        assert (item["id"], item["task_id"], item["requester_id"]) == (proposed["inbox_id"], task_id, emp), f"[{step}] 결재 항목"
        assert office.desk.said(room)[-1] == ("employee", "작업을 제안했습니다"), f"[{step}] 방의 글"
        assert [e["kind"] for e in store.list_task_events(task_id)] == ["created"], f"[{step}] 작업 이벤트"
        types = phone.sync(step, ["task.created", "inbox.created"])
        assert "task.created" in types and "inbox.created" in types, f"[{step}] 스트림: {types}"

        step = "단계5 작업 승인"
        phone.decide(item, "approve", step)
        assert store.get_task(task_id)["status"] == "backlog", f"[{step}] 승인하면 backlog"
        assert phone.inbox(step)["pending_count"] == 0, f"[{step}] 결재함 수"
        types = phone.sync(step, ["inbox.decided", "task.transitioned"])
        kinds = [e["kind"] for e in store.list_task_events(task_id)]
        assert kinds == ["created", "transition"], f"[{step}] 작업 이벤트: {kinds}"
        assert types[:2] == ["inbox.decided", "task.transitioned"], f"[{step}] 스트림: {types}"

        step = "단계5 배치·큐 진입"
        r = phone.http.post(f"/studio/tasks/{task_id}/assign", json={"assignee_id": emp})
        assert r.status_code == 200 and r.json()["assignee_id"] == emp, f"[{step}] 배치: {r.status_code} {r.text}"
        r = phone.http.post(f"/studio/tasks/{task_id}/start", json={})
        assert r.status_code == 200, f"[{step}] 시작 요청: {r.status_code} {r.text}"
        row = store.get_task(task_id)
        assert row["status"] == "backlog" and row["queued_at"] is not None, f"[{step}] backlog 에서 줄을 선다: {row}"
        assert phone.inbox(step)["pending_count"] == 0, f"[{step}] 결재함 수"
        types = phone.sync(step, ["task.updated"])
        assert types == ["task.updated", "task.updated"], f"[{step}] 스트림: {types}"
        kinds = [e["kind"] for e in store.list_task_events(task_id)]
        assert kinds == ["created", "transition", "assigned", "queued"], f"[{step}] 작업 이벤트(감사): {kinds}"

        # --- 6. the run: a slot of the queue, the mock runner held at a gate ------------------------------------
        step = "단계6 작업 런"
        crew = office.crew = Crew()
        office.runner = lab.runner = crew
        lab.engine._runners.clear()  # one runner from here on: the chat turns and the stages of the run are its calls
        line = office.line = Line(lab, monkeypatch, tmp_path)  # WorkEffects and the queue, connected as server.py does
        lab.runner = crew
        crew.gate.clear()
        # the proposal names no relay (a task in the line cannot be edited, 409): the employee's employee.yaml does
        assert not store.get_task(task_id)["relay"], f"[{step}] 제안서는 relay 를 비워 둔다, 직원 파일이 정한다"
        [device] = store.list_devices()
        r = phone.http.patch(f"/studio/devices/{device['id']}", json={"fcm_token": "fcm-phone"})
        assert r.status_code == 200, f"[{step}] FCM 토큰 등록: {r.status_code} {r.text}"
        pusher = office.pusher = push.Pusher(api.run_inbox, lambda: str(key_file))
        store.commit_listeners.append(pusher.committed)  # push.attach without its global listener and notifier

        def pushed(kind, ident=None):
            assert pusher.drain(), f"[{step}] 푸시 줄이 비지 않는다"
            return [m for m in http.messages()
                    if m["data"].get("type") == kind and (ident is None or m["data"].get("id") == ident)]

        phone.sync(step, [])
        line.q.kick()
        wait(lambda: store.get_task(task_id)["status"] == "working" and crew.now == 1, 30)
        row = store.get_task(task_id)
        assert row["queued_at"] is None and row["run_id"], f"[{step}] 슬롯을 얻으면 줄에서 나온다: {row}"
        run = lab.engine.load(row["run_id"])
        assert run.status in ("pending", "running"), f"[{step}] 런 상태: {run.status}"
        events = store.list_task_events(task_id)
        kinds = [e["kind"] for e in events]
        assert kinds[:4] == ["created", "transition", "assigned", "queued"] and kinds[-1] == "transition", \
            f"[{step}] 작업 이벤트(감사): {kinds}"
        assert events[-1]["rule"] == "T3", f"[{step}] 시작은 T3: {events[-1]}"
        assert phone.inbox(step)["pending_count"] == 0, f"[{step}] 결재함 수"
        types = phone.sync(step, ["task.transitioned"])
        assert types.count("task.transitioned") == 1, f"[{step}] 스트림: {types}"
        started = pushed("task", task_id)
        assert [(m["notification"]["title"], m["notification"]["body"]) for m in started] == \
            [(TEXT["title"], push.STARTED)], f"[{step}] 작업 시작 푸시: {started}"
        assert crew.errors == [], f"[{step}] {crew.errors}"

        # --- 7. a group room held at its exchange limit, while the run waits at the gate -------------------------
        step = "단계7 단체방"
        monkeypatch.setattr(office.desk.cfg, "studio_group_max_exchanges", 2)
        names = {office.desk.emp: "민수"}
        for name in ("지수", "현우"):
            member, _ = lab.world.staff(name)
            office.desk.hire(member)
            names[member] = name
        r = phone.http.post("/studio/rooms", json={"employee_ids": list(names), "title": "점프 회의"})
        assert r.status_code == 201, f"[{step}] 방 만들기: {r.status_code} {r.text}"
        group = r.json()["id"]
        a, b, _ = api.group_order(store, group)

        def mention(other):
            return lambda call: office.tool(call, "room_post", text=f"{names[other]} 씨 차례입니다", mentions=[other])

        crew.say(["", "", ""], mention(b), mention(a), mention(b))  # a (the owner's mention), b, a: then held
        r = phone.http.post(f"/studio/rooms/{group}/messages", json={"body": "둘이 이야기해 보세요", "mentions": [a]})
        assert r.status_code == 201, f"[{step}] 메시지: {r.status_code} {r.text}"
        assert len(office.waiting) == 1, f"[{step}] 첫 턴이 대기한다: {len(office.waiting)}"
        while office.waiting:
            office.waiting.pop(0)()
        assert crew.errors == [], f"[{step}] {crew.errors}"
        said = [(m["author_id"] or m["author_kind"], m["body"]) for m in store.list_messages(group)][1:]
        assert said[:4] == [("owner", "둘이 이야기해 보세요"), (a, f"{names[b]} 씨 차례입니다"),
                            (b, f"{names[a]} 씨 차례입니다"), (a, f"{names[b]} 씨 차례입니다")], f"[{step}] 방의 글: {said}"
        held = store.get_room(group)
        assert held["exchange_count"] == 2, f"[{step}] 왕복 수: {held['exchange_count']}"
        [notice] = [m for m in store.list_messages(group) if (m["meta"] or {}).get("event") == "group_exchange_limit"]
        assert (notice["author_kind"], notice["meta"]["limit"]) == ("system", 2), f"[{step}] 멈춤 알림: {notice}"
        assert store.list_messages(group)[-1]["id"] == notice["id"], f"[{step}] 알림이 마지막 글이다"
        assert api.group_halted(store, group) and api.group_owed(store, group) == [b], f"[{step}] 방은 멈췄고 b 의 턴이 밀렸다"
        assert not office.waiting and api.live_runs(store).keys() == {row["run_id"]}, f"[{step}] 남은 턴 없음, 작업 런만 산다"
        assert crew.now == 1 and store.get_task(task_id)["status"] == "working", f"[{step}] 작업 런은 게이트에 있다"
        assert phone.inbox(step)["pending_count"] == 0, f"[{step}] 결재함 수"
        types = phone.sync(step, ["message.created", "message.created", "message.created", "room.updated",
                                  "message.created", "room.updated", "message.created"])
        assert types.count("room.updated") == 3 and types[-1] == "message.created", f"[{step}] 스트림: {types}"  # the notice last
        counted = [e["data"]["room"]["exchange_count"] for e in store.list_stream_events(limit=10_000)
                   if e["type"] == "room.updated" and e["data"]["room"]["id"] == group]
        assert counted[-2:] == [1, 2], f"[{step}] room.updated 의 왕복 수: {counted}"
        limit = [m for m in pushed("room", group) if m["notification"]["title"] == push.NOTICE_EVENTS["group_exchange_limit"]]
        assert len(limit) == 1 and limit[0]["data"]["grade"] == "urgent", f"[{step}] 멈춤 푸시: {limit}"
        assert len(started) == 1 and len(pushed("task", task_id)) == 1, f"[{step}] 작업 시작 푸시는 그대로 하나"

        # --- 8. the review: the run asks for it from its build stage, finishes, and the task is in `review` ------
        step = "단계8 검수"
        filed = []

        def request_review(call):
            """What the employee's Claude Code does in its run: `task_request_review` with the token of that run."""
            token = api.actor_token(store.get_task(task_id)["run_id"])
            r = office.http.post("/studio/mcp/task_request_review", headers=bearer(token), json={
                "summary": "2단 점프를 넣었다",
                "checklist": [{"criterion": "테스트 통과", "status": "verified_auto", "note": "mock"}]})
            assert r.status_code == 200 and r.json()["ok"], f"[{step}] task_request_review: {r.status_code} {r.text}"
            filed.append(r.json())

        crew.on_stage["build"] = request_review
        crew.gate.set()
        line.settled({"id": task_id})
        assert crew.errors == [], f"[{step}] {crew.errors}"
        row = store.get_task(task_id)
        assert row["status"] == "review" and row["queued_at"] is None, f"[{step}] 작업 상태: {row['status']}"
        assert lab.engine.load(row["run_id"]).status == "done", f"[{step}] 런 상태"
        assert crew.stages == ["scout", "plan", "build", "review"], f"[{step}] 런의 단계: {crew.stages}"
        listed = phone.inbox(step)
        assert listed["pending_count"] == 1 and [i["kind"] for i in listed["items"]] == ["final_review"], \
            f"[{step}] 결재함: {listed['items']}"
        [review1] = listed["items"]
        assert review1["id"] == filed[0]["inbox_id"] and (review1["task_id"], review1["requester_id"]) == (task_id, emp), \
            f"[{step}] 결재 항목: {review1}"
        assert review1["title"] == TEXT["title"], f"[{step}] 결재 제목: {review1['title']}"
        kinds = [e["kind"] for e in store.list_task_events(task_id)]
        assert kinds[-1] == "transition" and kinds.count("transition") == 3, f"[{step}] 작업 이벤트(감사): {kinds}"
        assert store.list_task_events(task_id)[-1]["rule"] == "T6", f"[{step}] 검수 요청은 T6"
        types = phone.sync(step, ["inbox.created", "task.transitioned"])
        assert types.count("inbox.created") == 1 and types.count("task.transitioned") == 1, f"[{step}] 스트림: {types}"
        reviews = pushed("inbox", review1["id"])
        assert len(reviews) == 1 and reviews[0]["data"]["grade"] == "urgent" \
            and reviews[0]["notification"]["title"] == TEXT["title"], f"[{step}] 검수 푸시: {reviews}"
        assert {m["token"] for m in http.messages()} == {"fcm-phone"}, f"[{step}] 푸시는 폰으로만 간다"

        # --- 9. a rework: the run goes on, and the task comes back to `review` with a new request ----------------
        step = "단계9 재작업"
        [task_room] = store.list_rooms(kind="task", task_id=task_id)
        refused(phone.http.post(f"/studio/inbox/{review1['id']}/decide", json={"decision": "rework"}), 422,
                "invalid_decision")  # a rework needs its comment
        assert store.get_task(task_id)["status"] == "review", f"[{step}] 코멘트 없는 결정은 아무것도 바꾸지 않는다"

        def rework_review(call):
            if store.get_task(task_id)["status"] == "working":
                request_review(call)

        crew.on_stage.update({name: rework_review for name in ("scout", "plan", "build", "review")})
        crew.gate.clear()
        stages = len(crew.stages)
        out = phone.decide(review1, "rework", step, comment="착지 판정이 틀렸다")
        assert (out["inbox"]["status"], out["inbox"]["decision"]) == ("decided", "rework"), f"[{step}] 결재: {out['inbox']}"
        assert out["task"]["status"] == "working", f"[{step}] 재작업은 working: {out['task']}"
        wait(lambda: crew.now == 1, 30)
        assert store.get_task(task_id)["status"] == "working" and phone.inbox(step)["pending_count"] == 0, \
            f"[{step}] 런이 도는 동안 결재함은 비어 있다"
        bodies = [m["body"] for m in store.list_messages(task_room["id"])]
        assert any("재작업" in body and "착지 판정이 틀렸다" in body for body in bodies), f"[{step}] 작업방의 글: {bodies}"
        crew.gate.set()
        line.settled({"id": task_id})
        assert crew.errors == [], f"[{step}] {crew.errors}"
        assert len(crew.stages) > stages, f"[{step}] 러너가 다시 불렸다: {crew.stages}"
        row = store.get_task(task_id)
        assert row["status"] == "review" and row["queued_at"] is None, f"[{step}] 다시 review: {row['status']}"
        assert lab.engine.load(row["run_id"]).status == "done", f"[{step}] 런 상태"
        decided = store.get_inbox_item(review1["id"])
        assert (decided["status"], decided["decision"]) == ("decided", "rework"), f"[{step}] 이전 결재는 닫혔다"
        [review2] = phone.inbox(step)["items"]
        assert review2["kind"] == "final_review" and review2["id"] != review1["id"] and review2["task_id"] == task_id, \
            f"[{step}] 새 검수 결재: {review2}"
        events = store.list_task_events(task_id)
        assert [e["rule"] for e in events if e["kind"] == "transition"][-2:] == ["T8", "T6"], \
            f"[{step}] 작업 이벤트(감사): {[(e['kind'], e['rule']) for e in events]}"
        types = phone.sync(step, ["inbox.decided", "task.transitioned", "inbox.created", "task.transitioned"])
        assert types.count("inbox.created") == 1, f"[{step}] 스트림: {types}"
        assert len(pushed("inbox", review2["id"])) == 1 and len(pushed("inbox", review1["id"])) == 1, \
            f"[{step}] 검수 푸시가 하나씩: {http.messages()}"

        # --- 10. the pass: done, and the item is closed --------------------------------------------------------
        step = "단계10 통과"
        out = phone.decide(review2, "pass", step)
        assert (out["inbox"]["status"], out["inbox"]["decision"]) == ("decided", "pass"), f"[{step}] 결재: {out['inbox']}"
        row = store.get_task(task_id)
        assert row["status"] == "done" and row["queued_at"] is None, f"[{step}] 작업 상태: {row['status']}"
        assert phone.inbox(step)["pending_count"] == 0, f"[{step}] 결재함은 비었다"
        assert line.q.idle() and not line.q.slots(), f"[{step}] 슬롯이 남지 않았다"
        events = store.list_task_events(task_id)
        assert events[-1]["kind"] == "transition" and events[-1]["rule"] == "T7", \
            f"[{step}] 작업 이벤트(감사): {[(e['kind'], e['rule']) for e in events]}"
        types = phone.sync(step, ["inbox.decided", "task.transitioned"])
        assert types.count("inbox.decided") == 1 and types.count("task.transitioned") == 1, f"[{step}] 스트림: {types}"
        assert crew.errors == [] and crew.now == 0, f"[{step}] 러너 상태"

        # --- 11. a follow-up task: the finished one is its parent, proposed -> approved ------------------------
        step = "단계11 후속 작업"
        made = {}
        crew.say(["후속 작업을 제안했습니다"],
                 lambda call: made.update(office.tool(call, "followup_propose", parent_task_id=task_id,
                                                      **{**TEXT, "title": "3단 점프"})))
        phone.say(room, "이어서 할 일을 제안해 줘", step)
        office.turn()
        assert made.get("ok") and made["parent_id"] == task_id, f"[{step}] 도구 결과: {made}"
        follow = store.get_task(made["task_id"])
        assert (follow["status"], follow["parent_id"]) == ("proposed", task_id), f"[{step}] 후속 작업: {follow}"
        assert store.get_task(task_id)["status"] == "done", f"[{step}] 부모는 그대로 done"
        [item] = phone.inbox(step)["items"]
        assert (item["kind"], item["task_id"], item["requester_id"]) == ("task_proposal", follow["id"], emp), \
            f"[{step}] 결재 항목: {item}"
        children = phone.http.get("/studio/tasks", params={"parent_id": task_id}).json()["items"]
        assert [c["id"] for c in children] == [follow["id"]], f"[{step}] 부모에 연결된 작업: {children}"
        assert office.desk.said(room)[-1] == ("employee", "후속 작업을 제안했습니다"), f"[{step}] 방의 글"
        assert [e["kind"] for e in store.list_task_events(follow["id"])] == ["created"], f"[{step}] 작업 이벤트(감사)"
        types = phone.sync(step, ["task.created", "inbox.created"])
        assert "message.created" in types, f"[{step}] 스트림: {types}"
        assert len(pushed("inbox", item["id"])) == 1, f"[{step}] 결재 푸시"
        phone.decide(item, "approve", step)
        follow = store.get_task(follow["id"])
        assert (follow["status"], follow["parent_id"]) == ("backlog", task_id), f"[{step}] 승인하면 backlog: {follow}"
        assert phone.inbox(step)["pending_count"] == 0, f"[{step}] 결재함은 비었다"
        kinds = [e["kind"] for e in store.list_task_events(follow["id"])]
        assert kinds == ["created", "transition"], f"[{step}] 작업 이벤트(감사): {kinds}"
        types = phone.sync(step, ["inbox.decided", "task.transitioned"])
        assert crew.errors == [], f"[{step}] {crew.errors}"


def test_a_task_with_no_relay_follows_its_employee_file_and_is_refused_at_the_slot(line):  # noqa: F811
    """What the scenario relies on (a task with no relay gets it from employee.yaml) and its other side: the file names
    a relay that is not there, T3 refuses at the slot, and the task stays in backlog, out of the line, with the reason."""
    task = line.backlog(relay=None)
    folder = employees.folder(line.cfg.studio_dir, task["assignee_id"])
    (folder / "employee.yaml").write_bytes((YAML + "relay: nowhere\n").encode("utf-8"))
    assert not line.row(task)["relay"]
    ok(line.ask(task))
    wait(lambda: line.q.idle() and line.row(task)["queued_at"] is None, 30)
    row = line.row(task)
    assert (row["status"], row["run_id"], row["queued_at"]) == ("backlog", None, None), f"줄에서 빠진 backlog: {row}"
    events = line.store.list_task_events(task["id"])
    assert [e["kind"] for e in events] == ["created", "queued", "run"], f"작업 이벤트(감사): {[e['kind'] for e in events]}"
    assert "nowhere" in str(events[-1]), f"거절 사유가 이벤트에 남는다: {events[-1]}"
    assert line.gate.now == 0 and line.stages() == [], "어떤 단계도 돌지 않았다"

