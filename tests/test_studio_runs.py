"""Studio inbox (S-5, part 2): a run of the existing engine that stops at a design gate or at a budget files an
inbox item, the owner's decision resumes or ends the run, and an item whose run was released outside the inbox
is closed (§5.8). The demo relay on the mock runner: no AI is called."""
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from relay_agent.history import HistoryStore
from relay_agent.notify import Fanout
from relay_agent.pipeline import RelayEngine
from relay_agent.repos import RepoRegistry
from relay_agent.runners import MockRunner, RunnerError
from relay_agent.studio import Inbox, InboxError, api, runs
from relay_agent.usage import UsageStore
from test_studio_inbox import PAYLOAD, TOKEN, Fx, World, ok, refused

DEMO = Path(__file__).resolve().parent.parent / "relays" / "demo.yaml"
GOAL = "로그인 폼 고치기"
TASK_RUN = "스튜디오 작업의 런이다. 결재함에서 결정하라"


def demo(tmp_path, **changes):
    """A copy of relays/demo.yaml with a budget added: `max_run_cost_usd=…` or `<stage name>={…}`."""
    spec = yaml.safe_load(DEMO.read_text(encoding="utf-8"))
    for stage in spec["stages"]:
        stage.update(changes.pop(stage["name"], {}))
    spec.update(changes)
    (tmp_path / "relays").mkdir()
    shutil.copytree(DEMO.parent.parent / "prompts", tmp_path / "prompts")  # the stages say ../prompts/<name>.md
    path = tmp_path / "relays" / "demo.yaml"
    path.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    return path


class Pricey(MockRunner):
    """Every stage costs $0.02."""

    def run(self, call):
        result, usage = super().run(call)
        usage.cost_usd = 0.02
        return result, usage


class RunFx(Fx):
    """Fx whose next task run is a run of the engine: T3 asks `create_run` for the id."""
    run_id = None

    def create_run(self, task):
        return self.run_id or super().create_run(task)


class Lab:
    """The server app on a studio DB of its own (World) and an engine of its own on the mock runner, attached
    to the inbox the way server.py attaches its engine. Advancing is done in place, not on a thread, so what a
    decision starts has happened when the request returns."""

    def __init__(self, server, tmp_path, monkeypatch, fx):
        self.tmp, self.server, self.fx, self.n = tmp_path, server, fx, 0
        self.heard = []  # (run id, kind): what the notifier that was there before attach() still hears
        self.runner = MockRunner()  # a test may put another one here before its run advances
        (tmp_path / "project").mkdir()
        self.engine = RelayEngine(
            tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
            runner_factory=lambda _: self.runner, repos=RepoRegistry({"game": {"path": str(tmp_path / "project")}}),
            notifier=lambda run_id, kind, title, detail: self.heard.append((run_id, kind)))
        self.hooks = runs.attach(self.engine, api.run_inbox, advance=self.engine.advance)
        monkeypatch.setattr(api, "_hooks", self.hooks)
        monkeypatch.setattr(server, "engine", self.engine)
        monkeypatch.setattr(server, "_advance_bg",
                            lambda run_id, resume=False: self.engine.advance(run_id, resume=resume))
        self.world = World(server, fx, self.hooks)  # opens the store; its project's repo is `game`
        self.owner, self.store = self.world.owner, self.world.store

    def create(self, relay=DEMO, repo=None):
        """A run that has not advanced yet, in a folder of its own (the registered one when it names the repo)."""
        self.n += 1
        folder = self.tmp / ("project" if repo else f"work{self.n}")
        folder.mkdir(exist_ok=True)
        return self.engine.create(relay, GOAL, folder, workspace="none", repo=repo)

    def run(self, relay=DEMO, repo=None):
        """A run advanced until it stops or ends."""
        return self.engine.advance(self.create(relay, repo).id)

    def status(self, run):
        return self.engine.load(run.id).status

    def stages(self):
        return [call.stage for call in self.runner.calls]

    def events(self, run):
        return [event["kind"] for event in self.engine.history.events(run.id)]

    def pending(self):
        return ok(self.owner.get("/studio/inbox"))["items"]

    def decide(self, item, decision, **body):
        return self.owner.post(f"/studio/inbox/{item['id']}/decide", json={"decision": decision, **body})


