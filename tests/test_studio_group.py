"""Studio: the turns of a group room (ARCHITECTURE.md §4.6 · §6.4 · §6.6 · §8.4 · §9.5). The owner's message
gives every member a turn, one after the other in the room's order, or the ones it mentions; a member passes by
leaving no answer; a member's mention of another (`room_post`) puts that one's turn in, and those turns are
counted for the room and held at `studio_group_max_exchanges` until the owner writes or continues. The chat relay
on the mock runner: each turn does what the test wrote for its employee. No AI is called."""
import pytest

from relay_agent.runners import MockRunner
from relay_agent.studio import api, chat, push, runs
from test_studio_chat import Desk
from test_studio_runs import lab  # noqa: F401 - the fixture `party` stands on

TITLE = "점프 회의"


class Cast(MockRunner):
    """The `chat` stage of a turn does the next thing the test wrote for that employee (`moves`): a text is its
    answer — "" = it passes —, `(text, mentions)` is a `room_post` from the turn's own token, with no answer.
    Nothing left = it passes. `seen` is the head of each turn as `run.live` would carry it, and its prompt."""

    def __init__(self, store, client):
        super().__init__()
        self.store, self.client = store, client
        self.moves, self.seen, self.posted, self.errors = {}, [], [], []

    def run(self, call):
        try:  # the engine would only fail the run: the test must see what went wrong here
            [(run_id, head)] = [(run_id, head) for run_id, head in api.live_runs(self.store).items()
                                if head["mode"] != "work"]  # one turn at a time
            self.seen.append({**head, "prompt": call.prompt})
            answer = move = (self.moves.get(head["employee_id"]) or [""]).pop(0)
            if not isinstance(move, str):
                r = self.client.post("/studio/mcp/room_post", json={"text": move[0], "mentions": move[1]},
                                     headers={"Authorization": f"Bearer {api.actor_token(run_id)}"})
                assert r.status_code == 200, r.text
                self.posted.append(r.json())
                answer = ""
        except Exception as e:  # noqa: BLE001
            self.errors.append(e)
            raise
        self.script.setdefault(chat.STAGE, []).append(
            {"summary": "턴을 마쳤다", "state": "단체방에서 이야기했다", "open_issues": [], "next_steps": [],
             "output": answer})
        return super().run(call)


class Party:
    """Desk with the turns connected the way server.py connects them (`EngineHooks(turns=)`), and a group room of
    three employees. The line of the room runs when the test says so (`go`): the POST has returned by then, as on
    the server, where the line has a thread of its own."""

    def __init__(self, lab, monkeypatch):  # noqa: F811
        self.lab, self.waiting = lab, []
        self.desk = desk = Desk(lab, monkeypatch, self.waiting.append)
        monkeypatch.setattr(api, "_hooks", runs.EngineHooks(lab.engine, lab.engine.advance, chat=desk.turns.request,
                                                           turns=desk.turns))
        self.store, self.owner, self.cfg = desk.store, desk.owner, desk.cfg
        self.cast = lab.runner = Cast(desk.store, desk.owner)
        self.names = {desk.emp: "민수"}
        for name in ("지수", "현우"):
            self.names[self.hire(name)] = name
        r = self.owner.post("/studio/rooms", json={"employee_ids": list(self.names), "title": TITLE})
        assert r.status_code == 201, r.text
        self.room = r.json()["id"]
        self.order = api.group_order(self.store, self.room)

    def hire(self, name):
        employee_id, _ = self.lab.world.staff(name)
        self.desk.hire(employee_id)
        return employee_id

    def say(self, body, **more):
        r = self.owner.post(f"/studio/rooms/{self.room}/messages", json={"body": body, **more})
        assert r.status_code == 201, r.text
        return r.json()

    def go(self):
        """Run the room's line until nobody waits."""
        while self.waiting:
            self.waiting.pop(0)()
        assert self.cast.errors == [] and not self.desk.turns._rooms and api.live_runs(self.store) == {}

    def messages(self):
        return self.store.list_messages(self.room)

    def said(self):
        """(who, body) of the room's messages after the notice of its making; who = the employee's id, else the kind."""
        return [(m["author_id"] or m["author_kind"], m["body"]) for m in self.messages()][1:]

    def turns(self):
        return [seen["employee_id"] for seen in self.cast.seen]

    def count(self):
        return self.store.get_room(self.room)["exchange_count"]

    def notices(self, event):
        return [m for m in self.messages() if m["author_kind"] == "system" and (m["meta"] or {}).get("event") == event]

    def counted(self):
        """`exchange_count` as each `room.updated` of the room told it."""
        return [e["data"]["room"]["exchange_count"] for e in self.store.list_stream_events()
                if e["type"] == "room.updated" and e["data"]["room"]["id"] == self.room]

    def hold(self, monkeypatch, limit=2):
        """Two employees mention each other until the room is held at `limit` = 2: a (the owner's turn), b, a —
        and b's next turn is not made."""
        a, b, _ = self.order
        monkeypatch.setattr(self.cfg, "studio_group_max_exchanges", limit)
        self.cast.moves = {a: [("지수 씨 차례입니다", [b]), ("다시 지수 씨 차례입니다", [b])],
                           b: [("민수 씨 차례입니다", [a])]}
        self.say("둘이 이야기해 보세요", mentions=[a])
        self.go()


