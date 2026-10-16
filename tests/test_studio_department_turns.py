"""Studio: the turns of a department room are the group room's (ARCHITECTURE.md §4.6 · §6.4 · §6.6 · §8.4 · §9.5).
The owner's message gives every member a turn in the room's order, or the ones it mentions; a member passes by
leaving no answer; an employee's mention puts that member's turn in and the exchanges are held at
`studio_group_max_exchanges`. A 1:1 room is not changed. The chat relay on the mock runner, as test_studio_group.py."""
import pytest

from relay_agent.studio import api, push
from test_studio_group import Cast, Party  # noqa: F401 - Cast is what `Party` builds
from test_studio_runs import lab  # noqa: F401 - the fixture `dep` stands on


class Dep(Party):
    """`Party` with the room of the part `program` in place of the group room: its members are the three
    employees, the room's rows made here when the lab has none."""

    def __init__(self, lab, monkeypatch):  # noqa: F811
        super().__init__(lab, monkeypatch)
        rooms = self.store.list_rooms(kind="department", department_id="program")
        self.room = rooms[0]["id"] if rooms else self.store.create_room("department", title="프로그램",
                                                                         department_id="program")["id"]
        for employee_id in self.names:
            if self.store.get_room_member(self.room, employee_id) is None:
                self.store.create_room_member(self.room, employee_id)
        self.order = api.group_order(self.store, self.room)
        assert sorted(self.order) == sorted(self.names)

    def said(self):
        return [(m["author_id"] or m["author_kind"], m["body"]) for m in self.messages()]


@pytest.fixture
def dep(lab, monkeypatch):  # noqa: F811
    return Dep(lab, monkeypatch)


def test_a_message_without_a_mention_gives_every_member_a_turn_and_the_ones_that_pass_leave_nothing(dep):
    a, b, c = dep.order
    assert dep.store.get_room(dep.room)["kind"] == "department"
    dep.cast.moves = {a: ["제가 맡겠습니다"], b: [""], c: ["저는 테스트를 돕겠습니다"]}
    dep.say("안녕하세요")
    assert dep.cast.seen == [] and len(dep.waiting) == 1  # the POST does not wait for the turns
    dep.go()
    assert dep.turns() == [a, b, c]
    assert dep.said() == [("owner", "안녕하세요"), (a, "제가 맡겠습니다"), (c, "저는 테스트를 돕겠습니다")]
    assert dep.notices("run_error") == [] and dep.count() == 0
    for seen in dep.cast.seen:
        me = seen["employee_id"]
        assert (seen["room_id"], seen["mode"], seen["task_id"]) == (dep.room, "chat", None)
        assert "파트방 `프로그램`" in seen["prompt"] and "단체방" not in seen["prompt"]
        assert f"너는 {dep.names[me]} (id `{me}`)" in seen["prompt"] and "넘어간다" in seen["prompt"]
    assert f"{dep.names[a]}: 제가 맡겠습니다" in dep.cast.seen[2]["prompt"]
    assert dep.desk.member(dep.room, b)["fed_message_id"] == dep.messages()[1]["id"]  # the one who passed has read


def test_a_part_without_members_has_no_turn(dep):
    rooms = dep.store.list_rooms(kind="department", department_id="art")
    empty = rooms[0]["id"] if rooms else dep.store.create_room("department", title="미술", department_id="art")["id"]
    assert dep.store.list_room_members(room_id=empty) == []
    r = dep.owner.post(f"/studio/rooms/{empty}/messages", json={"body": "안녕하세요"})
    assert r.status_code == 201, r.text
    assert dep.waiting == [] and dep.cast.seen == []


def test_a_message_that_mentions_gives_only_those_a_turn(dep):
    a, b, c = dep.order
    dep.cast.moves = {b: ["네, 봤습니다"], a: ["저도 좋습니다"]}
    dep.say("지수 씨만 봐 주세요", mentions=[b])
    dep.go()
    assert dep.turns() == [b] and dep.said() == [("owner", "지수 씨만 봐 주세요"), (b, "네, 봤습니다")]
    assert dep.desk.member(dep.room, a)["fed_message_id"] in (0, None)
    dep.say("다들 어때요?")
    dep.go()
    assert dep.turns() == [b, a, b, c]


