"""V-4: the usage safety net — the limits over REST (studio/limits.py), the usage-window notice and the optional hold of
the line (studio/usage.py; ARCHITECTURE.md §4.6 · §5.9 · §6.4). Fake window rows and the mock runner: no Claude is
asked, and the local settings file is a file in tmp_path."""
import pytest
import yaml

from relay_agent.config import Config
from relay_agent.studio import api, limits, push, queue, usage
from relay_agent.studio.inbox import SYSTEM_ROOM
from test_studio_inbox import ok
from test_studio_queue import line  # noqa: F401 - the fixture `rig` stands on
from test_studio_runs import lab  # noqa: F401
from test_studio_talk import talk  # noqa: F401 - (4) is the exchange limit of a task
from test_studio_cutoff import bench  # noqa: F401
from test_studio_ws import wait

FAR = 4_000_000_000.0  # a reset time far ahead


class Window:
    """What `providers.status('claude')` says: the rows of `limits`, which the test sets."""

    def __init__(self, use=0.92, resets_at=FAR, window="five_hour", gating=True, stale=False):
        self.rows = [{"window": window, "utilization": use, "resets_at": resets_at, "gating": gating, "stale": stale}]

    def __call__(self):
        return [{"name": "claude", "limits": self.rows}]


def keep(line, monkeypatch):  # noqa: F811
    """The config of the lab is the server's own and shared by the tests: whatever a test sets is put back."""
    for field in (*limits.FIELDS.values(), "studio_usage_warn_at", "studio_usage_hold"):
        monkeypatch.setattr(line.cfg, field, getattr(line.cfg, field))


class Rig:
    """The line of `queue.line` with a usage watch on its store, the queue holding by the watch's word the way
    server.py connects it."""

    def __init__(self, line, monkeypatch):  # noqa: F811
        self.line, self.window = line, Window()
        self.cfg = line.cfg
        keep(line, monkeypatch)
        self.watch = self.make()
        line.q = queue.TaskQueue(line.engine, line.fx, api.run_inbox, limit=lambda: self.cfg.studio_max_runs,
                                 retry_after=None, hold=self.watch.held, on_held=self.watch.ask)
        line.fx.kick, line.fx.holds = line.q.kick, line.q.holds
        monkeypatch.setattr(api, "_queue", line.q)

    def make(self):
        return usage.UsageWatch(lambda: self.line.store, status=self.window, warn_at=lambda: self.cfg.studio_usage_warn_at,
                                hold_on=lambda: self.cfg.studio_usage_hold, kick=lambda: self.line.q.kick())

    def told(self):
        return [m for m in self.line.store.list_messages(SYSTEM_ROOM, limit=200) if (m["meta"] or {}).get("event") == "usage_high"]

    def items(self):
        return [i for i in self.line.store.list_inbox(kind="budget") if usage.is_hold(i)]


@pytest.fixture
def rig(line, monkeypatch):  # noqa: F811
    return Rig(line, monkeypatch)


