"""Studio: deleting a room (ARCHITECTURE.md §6.4 · §7.4). A group room goes whole — messages, members, read marks and
the attachment files of its messages; a 1:1 room is only cleared (`POST …/clear`); a task, part or system room
is refused. A room where a turn runs or waits is refused with `failed`. Approvals, tasks and memory stay. The
chat relay on the mock runner. Files are written and removed only under the test's own studio_dir."""
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from relay_agent.studio import api, sat
from test_studio_group import Party, party  # noqa: F401 - the fixture `party`
from test_studio_inbox import refused
from test_studio_runs import lab  # noqa: F401


def attach(party, room, name="shot.txt"):
    """A message of the room with an attachment file in place, as `POST …/messages` leaves it."""
    with party.store.transaction() as tx:
        message = party.store.create_message(room, "owner", tx=tx, body="")
        party.store.set_message_attachments(tx, message["id"], [
            {"name": name, "path": f"{message['id']}/{name}", "mime": "text/plain", "size": 2}])
    file = Path(party.cfg.studio_dir) / "attachments" / str(message["id"]) / name
    file.parent.mkdir(parents=True)
    file.write_text("hi", encoding="utf-8")
    return message["id"], file


def types(party, kind):
    return [e for e in party.store.list_stream_events(limit=10_000) if e["type"] == kind]


# --- which rooms --------------------------------------------------------------------------------------------------
def test_a_group_room_goes_with_its_messages_members_and_attachment_files(party):
    party.say("점프 버그 누가 볼래요?")
    party.go()
    mid, file = attach(party, party.room)
    other_mid, other_file = attach(party, party.desk.dm)  # another room's file is not touched
    row = party.store.get_room(party.room)

    r = party.owner.delete(f"/studio/rooms/{party.room}")
    assert r.status_code == 200 and r.json() == {"id": party.room, "deleted": True}
    assert party.store.get_room(party.room) is None
    assert party.store.list_messages(party.room) == [] and party.store.list_room_members(room_id=party.room, include_left=True) == []
    assert not file.exists() and not file.parent.exists() and other_file.exists()
    [event] = types(party, "room.deleted")
    assert event["data"] == {"room": row} and event["room_id"] == party.room
    assert party.owner.get(f"/studio/rooms/{party.room}").status_code == 404
    refused(party.owner.delete(f"/studio/rooms/{party.room}"), 404, "not_found")
    assert party.room not in [x["id"] for x in party.owner.get("/studio/rooms?archived=1").json()["items"]]


def test_a_kept_group_room_can_be_deleted_too(party):
    assert party.owner.post(f"/studio/rooms/{party.room}/archive").status_code == 200
    assert party.owner.delete(f"/studio/rooms/{party.room}").status_code == 200
    assert party.store.get_room(party.room) is None


def test_the_other_rooms_are_not_deleted_and_say_why(party):
    store = party.store
    party.lab.world.task("working")  # a task with its room
    rooms = {r["kind"]: r["id"] for r in store.list_rooms(include_archived=True) if r["kind"] != "group"}
    assert {"dm", "task", "department", "system"} <= set(rooms)
    for kind, room in rooms.items():
        body = refused(party.owner.delete(f"/studio/rooms/{room}"), 409, "precondition_failed")
        assert body["failed"] == [] and (kind == "dm" or kind in body["detail"])
        assert store.get_room(room) is not None
        if kind != "dm":  # clear is for 1:1 rooms only
            refused(party.owner.post(f"/studio/rooms/{room}/clear"), 409, "precondition_failed")
    assert "clear" in refused(party.owner.delete(f"/studio/rooms/{rooms['dm']}"), 409, "precondition_failed")["detail"]
    body = refused(party.owner.post(f"/studio/rooms/{party.room}/clear"), 409, "precondition_failed")
    assert "DELETE" in body["detail"] and store.get_room(party.room) is not None
    refused(party.owner.post("/studio/rooms/room_none/clear"), 404, "not_found")
    assert types(party, "room.deleted") == [] and types(party, "room.cleared") == []


# --- a turn at work -----------------------------------------------------------------------------------------------
def test_a_room_where_a_turn_waits_or_runs_is_refused_with_why(party):
    party.say("누가 볼래요?")  # the line is made, nobody has run yet
    body = refused(party.owner.delete(f"/studio/rooms/{party.room}"), 409, "precondition_failed")
    assert body["failed"] == ["turn_waiting"]
    assert party.store.get_room(party.room) is not None and types(party, "room.deleted") == []
    a = party.order[0]
    party.desk.turns._busy[(party.room, a)] = False  # a turn of the room is at work
    body = refused(party.owner.delete(f"/studio/rooms/{party.room}"), 409, "precondition_failed")
    assert body["failed"] == ["turn_running", "turn_waiting"]
    del party.desk.turns._busy[(party.room, a)]
    party.go()
    assert party.owner.delete(f"/studio/rooms/{party.room}").status_code == 200


