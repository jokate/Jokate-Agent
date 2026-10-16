"""V-3 (a): the PC is kept awake while a task run holds a slot or a chat / read turn goes on (studio/awake.py;
ARCHITECTURE.md §4.8). The call to the system is a fake that counts on / off: no power API is called."""
from collections import deque
from types import SimpleNamespace

from relay_agent.studio import awake, chat
from relay_agent.studio.awake import Awake
from test_studio_inbox import ok
from test_studio_queue import line  # noqa: F401 - the fixture the queue test stands on
from test_studio_runs import lab  # noqa: F401 - the fixture `line` stands on


class Setter:
    def __init__(self):
        self.calls = []

    def __call__(self, on):
        self.calls.append(on)


def test_nested_holds_switch_on_once_and_off_once():
    setter = Setter()
    keeper = Awake(setter)
    keeper.enable()
    with keeper.hold():
        with keeper.hold():
            assert setter.calls == [True] and keeper.count == 2
        assert setter.calls == [True]  # one is still at work
    assert setter.calls == [True, False] and keeper.count == 0
    with keeper.hold():
        pass
    assert setter.calls == [True, False, True, False]


def test_nothing_is_asked_of_the_system_before_enable_and_after_disable():
    setter = Setter()
    keeper = Awake(setter)
    with keeper.hold():
        assert setter.calls == []  # counted, not asked
        keeper.enable()
        assert setter.calls == [True]  # what is at work holds the PC awake from the start
        keeper.disable()
        assert setter.calls == [True, False] and keeper.count == 1
    assert setter.calls == [True, False]  # the release after disable asks nothing
    keeper.release()
    keeper.release()
    assert keeper.count == 0 and setter.calls == [True, False]


def test_a_release_without_its_acquire_stays_at_zero_and_a_failing_setter_is_not_raised():
    def broken(on):
        raise OSError("no power api")

    keeper = Awake(broken)
    keeper.enable()
    keeper.release()
    assert keeper.count == 0
    with keeper.hold():  # the run goes on although the PC could not be held
        assert keeper.count == 1
    assert keeper.count == 0


def test_the_servers_keeper_asks_the_system_for_nothing_in_a_test():
    assert awake.KEEPER.count == 0 and awake.KEEPER.on is False


def test_a_task_run_holds_the_pc_awake_for_as_long_as_it_holds_the_slot(line, monkeypatch):  # noqa: F811
    setter, seen = Setter(), []
    keeper = Awake(setter)
    keeper.enable()
    line.q.awake = keeper
    line.q.spawn = line.held.append
    real = line.engine.advance

    def advance(*args, **kwargs):
        seen.append((keeper.on, list(setter.calls)))
        return real(*args, **kwargs)

    monkeypatch.setattr(line.engine, "advance", advance)
    line.gate.go.set()
    task = line.backlog(title="절전 방지")
    ok(line.ask(task))
    assert setter.calls == []  # the slot is given, the thread has not begun
    while line.held:
        line.held.pop()()
    line.settled(task)
    assert seen and all(on and calls == [True] for on, calls in seen)  # on while the run was advanced
    assert setter.calls == [True, False] and keeper.count == 0


def test_a_task_thread_that_raises_still_lets_the_pc_sleep(line, monkeypatch):  # noqa: F811
    setter = Setter()
    keeper = Awake(setter)
    keeper.enable()
    line.q.awake = keeper
    line.q.spawn = line.held.append

    def boom(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(line.engine, "advance", boom)
    line.gate.go.set()
    task = line.backlog(title="실패")
    ok(line.ask(task))
    while line.held:
        line.held.pop()()
    assert setter.calls == [True, False] and keeper.count == 0


def _turns(keeper):
    return chat.ChatTurns(None, SimpleNamespace(relays_dir="relays"), spawn=lambda work: work(), awake=keeper)


def test_a_chat_turn_holds_the_pc_awake_and_lets_go_when_the_turn_raises():
    setter, seen = Setter(), []
    keeper = Awake(setter)
    keeper.enable()
    turns = _turns(keeper)

    def turn(employee_id, room_id):
        seen.append(keeper.on)
        raise RuntimeError("the run failed")

    turns.turn = turn
    turns._work("emp", "room")  # nobody waits for the thread: the failure is logged
    assert seen == [True] and setter.calls == [True, False] and keeper.count == 0
    assert turns._busy == {}


def test_a_group_rooms_line_holds_the_pc_awake_until_the_line_is_empty():
    setter, seen = Setter(), []
    keeper = Awake(setter)
    keeper.enable()
    turns = _turns(keeper)

    def group_turn(employee_id, room_id, mentioned):
        seen.append((employee_id, keeper.on))
        if employee_id == "a":
            raise RuntimeError("one turn fails; the line goes on")

    turns._group_turn = group_turn
    turns._rooms["room"] = deque([("a", False), ("b", False)])
    turns._group_work("room")
    assert seen == [("a", True), ("b", True)]
    assert setter.calls == [True, False] and keeper.count == 0 and "room" not in turns._rooms


def test_an_empty_group_line_still_pairs_its_hold():
    setter = Setter()
    keeper = Awake(setter)
    keeper.enable()
    _turns(keeper)._group_work("nobody")
    assert setter.calls == [True, False]
