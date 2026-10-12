"""Studio campaigns, part 1 (S-14): a running campaign goes on by itself (§4.10 · §4.6). The mock campaign of
CAMPAIGN.md — two tasks in a row with a milestone between them and one beside them — in the three modes, on the
queue and the mock runner of test_studio_queue: which tasks start, in which order, and where it stops. Then the
look itself on the task machine alone, and the places it is connected at. No AI is called.

Part 2: the mode changed on the way, a campaign that is paused or cancelled (what leaves the line and what is put
back), and the notices "배치 필요" · "선행 거절" in the system room, each once (§5.9). The REST of campaigns is in
test_studio_api_tasks, the "선행 반려" mark in test_studio_inbox."""
from datetime import datetime, timedelta

import pytest

import relay_agent.studio.store as studio_store
from relay_agent.campaign import CampaignRunner
from relay_agent.runners import MockRunner
from relay_agent.studio import Actor, api, campaigns, employees, work
from relay_agent.studio import tasks as task_rules
from relay_agent.studio.inbox import SYSTEM_ROOM, Inbox
from relay_agent.studio.tasks import OWNER, SYSTEM
from test_studio_inbox import PAYLOAD, TEXT, ok
from test_studio_queue import line  # noqa: F401 - the fixture `auto` stands on
from test_studio_runs import lab  # noqa: F401 - the fixture `line` stands on
from test_studio_tasks import world  # noqa: F401
from test_studio_work import FILES, STAGES, YAML

EMPTY = {"started": [], "unassigned": [], "finished": []}
# the mock campaign: (title, the titles it waits for, is it a milestone) — A → M(고비) → B, and C beside them
MOCK = (("A", (), False), ("M", ("A",), True), ("B", ("M",), False), ("C", (), False))


class Clock:
    """`tasks.now`, one second later at every call. `store.now` is to the second and the line breaks a tie by
    id (§4.6): the two tasks one look puts in the line would start in an order of chance."""

    def __init__(self):
        self.at = datetime.now().replace(microsecond=0)

    def __call__(self):
        self.at += timedelta(seconds=1)
        return self.at.isoformat(timespec="seconds")


class Asker(MockRunner):
    """At its last stage a run asks for the final review of its task (T6), as an employee's does — the task
    that holds the slot then. A task whose title is in `mute` does not: its run ends without one (T4, §4.7)."""

    def __init__(self, line):  # noqa: F811
        super().__init__()
        self.line, self.mute = line, set()

    def run(self, call):
        if call.stage == STAGES[-1]:
            [task_id] = self.line.q.slots()
            task = self.line.store.get_task(task_id)
            if task["title"] not in self.mute:
                api.run_inbox().create("final_review", Actor.employee(task["assignee_id"]), title="검수",
                                       payload=PAYLOAD["final_review"], task_id=task_id)
        return super().run(call)