# --- (1) the limits over REST ------------------------------------------------------------------------------------
def test_limits_are_read_changed_kept_in_the_local_file_and_range_checked(line, monkeypatch, tmp_path):  # noqa: F811
    keep(line, monkeypatch)
    local = tmp_path / "relay.config.local.yaml"
    local.write_text("repos:\n  game:\n    path: D:/game\n", encoding="utf-8")
    monkeypatch.setattr(limits, "LOCAL_CONFIG", local)
    now = ok(line.owner.get("/studio/machine/limits"))
    assert now == {"max_runs": line.cfg.studio_max_runs, "max_exchanges": line.cfg.studio_max_exchanges,
                   "group_max_exchanges": line.cfg.studio_group_max_exchanges,
                   "group_rounds": line.cfg.studio_group_rounds}

    for bad in ({}, {"max_runs": 0}, {"max_runs": -1}, {"max_runs": 21}, {"max_runs": "5"}, {"max_runs": True},
                {"max_runs": 2.5}, {"max_exchanges": 201}, {"group_max_exchanges": 0}, {"group_rounds": 0}, {"group_rounds": 11}, {"unknown": 1}):
        assert line.owner.patch("/studio/machine/limits", json=bad).status_code == 422, bad
    assert ok(line.owner.get("/studio/machine/limits")) == now and yaml.safe_load(local.read_text("utf-8")) == {
        "repos": {"game": {"path": "D:/game"}}}  # nothing changed, nothing written

    out = ok(line.owner.patch("/studio/machine/limits", json={"max_runs": 1, "group_max_exchanges": 4}))
    assert out == {**now, "max_runs": 1, "group_max_exchanges": 4}
    assert ok(line.owner.get("/studio/machine/limits")) == out
    assert ok(line.owner.get("/studio/machine"))["limits"]["max_concurrent_runs"] == 1
    saved = yaml.safe_load(local.read_text("utf-8"))
    assert saved == {"repos": {"game": {"path": "D:/game"}}, "studio_max_runs": 1, "studio_group_max_exchanges": 4}
    again = Config.load(tmp_path / "relay.config.yaml")  # a restart reads the file: the value is still there
    assert (again.studio_max_runs, again.studio_group_max_exchanges, again.studio_max_exchanges) == (
        1, 4, line.cfg.studio_max_exchanges)
    out = ok(line.owner.patch("/studio/machine/limits", json={"group_rounds": 5}))  # T1.1: the most rounds of a discussion
    assert out == {**now, "max_runs": 1, "group_max_exchanges": 4, "group_rounds": 5} and line.cfg.studio_group_rounds == 5
    assert yaml.safe_load(local.read_text("utf-8"))["studio_group_rounds"] == 5
    assert Config.load(tmp_path / "relay.config.yaml").studio_group_rounds == 5


def _limit_cfg():
    import types
    return types.SimpleNamespace(studio_max_runs=3, studio_max_exchanges=10, studio_group_max_exchanges=5,
                                  studio_group_rounds=3)


