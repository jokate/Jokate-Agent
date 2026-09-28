from pathlib import Path

from relay_agent import router
from relay_agent.campaign import CampaignRunner, parse_tasks
from relay_agent.history import HistoryStore
from relay_agent.pipeline import CampaignSpec, RelayEngine, RelaySpec
from relay_agent.runners import MockRunner, RunnerError
from relay_agent.usage import UsageStore

RELAYS = Path(__file__).resolve().parent.parent / "relays"


def ok(summary="끝", open_issues=(), next_steps=()):
    return {"summary": summary, "state": summary, "open_issues": list(open_issues), "next_steps": list(next_steps)}


def setup(tmp_path, plan, build):
    relays = tmp_path / "relays"
    relays.mkdir()
    (relays / "role.md").write_text("r", encoding="utf-8")
    (relays / "planner.yaml").write_text(
        "name: planner\nhidden: true\nstages:\n  - {name: plan, provider: mock, prompt: role.md}\n", encoding="utf-8")
    (relays / "task.yaml").write_text(
        "name: task\nstages:\n  - {name: build, provider: mock, prompt: role.md}\n", encoding="utf-8")
    runner = MockRunner({"plan": plan, "build": build})
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: runner)
    slept = []
    campaigns = CampaignRunner(engine, relays, sleep=slept.append)
    campaigns.spawn = lambda cid: None  # tests drive in the foreground
    project = tmp_path / "project"
    project.mkdir()
    return campaigns, engine, runner, project, slept


def spec(**kw):
    return CampaignSpec(planner="planner", task_relay="task", **kw)


def test_campaign_runs_every_task_in_one_session_and_adds_a_followup_for_open_issues(tmp_path):
    plan = [ok("계획", next_steps=["1. 데이터 :: 설정 데이터를 만든다", "로직 :: 메인 로직을 붙인다"])]
    build = [ok("데이터 일부", open_issues=["씬 배치가 남음"]), ok("씬 배치 끝"), ok("로직 끝")]
    campaigns, engine, runner, project, _ = setup(tmp_path, plan, build)

    c, plan_run = campaigns.start("로드맵 전부", project, spec=spec())
    c = campaigns.drive(c.id)

    assert c.status == "done" and c.progress() == "3/3"
    assert [(t.id, t.status) for t in c.tasks] == [("T1", "done"), ("T1.1", "done"), ("T2", "done")]
    assert c.tasks[1].followup_of == "T1" and "씬 배치가 남음" in c.tasks[1].goal
    runs = [plan_run.id] + [r for t in c.tasks for r in t.run_ids]
    assert {engine.load(r).session_id for r in runs} == {c.session_id}  # each run gets the previous hand-over
    first_task_prompt = runner.calls[1].prompt
    assert "[캠페인 0/2] T1 데이터" in first_task_prompt and "▶ T1 데이터" in first_task_prompt
    kinds = [e["kind"] for e in engine.history.events(runs[-1])]
    assert "campaign_done" in kinds


def test_a_usage_limit_waits_for_the_reset_and_continues_the_same_run(tmp_path):
    plan = [ok("계획", next_steps=["하나 :: 한다"])]
    build = [RunnerError("usage limit reached", "quota"), ok("끝")]
    campaigns, engine, _, project, slept = setup(tmp_path, plan, build)

    c, _ = campaigns.start("x", project, spec=spec())
    c = campaigns.drive(c.id)

    assert c.status == "done"
    assert len(c.tasks[0].run_ids) == 1 and c.tasks[0].attempts == 0  # resumed, not a new run nor a failed try
    assert sum(slept) >= 25 * 60  # no provider info in the test: the default wait (30 min)