class Auto:
    """Line with the campaigns connected the way server.py connects them (`CampaignRunner.studio`,
    `fx.campaign`). The work of a slot is the test's to run, in place (`drive`)."""

    def __init__(self, line, monkeypatch):  # noqa: F811
        self.line, self.store, self.cid, self.tasks = line, line.store, None, {}
        line.q.spawn = line.held.append
        self.asker = line.lab.runner = Asker(line)
        monkeypatch.setattr(task_rules, "now", Clock())
        self.runner = CampaignRunner(line.engine, line.relays)
        self.runner.studio = campaigns.Autopilot(api.run_inbox).look
        line.fx.campaign = self.runner.advance_studio

    def member(self, title, **fields):
        """A backlog task of the campaign, with an assignee hired into its project (`Line.backlog`)."""
        line = self.line
        line.n += 1
        emp = line.store.create_employee(f"지수{line.n}", "program", project_id=line.pid)["id"]
        folder = employees.folder(line.cfg.studio_dir, emp)
        folder.mkdir(parents=True, exist_ok=True)
        for name, body in {**FILES, "employee.yaml": YAML}.items():
            (folder / name).write_bytes(body.encode("utf-8"))
        task = line.m.create_task(OWNER, "backlog", line.pid, assignee_id=emp, relay="demo", campaign_id=self.cid,
                                  **{**TEXT, "title": title}, **fields).task
        self.tasks[title] = task
        return task

    def campaign(self, mode):
        """MOCK in that mode, started by the owner: its id."""
        self.cid = self.store.create_campaign(self.line.pid, "1막", "1막을 끝낸다", mode=mode)["id"]
        for title, before, milestone in MOCK:
            self.member(title, depends_on=[self.tasks[name]["id"] for name in before], is_milestone=milestone)
        assert not self.line.held  # a draft starts nothing
        assert self.line.m.transition_campaign(self.cid, "running", OWNER)["status"] == "running"
        return self.cid

    def drive(self):
        """Run what holds a slot until nothing does: the titles of the tasks in the order their runs began."""
        order = []
        while self.line.held:
            [task_id] = self.line.q.slots()  # one project: one writer
            order.append(self.store.get_task(task_id)["title"])
            self.line.held.pop(0)()
        return order

    def statuses(self):
        return {title: self.store.get_task(task["id"])["status"] for title, task in self.tasks.items()}

    def idle(self):
        """The tasks nobody has asked to start: backlog, out of the line."""
        rows = {title: self.store.get_task(task["id"]) for title, task in self.tasks.items()}
        return [title for title, row in rows.items() if row["status"] == "backlog" and row["queued_at"] is None]

    def passes(self, title):
        """The owner passes that task's final review (T7)."""
        [item] = self.store.list_inbox(task_id=self.tasks[title]["id"], status="pending")
        assert ok(self.line.lab.decide(item, "pass"))["task"]["status"] == "done"

    def status(self):
        return self.store.get_campaign(self.cid)["status"]


@pytest.fixture
def auto(line, monkeypatch):  # noqa: F811
    return Auto(line, monkeypatch)


def test_art_is_an_optional_part_nothing_waits_for_it(auto):
    # T3: the part and its room are there and no one is in it — the campaign goes on, no "배치 필요" comes of it
    assert auto.store.get_department("art")["builtin"] is True and auto.store.list_employees(department_id="art") == []
    auto.campaign("nonstop")
    assert auto.drive() == ["A", "C", "M", "B"] and auto.idle() == []
    assert told(auto.line, "needs_assignment") == []
    assert not [m for m in auto.store.list_messages(SYSTEM_ROOM, limit=200) if "아트" in m["body"]]


# --- the mock campaign in the three modes (CAMPAIGN.md S-14 · 467–471행) -----------------------------------------
def test_nonstop_goes_to_the_end_without_the_owner(auto):
    cid = auto.campaign("nonstop")
    assert auto.drive() == ["A", "C", "M", "B"]  # C stood in the line before M: M joined it at A's review
    assert set(auto.statuses().values()) == {"review"} and auto.idle() == []
    [asked] = [event for event in auto.store.list_task_events(auto.tasks["M"]["id"]) if event["kind"] == "queued"]
    assert (asked["actor_kind"], asked["reason"]) == ("system", campaigns.REASON)
    assert auto.status() == "running" and auto.runner.advance_studio(cid) == EMPTY  # nothing was passed yet

    auto.member("D")  # a task that joins a running campaign starts as well (C2)
    assert auto.drive() == ["D"] and auto.statuses()["D"] == "review"


def test_milestone_stops_behind_the_milestone_until_the_owner_passes_it(auto):
    auto.campaign("milestone")
    assert auto.drive() == ["A", "C", "M"]  # A is no milestone: its review is enough for M
    assert auto.statuses() == {"A": "review", "M": "review", "B": "backlog", "C": "review"}
    assert auto.idle() == ["B"]
    auto.passes("C")
    assert auto.drive() == [] and auto.idle() == ["B"]  # another branch: B waits for M
    auto.passes("M")
    assert auto.drive() == ["B"] and auto.statuses()["B"] == "review" and auto.status() == "running"


def test_each_starts_a_task_only_after_what_it_waits_for_was_passed_and_ends_done(auto):
    cid = auto.campaign("each")
    assert auto.drive() == ["A", "C"]
    assert auto.statuses() == {"A": "review", "M": "backlog", "B": "backlog", "C": "review"}
    assert auto.idle() == ["M", "B"]
    auto.passes("C")
    assert auto.drive() == [] and auto.idle() == ["M", "B"]
    auto.passes("A")
    assert auto.drive() == ["M"] and auto.idle() == ["B"]
    auto.passes("M")
    assert auto.drive() == ["B"] and auto.idle() == [] and auto.status() == "running"
    auto.passes("B")  # the last one: every task has ended
    done = auto.store.get_campaign(cid)
    assert done["status"] == "done" and done["finished_at"] and set(auto.statuses().values()) == {"done"}
    assert auto.runner.advance_studio() == EMPTY and not auto.line.held