@pytest.fixture
def lab(tmp_path, monkeypatch):
    import relay_agent.server as server

    fx = RunFx()
    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    monkeypatch.setattr(api, "_effects", fx)
    api.close_store()
    yield Lab(server, tmp_path, monkeypatch, fx)
    api.close_store()


# --- (나) a run that is no task's: filed -> decided -> the run goes on or ends ---------------------------------
def test_a_design_gate_files_an_item_and_approving_it_takes_the_run_to_its_end(lab):
    run = lab.run(repo="game")
    assert run.status == "awaiting_approval" and run.baton.stop.kind == "awaiting_approval"
    listed = ok(lab.owner.get("/studio/inbox"))
    [item] = listed["items"]
    assert listed["pending_count"] == 1
    assert (item["kind"], item["task_id"], item["run_id"], item["requester_kind"], item["requester_id"]) == (
        "design_approval", None, run.id, "system", None)
    assert item["title"] == GOAL and item["project_id"] == lab.world.pid  # the session's title; projects.repo
    assert item["payload"] == {"stage": "plan", "stop_reason": run.baton.stop.reason, "next_stage": "build",
                               "done_stages": ["scout", "plan"], "remaining_stages": ["build", "review"],
                               "design_summary": "plan 완료(mock)"}
    assert lab.heard == [(run.id, "awaiting_approval")]  # the toast's place still hears it
    view = ok(lab.owner.get(f"/studio/inbox/{item['id']}"))
    assert view["task"] is None and view["allowed_decisions"] == ["approve", "revise"]

    out = ok(lab.decide(item, "approve"))
    assert out["task"] is None
    assert (out["inbox"]["status"], out["inbox"]["decision"], out["inbox"]["delivery"]) == ("decided", "approve", "none")
    assert lab.status(run) == "done" and lab.stages() == ["scout", "plan", "build", "review"]
    types = lab.world.types()
    assert types.count("inbox.created") == 1 and types.count("inbox.decided") == 1 and "inbox.cancelled" not in types
    assert lab.pending() == []
    assert refused(lab.decide(item, "approve"), 409, "already_decided")["inbox"]["status"] == "decided"


def test_a_revise_leaves_its_comment_for_the_next_stage_and_the_run_goes_on(lab):
    run = lab.run()
    [item] = lab.pending()
    assert item["project_id"] is None  # this run names no repository
    refused(lab.decide(item, "revise"), 422, "invalid_decision")  # §5.2: a revise needs its comment
    refused(lab.decide(item, "reject"), 422, "invalid_decision")  # ... and a design approval has no reject
    assert lab.status(run) == "awaiting_approval"

    out = ok(lab.decide(item, "revise", comment="토글은 아이콘으로"))
    assert (out["inbox"]["decision"], out["inbox"]["decision_comment"]) == ("revise", "토글은 아이콘으로")
    assert lab.status(run) == "done"
    notes = [e["detail"]["text"] for e in lab.engine.history.events(run.id) if e["kind"] == "user_interject"]
    assert notes == ["토글은 아이콘으로"] and "user_notes_applied" in lab.events(run)  # build started with it