@pytest.fixture
def party(lab, monkeypatch):  # noqa: F811
    return Party(lab, monkeypatch)


# --- the owner's message ------------------------------------------------------------------------------------------
def test_a_message_without_a_mention_gives_every_member_a_turn_in_the_rooms_order(party):
    a, b, c = party.order
    members = party.owner.get(f"/studio/rooms/{party.room}").json()["members"]  # what the app has of the order
    assert party.order == [m["employee_id"] for m in sorted(members, key=lambda m: (m["joined_at"], m["employee_id"]))]
    party.cast.moves = {a: ["제가 맡겠습니다"], b: [""], c: ["저는 테스트를 돕겠습니다"]}

    party.say("점프 버그 누가 볼래요?")
    assert party.cast.seen == [] and len(party.waiting) == 1  # the POST does not wait for the turns
    party.go()
    assert party.turns() == [a, b, c]  # one after the other
    # b passed: no message of its own, and none that says so
    assert party.said() == [("owner", "점프 버그 누가 볼래요?"), (a, "제가 맡겠습니다"), (c, "저는 테스트를 돕겠습니다")]
    assert party.notices("run_error") == [] and party.count() == 0  # the owner's turns are no exchange
    first, second, third = party.cast.seen
    # §7.4: what `run.live` carries of each turn — whose it is, and in which room
    assert [{key: seen[key] for key in api.LIVE_HEAD if key != "run_id"} for seen in party.cast.seen] == [
        {"employee_id": employee_id, "room_id": party.room, "task_id": None, "mode": "chat"} for employee_id in (a, b, c)]
    assert len({seen["run_id"] for seen in party.cast.seen}) == 3
    # a later member reads what an earlier one just answered, with the author's name
    assert "owner: 점프 버그 누가 볼래요?" in first["prompt"] and "제가 맡겠습니다" not in first["prompt"]
    assert f"{party.names[a]}: 제가 맡겠습니다" in second["prompt"]
    assert f"{party.names[a]}: 제가 맡겠습니다" in third["prompt"] and "owner: 점프 버그 누가 볼래요?" in third["prompt"]
    for seen in party.cast.seen:  # who is in the room, who the reader is, how to pass and how to ask another
        me = seen["employee_id"]
        assert f"단체방 `{TITLE}`" in seen["prompt"] and f"너는 {party.names[me]} (id `{me}`)" in seen["prompt"]
        assert all(f"{name} (id `{employee_id}`)" in seen["prompt"] for employee_id, name in party.names.items())
        assert "넘어간다" in seen["prompt"] and "room_post(text, mentions=" in seen["prompt"]
    # the one who passed has read all the same: its row of the room goes on from there
    answer = party.messages()[2]
    passed = party.desk.member(party.room, b)
    assert (answer["author_id"], passed["fed_message_id"]) == (a, answer["id"]) and passed["session_id"]
    assert [run.status for run in party.desk.runs(party.room, b)] == ["done"]