def test_a_task_that_waits_for_a_decision_stops_its_own_branch_only(auto):
    auto.asker.mute.add("A")  # its run ends without a review request: the system asks (T4)
    cid = auto.campaign("nonstop")
    assert auto.drive() == ["A", "C"]  # the slot A let go is C's
    assert auto.statuses() == {"A": "blocked", "M": "backlog", "B": "backlog", "C": "review"}
    assert auto.idle() == ["M", "B"]
    [item] = auto.store.list_inbox(task_id=auto.tasks["A"]["id"], status="pending")
    assert item["kind"] == "question"
    assert auto.runner.advance_studio(cid) == EMPTY and auto.status() == "running"


# --- on the way: the mode, a pause, a cancel (S-14 2/2, §4.10) -----------------------------------------------------
def test_the_mode_can_be_changed_on_the_way_and_holds_from_the_next_start(auto):
    cid, owner = auto.campaign("each"), auto.line.owner
    assert auto.drive() == ["A", "C"] and auto.idle() == ["M", "B"]  # each: M waits until A was passed
    assert ok(owner.patch(f"/studio/campaigns/{cid}", json={"mode": "nonstop"}))["mode"] == "nonstop"
    assert auto.drive() == ["M", "B"] and auto.idle() == []  # from now on A's review is enough, and M's for B
    assert set(auto.statuses().values()) == {"review"}

    assert ok(owner.patch(f"/studio/campaigns/{cid}", json={"mode": "each"}))["mode"] == "each"
    assert set(auto.statuses().values()) == {"review"} and not auto.line.held  # what has started is not taken back
    auto.member("E", depends_on=[auto.tasks["B"]["id"]])
    assert auto.drive() == [] and auto.idle() == ["E"]  # the next start follows the new mode: B was not passed
    auto.passes("B")
    assert auto.drive() == ["E"] and auto.status() == "running"


def test_a_pause_takes_what_waits_in_the_line_out_and_a_resume_puts_it_back(auto):
    cid, owner, paused = auto.campaign("nonstop"), auto.line.owner, []
    plain = auto.asker.run

    def run(call):  # the owner pauses the campaign while A is at work and C waits for A's slot
        if not paused:
            paused.append(auto.line.m.transition_campaign(cid, "paused", OWNER)["status"])
        return plain(call)

    auto.asker.run = run
    assert auto.drive() == ["A"] and paused == ["paused"]  # A goes on to its review; C never got the slot
    assert auto.statuses() == {"A": "review", "M": "backlog", "B": "backlog", "C": "backlog"}
    assert auto.idle() == ["M", "B", "C"] and auto.status() == "paused"  # nor M, though A's review fills it
    last = auto.store.list_task_events(auto.tasks["C"]["id"])[-1]
    assert (last["kind"], last["actor_kind"], last["reason"]) == ("unqueued", "system", campaigns.HELD["paused"])

    assert ok(owner.post(f"/studio/campaigns/{cid}/resume"))["status"] == "running"
    assert auto.drive() == ["M", "C", "B"]  # C is put back by itself, behind M (the order of the tasks)
    assert set(auto.statuses().values()) == {"review"} and auto.idle() == []