def test_a_run_budget_stop_files_a_budget_item_and_rejecting_it_ends_the_run(lab, tmp_path):
    lab.runner = Pricey()
    run = lab.run(demo(tmp_path, max_run_cost_usd=0.01))
    assert run.status == "awaiting_approval" and run.baton.stop.kind == "budget"
    [item] = lab.pending()
    assert (item["kind"], item["task_id"], item["run_id"]) == ("budget", None, run.id)
    assert item["payload"] == {"scope": "run", "stage": "plan", "next_stage": "plan", "spent_usd": 0.02,
                               "limit_usd": 0.01, "stop_reason": run.baton.stop.reason}
    assert lab.heard == [(run.id, "budget_exceeded")]
    assert ok(lab.owner.get(f"/studio/inbox/{item['id']}"))["allowed_decisions"] == ["approve", "reject"]

    out = ok(lab.decide(item, "reject"))
    assert (out["inbox"]["status"], out["inbox"]["decision"], out["task"]) == ("decided", "reject", None)
    assert lab.status(run) == "cancelled" and lab.stages() == ["scout"]  # engine.cancel: nothing more ran
    assert lab.pending() == []


def test_approving_a_budget_item_lets_the_run_go_on_to_its_next_stop(lab, tmp_path):
    lab.runner = Pricey()
    run = lab.run(demo(tmp_path, max_run_cost_usd=0.01))
    [first] = lab.pending()
    assert ok(lab.decide(first, "approve"))["inbox"]["status"] == "decided"
    # one more budget on top of what was spent: plan runs, and its gate is the next stop — a new item of that run
    assert lab.stages() == ["scout", "plan"] and lab.status(run) == "awaiting_approval"
    [second] = lab.pending()
    assert (second["kind"], second["run_id"], second["payload"]["stage"]) == ("design_approval", run.id, "plan")
    assert second["id"] != first["id"] and lab.world.item(first)["status"] == "decided"


def test_a_stage_budget_stop_is_a_budget_item_of_scope_stage(lab, tmp_path):
    caps = []

    class Capped(MockRunner):
        def run(self, call):
            caps.append(call.max_budget_usd)
            if len(caps) == 1:
                error = RunnerError("단계 예산 $0.4 도달", "budget")
                error.cost_usd = 0.41
                raise error
            return super().run(call)

    lab.runner = Capped()
    run = lab.run(demo(tmp_path, scout={"max_budget_usd": 0.4}))
    assert run.status == "awaiting_approval" and run.baton.stop.kind == "stage_budget"
    [item] = lab.pending()
    assert item["kind"] == "budget" and item["payload"] == {
        "scope": "stage", "stage": "scout", "next_stage": "scout", "spent_usd": 0.41, "limit_usd": 0.4,
        "stop_reason": run.baton.stop.reason}

    ok(lab.decide(item, "approve"))
    assert caps[:2] == [0.4, 0.8]  # engine.approve doubled the stage's cap and scout ran again
    assert lab.status(run) == "awaiting_approval" and [i["kind"] for i in lab.pending()] == ["design_approval"]


# --- (나) released outside the inbox --------------------------------------------------------------------------
def test_a_run_released_outside_closes_its_item_as_stale(lab):
    run = lab.run()
    [item] = lab.pending()
    lab.engine.approve(run.id)  # `relay approve`: the CLI calls the engine itself, nothing tells the inbox

    out = refused(lab.decide(item, "revise", comment="늦은 코멘트"), 409, "stale_state")
    assert (out["inbox"]["id"], out["inbox"]["status"], out["inbox"]["decision"]) == (item["id"], "cancelled", None)
    assert lab.world.item(item)["status"] == "cancelled" and lab.world.types().count("inbox.cancelled") == 1
    assert "user_interject" not in lab.events(run)  # the comment is not left in a run that has gone on
    assert lab.status(run) == "pending" and lab.stages() == ["scout", "plan"]  # nor did the decision advance it
    assert refused(lab.decide(item, "approve"), 409, "already_decided")["inbox"]["status"] == "cancelled"


def test_listing_the_inbox_closes_the_items_of_runs_that_no_longer_wait(lab):
    gone, kept = lab.run(), lab.run()
    assert len(lab.pending()) == 2
    lab.engine.cancel(gone.id)  # again without the server

    listed = ok(lab.owner.get("/studio/inbox"))
    assert [i["run_id"] for i in listed["items"]] == [kept.id] and listed["pending_count"] == 1
    closed = ok(lab.owner.get("/studio/inbox", params={"status": "cancelled"}))["items"]
    assert [(i["run_id"], i["decision"]) for i in closed] == [(gone.id, None)]
    assert lab.status(kept) == "awaiting_approval"