def test_a_message_that_mentions_gives_only_those_a_turn_and_each_keeps_its_own_conversation(party):
    a, b, c = party.order
    party.cast.moves = {b: ["네, 봤습니다", "저는 괜찮습니다"], a: ["저도 좋습니다"]}
    party.say("지수 씨만 봐 주세요", mentions=[b])
    party.go()
    assert party.turns() == [b] and party.said()[1:] == [(b, "네, 봤습니다")]
    assert party.desk.member(party.room, a)["fed_message_id"] == 0 and party.desk.member(party.room, a)["session_id"] is None

    party.say("다들 어때요?")
    party.go()
    assert party.turns() == [b, a, b, c]
    again, late = party.cast.calls[2], party.cast.calls[1]
    assert again.resume_session == party.cast.calls[0].session_id and late.resume_session is None  # §3.7: one each
    # the one who had no turn yet reads all that came since, the one who had reads what is new
    assert "owner: 지수 씨만 봐 주세요" in late.prompt and f"{party.names[b]}: 네, 봤습니다" in late.prompt
    assert "owner: 다들 어때요?" in again.prompt and f"{party.names[a]}: 저도 좋습니다" in again.prompt
    assert "지수 씨만 봐 주세요" not in again.prompt and "단체방" in again.prompt  # the note comes with every turn
    assert party.desk.member(party.desk.dm)["session_id"] is None  # the 1:1 room is another conversation


# --- an employee's mention ----------------------------------------------------------------------------------------
def test_an_employees_mention_puts_that_members_turn_in_and_a_turn_that_answered_is_counted(party):
    a, b, c = party.order
    party.cast.moves = {a: [("지수 씨, 로그 좀 봐 줄래요?", [b])], b: [("현우 씨도 확인 부탁해요", [c])], c: [""]}
    party.say("민수 씨, 점프 버그 봐 주세요", mentions=[a])
    party.go()
    assert party.turns() == [a, b, c]
    assert [(posted["ok"], posted["queued"]) for posted in party.cast.posted] == [(True, [b]), (True, [c])]
    assert party.said()[1:] == [(a, "지수 씨, 로그 좀 봐 줄래요?"), (b, "현우 씨도 확인 부탁해요")]
    asked = party.messages()[2]
    assert (asked["mentions"], asked["run_id"]) == ([b], party.cast.seen[0]["run_id"])
    assert f"{party.names[a]}: 지수 씨, 로그 좀 봐 줄래요?" in party.cast.seen[1]["prompt"]
    # a's turn was the owner's; b's was a mention's and left a message; c's was a mention's and passed
    assert party.count() == 1 and party.counted()[-1] == 1 and party.notices("run_error") == []

    before = len(party.counted())
    party.say("고마워요", mentions=[c])  # the owner's message counts anew
    assert party.count() == 0 and party.counted()[before:] == [0]
    party.go()
    assert party.turns() == [a, b, c, c] and party.count() == 0


def test_a_mention_of_oneself_or_of_one_who_is_not_in_the_room_is_refused(party):
    a, b, _ = party.order
    outsider = party.hire("영희")
    party.cast.moves = {a: [("혼잣말입니다", [a]), ("영희 씨 봐 주세요", [outsider])]}
    party.say("민수 씨?", mentions=[a])
    party.say("민수 씨!", mentions=[a])
    party.go()
    assert [(posted["ok"], posted["code"]) for posted in party.cast.posted] == [(False, "precondition_failed")]
    assert party.turns() == [a] and party.said() == [("owner", "민수 씨?"), ("owner", "민수 씨!")]  # one turn read both
    assert party.count() == 0 and party.desk.member(party.room, b)["fed_message_id"] == 0

    party.say("다시요", mentions=[a])
    party.go()
    assert party.turns() == [a, a] and len(party.cast.posted) == 2 and party.said()[2:] == [("owner", "다시요")]
    assert (party.cast.posted[1]["ok"], party.cast.posted[1]["code"]) == (False, "precondition_failed")
    assert party.notices("run_error") == [] and party.count() == 0  # a turn that wrote nothing passed