def test_a_pause_reaches_the_task_that_holds_a_slot_but_has_not_started(auto):
    # §4.10 '멈춤': A got its slot when the campaign started, but T3 is the work of that slot and has not run yet
    cid, owner = auto.campaign("nonstop"), auto.line.owner
    [slot] = auto.line.q.slots()
    assert slot == auto.tasks["A"]["id"] and auto.statuses()["A"] == "backlog" and len(auto.line.held) == 1
    assert ok(owner.post(f"/studio/campaigns/{cid}/pause"))["status"] == "paused"
    assert auto.idle() == ["A", "M", "B", "C"]  # out of the line with the pause itself, before the slot's work
    assert auto.drive() == ["A"]  # that work finds A out of the line: no T3 and no run, and the slot is let go
    row = auto.store.get_task(slot)
    assert auto.idle() == ["A", "M", "B", "C"] and not list(auto.line.q.slots()) and not auto.line.held
    assert (row["status"], row["run_id"], row["started_at"]) == ("backlog", None, None)
    events = [(event["kind"], event["actor_kind"], event["reason"]) for event in auto.store.list_task_events(slot)]
    assert events[-2:] == [("queued", "system", campaigns.REASON), ("unqueued", "system", campaigns.HELD["paused"])]
    assert "transition" not in [kind for kind, _, _ in events]
    assert auto.store.list_inbox(task_id=slot, status="pending") == [] and auto.asker.calls == []

    assert ok(owner.post(f"/studio/campaigns/{cid}/resume"))["status"] == "running"
    assert auto.drive() == ["A", "C", "M", "B"]  # put back by itself, and it goes on as if never held
    assert set(auto.statuses().values()) == {"review"} and auto.idle() == []


def test_the_owner_hears_of_an_unassigned_task_anew_after_a_resume_and_not_after_an_edit(auto):
    # §5.9 through the REST and the effects: the start and the resume pass `again` on to the look, the edit does not
    line, owner = auto.line, auto.line.owner
    cid = auto.cid = auto.store.create_campaign(line.pid, "1막", "1막을 끝낸다", mode="each")["id"]
    nobody = line.m.create_task(OWNER, "backlog", line.pid, relay="demo", campaign_id=cid,
                                **{**TEXT, "title": "N"}).task
    assert told(line, "needs_assignment") == []  # a draft is not looked at
    assert ok(owner.post(f"/studio/campaigns/{cid}/start"))["status"] == "running"
    assert told(line, "needs_assignment") == [(nobody["id"], None)]
    assert ok(owner.patch(f"/studio/campaigns/{cid}", json={"title": "2막", "mode": "nonstop"}))["title"] == "2막"
    assert len(told(line, "needs_assignment")) == 1  # the look after an edit says nothing anew
    assert ok(owner.post(f"/studio/campaigns/{cid}/pause"))["status"] == "paused"
    assert len(told(line, "needs_assignment")) == 1
    assert ok(owner.post(f"/studio/campaigns/{cid}/resume"))["status"] == "running"
    assert told(line, "needs_assignment") == [(nobody["id"], None)] * 2
    assert "2막" in notes(line, "needs_assignment")[-1]["body"] and not line.held


def test_a_cancelled_campaign_starts_nothing_and_leaves_its_tasks_as_they_are(auto):
    cid, owner = auto.campaign("each"), auto.line.owner
    assert auto.drive() == ["A", "C"] and auto.idle() == ["M", "B"]
    done = ok(owner.post(f"/studio/campaigns/{cid}/cancel"))
    assert done["status"] == "cancelled" and done["finished_at"]
    auto.passes("A")  # M's predecessor is filled, but the campaign goes on no more
    assert auto.drive() == [] and auto.idle() == ["M", "B"]
    assert auto.statuses() == {"A": "done", "M": "backlog", "B": "backlog", "C": "review"}  # no task was cancelled
    assert auto.runner.advance_studio() == EMPTY and auto.status() == "cancelled"


# --- the look itself, on the task machine alone -------------------------------------------------------------------
def pilot(world):  # noqa: F811
    return campaigns.Autopilot(lambda: Inbox(world.store, world.m))


def campaign(world, status="running", mode="each"):  # noqa: F811
    return world.store.create_campaign(world.project["id"], "1막", "첫 판", mode=mode, status=status)["id"]


def test_a_look_asks_for_what_is_ready_has_an_assignee_and_never_stood_in_the_line(world):  # noqa: F811
    look, cid = pilot(world).look, campaign(world)
    first = world.register(campaign_id=cid)
    nobody = world.register(campaign_id=cid, assignee_id=None)
    world.register(campaign_id=cid, depends_on=[first["id"]])  # waits for `first`
    world.propose(campaign_id=cid)                             # not approved
    world.register()                                           # of no campaign
    assert look() == {"started": [first["id"]], "unassigned": [nobody["id"]], "finished": []}
    event = world.events(first["id"])[-1]
    assert (event["kind"], event["actor_kind"], event["reason"]) == ("queued", "system", campaigns.REASON)
    assert look(cid) == {**EMPTY, "unassigned": [nobody["id"]]}  # in the line already

    world.m.unqueue(first["id"], OWNER)  # the owner took the start back
    back = world.reach("review", campaign_id=cid)
    world.step(back["id"], "T9")  # reverted: backlog again, with its assignee
    assert [world.store.get_task(task["id"])["queued_at"] for task in (first, back)] == [None, None]
    assert look(cid) == {**EMPTY, "unassigned": [nobody["id"]]}  # neither starts again by itself

    world.store.update_task(nobody["id"], assignee_id=world.mate["id"])
    assert look(cid) == {**EMPTY, "started": [nobody["id"]]}


