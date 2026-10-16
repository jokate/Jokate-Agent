"""Progress counts and the daily report (S-13; ARCHITECTURE §6.4 `GET /studio/stats` · `progress`, §5.9 일일 보고):
the counts by status and the done ratio, what each employee finished and spent, the mean wait of the inbox, the
period; the report's three lines, a model answer that breaks the form, one report a day, and its push as the digest.
No AI is called — the writer is a function of the test — and the clock is the test's."""
import copy
import importlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from relay_agent.studio import Actor, Effects, TaskMachine, api, push, stats, tasks
from relay_agent.studio.tasks import OWNER, SYSTEM

store_module = importlib.import_module("relay_agent.studio.store")
inbox_module = importlib.import_module("relay_agent.studio.inbox")

WATCH = stats.Reporter.watch  # the real one: conftest.py lets no test's server start the timer
DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
TOKEN = "shared-Token-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TEXT = dict(title="2단 점프", reason="조작감", spec="공중에서 한 번 더", done_criteria="테스트 통과", type="feature")
QUESTION = {"inbox": {"kind": "question", "title": "점프 높이는?", "payload": {"question": "점프 높이는?"}}}
REVIEW = {"inbox": {"title": "검수 요청", "payload": {"summary": "끝"}}}
PATHS = {  # state -> the steps from a task the owner registered: (to, who, arguments)
    "backlog": [],
    "cancelled": [("cancelled", "owner", {})],
    "blocked": [("working", "system", {}), ("blocked", "employee", QUESTION)],
    "done": [("working", "system", {}), ("review", "employee", REVIEW), ("done", "owner", {})],
    "reworked": [("working", "system", {}), ("review", "employee", REVIEW), ("working", "owner", {}),   # T8
                 ("review", "employee", REVIEW), ("done", "owner", {})],
    "reverted": [("working", "system", {}), ("review", "employee", REVIEW), ("backlog", "owner", {}),   # T9
                 ("working", "system", {}), ("review", "employee", REVIEW), ("done", "owner", {})],
}
DAY1, DAY2 = "2026-03-02", "2026-03-03"
COST = {"run_001": 0.1, "run_002": 0.2, "run_003": 0.4, "run_004": 0.8}  # what the engine would say of each run
ZERO = dict.fromkeys(tasks.STATES, 0)


def at(day, clock):
    return datetime.fromisoformat(f"{day}T{clock}:00")


class Fx(Effects):
    """Stands in for the runs: each start (T3) gets the next run id."""

    def __init__(self):
        self.runs = 0

    def create_run(self, task):
        self.runs += 1
        return f"run_{self.runs:03d}"

    def check(self, rule, task):
        pass

    def before(self, rule, task):
        pass

    def after(self, what, row, event_ids):
        pass


class Clock:
    """`now()` of the store, the task machine and the inbox: every row is stamped with the time the test set."""

    def __init__(self, monkeypatch):
        self.at = f"{DAY1}T09:00:00"
        for module in (store_module, tasks, inbox_module):
            monkeypatch.setattr(module, "now", lambda: self.at)
        monkeypatch.setattr(stats, "_now", lambda: self.at)  # the "today" of `GET /studio/growth`