def test_the_exchange_limit_is_held_once_and_pushed_and_continue_goes_on(dep, monkeypatch):
    a, b, c = dep.order
    dep.hold(monkeypatch)
    assert dep.turns() == [a, b, a] and dep.count() == 2
    assert dep.cast.posted[-1]["ok"] and dep.cast.posted[-1]["queued"] == [b]
    [notice] = dep.notices("group_exchange_limit")
    assert notice["meta"] == {"event": "group_exchange_limit", "limit": 2}
    grade, title, _, link = push._of_message({"message": notice}, dep.store)
    assert (grade, title, link) == (push.URGENT, push.NOTICE_EVENTS["group_exchange_limit"],
                                    {"type": "room", "id": dep.room})
    said = dep.messages()[1]  # an employee's message in the room is pushed quietly, like a group room's
    assert push._of_message({"message": said}, dep.store)[0] == push.QUIET
    assert api.group_halted(dep.store, dep.room) and api.group_owed(dep.store, dep.room) == [b]

    path = f"/studio/rooms/{dep.room}/continue"
    dep.cast.moves = {b: ["이어서 답합니다"]}
    r = dep.owner.post(path)
    assert r.status_code == 200 and r.json()["exchange_count"] == 0, r.text
    [continued] = dep.notices("group_continued")
    assert push._of_message({"message": continued}, dep.store) is None
    dep.go()
    assert dep.turns() == [a, b, a, b] and dep.said()[-1] == (b, "이어서 답합니다")
    before = len(dep.messages())
    again = dep.owner.post(path)  # no longer held: its row, nothing more
    assert again.status_code == 200 and len(dep.messages()) == before and dep.waiting == []
    refused = dep.owner.post(f"/studio/rooms/{dep.desk.dm}/continue")
    assert (refused.status_code, refused.json()["code"]) == (409, "precondition_failed")

    dep.say("계속하세요")  # the owner's message counts anew
    assert dep.count() == 0
    dep.go()
    assert len(dep.notices("group_exchange_limit")) == 1


def test_a_mention_of_oneself_or_of_a_stranger_is_refused_in_a_department_room(dep):
    a, _, _ = dep.order
    outsider = dep.hire("영희")  # in the part, but not a member of the room
    dep.cast.moves = {a: [("혼잣말입니다", [a]), ("영희 씨 봐 주세요", [outsider])]}
    dep.say("민수 씨?", mentions=[a])
    dep.say("민수 씨!", mentions=[a])
    dep.go()
    assert [(p["ok"], p["code"]) for p in dep.cast.posted] == [(False, "precondition_failed")]
    assert dep.turns() == [a] and dep.count() == 0


def test_a_same_part_employee_without_a_row_has_no_turn_until_the_row_is_there(dep):
    outsider = dep.hire("영희")  # in the part, no member row
    assert outsider not in api.group_order(dep.store, dep.room)
    dep.say("안녕하세요")
    dep.go()
    assert outsider not in dep.turns()
    dep.store.create_room_member(dep.room, outsider)
    assert outsider in api.group_order(dep.store, dep.room)  # not its place: rows of one second have no fixed order
    dep.say("영희 씨도 들어왔습니다")
    dep.go()
    assert outsider in dep.turns()


def test_an_archived_part_room_keeps_the_owners_message_but_gives_no_turn_and_refuses_continue(dep):
    r = dep.owner.post("/studio/departments", json={"name": "빈 파트"})
    assert r.status_code == 201, r.text
    part = r.json()["id"]
    [room] = dep.store.list_rooms(kind="department", department_id=part)
    assert dep.owner.post(f"/studio/departments/{part}/archive").status_code == 200
    seen = len(dep.cast.seen)
    posted = dep.owner.post(f"/studio/rooms/{room['id']}/messages", json={"body": "남는 글"})
    assert posted.status_code == 201, posted.text
    assert dep.waiting == [] and len(dep.cast.seen) == seen
    refused = dep.owner.post(f"/studio/rooms/{room['id']}/continue")
    assert (refused.status_code, refused.json()["code"]) == (409, "precondition_failed")


def test_a_one_to_one_room_is_as_before(dep):
    dm, emp = dep.desk.dm, dep.desk.emp
    dep.cast.moves = {emp: [("메모를 남겼습니다", []), "네"]}
    r = dep.owner.post(f"/studio/rooms/{dm}/messages", json={"body": "민수 씨"})
    assert r.status_code == 201, r.text
    dep.go()
    assert dep.turns() == [emp] and dep.store.get_room(dm)["exchange_count"] == 0  # the employee's turn, nobody else's
    assert [(p["ok"], p["queued"]) for p in dep.cast.posted] == [(True, [])]  # a room_post in a 1:1 room queues nothing
    r = dep.owner.post(f"/studio/rooms/{dm}/messages", json={"body": "다시"})
    dep.go()
    assert dep.turns() == [emp, emp] and dep.store.get_room(dm)["exchange_count"] == 0