def test_the_dashboard_approve_and_cancel_close_the_item(lab):
    approved, cancelled = lab.run(), lab.run()
    assert len(lab.pending()) == 2
    assert lab.owner.post(f"/runs/{approved.id}/approve").status_code == 200
    assert lab.owner.post(f"/runs/{cancelled.id}/cancel").status_code == 200
    for run in (approved, cancelled):  # closed by the endpoints, before any list looked
        [item] = lab.store.list_inbox(run_id=run.id)
        assert (item["status"], item["decision"]) == ("cancelled", None)
    assert lab.world.types().count("inbox.cancelled") == 2
    assert lab.status(approved) == "done" and lab.status(cancelled) == "cancelled" and lab.pending() == []


def test_file_run_replaces_what_an_unheard_stop_left(lab):
    box = api.run_inbox()
    first = box.file_run("design_approval", "run_a", title="로그인\n둘째 줄", payload=PAYLOAD["design_approval"])
    assert (first["title"], first["task_id"], first["requester_kind"]) == ("로그인", None, "system")
    assert box.file_run("design_approval", "run_a", title="로그인", payload=PAYLOAD["design_approval"]) == first
    second = box.file_run("budget", "run_a", title="로그인", payload=PAYLOAD["budget"])
    assert lab.world.item(first)["status"] == "cancelled"
    assert [i["id"] for i in lab.store.list_inbox(status="pending", run_id="run_a")] == [second["id"]]
    with pytest.raises(InboxError) as refusal:
        box.file_run("budget", "run_b", title="예산", payload={"scope": "run", "stage": "plan"})
    assert refusal.value.code == "missing_fields" and "spent_usd" in refusal.value.extra["fields"]
    with pytest.raises(ValueError):  # the caller's bug: only the two kinds of a run's stop
        box.file_run("question", "run_b", title="질문", payload=PAYLOAD["question"])
    assert lab.store.list_inbox(run_id="run_b") == []


# --- the engine does not depend on the studio layer -----------------------------------------------------------
def test_a_receiver_that_fails_keeps_neither_the_others_nor_the_run(lab, monkeypatch):
    seen = []

    def broken(*args, **kwargs):
        raise RuntimeError("오늘은 안 된다")

    Fanout(broken, None, lambda *args: seen.append(args))("r1", "run_done", "제목", {})
    assert seen == [("r1", "run_done", "제목", {})]

    monkeypatch.setattr(Inbox, "file_run", broken)  # the studio side breaks while it files
    run = lab.run()
    assert run.status == "awaiting_approval" and lab.heard == [(run.id, "awaiting_approval")]
    assert lab.store.list_inbox() == []
    assert lab.owner.post(f"/runs/{run.id}/approve").status_code == 200 and lab.status(run) == "done"


def test_nothing_is_filed_and_the_dashboard_works_while_the_store_is_not_open(lab):
    api.close_store()
    run = lab.run()
    assert run.status == "awaiting_approval" and api.run_inbox() is None
    assert lab.owner.post(f"/runs/{run.id}/approve").status_code == 200 and lab.status(run) == "done"
    assert api.run_inbox() is None  # the old endpoints did not open it either
    assert api.get_store().list_inbox() == []