def test_saving_limits_keeps_the_comments_of_the_local_file(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text("# 머리 주석\nrepos:\n  game:\n    # 저장소 주석\n    path: D:/game\nstudio_max_runs: 3  # 동시 실행\n",
                     encoding="utf-8")
    cfg = _limit_cfg()
    out = limits.save(cfg, {"max_runs": 5, "max_exchanges": 7}, path=local)
    assert out["max_runs"] == 5 and out["max_exchanges"] == 7
    text = local.read_text("utf-8")
    assert "# 머리 주석" in text and "# 저장소 주석" in text
    assert "studio_max_runs: 5  # 동시 실행" in text.splitlines()
    assert text.rstrip().endswith("studio_max_exchanges: 7")
    assert yaml.safe_load(text) == {"repos": {"game": {"path": "D:/game"}}, "studio_max_runs": 5, "studio_max_exchanges": 7}


def test_saving_limits_into_a_shape_it_cannot_patch_writes_the_file_anew(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text("studio_max_runs:\n  3\n", encoding="utf-8")
    cfg = _limit_cfg()
    limits.save(cfg, {"max_runs": 4}, path=local)
    assert yaml.safe_load(local.read_text("utf-8")) == {"studio_max_runs": 4} and cfg.studio_max_runs == 4


def test_a_file_that_cannot_be_written_changes_nothing(line, monkeypatch, tmp_path):  # noqa: F811
    keep(line, monkeypatch)
    monkeypatch.setattr(limits, "LOCAL_CONFIG", tmp_path / "no" / "such" / "dir" / "local.yaml")
    before = ok(line.owner.get("/studio/machine/limits"))
    r = line.owner.patch("/studio/machine/limits", json={"max_runs": 2})
    assert r.status_code == 500, r.text
    assert ok(line.owner.get("/studio/machine/limits")) == before


def test_a_changed_max_runs_is_the_queues_at_once(rig, monkeypatch, tmp_path):  # rig keeps the config
    line = rig.line
    monkeypatch.setattr(limits, "LOCAL_CONFIG", tmp_path / "relay.config.local.yaml")
    ok(line.owner.patch("/studio/machine/limits", json={"max_runs": 1}))
    first, second = line.backlog(line.project(), title="첫째"), line.backlog(line.project(), title="둘째")
    for task in (first, second):
        ok(line.ask(task))
    wait(lambda: line.gate.now == 1)
    assert set(line.q.slots()) == {first["id"]} and line.row(second)["status"] == "backlog"  # one slot in all
    ok(line.owner.patch("/studio/machine/limits", json={"max_runs": 2}))  # no restart: the second starts
    wait(lambda: line.gate.now == 2)
    assert set(line.q.slots()) == {first["id"], second["id"]}
    line.gate.go.set()
    line.settled(first, second)


# --- (2) the notice, and the work goes on ------------------------------------------------------------------------
def test_a_window_at_the_threshold_is_told_once_pushed_at_once_and_the_task_starts_all_the_same(rig):
    line, window = rig.line, rig.window
    assert rig.cfg.studio_usage_warn_at == 0.90 and rig.cfg.studio_usage_hold is False
    window.rows[0]["utilization"] = 0.85
    assert rig.watch.look() == [] and rig.told() == []  # under the threshold
    window.rows[0]["utilization"] = 0.92
    [message] = rig.watch.look()
    assert message["author_kind"] == "system" and "92%" in message["body"] and "그대로 시작" in message["body"]
    assert message["meta"] == {"event": "usage_high", "provider": "claude", "window": "five_hour",
                               "utilization": 0.92, "resets_at": FAR, "warn_at": 0.90, "held": False}
    grade, title, _, link = push._of_message({"message": message}, line.store)
    assert (grade, title, link) == (push.URGENT, "사용량 한도", {"type": "room", "id": SYSTEM_ROOM})
    assert rig.watch.look() == [] and rig.watch.look() == [] and len(rig.told()) == 1  # the same window: once
    assert rig.make().look() == [] and len(rig.told()) == 1  # and not again after a restart (the room is the record)

    task = line.backlog()
    ok(line.ask(task))
    wait(lambda: line.gate.now == 1)  # no hold by default: it began
    assert line.row(task)["status"] == "working" and rig.items() == []
    line.gate.go.set()
    line.settled(task)


def test_a_reset_window_is_told_anew_and_stale_other_and_off_are_not(rig):
    window = rig.window
    assert len(rig.watch.look()) == 1
    window.rows[0]["resets_at"] = FAR + 5 * 3600  # the next window
    assert len(rig.watch.look()) == 1 and len(rig.told()) == 2
    window.rows.append({"window": "seven_day", "utilization": 0.99, "resets_at": FAR, "gating": False, "stale": False})
    window.rows.append({"window": "seven_day", "utilization": 0.0, "resets_at": 1.0, "gating": True, "stale": True})
    assert rig.watch.look() == []  # not gating / stale
    window.rows[0]["resets_at"] = FAR + 10 * 3600
    rig.cfg.studio_usage_warn_at = 0  # 0 or less = off
    assert rig.watch.look() == [] and len(rig.told()) == 2


def test_the_threshold_is_below_the_95_percent_switch():
    from relay_agent.providers import ProviderSpec

    assert Config().studio_usage_warn_at < ProviderSpec.model_fields["switch_at_utilization"].default


# --- (3) the hold ------------------------------------------------------------------------------------------------
def test_with_the_hold_on_a_task_waits_for_the_owner_and_starts_after_the_approval(rig):
    line, window = rig.line, rig.window
    rig.cfg.studio_usage_hold = True
    task = line.backlog()
    ok(line.ask(task))
    assert line.row(task)["status"] == "backlog" and line.row(task)["queued_at"] and line.gate.now == 0  # held back
    [item] = rig.items()
    assert (item["status"], item["task_id"], item["run_id"], item["requester_kind"]) == ("pending", None, None, "system")
    assert item["payload"]["stop_reason"] == "usage_hold" and item["payload"]["window"] == "five_hour"
    grade, title, *_ = push._of_inbox({"inbox": item}, line.store)
    assert grade == push.URGENT
    line.q.kick()
    line.q.kick()
    assert len(rig.items()) == 1 and line.row(task)["status"] == "backlog"  # asked once
    listed = ok(line.owner.get("/studio/inbox"))["items"]  # the sweep of a list does not close a run-less item
    assert [i["id"] for i in listed if i["id"] == item["id"]] == [item["id"]]
    assert ok(line.owner.get(f"/studio/inbox/{item['id']}"))["allowed_decisions"] == ["approve", "reject"]
    assert line.lab.decide(item, "revise", comment="다르게").status_code == 422

    done = ok(line.lab.decide(item, "approve"))
    assert done["inbox"]["status"] == "decided" and done["inbox"]["decision"] == "approve" and done["task"] is None
    wait(lambda: line.gate.now == 1)  # the decision looked at the line again: the task began
    assert line.row(task)["status"] == "working"
    line.gate.go.set()
    line.settled(task)
    assert len(rig.items()) == 1  # no second question for this window


def test_a_working_task_is_not_held_and_a_rejected_hold_lasts_until_the_window_is_reset(rig):
    line, window = rig.line, rig.window
    first = line.backlog()
    ok(line.ask(first))
    wait(lambda: line.gate.now == 1)  # started before the window was high
    rig.cfg.studio_usage_hold = True
    second = line.backlog(line.project())
    ok(line.ask(second))
    assert line.row(second)["status"] == "backlog" and line.row(first)["status"] == "working"
    [item] = rig.items()
    ok(line.lab.decide(item, "reject"))
    line.q.kick()
    # policy: a rejected hold stays until the window is reset (provider, window, resets_at change) — no second question
    assert line.row(second)["status"] == "backlog" and len(rig.items()) == 1  # still held, and not asked again
    window.rows[0]["resets_at"] = FAR + 5 * 3600  # the window was reset (and is high again): a new question
    line.q.kick()
    assert [i["status"] for i in rig.items()] == ["decided", "pending"]
    window.rows[0]["utilization"] = 0.1  # low
    rig.watch.look()  # the look at the line frees what the window held
    wait(lambda: line.row(second)["status"] == "working")
    line.gate.go.set()
    line.settled(first, second)


def test_the_hold_off_asks_nothing_and_a_look_does_not_kick(rig):
    kicks = []
    watch = usage.UsageWatch(lambda: rig.line.store, status=rig.window, warn_at=lambda: 0.9, kick=lambda: kicks.append(1))
    watch.look()
    assert kicks == [] and watch.held() is False and watch.ask() is None and rig.items() == []


def test_the_watch_looks_now_and_every_minute_until_it_is_stopped(rig):
    class Timers:
        def __init__(self):
            self.waits = []

        def __call__(self, delay, fn):
            self.waits.append((delay, fn))
            return type("T", (), {"cancel": lambda self: None})()

    timers = Timers()
    rig.watch.watch(timer=timers)
    delay, fn = timers.waits.pop(0)
    assert delay == 0
    fn()
    assert len(rig.told()) == 1 and [d for d, _ in timers.waits] == [usage.CHECK_EVERY] == [60.0]
    rig.watch.unwatch()
    timers.waits.pop(0)[1]()
    assert timers.waits == []


# --- (4) the exchange limit: stop (T4 question) -> decision -> go on (T5) ------------------------------------------
def test_a_limit_changed_over_rest_stops_the_exchanges_and_the_owners_answer_goes_on(line, talk, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(limits, "LOCAL_CONFIG", tmp_path / "relay.config.local.yaml")
    keep(line, monkeypatch)
    ok(line.owner.patch("/studio/machine/limits", json={"max_exchanges": 2}))
    task, run_id, me, room = talk.at_work()
    mate = talk.mate(room)
    talk.replies.append(("답 1", [me]))
    assert talk.call("room_post", talk.token(task), text="질문 1", mentions=[mate])["queued"] == [mate]
    talk.rested()
    assert line.row(task)["exchange_count"] == 2  # the new limit of 2 is the one looked at
    out = talk.call("room_post", talk.token(task), text="질문 2", mentions=[mate])
    assert (out["ok"], out["code"]) == (False, "exchange_limit")
    item = line.store.get_inbox_item(out["inbox_id"])
    assert (item["kind"], item["payload"]["reason"], item["status"]) == ("question", "exchange_limit", "pending")
    assert line.row(task)["status"] == "blocked"
    talk.bench.clock.advance(60)
    wait(lambda: line.q.idle(), 30)

    done = ok(line.lab.decide(item, "answer", data={"option_id": "continue"}))
    assert (done["task"]["status"], done["task"]["exchange_count"]) == ("working", 0)
    wait(lambda: line.gate.now == 1)
    ok(line.owner.patch("/studio/machine/limits", json={"max_exchanges": 3}))
    talk.replies.append(("답 2", [me]))
    out = talk.call("room_post", talk.token(task), text="질문 3", mentions=[mate])
    assert out["ok"] and out["queued"] == [mate], out
    talk.rested()
    assert line.row(task)["exchange_count"] == 2