@pytest.mark.parametrize("status", ["draft", "paused", "done", "cancelled"])
def test_only_a_running_campaign_is_looked_at(world, status):  # noqa: F811
    cid = campaign(world, status)
    task = world.register(campaign_id=cid)
    assert pilot(world).look() == EMPTY and pilot(world).look(cid) == EMPTY
    assert world.store.get_task(task["id"])["queued_at"] is None
    assert world.store.get_campaign(cid)["status"] == status
    assert pilot(world).look("cmp_none") == EMPTY
    assert campaigns.Autopilot(lambda: None).look() == EMPTY  # the store is not open


def test_a_start_that_is_refused_does_not_keep_the_next_task(world):  # noqa: F811
    cid = campaign(world)
    world.store.update_employee(world.mate["id"], status="on_leave")
    away = world.register(campaign_id=cid, assignee_id=world.mate["id"])
    here = world.register(campaign_id=cid)
    assert pilot(world).look(cid) == {**EMPTY, "started": [here["id"]]}
    assert world.store.get_task(away["id"])["queued_at"] is None


def test_a_refused_start_is_asked_again_at_the_next_look(world):  # noqa: F811
    # a refusal leaves no trace (no `queued_at`, no `queued` event), so condition 3 of §4.10 does not hold the
    # task back: it starts late, at the first look after its assignee is back and placed here — but it starts
    look, cid, mate = pilot(world).look, campaign(world), world.mate["id"]
    away = world.register(campaign_id=cid, assignee_id=mate)

    def trace():
        queued = [event for event in world.events(away["id"]) if event["kind"] == "queued"]
        return world.store.get_task(away["id"])["queued_at"], queued

    world.store.update_employee(mate, status="on_leave")
    assert look(cid) == EMPTY and trace() == (None, [])  # on leave

    tool = world.store.create_project("툴", "D:/툴", "tool")
    world.store.update_employee(mate, status="idle", project_id=tool["id"])
    assert look(cid) == EMPTY and trace() == (None, [])  # back, but placed on another project

    world.store.update_employee(mate, project_id=world.project["id"])
    assert look(cid) == {**EMPTY, "started": [away["id"]]}
    queued_at, [event] = trace()
    assert queued_at is not None
    assert (event["kind"], event["actor_kind"], event["reason"]) == ("queued", "system", campaigns.REASON)
    assert look(cid) == EMPTY  # in the line: not asked twice


def test_a_campaign_whose_tasks_have_all_ended_is_done_and_one_without_tasks_is_not(world):  # noqa: F811
    look, cid, empty = pilot(world).look, campaign(world), campaign(world)
    for state in ("done", "cancelled", "rejected"):
        world.reach(state, campaign_id=cid)
    last = world.reach("review", campaign_id=cid)
    assert look() == EMPTY and world.store.get_campaign(cid)["status"] == "running"
    world.step(last["id"], "T7")
    assert look() == {**EMPTY, "finished": [cid]}
    done = world.store.get_campaign(cid)
    assert done["status"] == "done" and done["finished_at"]
    assert look() == EMPTY and world.store.get_campaign(empty)["status"] == "running"


# --- the hold and the notices (S-14 2/2: §4.10 '멈춤', §5.9) ---------------------------------------------------------
def frozen(monkeypatch):
    """One clock for the store and the task machine, and it stands still: every row of the test is stamped with
    the same second. What was said and what changed since is told by event ids, not by times (`campaigns._needs`)."""
    at = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
    monkeypatch.setattr(task_rules, "now", lambda: at)
    monkeypatch.setattr(studio_store, "now", lambda: at)