# --- (가) a task's run, and (다) the old endpoints on it -------------------------------------------------------
def test_a_task_runs_stop_blocks_the_task_and_the_old_endpoints_refuse_that_run(lab):
    made = lab.create()
    lab.fx.run_id = made.id
    task = lab.world.task()  # working, and its run is that one
    assert (task["status"], task["run_id"]) == ("working", made.id)
    run = lab.engine.advance(made.id)
    assert run.status == "awaiting_approval"

    [item] = lab.pending()
    assert (item["kind"], item["task_id"], item["run_id"], item["requester_kind"]) == (
        "design_approval", task["id"], run.id, "system")
    assert item["title"] == task["title"] and item["project_id"] == task["project_id"]
    assert (item["payload"]["stage"], item["payload"]["next_stage"]) == ("plan", "build")
    assert lab.store.get_task(task["id"])["status"] == "blocked" and ("after", "T4") in lab.fx.calls  # §4.7

    for path in ("approve", "resume", "changes/apply", "changes/discard", "changes/rollback"):  # §5.8 다
        r = lab.owner.post(f"/runs/{run.id}/{path}")
        assert (r.status_code, r.json()) == (409, {"detail": TASK_RUN}), path
    assert lab.status(run) == "awaiting_approval" and lab.world.item(item)["status"] == "pending"
    assert lab.owner.post(f"/runs/{run.id}/interject", json={"text": "참고"}).status_code == 200

    out = ok(lab.decide(item, "approve"))  # T5. The run has stopped: its decision text goes in at once (§4.7)
    assert (out["task"]["status"], out["inbox"]["status"], out["inbox"]["delivery"]) == ("working", "decided", "delivered")
    notes = [e["detail"]["text"] for e in lab.engine.history.events(run.id) if e["kind"] == "user_interject"]
    assert len(notes) == 2 and notes[0] == "참고"  # the owner's own note, then the decision text
    assert ("after", "T5") in lab.fx.calls and lab.status(run) == "awaiting_approval"  # going on is the queue's
    assert lab.owner.post(f"/runs/{run.id}/cancel").status_code == 200 and lab.status(run) == "cancelled"
    assert lab.world.item(item)["status"] == "decided"  # a cancel closes only items that have no task


def test_a_task_that_is_already_blocked_gets_no_second_item(lab):
    made = lab.create()
    lab.fx.run_id = made.id
    task = lab.world.task()
    asked = lab.world.file("question", task)  # ask_owner blocked the task while its run went on
    assert lab.engine.advance(made.id).status == "awaiting_approval"  # ... and then the run stops at its gate
    assert [i["id"] for i in lab.pending()] == [asked["id"]]
    assert lab.store.get_task(task["id"])["status"] == "blocked"


# --- (나) at the server's start: the stops nobody heard --------------------------------------------------------
@contextmanager
def deaf(lab):
    """Runs that stop in here file nothing, as when the CLI advanced them in a process of its own: nobody is
    told that the engine let the run go."""
    hook, lab.engine.after_advance = lab.engine.after_advance, None
    try:
        yield
    finally:
        lab.engine.after_advance = hook


def test_the_start_files_a_gate_nobody_heard_once_and_its_decision_works(lab):
    with deaf(lab):
        run = lab.run(repo="game")
    assert run.status == "awaiting_approval" and lab.store.list_inbox() == []

    lab.server.file_waiting_runs()
    [item] = lab.pending()
    assert (item["kind"], item["task_id"], item["run_id"], item["requester_kind"]) == (
        "design_approval", None, run.id, "system")
    assert item["title"] == GOAL and item["project_id"] == lab.world.pid
    assert item["payload"] == {"stage": "plan", "stop_reason": run.baton.stop.reason, "next_stage": "build",
                               "done_stages": ["scout", "plan"], "remaining_stages": ["build", "review"],
                               "design_summary": "plan 완료(mock)"}  # what a stop that was heard files

    lab.server.file_waiting_runs()  # the next start finds the item that is there
    assert [i["id"] for i in lab.pending()] == [item["id"]]
    types = lab.world.types()
    assert types.count("inbox.created") == 1 and "inbox.cancelled" not in types

    assert ok(lab.decide(item, "approve"))["inbox"]["status"] == "decided"
    assert lab.status(run) == "done" and lab.stages() == ["scout", "plan", "build", "review"]