# --- clearing a 1:1 room ------------------------------------------------------------------------------------------
def test_clearing_a_dm_room_empties_it_and_starts_the_conversation_over(party):
    desk = party.desk
    party.cast.moves = {desk.emp: ["안녕하세요, 민수입니다", "처음 뵙겠습니다"]}
    desk.say("안녕?")
    party.go()
    assert desk.member()["session_id"] and desk.member()["fed_message_id"]
    mid, file = attach(party, desk.dm)
    memory = Path(party.cfg.studio_dir) / "employees" / desk.emp / "MEMORY.md"
    memory.write_text("기억", encoding="utf-8")

    r = party.owner.post(f"/studio/rooms/{desk.dm}/clear")
    assert r.status_code == 200 and r.json()["id"] == desk.dm and r.json()["owner_read_id"] == 0
    assert party.store.get_room(desk.dm) is not None and party.store.list_messages(desk.dm) == []
    member = desk.member()
    assert (member["session_id"], member["conversation_id"], member["fed_message_id"]) == (None, None, 0)
    assert not file.exists() and memory.read_text(encoding="utf-8") == "기억"
    assert not [k for k in desk.turns._policy_seen if k[0] == desk.dm]
    [event] = types(party, "room.cleared")
    assert event["room_id"] == desk.dm and event["data"]["room"]["id"] == desk.dm
    assert [m["employee_id"] for m in event["data"]["members"]] == [desk.emp]
    assert types(party, "room.deleted") == []
    # the employee has the room again: a new first turn, nothing resumed
    desk.say("다시?")
    party.go()
    assert desk.said()[-1] == ("employee", "처음 뵙겠습니다") and desk.member()["session_id"]
    assert party.owner.post(f"/studio/rooms/{desk.dm}/clear").status_code == 200  # an empty room: clears again


def test_a_dm_room_with_a_turn_waiting_is_not_cleared(party):
    key = (party.desk.dm, party.desk.emp)
    party.desk.turns._busy[key] = False
    body = refused(party.owner.post(f"/studio/rooms/{party.desk.dm}/clear"), 409, "precondition_failed")
    assert body["failed"] == ["turn_running"] and types(party, "room.cleared") == []
    del party.desk.turns._busy[key]


# --- what points at a deleted room ----------------------------------------------------------------------------------
def test_an_approval_and_a_task_that_name_a_deleted_room_still_list_and_show(party):
    world = party.lab.world
    task = world.task("working")
    item = party.store.create_inbox_item("question", "물음", "system", project_id=world.pid, task_id=task["id"],
                                         payload={"reason": "asked", "body": "높이는?", "options": [],
                                                  "room_id": party.room})
    assert party.owner.delete(f"/studio/rooms/{party.room}").status_code == 200
    listed = party.owner.get("/studio/inbox")
    assert listed.status_code == 200 and item["id"] in [x["id"] for x in listed.json()["items"]]
    assert party.owner.get(f"/studio/inbox/{item['id']}").status_code == 200
    assert party.owner.get("/studio/tasks").status_code == 200
    assert party.owner.get(f"/studio/tasks/{task['id']}").status_code == 200
    assert party.store.get_task(task["id"]) is not None


# --- who may ----------------------------------------------------------------------------------------------------
def test_the_device_token_may_and_an_actor_token_may_not(party, monkeypatch):
    import relay_agent.server as server
    remote = TestClient(server.app, client=("192.168.0.7", 50000))
    token = "sdt_" + "k" * 43
    party.store.create_device("폰", hashlib.sha256(token.encode()).hexdigest())
    device = {"Authorization": f"Bearer {token}"}
    actor = {"Authorization": "Bearer " + sat.issue(run="run_x", emp=party.order[0], room=party.room, mode="chat")}
    dm = party.desk.dm
    # the token's run is not live in this test: the subject it would establish is given (§8.2 is tested elsewhere)
    subject = {"kind": "employee", "employee_id": party.order[0], "run_id": "run_x", "room_id": party.room,
               "task_id": None, "mode": "chat"}
    monkeypatch.setattr(api, "actor_subject", lambda token: subject)
    for client, headers in ((party.owner, actor), (remote, actor)):
        refused(client.delete(f"/studio/rooms/{party.room}", headers=headers), 403, "actor_not_allowed")
        refused(client.post(f"/studio/rooms/{dm}/clear", headers=headers), 403, "actor_not_allowed")
    assert party.store.get_room(party.room) is not None
    assert remote.post(f"/studio/rooms/{dm}/clear", headers=device).status_code == 200
    assert remote.delete(f"/studio/rooms/{party.room}", headers=device).status_code == 200
    assert party.store.get_room(party.room) is None
    refused(remote.delete(f"/studio/rooms/{party.room}"), 401, "unauthorized")