KEYS = {"needs_assignment": {"event", "task_id", "assigned_event_id"},  # the `meta` of the two notices (§7.6)
        "upstream_rejected": {"event", "task_id", "upstream_task_id"}}


def notes(world, event):  # noqa: F811
    """The notices of that kind in the system room, oldest first."""
    lines = [m for m in world.store.list_messages(SYSTEM_ROOM, limit=200) if (m["meta"] or {}).get("event") == event]
    assert all(m["author_kind"] == "system" and set(m["meta"]) == KEYS[event] for m in lines)
    return lines


def told(world, event):  # noqa: F811
    """Those notices as (the task, the predecessor it names)."""
    return [(m["meta"]["task_id"], m["meta"].get("upstream_task_id")) for m in notes(world, event)]


@pytest.mark.parametrize("status", ["paused", "cancelled"])
def test_a_pause_or_a_cancel_takes_the_campaigns_tasks_out_of_the_line_in_its_own_commit(world, status):  # noqa: F811
    # §4.10 '멈춤': no look follows the change here (the fake effects do nothing) — a server that stops right after
    # the commit leaves nothing of the campaign in the line for the queue, which does not know of campaigns
    look, cid = pilot(world).look, campaign(world)
    ours = world.register(campaign_id=cid)
    theirs = world.register(campaign_id=cid, assignee_id=world.mate["id"])
    world.m.request_start(theirs["id"], OWNER)  # the owner's own start
    assert look(cid) == {**EMPTY, "started": [ours["id"]]}
    queued_at, mark = world.store.get_task(ours["id"])["queued_at"], world.mark()

    world.m.transition_campaign(cid, status, OWNER)
    mine, other = (world.store.get_task(task["id"]) for task in (ours, theirs))
    assert (mine["queued_at"], mine["status"]) == (None, "backlog")
    assert other["queued_at"] is not None and other["status"] == "backlog"  # the owner's word stays in the line
    event = world.events(ours["id"])[-1]
    assert (event["kind"], event["actor_kind"], event["reason"]) == ("unqueued", "system", campaigns.HELD[status])
    assert event["detail"] == {"queued_at": queued_at} and world.events(theirs["id"])[-1]["kind"] == "queued"
    first, second = world.stream(mark)  # one commit: the campaign, then the task that left the line
    assert first["type"] == "campaign.updated" and first["data"]["campaign"]["status"] == status
    assert (second["type"], second["data"]["change"], second["data"]["event_id"]) == (
        "task.updated", "unqueued", event["id"])
    assert second["data"]["task"] == mine
    assert world.fx.calls[-1] == ("after", "campaign")
    assert look(cid) == EMPTY and world.events(ours["id"])[-1] == event  # the look behind it finds nothing to do


def test_the_look_at_a_held_campaign_takes_out_what_was_put_in_the_line_after_the_pause(world):  # noqa: F811
    # the net (`Autopilot._hold`): a look that was on its way when the campaign was paused asks for a start a
    # moment after that commit. Only the look at that campaign gives the place back — the one at all of them
    # sees the running campaigns only (a known gap of §4.10)
    look, cid = pilot(world).look, campaign(world)
    late = world.register(campaign_id=cid)
    world.m.transition_campaign(cid, "paused", OWNER)
    world.m.request_start(late["id"], SYSTEM, reason=campaigns.REASON)
    assert look() == EMPTY and world.store.get_task(late["id"])["queued_at"] is not None
    assert look(cid) == EMPTY and world.store.get_task(late["id"])["queued_at"] is None
    event = world.events(late["id"])[-1]
    assert (event["kind"], event["actor_kind"], event["reason"]) == ("unqueued", "system", campaigns.HELD["paused"])