def test_the_start_files_a_budget_stop_with_what_its_event_said(lab, tmp_path):
    lab.runner = Pricey()
    with deaf(lab):
        run = lab.run(demo(tmp_path, max_run_cost_usd=0.01))
    assert run.baton.stop.kind == "budget" and lab.store.list_inbox() == []

    [item] = runs.file_waiting(lab.engine, api.run_inbox)
    assert (item["kind"], item["task_id"], item["run_id"]) == ("budget", None, run.id)
    assert item["payload"] == {"scope": "run", "stage": "plan", "next_stage": "plan", "spent_usd": 0.02,
                               "limit_usd": 0.01, "stop_reason": run.baton.stop.reason}
    assert [i["id"] for i in lab.pending()] == [item["id"]]


def test_the_start_files_a_budget_stop_whose_event_record_is_gone(lab, tmp_path):
    lab.runner = Pricey()
    with deaf(lab):
        run = lab.run(demo(tmp_path, max_run_cost_usd=0.01))
    other = RelayEngine(  # the same runs folder, an event record of its own: it knows nothing of that stop
        tmp_path / "runs", UsageStore(tmp_path / "u2.sqlite"), HistoryStore(tmp_path / "h2.sqlite"),
        runner_factory=lambda _: lab.runner, repos=RepoRegistry({"game": {"path": str(tmp_path / "project")}}))
    assert other.history.events(run.id) == [] and other.load(run.id).status == "awaiting_approval"

    [item] = runs.file_waiting(other, api.run_inbox)
    assert (item["kind"], item["run_id"], item["title"]) == ("budget", run.id, GOAL)  # no session: the goal's line
    payload = item["payload"]
    assert (payload["scope"], payload["stage"], payload["stop_reason"]) == ("run", "plan", run.baton.stop.reason)
    assert (payload["spent_usd"], payload["limit_usd"]) == (0.02, 0.0)  # what the run has spent; no limit known
    assert payload["next_stage"] == run.baton.stop.remaining_stages[0]


def test_the_start_leaves_a_pending_item_and_a_tasks_run_alone(lab):
    with deaf(lab):
        run = lab.run()
        made = lab.create()
        lab.fx.run_id = made.id
        task = lab.world.task()  # working, and its run is that one
        assert lab.engine.advance(made.id).status == "awaiting_approval"
    kept = api.run_inbox().file_run("design_approval", run.id, title="앞서 올린 것",
                                    payload=PAYLOAD["design_approval"])  # not what the run would file now

    assert runs.file_waiting(lab.engine, api.run_inbox) == []
    assert [(i["id"], i["title"]) for i in lab.pending()] == [(kept["id"], "앞서 올린 것")]
    assert "inbox.cancelled" not in lab.world.types()
    assert lab.store.list_inbox(run_id=made.id) == []  # the task's run: §4.8 step 1 (S-8), not this
    assert lab.store.get_task(task["id"])["status"] == "working" and ("after", "T4") not in lab.fx.calls


def test_the_start_is_not_stopped_by_a_closed_store_or_a_run_that_cannot_be_filed(lab, monkeypatch):
    with deaf(lab):
        lab.run(), lab.run()
    doomed, fine = [run.id for run in lab.engine.list_runs()]  # the order the start goes through them

    api.close_store()
    assert runs.file_waiting(lab.engine, api.run_inbox) == []
    lab.server.file_waiting_runs()  # ... and the startup function does not raise either
    assert api.get_store().list_inbox() == []  # (this opens it again)

    file = runs.RunSink.file

    def first_fails(sink, run_id, *heard):
        if run_id == doomed:
            raise RuntimeError("오늘은 안 된다")
        return file(sink, run_id, *heard)

    monkeypatch.setattr(runs.RunSink, "file", first_fails)
    assert [item["run_id"] for item in runs.file_waiting(lab.engine, api.run_inbox)] == [fine]

    def broken():
        raise OSError("runs 폴더를 읽지 못했다")

    monkeypatch.setattr(lab.engine, "list_runs", broken)
    assert runs.file_waiting(lab.engine, api.run_inbox) == []
    lab.server.file_waiting_runs()