def test_a_task_that_keeps_failing_pauses_and_skip_moves_on(tmp_path):
    plan = [ok("계획", next_steps=["깨짐 :: 실패한다", "다음 :: 된다"])]
    build = [RunnerError("boom"), RunnerError("boom again"), ok("다음 끝")]
    campaigns, _, _, project, _ = setup(tmp_path, plan, build)

    c, _ = campaigns.start("x", project, spec=spec(max_attempts=2))
    c = campaigns.drive(c.id)
    assert c.status == "paused" and c.tasks[0].status == "failed" and "boom again" in c.reason

    campaigns.request(c.id, "skip")
    c = campaigns.drive(c.id)
    assert c.status == "done" and [t.status for t in c.tasks] == ["skipped", "done"]


def test_the_cost_ceiling_pauses_and_resume_allows_one_more_budget(tmp_path, monkeypatch):
    plan = [ok("계획", next_steps=["하나 :: 한다"])]
    campaigns, _, _, project, _ = setup(tmp_path, plan, [ok("끝")])
    monkeypatch.setattr(campaigns, "_cost", lambda c: 5.0)

    c, _ = campaigns.start("x", project, spec=spec(max_cost_usd=3.0))
    c = campaigns.drive(c.id)
    assert c.status == "paused" and "비용 한도" in c.reason

    campaigns.request(c.id, "resume")
    c = campaigns.drive(c.id)
    assert c.status == "done" and c.spec.max_cost_usd == 8.0


def test_pause_request_stops_after_the_current_task(tmp_path):
    plan = [ok("계획", next_steps=["하나 :: 한다", "둘 :: 한다"])]
    campaigns, _, runner, project, _ = setup(tmp_path, plan, [ok("하나 끝"), ok("둘 끝")])
    c, _ = campaigns.start("x", project, spec=spec())
    original = runner.run

    def run_and_request_pause(call):
        if call.stage == "build":
            campaigns.request(c.id, "pause")  # arrives while the first task runs
        return original(call)

    runner.run = run_and_request_pause
    c = campaigns.drive(c.id)
    assert c.status == "paused" and [t.status for t in c.tasks] == ["done", "pending"]


def test_parse_tasks_reads_numbered_title_goal_lines_and_caps_them():
    tasks = parse_tasks(["1. 데이터 :: 만든다", "- 로직", "", "이 단계는 정해진 결과 형식 없이 ...", "셋 :: c"], limit=2)
    assert [(t.id, t.title, t.goal) for t in tasks] == [("T1", "데이터", "만든다"), ("T2", "로직", "로직")]


def test_shipped_campaign_relays_and_the_router_leaves_them_out():
    spec_, _ = RelaySpec.load(RELAYS / "campaign.yaml")
    planner, _ = RelaySpec.load(RELAYS / f"{spec_.campaign.planner}.yaml")
    assert spec_.campaign is not None and planner.hidden and not planner.stages[0].writes
    names = [o["name"] for o in router.candidates(RELAYS, [], ["demo", "auto"], RelaySpec.load)]
    assert "campaign" not in names and "campaign-plan" not in names and "quick" in names


def test_server_starts_a_campaign_from_post_runs_and_controls_it(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import relay_agent.server as server

    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: MockRunner())
    monkeypatch.setattr(server, "engine", engine)
    monkeypatch.setattr(server, "campaigns", CampaignRunner(engine, RELAYS))
    project = tmp_path / "project"
    project.mkdir()
    client = TestClient(server.app)

    r = client.post("/runs", json={"goal": "로드맵 전부", "relay": "campaign", "workdir": str(project), "start": False})
    assert r.status_code == 200 and r.json()["relay"].endswith("campaign-plan.yaml")
    listed = client.get("/campaigns", params={"session_id": r.json()["session_id"]}).json()
    assert len(listed) == 1 and listed[0]["status"] == "planning" and listed[0]["plan_run_id"] == r.json()["id"]
    assert client.post(f"/campaigns/{listed[0]['id']}/cancel").json()["status"] == "cancelled"
    names = [x["name"] for x in client.get("/relays").json()]
    assert "campaign" in names and "campaign-plan" not in names  # the planner is internal