def test_a_held_campaign_gives_back_only_the_places_in_the_line_it_took(world):  # noqa: F811
    look, cid = pilot(world).look, campaign(world)
    ours = world.register(campaign_id=cid)
    theirs = world.register(campaign_id=cid, assignee_id=world.mate["id"])
    world.m.request_start(theirs["id"], OWNER)  # the owner's own start
    assert look(cid) == {**EMPTY, "started": [ours["id"]]}

    def waits(task):
        return world.store.get_task(task["id"])["queued_at"] is not None

    world.m.transition_campaign(cid, "paused", OWNER)
    assert look(cid) == EMPTY and (waits(ours), waits(theirs)) == (False, True)
    event = world.events(ours["id"])[-1]
    assert (event["kind"], event["actor_kind"], event["reason"]) == ("unqueued", "system", campaigns.HELD["paused"])
    assert look(cid) == EMPTY and world.events(ours["id"])[-1] == event  # nothing more to give back

    world.m.transition_campaign(cid, "running", OWNER)
    assert look(cid) == {**EMPTY, "started": [ours["id"]]}  # put back by itself: that stay did not count
    world.m.transition_campaign(cid, "cancelled", OWNER)
    assert look(cid) == EMPTY and (waits(ours), waits(theirs)) == (False, True)
    assert world.events(ours["id"])[-1]["reason"] == campaigns.HELD["cancelled"]
    assert [world.store.get_task(task["id"])["status"] for task in (ours, theirs)] == ["backlog", "backlog"]


def test_a_task_nobody_is_assigned_to_is_told_of_once(world, monkeypatch):  # noqa: F811
    frozen(monkeypatch)  # all of it in one second: nothing is told by a time
    look, cid, mate = pilot(world).look, campaign(world), world.mate["id"]
    nobody = world.register(campaign_id=cid, assignee_id=None)
    ready = world.register(campaign_id=cid)  # has an assignee: nothing is said of it
    assert look(cid) == {"started": [ready["id"]], "unassigned": [nobody["id"]], "finished": []}
    assert told(world, "needs_assignment") == [(nobody["id"], None)]
    [note] = [m for m in world.store.list_messages(SYSTEM_ROOM) if (m["meta"] or {}).get("event")]
    assert "배치 필요" in note["body"] and nobody["id"] in note["body"] and "1막" in note["body"]
    assert note["meta"]["assigned_event_id"] == 0  # nobody was ever assigned to it
    for scope in (cid, None, cid):  # every later look finds it again, and says nothing
        assert look(scope) == {**EMPTY, "unassigned": [nobody["id"]]}
    assert len(told(world, "needs_assignment")) == 1

    world.m.edit_campaign(cid, OWNER, {"title": "2막", "goal": "둘째 판", "mode": "nonstop"})
    assert look(cid) == {**EMPTY, "unassigned": [nobody["id"]]}
    assert len(told(world, "needs_assignment")) == 1  # an edit of the campaign says nothing anew

    world.m.assign(nobody["id"], OWNER, mate)  # somebody was assigned ...
    world.m.assign(nobody["id"], OWNER, None)  # ... and taken off again, in the second of the line: anew, once
    assert look(cid)["unassigned"] == look(cid)["unassigned"] == [nobody["id"]]
    assert told(world, "needs_assignment") == [(nobody["id"], None)] * 2
    newest = [event["id"] for event in world.events(nobody["id"]) if event["kind"] == "assigned"][-1]
    assert notes(world, "needs_assignment")[-1]["meta"]["assigned_event_id"] == newest

    world.m.transition_campaign(cid, "paused", OWNER)
    assert look(cid) == EMPTY and len(told(world, "needs_assignment")) == 2  # a paused campaign says nothing
    world.m.transition_campaign(cid, "running", OWNER)  # started again: once more
    assert world.fx.calls[-1] == ("after", task_rules.CAMPAIGN_STARTED)  # what tells the effects to look `again`
    assert look(cid, again=True) == {"started": [ready["id"]], "unassigned": [nobody["id"]], "finished": []}
    assert look(cid) == {**EMPTY, "unassigned": [nobody["id"]]}
    assert told(world, "needs_assignment") == [(nobody["id"], None)] * 3
    assert look(None, again=True) == {**EMPTY, "unassigned": [nobody["id"]]}  # `again` is for one campaign only
    assert len(told(world, "needs_assignment")) == 3