# --- the limit ----------------------------------------------------------------------------------------------------
def test_at_the_limit_no_turn_is_put_in_and_the_room_says_so_once_and_the_owner_is_pushed(party, monkeypatch):
    a, b, c = party.order
    assert party.cfg.studio_group_max_exchanges == 6  # the default
    party.hold(monkeypatch)
    assert party.turns() == [a, b, a]  # a's first is the owner's; b's and a's second are the two of the limit
    assert party.count() == 2 and party.counted()[-2:] == [1, 2]
    *_, kept, notice = party.messages()
    assert (kept["author_id"], kept["body"], kept["mentions"]) == (a, "다시 지수 씨 차례입니다", [b])  # the message stays
    assert party.cast.posted[-1]["ok"] and party.cast.posted[-1]["queued"] == [b]  # it waited in the line
    assert (notice["author_kind"], notice["meta"]) == ("system", {"event": "group_exchange_limit", "limit": 2})
    assert party.notices("group_exchange_limit") == [notice]
    grade, title, _, link = push._of_message({"message": notice}, party.store)
    assert (grade, title, link) == (push.URGENT, push.NOTICE_EVENTS["group_exchange_limit"],
                                    {"type": "room", "id": party.room})
    assert api.group_halted(party.store, party.room) and api.group_owed(party.store, party.room) == [b]
    assert party.owner.get(f"/studio/rooms/{party.room}").json()["unread"] >= 1  # the notice waits to be read

    # more mentions while it is held: no turn, and no second notice
    before = len(party.messages())
    assert party.desk.turns.group(party.room, [b, c], True) == [b, c]
    party.go()
    assert party.turns() == [a, b, a] and len(party.messages()) == before and party.count() == 2


def test_continue_counts_anew_and_puts_in_the_turns_that_were_held(party, monkeypatch):
    a, b, _ = party.order
    path = f"/studio/rooms/{party.room}/continue"
    r = party.owner.post(path)  # a room that is not held: its row, and nothing happens
    assert r.status_code == 200 and r.json()["id"] == party.room and len(party.messages()) == 1 and party.waiting == []

    party.hold(monkeypatch)
    party.cast.moves = {b: ["이어서 답합니다"]}
    r = party.owner.post(path)
    assert r.status_code == 200 and r.json()["exchange_count"] == 0 and party.count() == 0, r.text
    notice = party.messages()[-1]
    assert (notice["author_kind"], notice["meta"]) == ("system", {"event": "group_continued"})
    assert push._of_message({"message": notice}, party.store) is None  # the owner's own doing is not pushed
    assert not api.group_halted(party.store, party.room) and party.counted()[-1] == 0
    party.go()
    assert party.turns() == [a, b, a, b] and party.said()[-1] == (b, "이어서 답합니다")
    held = party.cast.seen[-1]["prompt"]  # it reads the mention it was held from, not the room's two notices
    assert f"{party.names[a]}: 다시 지수 씨 차례입니다" in held and "멈췄습니다" not in held and "이어 갑니다" not in held
    assert party.count() == 1  # the turn that went on was a mention's

    before = len(party.messages())
    again = party.owner.post(path)  # asked again: nothing more
    assert again.status_code == 200 and again.json()["exchange_count"] == 1
    assert len(party.messages()) == before and party.waiting == [] and len(party.notices("group_exchange_limit")) == 1

    assert party.owner.post("/studio/rooms/room_none/continue").status_code == 404
    refused = party.owner.post(f"/studio/rooms/{party.desk.dm}/continue")
    assert (refused.status_code, refused.json()["code"]) == (409, "precondition_failed")
    assert party.owner.post(f"/studio/rooms/{party.room}/archive").status_code == 200
    refused = party.owner.post(path)
    assert (refused.status_code, refused.json()["code"]) == (409, "precondition_failed")