class World:
    """The server app on a studio DB of its own: two projects, two employees, and a machine to set states up."""

    def __init__(self, server, fx, clock):
        self.owner = TestClient(server.app, headers=AUTH)
        self.fx, self.clock = fx, clock
        self.store = s = api.get_store()
        self.m = TaskMachine(s, fx)
        self.pid = s.create_project("게임", "D:/game", "game")["id"]
        self.other = s.create_project("도구", "D:/tool", "tool")["id"]
        self.minsu = s.create_employee("민수", "program", project_id=self.pid)["id"]
        self.jia = s.create_employee("지아", "program", project_id=self.pid)["id"]
        self.hired = 0

    def reopen(self):
        """The server stopped and started: the same DB file, opened anew."""
        api.close_store()
        self.store = api.get_store()
        self.m = TaskMachine(self.store, self.fx)

    def hire(self):
        self.hired += 1
        return self.store.create_employee(f"직원{self.hired}", "program", project_id=self.pid)["id"]

    def task(self, state="backlog", *, at=None, who=None, campaign=None, **over):
        """A task of the first project taken to `state` by the machine, every step at the time `at`. `who` is
        its assignee (a new employee when not given). `campaign`: put in that campaign once it is there."""
        if at:
            self.clock.at = at
        if state == "rejected":
            row = self.m.create_task(Actor.employee(who or self.minsu), "proposed", self.pid, **{**TEXT, **over}).task
            self.m.transition(row["id"], "rejected", OWNER, comment="지금은 안 한다")
        else:
            fields = {"assignee_id": who or self.hire(), **TEXT, **over}
            row = self.m.create_task(OWNER, "backlog", self.pid, **fields).task
            for to, actor, kwargs in PATHS[state]:
                if to == "working" and actor == "system":  # T3: the start is asked for first
                    self.m.request_start(row["id"], OWNER)
                by = {"owner": OWNER, "system": SYSTEM}.get(actor) or Actor.employee(row["assignee_id"])
                self.m.transition(row["id"], to, by, **copy.deepcopy(kwargs))
                if to == "working" and actor == "owner":  # T8 waits in the line: the slot came, the run goes on
                    with self.store.transaction() as tx:
                        tx.exec("UPDATE tasks SET queued_at = NULL WHERE id = ?", (row["id"],))
        if campaign:
            self.store.update_task(row["id"], campaign_id=campaign)
        return self.store.get_task(row["id"])

    def stats(self, **params):
        r = self.owner.get("/studio/stats", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    def growth(self):
        r = self.owner.get("/studio/growth")
        assert r.status_code == 200, r.text
        return r.json()

    def people(self, **params):
        """employee id -> (tasks_done, cost_usd) of that answer."""
        return {e["id"]: (e["tasks_done"], e["cost_usd"]) for e in self.stats(**params)["employees"]}

    def reports(self):
        return [m for m in self.store.list_messages("room_system", limit=500)
                if (m["meta"] or {}).get("event") == "daily_report"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    import relay_agent.server as server

    fx = Fx()
    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    monkeypatch.setattr(api, "_effects", fx)
    monkeypatch.setattr(api, "_run_cost", lambda run_id: COST.get(run_id, 0.0))
    api.close_store()
    clock = Clock(monkeypatch)
    yield World(server, fx, clock)
    api.close_store()


def studio_of(world):
    """Two days of work: campaign `a` of the first project with six tasks, an empty campaign `b` of the other
    project, and a task of no campaign. Returns (a, b, the employee whose task is blocked)."""
    s = world.store
    a = s.create_campaign(world.pid, "1막", "1막을 끝낸다", mode="each", status="draft")["id"]
    b = s.create_campaign(world.other, "도구", "도구를 낸다", mode="each", status="draft")["id"]
    world.task("done", at=f"{DAY1}T10:00:00", who=world.minsu, campaign=a)                    # run_001
    world.task("done", at=f"{DAY2}T10:00:00", who=world.minsu, campaign=a)                    # run_002
    world.task("done", at=f"{DAY2}T11:00:00", who=world.jia)                                  # run_003
    stuck = world.task("blocked", at=f"{DAY2}T12:00:00", campaign=a, title="보스 패턴")        # run_004
    world.task("backlog", campaign=a)
    world.task("cancelled", campaign=a)
    world.task("rejected", campaign=a)
    return a, b, stuck["assignee_id"]


# --- progress (§6.4) ------------------------------------------------------------------------------------------
def test_the_done_ratio_leaves_the_cancelled_and_the_rejected_out():
    progress = stats.progress({"done": 2, "backlog": 1, "blocked": 1, "cancelled": 3, "rejected": 1})
    assert progress["by_status"] == {**ZERO, "done": 2, "backlog": 1, "blocked": 1, "cancelled": 3, "rejected": 1}
    assert progress["total"] == 8 and progress["done_ratio"] == pytest.approx(2 / 4)
    assert stats.progress({"done": 3, "cancelled": 2})["done_ratio"] == 1.0  # all that was left to do is done
    assert stats.progress({}) == {"by_status": ZERO, "total": 0, "done_ratio": 0.0}
    assert stats.progress({"cancelled": 2, "rejected": 1}) == {
        "by_status": {**ZERO, "cancelled": 2, "rejected": 1}, "total": 3, "done_ratio": 0.0}


# --- GET /studio/stats ----------------------------------------------------------------------------------------
def test_stats_counts_the_campaigns_the_employees_and_the_inbox(world):
    a, b, stuck = studio_of(world)
    out = world.stats()
    assert set(out) == {"campaigns", "employees", "inbox"}
    assert out["campaigns"] == [
        {"id": a, "progress": {"by_status": {**ZERO, "done": 2, "blocked": 1, "backlog": 1, "cancelled": 1,
                                             "rejected": 1}, "total": 6, "done_ratio": 0.5}},
        {"id": b, "progress": {"by_status": ZERO, "total": 0, "done_ratio": 0.0}}]
    assert world.owner.get("/studio/campaigns").json()["items"][0]["progress"] == out["campaigns"][0]["progress"]

    assert [e["id"] for e in out["employees"]] == [e["id"] for e in world.store.list_employees()]  # as they were made
    assert all(set(e) == {"id", "tasks_done", "cost_usd"} for e in out["employees"])
    people = world.people()
    assert people[world.minsu] == (2, 0.3) and people[world.jia] == (1, 0.4) and people[stuck] == (0, 0.8)
    assert sorted(v for k, v in people.items() if k not in (world.minsu, world.jia, stuck)) == [(0, 0.0), (0, 0.0)]
    assert out["inbox"]["pending"] == 1  # the question the blocked task stands on


def test_stats_are_narrowed_by_project_and_campaign(world):
    a, b, stuck = studio_of(world)
    one = world.stats(campaign_id=a)
    assert [c["id"] for c in one["campaigns"]] == [a] and one["inbox"]["pending"] == 1
    people = world.people(campaign_id=a)
    assert people[world.minsu] == (2, 0.3) and people[world.jia] == (0, 0.0) and people[stuck] == (0, 0.8)

    empty = world.stats(campaign_id=b)
    assert [c["id"] for c in empty["campaigns"]] == [b]
    assert empty["inbox"] == {"pending": 0, "avg_wait_seconds": None}
    assert set(world.people(campaign_id=b).values()) == {(0, 0.0)}

    elsewhere = world.stats(project_id=world.other)
    assert [c["id"] for c in elsewhere["campaigns"]] == [b] and elsewhere["employees"] == []
    assert world.people(project_id=world.pid) == world.people()
    assert world.stats(campaign_id="cmp_none")["campaigns"] == []

    world.store.update_employee(world.jia, retired_at=f"{DAY2}T13:00:00")  # one who left keeps what was done
    left = world.store.create_employee("퇴사자", "program", project_id=world.pid)["id"]
    world.store.update_employee(left, retired_at=f"{DAY2}T13:00:00")
    people = world.people()
    assert people[world.jia] == (1, 0.4) and left not in people


def test_stats_count_what_happened_inside_the_period(world):
    a, b, stuck = studio_of(world)
    day2 = world.people(**{"from": DAY2, "to": DAY2})
    assert day2[world.minsu] == (1, 0.2) and day2[world.jia] == (1, 0.4) and day2[stuck] == (0, 0.8)
    day1 = world.people(**{"from": DAY1, "to": DAY1})  # a day as `to` is that whole day
    assert day1[world.minsu] == (1, 0.1) and day1[world.jia] == (0, 0.0) and day1[stuck] == (0, 0.0)
    assert world.people(to=f"{DAY2}T10:30:00")[world.minsu] == (2, 0.3)
    assert world.people(to=f"{DAY2}T10:00:00")[world.minsu] == (1, 0.1)              # `to` itself is outside
    assert world.people(**{"from": f"{DAY2}T10:00:00"})[world.minsu] == (1, 0.2)     # `from` itself is inside
    assert set(world.people(**{"from": "2026-04-01"}).values()) == {(0, 0.0)}

    later = world.stats(**{"from": "2026-04-01"})  # the progress and the waiting items are those of now
    assert later["campaigns"] == world.stats()["campaigns"] and later["inbox"]["pending"] == 1

    for bad in ({"from": "어제"}, {"to": "2026-13-01"}, {"from": DAY2, "to": DAY1}):
        assert world.owner.get("/studio/stats", params=bad).status_code == 422, bad


def test_the_cost_of_a_task_that_changed_hands_is_the_new_assignees(world):
    world.clock.at = f"{DAY1}T10:00:00"
    tid = world.m.create_task(OWNER, "backlog", world.pid, assignee_id=world.minsu, **TEXT).task["id"]
    world.m.request_start(tid, OWNER)
    world.m.transition(tid, "working", SYSTEM)                                        # run_001, 민수 at work
    world.m.transition(tid, "review", Actor.employee(world.minsu), **copy.deepcopy(REVIEW))
    world.m.transition(tid, "backlog", OWNER)                                         # T9: sent back
    assert world.people()[world.minsu] == (0, 0.1)
    world.m.assign(tid, OWNER, world.jia)
    task = world.store.get_task(tid)
    assert (task["status"], task["assignee_id"], task["run_id"]) == ("backlog", world.jia, None)
    people = world.people()  # the T3 row keeps no assignee: the old run is counted to the assignee of now
    assert people[world.jia] == (0, 0.1) and people[world.minsu] == (0, 0.0)


def test_a_request_reads_each_run_of_the_period_once(world, monkeypatch):
    studio_of(world)
    read = []
    monkeypatch.setattr(api, "_run_cost", lambda run_id: read.append(run_id) or COST[run_id])
    world.stats()
    assert read == ["run_001", "run_002", "run_003", "run_004"]
    del read[:]
    world.stats(**{"from": DAY2, "to": DAY2})  # the period is what narrows the reading
    assert read == ["run_002", "run_003", "run_004"]
    del read[:]
    world.stats(**{"from": DAY2, "to": DAY2})  # nothing is kept: the next request reads them again
    assert read == ["run_002", "run_003", "run_004"]


def test_the_inbox_wait_is_the_mean_of_the_items_decided_in_the_period(world):
    s = world.store

    def item(closed=None, status="decided"):
        world.clock.at = f"{DAY1}T09:00:00"
        row = s.create_inbox_item("question", "점프 높이는?", "system", project_id=world.other, payload={})
        if closed:
            decision = {"decision": "approve"} if status == "decided" else {}
            s.update_inbox_item(row["id"], status=status, closed_at=closed, **decision)

    assert world.stats(project_id=world.other)["inbox"] == {"pending": 0, "avg_wait_seconds": None}
    item(f"{DAY1}T09:01:00")               # decided after a minute
    item(f"{DAY2}T09:00:00")               # decided after a day
    item(f"{DAY1}T09:00:30", "cancelled")  # not decided: not in the mean
    item()                                 # still waiting
    assert world.stats(project_id=world.other)["inbox"] == {"pending": 1, "avg_wait_seconds": 43230.0}
    day1 = world.stats(project_id=world.other, **{"from": DAY1, "to": DAY1})["inbox"]
    assert day1 == {"pending": 1, "avg_wait_seconds": 60.0}  # by the time of the decision
    assert world.stats(project_id=world.other, **{"from": "2026-04-01"})["inbox"] == {
        "pending": 1, "avg_wait_seconds": None}
    assert world.stats(project_id=world.pid)["inbox"]["pending"] == 0


def test_the_period_is_read_as_local_time():
    assert stats.bounds(None, None) == (None, None) and stats.bounds("", " ") == (None, None)
    assert stats.bounds(DAY1, DAY1) == (f"{DAY1}T00:00:00", f"{DAY2}T00:00:00")
    assert stats.bounds(f"{DAY1}T09:30", f"{DAY1}T10:00:05.900000") == (f"{DAY1}T09:30:00", f"{DAY1}T10:00:05")
    local = datetime(2026, 3, 2, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert stats.bounds(f"{DAY1}T00:00:00+00:00", None) == (local.isoformat(timespec="seconds"), None)
    for bad in (("어제", None), (None, "2026-13-01"), (DAY2, DAY1), (f"{DAY1}T10:00:00", f"{DAY1}T10:00:00")):
        with pytest.raises(ValueError):
            stats.bounds(*bad)


# --- the daily report (§5.9) ----------------------------------------------------------------------------------
def test_the_report_is_three_lines_in_the_hr_employees_name(world):
    hr = world.store.create_employee("인사", "hr")["id"]
    world.task("done", at=f"{DAY1}T10:00:00", who=world.minsu)
    world.task("blocked", at=f"{DAY1}T11:00:00", title="보스 패턴")
    waiting, = [row["title"] for row in world.store.list_inbox(status="pending")]

    message = stats.report(world.store, now=at(DAY1, "18:00"))
    lines = message["body"].split("\n")
    assert lines == ["한 일: 1개 — 2단 점프", "막힌 것: 1개 — 보스 패턴", f"결재 대기: 1개 — {waiting}"]
    assert stats.well_formed(message["body"]) == lines
    assert (message["room_id"], message["author_kind"], message["author_id"]) == ("room_system", "employee", hr)
    assert message["meta"] == {"event": "daily_report", "date": DAY1, "from": "2026-03-01T18:00:00",
                               "to": f"{DAY1}T18:00:00"}
    assert world.reports() == [message]

    event = world.store.list_stream_events(limit=5000)[-1]  # the push of it is the digest, and opens the room
    assert event["type"] == "message.created" and event["data"]["message"]["id"] == message["id"]
    assert stats.EVENT == push.DAILY_REPORT
    assert push.classify(event, world.store) == (
        push.DIGEST, push.DAILY_TITLE, "한 일: 1개 — 2단 점프", {"type": "room", "id": "room_system"})


def test_the_rule_names_three_titles_and_counts_the_rest():
    found = {"done": ["첫\n작업", "둘째", "셋째", "넷째", "다섯째"], "blocked": ["가" * 300], "pending": []}
    lines = stats.rule_lines(found)
    assert lines[0] == "한 일: 5개 — 첫 작업, 둘째, 셋째 외 2개"
    assert lines[1] == "막힌 것: 1개 — " + "가" * 39 + "…" and lines[2] == "결재 대기: 없음"
    assert stats.well_formed("\n".join(lines)) == lines
    long = stats.rule_lines({"done": ["가" * 300] * 9, "blocked": ["나 " * 200] * 9, "pending": ["다\r\n" * 99] * 9})
    assert stats.well_formed("\n".join(long)) == long and all(len(line) <= stats.LINE_MAX for line in long)
    assert stats.rule_lines({}) == ["한 일: 없음", "막힌 것: 없음", "결재 대기: 없음"]


GOOD = "한 일: 2단 점프를 끝냈다\n막힌 것: 없음\n결재 대기: 점프 높이를 정해 주세요"


@pytest.mark.parametrize("answer", [
    "오늘은 2단 점프를 끝냈습니다.",                            # no form at all
    "한 일: 점프\n막힌 것: 없음",                               # two lines
    GOOD + "\n수고하셨습니다",                                  # a fourth line
    "오늘의 보고입니다\n" + GOOD,                               # a line before
    "한 일: 점프\n\n막힌 것: 없음\n결재 대기: 없음",            # a blank line between
    "막힌 것: 없음\n한 일: 점프\n결재 대기: 없음",              # the order
    "한 일:\n막힌 것: 없음\n결재 대기: 없음",                   # a label with nothing after it
    "- 한 일: 점프\n- 막힌 것: 없음\n- 결재 대기: 없음",        # markdown
    "한 일: " + "가" * 200 + "\n막힌 것: 없음\n결재 대기: 없음",  # a line too long
    "", None, 3,
])
def test_a_model_answer_that_breaks_the_form_is_replaced_by_the_rule(answer):
    found = {"done": ["2단 점프"], "blocked": [], "pending": ["점프 높이는?"]}
    lines = stats.rule_lines(found)
    asked = []
    assert stats.well_formed(answer) is None
    assert stats.polish(found, lines, lambda facts: asked.append(facts) or (answer, None)) == lines
    assert len(asked) == 1 and "2단 점프" in asked[0] and lines[2] in asked[0]  # one call, with the material


def test_a_model_answer_in_the_form_is_what_the_report_says(world):
    found = {"done": ["2단 점프"], "blocked": [], "pending": ["점프 높이는?"]}
    lines = stats.rule_lines(found)
    assert stats.polish(found, lines, lambda facts: (f"\n  {GOOD}\r\n", object())) == GOOD.split("\n")
    assert stats.polish(found, lines, lambda facts: GOOD) == GOOD.split("\n")  # the text alone will do
    assert stats.polish(found, lines, None) == lines

    def broken(facts):
        raise RuntimeError("claude 가 없다")

    assert stats.polish(found, lines, broken) == lines

    world.task("done", at=f"{DAY1}T10:00:00", who=world.minsu)
    message = stats.report(world.store, now=at(DAY1, "18:00"), writer=lambda facts: (GOOD, None))
    assert message["body"] == GOOD and stats.well_formed(message["body"])


def test_a_day_has_one_report_and_the_next_goes_on_from_it(world):
    world.task("done", at=f"{DAY1}T10:00:00", who=world.minsu, title="첫 작업")
    asked = []

    def writer(facts):
        asked.append(facts)
        return "엉뚱한 답", None

    first = stats.report(world.store, now=at(DAY1, "18:00"), writer=writer)
    assert first["body"].split("\n") == ["한 일: 1개 — 첫 작업", "막힌 것: 없음", "결재 대기: 없음"]
    assert (first["author_kind"], first["author_id"]) == ("system", None)  # no HR employee: the system's words
    event = world.store.list_stream_events(limit=5000)[-1]
    assert push.classify(event, world.store)[0] == push.DIGEST             # the digest all the same
    assert stats.report(world.store, now=at(DAY1, "23:59"), writer=writer) is None and len(asked) == 1

    world.reopen()  # the room is the record: a server that starts again writes no second one
    reporter = stats.Reporter(lambda: world.store, at=lambda: "18:00", clock=lambda: at(DAY1, "18:30"),
                              writer=lambda model: writer)
    assert reporter.look() is None and stats.report(world.store, now=at(DAY1, "18:31"), writer=writer) is None
    assert len(asked) == 1 and world.reports() == [first]

    world.task("done", at=f"{DAY2}T09:00:00", who=world.minsu, title="둘째 작업")
    second = stats.report(world.store, now=at(DAY2, "18:00"))
    assert second["body"].split("\n")[0] == "한 일: 1개 — 둘째 작업"  # only what was done since the last report
    assert second["meta"] == {"event": "daily_report", "date": DAY2, "from": first["meta"]["to"],
                              "to": f"{DAY2}T18:00:00"}
    assert world.reports() == [first, second]


def test_a_report_that_runs_past_midnight_is_of_the_day_it_began_on(world):
    world.task("done", at=f"{DAY1}T10:00:00", who=world.minsu, title="첫 작업")

    def slow(facts):  # the model answers after midnight
        world.clock.at = f"{DAY2}T00:00:30"
        return GOOD, None

    first = stats.report(world.store, now=at(DAY1, "23:59"), writer=slow)
    assert first["body"] == GOOD and world.reports()[0]["created_at"].startswith(DAY2)  # written on the next day
    assert (first["meta"]["date"], first["meta"]["to"]) == (DAY1, f"{DAY1}T23:59:00")

    world.task("done", at=f"{DAY2}T09:00:00", who=world.minsu, title="둘째 작업")
    second = stats.report(world.store, now=at(DAY2, "18:00"))  # the next day has its own
    assert second is not None and second["body"].split("\n")[0] == "한 일: 1개 — 둘째 작업"
    assert (second["meta"]["date"], second["meta"]["from"]) == (DAY2, first["meta"]["to"])
    assert world.reports() == [first, second]


def test_the_reporter_writes_once_the_time_of_day_has_come(world):
    clock = SimpleNamespace(at=at(DAY1, "17:59"), report_at="18:00")
    made = []
    reporter = stats.Reporter(lambda: world.store, at=lambda: clock.report_at, model=lambda: "haiku",
                              clock=lambda: clock.at, writer=lambda model: made.append(model))
    assert reporter.look() is None and made == [] and world.reports() == []
    clock.at = at(DAY1, "18:00")
    clock.report_at = ""  # the setting is read at every look: none named, no report
    assert reporter.look() is None and world.reports() == []
    clock.report_at = "18:00"
    assert reporter.look()["meta"]["date"] == DAY1 and made == ["haiku"]
    assert reporter.look() is None and made == ["haiku"] and len(world.reports()) == 1  # no model for nothing

    def no_store():
        raise RuntimeError("studio DB 가 열리지 않았다")

    clock.at = at(DAY2, "19:00")
    assert stats.Reporter(no_store, at=lambda: "18:00", clock=lambda: clock.at).look() is None
    assert stats.Reporter(lambda: None, at=lambda: "18:00", clock=lambda: clock.at).look() is None


@pytest.mark.parametrize("setting, clock, expected", [
    ("18:00", "17:59", False), ("18:00", "18:00", True), ("18:00", "23:59", True), ("9:05", "09:04", False),
    ("9:05", "09:05", True), (" 07:30 ", "08:00", True), ("", "23:59", False), (None, "23:59", False),
    ("24:00", "23:59", False), ("18:60", "23:59", False), ("저녁", "23:59", False), ("00:00", "00:00", True),
])
def test_the_time_of_day_is_read_from_the_setting(setting, clock, expected):
    assert stats.due(setting, at(DAY1, clock)) is expected


class Timers:
    """`timer(delay, fn)` of a test: the waits are kept and none runs by itself."""

    def __init__(self):
        self.waits, self.cancelled = [], 0

    def __call__(self, delay, fn):
        self.waits.append((delay, fn))
        return SimpleNamespace(cancel=self._cancel)

    def _cancel(self):
        self.cancelled += 1

    def fire(self):
        delay, fn = self.waits.pop(0)
        fn()
        return delay


def test_the_first_look_waits_on_a_timer_too():
    timers, looks = Timers(), []
    reporter = stats.Reporter(lambda: None, at=lambda: "")
    reporter.look = lambda: looks.append("look")
    WATCH(reporter, 60.0, timers)
    WATCH(reporter, 60.0, timers)  # a second call while one goes on does nothing
    assert looks == [] and [delay for delay, _ in timers.waits] == [0]  # the server's start does not wait for it
    assert timers.fire() == 0 and looks == ["look"] and [delay for delay, _ in timers.waits] == [60.0]
    assert timers.fire() == 60.0 and looks == ["look", "look"]
    reporter.unwatch()
    assert timers.cancelled == 1
    timers.fire()  # one that fires all the same does nothing
    assert looks == ["look", "look"] and timers.waits == []


def test_no_model_is_made_without_a_name_or_without_claude(monkeypatch):
    monkeypatch.setattr(stats.shutil, "which", lambda name: None)
    assert stats.model_writer("haiku") is None
    monkeypatch.setattr(stats.shutil, "which", lambda name: "C:/bin/claude.exe")
    assert stats.model_writer("") is None and stats.model_writer(None) is None
    writer = stats.model_writer(" haiku ")  # made, never called here
    assert (writer.model, writer.exe, writer.timeout_s) == ("haiku", "C:/bin/claude.exe", 90)
    assert all(f"{label}: " in writer.system for label in stats.LABELS)


# --- GET /studio/growth (§6.4: the rule tables) ---------------------------------------------------------------
DAY3 = "2026-03-04"
NOTHING = {"level": 1, "xp": 0, "level_xp": 0, "next_level_xp": 100}
NOBODY = {**NOTHING, "counts": {"passed": 0, "clean": 0}}
EMPTY = {"count": 0, "items": []}
WHAT = {"passed": "검수 통과", "clean": "재작업 없이 통과", "decided": "처리한 결재"}  # the words of the document


def passes(n, *, reworked=(), who="emp_a"):
    """`n` rows as `store.list_passes` gives them, a minute apart; the numbers in `reworked` were sent back."""
    return [{"id": f"task_{i}", "title": f"작업 {i}", "assignee_id": who, "at": f"{DAY1}T10:{i:02d}:00",
             "reworked": i in reworked} for i in range(1, n + 1)]


def closed(n):
    """The `closed_at` of `n` decided inbox items, a minute apart."""
    return [f"{DAY2}T{i // 60:02d}:{i % 60:02d}:00" for i in range(1, n + 1)]


def achieved(out):
    return {a["id"]: (a["achieved"], a["achieved_at"]) for a in out["achievements"]}


def grown(world):
    """Two days of the studio: five tasks passed — one after a rework, one after it was taken back — and one
    blocked. Returns the tasks by name."""
    return {
        "first": world.task("done", at=f"{DAY1}T10:00:00", who=world.minsu, title="첫 작업"),
        "reworked": world.task("reworked", at=f"{DAY1}T11:00:00", who=world.minsu, title="다시 한 작업"),
        "jia": world.task("done", at=f"{DAY2}T10:00:00", who=world.jia, title="지아의 작업"),
        "reverted": world.task("reverted", at=f"{DAY2}T11:00:00", who=world.jia, title="되돌린 작업"),
        "last": world.task("done", at=f"{DAY2}T12:00:00", who=world.minsu, title="마지막 작업"),
        "stuck": world.task("blocked", at=f"{DAY2}T13:00:00", title="보스 패턴"),
    }


def test_a_level_begins_where_the_rule_says():
    assert (stats.XP_PASS, stats.XP_CLEAN, stats.XP_DECIDED) == (10, 5, 2)
    assert [stats.level_floor(n) for n in range(1, 6)] == [0, 100, 300, 600, 1000]  # 50 × n × (n − 1)
    assert [stats.level_of(xp) for xp in (0, 99, 100, 299, 300, 599, 600, 999, 1000)] == [1, 1, 2, 2, 3, 3, 4, 4, 5]
    assert stats._grade(0) == NOTHING
    assert stats._grade(250) == {"level": 2, "xp": 250, "level_xp": 100, "next_level_xp": 300}
    assert stats._now is store_module.now  # "today" is told by the clock that stamps the rows


def test_experience_and_achievements_follow_the_rule_tables():
    rows = passes(12, reworked={2, 5})
    rows[0]["assignee_id"] = None      # a task of nobody: the studio's only
    rows[11]["assignee_id"] = "emp_b"
    out = stats.grow(rows, closed(14))
    assert out["studio"] == {"level": 2, "xp": 12 * 10 + 10 * 5 + 14 * 2, "level_xp": 100, "next_level_xp": 300,
                             "counts": {"passed": 12, "clean": 10, "decided": 14}}
    assert out["studio"]["xp"] == 198
    assert out["employees"] == {  # no experience of the inbox for an employee
        "emp_a": {"level": 2, "xp": 140, "level_xp": 100, "next_level_xp": 300, "counts": {"passed": 10, "clean": 8}},
        "emp_b": {**NOTHING, "xp": 15, "counts": {"passed": 1, "clean": 1}}}
    assert [(a["id"], a["title"]) for a in out["achievements"]] == [row[:2] for row in stats.ACHIEVEMENTS]
    assert achieved(out) == {  # at the time of the row that reached the count
        "first_pass": (True, f"{DAY1}T10:01:00"), "pass_10": (True, f"{DAY1}T10:10:00"), "pass_50": (False, None),
        "clean_5": (True, f"{DAY1}T10:07:00"), "clean_20": (False, None),   # clean: 1, 3, 4, 6, 7
        "decide_10": (True, f"{DAY2}T00:10:00"), "decide_100": (False, None)}
    assert stats.grow(rows, closed(14)) == out  # the same rows, the same answer

    most = stats.grow(passes(50), closed(100))
    assert most["studio"] == {"level": 4, "xp": 500 + 250 + 200, "level_xp": 600, "next_level_xp": 1000,
                              "counts": {"passed": 50, "clean": 50, "decided": 100}}
    assert achieved(most) == {
        "first_pass": (True, f"{DAY1}T10:01:00"), "pass_10": (True, f"{DAY1}T10:10:00"),
        "pass_50": (True, f"{DAY1}T10:50:00"), "clean_5": (True, f"{DAY1}T10:05:00"),
        "clean_20": (True, f"{DAY1}T10:20:00"), "decide_10": (True, f"{DAY2}T00:10:00"),
        "decide_100": (True, f"{DAY2}T01:40:00")}
    short = achieved(stats.grow(passes(49, reworked=set(range(20, 50))), closed(99)))  # one short of each
    assert [short[key][0] for key in ("pass_50", "clean_20", "decide_100")] == [False, False, False]

    assert stats.grow([], []) == {
        "studio": {**NOTHING, "counts": {"passed": 0, "clean": 0, "decided": 0}}, "employees": {},
        "achievements": [{"id": key, "title": title, "achieved": False, "achieved_at": None}
                         for key, title, _, _ in stats.ACHIEVEMENTS]}


def test_growth_is_counted_from_the_records(world):
    t = grown(world)
    world.clock.at = f"{DAY2}T18:00:00"
    out = world.growth()
    assert set(out) == {"studio", "employees", "achievements", "today"}
    # 5 passed, 3 of them never sent back, 7 decided (5 passes, 1 rework, 1 taken back): 50 + 15 + 14
    assert out["studio"] == {**NOTHING, "xp": 79, "counts": {"passed": 5, "clean": 3, "decided": 7}}
    assert [e["id"] for e in out["employees"]] == [e["id"] for e in world.store.list_employees()]  # as they were made
    people = {e["id"]: e for e in out["employees"]}
    assert people[world.minsu] == {"id": world.minsu, **NOTHING, "xp": 40, "counts": {"passed": 3, "clean": 2}}
    assert people[world.jia] == {"id": world.jia, **NOTHING, "xp": 25, "counts": {"passed": 2, "clean": 1}}
    stuck = t["stuck"]["assignee_id"]
    assert people[stuck] == {"id": stuck, **NOBODY} and len(people) == 3
    assert out["achievements"][0] == {"id": "first_pass", "title": "첫 통과", "achieved": True,
                                      "achieved_at": f"{DAY1}T10:00:00"}
    assert [(a["id"], a["achieved"], a["achieved_at"]) for a in out["achievements"][1:]] == [
        (row[0], False, None) for row in stats.ACHIEVEMENTS[1:]]

    waiting = world.store.list_inbox(status="pending")
    assert out["today"] == {
        "date": DAY2, "from": f"{DAY2}T00:00:00", "to": f"{DAY3}T00:00:00",
        "done": {"count": 3, "items": [{"id": t[k]["id"], "title": t[k]["title"]} for k in ("jia", "reverted", "last")]},
        "blocked": {"count": 1, "items": [{"id": t["stuck"]["id"], "title": "보스 패턴"}]},
        "pending": {"count": 1, "items": [{"id": waiting[0]["id"], "title": "점프 높이는?"}]}}
    # the summary and the daily report read the same material
    assert stats.material(world.store, f"{DAY2}T00:00:00", f"{DAY3}T00:00:00") == {
        key: [item["title"] for item in out["today"][key]["items"]] for key in stats.KEYS}


def test_growth_counted_again_is_the_same(world):
    grown(world)
    world.clock.at = f"{DAY2}T18:00:00"
    first = world.growth()
    assert world.growth() == first and stats.growth(world.store) == first
    world.reopen()  # nothing is kept: the same DB opened anew gives the same numbers
    assert world.growth() == first
    world.clock.at = "2026-03-09T08:00:00"  # another day: only today's `done` moves
    later = world.growth()
    assert all(later[key] == first[key] for key in ("studio", "employees", "achievements"))
    assert later["today"]["done"] == EMPTY
    assert all(later["today"][key] == first["today"][key] for key in ("blocked", "pending"))


def test_a_task_passed_after_it_was_sent_back_is_not_a_clean_pass(world):
    world.task("reworked", at=f"{DAY1}T10:00:00", who=world.minsu)
    assert [(row["assignee_id"], row["reworked"]) for row in world.store.list_passes()] == [(world.minsu, True)]
    out = world.growth()  # the rework and the pass are two decided items: 10 + 0 + 2 × 2
    assert out["studio"] == {**NOTHING, "xp": 14, "counts": {"passed": 1, "clean": 0, "decided": 2}}

    world.task("reverted", at=f"{DAY1}T11:00:00", who=world.jia)  # taken back, then passed: not clean either
    world.task("done", at=f"{DAY1}T12:00:00", who=world.jia)
    world.task("rejected", at=f"{DAY1}T13:00:00")                 # a proposal the owner rejected: a decided item
    world.task("cancelled", at=f"{DAY1}T14:00:00")                # no record of growth at all
    assert [(row["assignee_id"], row["reworked"]) for row in world.store.list_passes()] == [
        (world.minsu, True), (world.jia, True), (world.jia, False)]
    out = world.growth()
    assert out["studio"] == {**NOTHING, "xp": 30 + 5 + 12, "counts": {"passed": 3, "clean": 1, "decided": 6}}
    people = {e["id"]: e for e in out["employees"]}
    assert people[world.minsu] == {"id": world.minsu, **NOTHING, "xp": 10, "counts": {"passed": 1, "clean": 0}}
    assert people[world.jia] == {"id": world.jia, **NOTHING, "xp": 25, "counts": {"passed": 2, "clean": 1}}


def test_growth_without_records_is_level_one_and_an_empty_day(world):
    out = world.growth()
    assert out["studio"] == {**NOTHING, "counts": {"passed": 0, "clean": 0, "decided": 0}}
    assert out["employees"] == [{"id": world.minsu, **NOBODY}, {"id": world.jia, **NOBODY}]
    assert out["achievements"] == [{"id": key, "title": title, "achieved": False, "achieved_at": None}
                                   for key, title, _, _ in stats.ACHIEVEMENTS]
    assert out["today"] == {"date": DAY1, "from": f"{DAY1}T00:00:00", "to": f"{DAY2}T00:00:00",
                            "done": EMPTY, "blocked": EMPTY, "pending": EMPTY}
    world.task("backlog")    # work that was not passed is no record of growth
    world.task("cancelled")
    again = world.growth()
    assert all(again[key] == out[key] for key in ("studio", "achievements", "today"))
    assert all(e["xp"] == 0 and e["level"] == 1 for e in again["employees"])


def test_one_who_left_stays_in_growth_only_with_experience(world):
    idle = world.hire()  # made before the one who is stuck: the one to drop out is in the middle of the list
    t = grown(world)
    stuck = t["stuck"]["assignee_id"]
    world.clock.at = f"{DAY2}T18:00:00"
    before = world.growth()
    assert [e["id"] for e in before["employees"]] == [world.minsu, world.jia, idle, stuck]
    assert before["employees"][2] == {"id": idle, **NOBODY}

    for left in (world.jia, idle):
        world.store.update_employee(left, retired_at=f"{DAY2}T17:00:00")
    out = world.growth()
    assert [e["id"] for e in out["employees"]] == [world.minsu, world.jia, stuck]  # still as they were made
    people = {e["id"]: e for e in out["employees"]}
    assert people[world.jia] == {"id": world.jia, **NOTHING, "xp": 25, "counts": {"passed": 2, "clean": 1}}
    assert out["employees"] == [e for e in before["employees"] if e["id"] != idle]  # only the one without a record
    assert all(out[key] == before[key] for key in ("studio", "achievements", "today"))  # the studio's is the studio's

    for left in (stuck, world.minsu):  # one at work without a record, one with experience
        world.store.update_employee(left, retired_at=f"{DAY2}T17:30:00")
    last = world.growth()
    assert [e["id"] for e in last["employees"]] == [world.minsu, world.jia]
    assert world.store.list_employees() == [] and last["studio"] == before["studio"]


def test_the_experience_of_a_task_that_changed_hands_is_the_new_assignees(world):
    def people():
        return {e["id"]: e for e in world.growth()["employees"]}

    world.clock.at = f"{DAY1}T10:00:00"
    tid = world.m.create_task(OWNER, "backlog", world.pid, assignee_id=world.minsu, **TEXT).task["id"]
    world.m.request_start(tid, OWNER)
    world.m.transition(tid, "working", SYSTEM)
    world.m.transition(tid, "review", Actor.employee(world.minsu), **copy.deepcopy(REVIEW))
    world.m.transition(tid, "backlog", OWNER)                                         # T9: sent back
    assert people() == {world.minsu: {"id": world.minsu, **NOBODY}, world.jia: {"id": world.jia, **NOBODY}}
    assert world.growth()["studio"]["counts"]["passed"] == 0

    world.m.assign(tid, OWNER, world.jia)
    world.m.request_start(tid, OWNER)
    world.m.transition(tid, "working", SYSTEM)
    world.m.transition(tid, "review", Actor.employee(world.jia), **copy.deepcopy(REVIEW))
    world.m.transition(tid, "done", OWNER)
    assert [(row["assignee_id"], row["reworked"]) for row in world.store.list_passes()] == [(world.jia, True)]
    found = people()  # the pass is of the one who has the task now; the one who had it first has nothing of it
    assert found[world.jia] == {"id": world.jia, **NOTHING, "xp": 10, "counts": {"passed": 1, "clean": 0}}
    assert found[world.minsu] == {"id": world.minsu, **NOBODY}
    studio = {**NOTHING, "xp": 14, "counts": {"passed": 1, "clean": 0, "decided": 2}}  # taken back, passed: 10 + 2 × 2
    assert world.growth()["studio"] == studio

    with pytest.raises(tasks.TransitionError) as e:  # the machine moves no task that was passed to another
        world.m.assign(tid, OWNER, world.minsu)
    assert (e.value.code, e.value.from_status) == ("precondition_failed", "done")
    assert world.store.get_task(tid)["assignee_id"] == world.jia

    world.store.update_task(tid, assignee_id=world.minsu)  # the store's own write: the read is of the assignee of now
    found = people()
    assert found[world.minsu] == {"id": world.minsu, **NOTHING, "xp": 10, "counts": {"passed": 1, "clean": 0}}
    assert found[world.jia] == {"id": world.jia, **NOBODY}
    world.store.update_task(tid, assignee_id=None)         # a pass of nobody is the studio's only
    assert people() == {world.minsu: {"id": world.minsu, **NOBODY}, world.jia: {"id": world.jia, **NOBODY}}
    assert world.growth()["studio"] == studio


def test_today_is_the_local_day_of_the_server(world):
    world.task("done", at=f"{DAY1}T23:59:59", who=world.minsu, title="자정 전")
    world.task("done", at=f"{DAY2}T00:00:00", who=world.minsu, title="자정")

    def today(moment):
        world.clock.at = moment
        day = world.growth()["today"]
        return day["date"], day["from"], day["to"], [item["title"] for item in day["done"]["items"]]

    assert today(f"{DAY1}T23:59:59") == (DAY1, f"{DAY1}T00:00:00", f"{DAY2}T00:00:00", ["자정 전"])
    assert today(f"{DAY2}T00:00:00") == (DAY2, f"{DAY2}T00:00:00", f"{DAY3}T00:00:00", ["자정"])
    assert today(f"{DAY2}T23:59:59") == (DAY2, f"{DAY2}T00:00:00", f"{DAY3}T00:00:00", ["자정"])
    assert today(f"{DAY3}T00:00:00") == (DAY3, f"{DAY3}T00:00:00", "2026-03-05T00:00:00", [])
    assert stats.growth(world.store, moment=f"{DAY1}T12:00:00")["today"]["done"]["count"] == 1
    assert world.growth()["studio"]["counts"]["passed"] == 2  # the levels are of every record, not of the day


# --- the server and the document ------------------------------------------------------------------------------
def test_the_server_starts_and_stops_the_daily_report(world, monkeypatch):
    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "studio_report_at", "07:30")
    monkeypatch.setattr(server.cfg, "studio_report_model", "")
    assert server.reporter.at() == "07:30" and server.reporter.model() == ""  # the settings, read at every look
    assert server.reporter.store() is world.store
    assert server.start_daily_report in server.app.router.on_startup
    assert server.stop_daily_report in server.app.router.on_shutdown
    calls = []
    monkeypatch.setattr(server.reporter, "watch", lambda: calls.append("watch"))
    monkeypatch.setattr(server.reporter, "unwatch", lambda: calls.append("unwatch"))
    server.start_daily_report()
    server.stop_daily_report()
    assert calls == ["watch", "unwatch"]


def test_the_document_holds_what_was_settled():
    text = DOC.read_text(encoding="utf-8")
    rest = text[text.index("### 6.4 엔드포인트"):text.index("### 7.6")]
    assert "S-13 이 확정한다" not in rest
    for word in ("`done / (total − rejected − cancelled)`", "`avg_wait_seconds`", "`from <= 시각 < to`",
                 "옛 런의 비용도 지금 담당에게 간다", "런마다 한 번"):
        assert word in rest, word
    # the rule tables of `GET /studio/growth` say what the code counts with
    for word in ("| `GET /studio/growth` | — | `{studio, employees[], achievements[], today}` |",
                 "`50 × n × (n − 1)`", "`00:00 <= 시각 < 다음 날 00:00`", "`stats.entries`",
                 f"| 검수 통과 1건 | {stats.XP_PASS} |", f"| 재작업 없이 통과 1건 | +{stats.XP_CLEAN} |",
                 f"| 처리한 결재 1건 | {stats.XP_DECIDED} |"):
        assert word in rest, word
    for level in range(1, 6):
        assert f"| {level} | {stats.level_floor(level)} |" in rest, level
    for key, title, what, need in stats.ACHIEVEMENTS:
        assert f"| `{key}` | {title} | {WHAT[what]} {need}건 |" in rest, key
    assert "| `relay_agent/studio/stats.py` | 진척 집계, 일일 보고, 성장 수치 · 오늘의 결산 | S-13 | 확정 |" in text
    policy = text[text.index("### 5.9 알림"):text.index("### 5.10")]
    assert "분류만 있다" not in policy
    for word in ("`Config.studio_report_at`", "`Config.studio_report_model`", "`meta.date`", "`한 일: …`",
                 "보고를 시작한 시각"):
        assert word in policy, word
    meta = text[text.index("### 7.6"):text.index("## 8. studio MCP")]
    assert "`date` · `from` · `to`" in meta