def test_a_task_behind_a_rejected_or_cancelled_one_is_told_of_once_and_not_started(world, monkeypatch):  # noqa: F811
    frozen(monkeypatch)
    look, cid = pilot(world).look, campaign(world, mode="nonstop")
    gone = world.reach("cancelled", campaign_id=cid)
    denied = world.reach("rejected", campaign_id=cid)
    behind = world.register(campaign_id=cid, assignee_id=world.mate["id"], depends_on=[gone["id"], denied["id"]])
    pairs = [(behind["id"], gone["id"]), (behind["id"], denied["id"])]
    assert look(cid) == EMPTY and told(world, "upstream_rejected") == pairs  # never filled, in any mode
    assert look(cid) == EMPTY and look() == EMPTY and told(world, "upstream_rejected") == pairs
    assert world.store.get_task(behind["id"])["queued_at"] is None and told(world, "needs_assignment") == []

    world.m.edit_campaign(cid, OWNER, {"title": "2막", "mode": "each"})
    assert look(cid) == EMPTY and told(world, "upstream_rejected") == pairs  # an edit says nothing anew

    world.m.transition_campaign(cid, "paused", OWNER)
    world.m.transition_campaign(cid, "running", OWNER)  # started again: once more
    assert look(cid, again=True) == EMPTY and look(cid) == EMPTY and told(world, "upstream_rejected") == pairs * 2

    world.step(behind["id"], "T10")  # the owner gives the task up: the campaign has ended
    assert look(cid) == {**EMPTY, "finished": [cid]} and told(world, "upstream_rejected") == pairs * 2


# --- where it is connected ----------------------------------------------------------------------------------------
def test_the_runner_passes_the_look_on_and_never_raises(tmp_path):
    runner = CampaignRunner(None, tmp_path / "relays", folder=tmp_path / "campaigns")
    assert runner.studio is None and runner.advance_studio() == {} and runner.advance_studio("cmp_1") == {}
    seen = []
    runner.studio = lambda campaign_id=None, **hint: seen.append((campaign_id, hint)) or {"started": ["tsk_1"]}
    assert runner.advance_studio() == {"started": ["tsk_1"]} == runner.advance_studio("cmp_1")
    assert runner.advance_studio("cmp_1", again=True) == {"started": ["tsk_1"]}
    assert seen == [(None, {}), ("cmp_1", {}), ("cmp_1", {"again": True})]  # the hint goes on only when it is given

    def broken(campaign_id=None):
        raise RuntimeError("DB 가 닫혔다")

    runner.studio = broken
    assert runner.advance_studio("cmp_1") == {} == runner.advance_studio("cmp_1", again=True)
    assert not (tmp_path / "campaigns").exists()  # a studio campaign has no file among the old ones


def test_the_effects_look_at_the_campaigns_only_after_a_change_that_matters():
    fx, seen = work.WorkEffects(None, None, lambda: None), []
    row = {"id": "tsk_1", "campaign_id": "cmp_1", "queued_at": None, "run_id": None, "assignee_id": None}
    fx.after("T6", row, ())  # nobody listens
    fx.campaign = seen.append
    one = ("C2", "T1", "T2", "T6", "T7", "T10", "T11", "assigned", "deps")
    for what in one:
        fx.after(what, row, ())
    assert seen == ["cmp_1"] * len(one)
    del seen[:]
    fx.after("edited", row, ())  # the task may have left a campaign: every running one
    fx.after("campaign", {"id": "cmp_2", "status": "running"}, ())
    assert seen == [None, "cmp_2"]
    assert set(campaigns.LOOKS) == {*one, "edited", "campaign", task_rules.CAMPAIGN_STARTED}
    del seen[:]
    fx.campaign = lambda campaign_id, **hint: seen.append((campaign_id, hint))
    fx.after(task_rules.CAMPAIGN_STARTED, {"id": "cmp_2", "status": "running"}, ())  # started or resumed (§5.9)
    assert seen == [("cmp_2", {"again": True})]
    del seen[:]
    fx.campaign = seen.append
    for what in ("C1", "T3", "T4", "T5", "T8", "T9", "queued", "unqueued", "progress", "run", "exchange", "limit"):
        fx.after(what, row, ())
    fx.after("T7", {**row, "campaign_id": None}, ())  # a task of no campaign
    assert seen == []

    def broken(campaign_id):
        raise RuntimeError("DB 가 닫혔다")

    fx.campaign = broken
    fx.after("T7", row, ())  # logged: the change stands


def test_the_server_connects_the_look_through_the_campaign_runner():
    import relay_agent.server as server

    look = server.campaigns.studio
    assert isinstance(look.__self__, campaigns.Autopilot) and look.__self__.inbox is api.run_inbox
    assert server.task_runs.campaign == server.campaigns.advance_studio
    assert server.task_runs.kick == server.task_queue.kick  # the queue's look is still its own