def test_the_owners_message_in_a_held_room_counts_anew_and_goes_on(party, monkeypatch):
    a, b, c = party.order
    party.hold(monkeypatch)
    party.cast.moves = {a: [""], b: ["이제 봤습니다"], c: [""]}
    party.say("계속하세요")
    assert party.count() == 0 and not api.group_halted(party.store, party.room)
    party.go()
    assert party.turns() == [a, b, a, a, b, c] and party.said()[-1] == (b, "이제 봤습니다")
    assert f"{party.names[a]}: 다시 지수 씨 차례입니다" in party.cast.seen[4]["prompt"]  # what it was held from
    assert party.count() == 0 and len(party.notices("group_exchange_limit")) == 1
    assert party.owner.post(f"/studio/rooms/{party.room}/continue").json()["exchange_count"] == 0
    assert party.waiting == [] and party.notices("group_continued") == []  # not held any more: nothing to continue

    # the count goes on from 0, and the room is told again when it is held again
    party.cast.moves = {a: [("지수 씨, 한 번 더", [b]), ("또 지수 씨", [b])], b: [("민수 씨, 한 번 더", [a])]}
    party.say("한 번 더 해 보세요", mentions=[a])
    party.go()
    assert party.count() == 2 and len(party.notices("group_exchange_limit")) == 2


def test_a_held_room_is_told_past_the_page_and_a_turn_held_is_found_past_the_page(party, monkeypatch):
    a, b, c = party.order
    party.hold(monkeypatch)
    monkeypatch.setattr(api, "MAX_PAGE", 2)
    for n in range(5):  # more than a page of the employees' messages after the notice, none of the owner's
        party.store.create_message(party.room, "employee", author_id=c, body=f"잡담 {n}")
    assert api.group_halted(party.store, party.room)
    # a turn put in now is still held, and the room is not told a second time
    before, turns = len(party.messages()), party.turns()
    assert party.desk.turns.group(party.room, [b], True) == [b]
    party.go()
    assert len(party.notices("group_exchange_limit")) == 1 and party.turns() == turns and len(party.messages()) == before
    assert api.group_owed(party.store, party.room) == [b]
    # c never had a turn: it reads from the room's start, and a's mention of c lies past the first page
    assert party.desk.member(party.room, c)["fed_message_id"] in (0, None)
    party.store.create_message(party.room, "employee", author_id=a, body="현우 씨도 봐 주세요", mentions=[c])
    assert api.group_owed(party.store, party.room) == [b, c]
    party.cast.moves = {b: [""], c: [""]}
    r = party.owner.post(f"/studio/rooms/{party.room}/continue")
    assert r.status_code == 200 and r.json()["exchange_count"] == 0
    assert len(party.notices("group_continued")) == 1 and not api.group_halted(party.store, party.room)

    # the other way: once the owner wrote, no older notice of the limit is looked for
    party.say("다시 시작합시다", mentions=[a])
    for n in range(4):  # a whole number of pages
        party.store.create_message(party.room, "employee", author_id=c, body=f"또 잡담 {n}")
    assert not api.group_halted(party.store, party.room)


# --- push (§5.9) --------------------------------------------------------------------------------------------------
def test_an_employees_message_in_a_group_room_is_pushed_as_in_a_dm_room_and_not_from_a_muted_one(party):
    a, _, _ = party.order
    said = party.store.create_message(party.room, "employee", author_id=a, body="넵")
    dm = party.store.create_message(party.desk.dm, "employee", author_id=party.desk.emp, body="넵")
    grade, title, line, link = push._of_message({"message": said}, party.store)
    assert (grade, title, line, link) == (push.QUIET, party.names[a], "넵", {"type": "room", "id": party.room})
    assert push._of_message({"message": dm}, party.store)[0] == push.QUIET  # the same grade
    assert party.owner.patch(f"/studio/rooms/{party.room}", json={"muted": True}).status_code == 200
    assert push._of_message({"message": said}, party.store) is None
