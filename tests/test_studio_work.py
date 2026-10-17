"""Studio task runs (S-8, part 1): the values a task's run is made with (§9.5 의 1–7) reach the engine, what must
be refused is refused, and `GET /studio/models` lists Claude Code alone. The demo relay — its stages turned to
the `claude` provider, as a studio run must have them — on the mock runner: no AI is called."""
import re
import shutil
from pathlib import Path

import pytest
import yaml

from relay_agent.config import ROOT
from relay_agent.pipeline import RelaySpec
from relay_agent.repos import RepoRegistry
from relay_agent.studio import TaskMachine, TransitionError, api, chat, employees, sat, work
from relay_agent.studio.tasks import OWNER, SYSTEM
from test_studio_inbox import DOC, TEXT, ok
from test_studio_runs import DEMO, GOAL, lab  # noqa: F401 - the fixture `shop` stands on

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
STAGES = ("scout", "plan", "build", "review")  # relays/demo.yaml: haiku · fable · sonnet · fable


def entry(model, tier, efforts=EFFORTS, until=None):
    return {"model": model, "tier": tier, "exhausted_until": until, "efforts": list(efforts)}


CATALOG = [  # ProviderRegistry.catalog() on a PC with three AIs; `opus` is benched
    {"provider": "claude", "label": "Claude Code", "tools": True,
     "models": [entry("fable", "최상급"), entry("opus", "고급", until="2026-10-11T12:00:00+00:00"),
                entry("sonnet", "표준"), entry("haiku", "경량", efforts=[])]},
    {"provider": "anthropic_api", "label": "Claude API", "tools": False, "models": [entry("opus", "고급")]},
    {"provider": "codex", "label": "Codex CLI", "tools": True, "models": [entry("", "", efforts=[])]},
]
PLAIN = "version: 1\ndepartment: program\nmcp: []\n"
YAML = PLAIN + ('work:\n  "*": {model: sonnet, effort: medium}\n  plan: {model: fable, effort: max}\n'
                "  ghost: {model: opus}\ncap: {model: opus, effort: high}\n")
FILES = {"PERSONA.md": "나는 지수다.\n", "GUIDE.md": "모르면 묻는다.\n", "MEMORY.md": "점프 높이는 300 이다.\n"}
EDITS = {"build": {"tools": ["Read", "Edit"]}}  # a stage that changes files: what `needs_writes` asks of a relay


def codes(problems):
    return [p["code"] for p in problems]


def relay(folder, name, provider="claude", **stages):
    """relays/demo.yaml under another name, every stage on that provider."""
    spec = yaml.safe_load(DEMO.read_text(encoding="utf-8"))
    spec["name"] = name
    for stage in spec["stages"]:
        stage["provider"] = provider
        stage.update(stages.get(stage["name"], {}))
    path = folder / f"{name}.yaml"
    path.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    return path


class Shop:
    """Lab — the server app, a studio DB and an engine on the mock runner — with a relay folder of its own and
    the task machine on WorkEffects, the way server.py connects it. The models are CATALOG, not this PC's."""

    def __init__(self, lab, monkeypatch, tmp_path):  # noqa: F811
        self.lab, self.engine, self.store, self.owner, self.cfg = lab, lab.engine, lab.store, lab.owner, lab.server.cfg
        self.relays = tmp_path / "relays"
        self.relays.mkdir()
        shutil.copytree(DEMO.parent.parent / "prompts", tmp_path / "prompts")  # the stages say ../prompts/<name>.md
        relay(self.relays, "demo")
        relay(self.relays, "edit", **EDITS)
        relay(self.relays, "mockdemo", provider="mock")
        relay(self.relays, "mockedit", provider="mock", **EDITS)
        auto = yaml.safe_load((DEMO.parent / "auto.yaml").read_text(encoding="utf-8"))
        auto["router"] = {**auto["router"], "candidates": [], "exclude": [], "fallback": "mockedit"}
        (self.relays / "auto.yaml").write_text(yaml.safe_dump(auto, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(self.cfg, "relays_dir", self.relays)
        self.fx = work.WorkEffects(lab.engine, self.cfg, api.get_store, catalog=lambda: CATALOG)
        monkeypatch.setattr(api, "_effects", self.fx)
        self.m = TaskMachine(lab.store, self.fx)
        self.pid, self.n = lab.world.pid, 0

    def hire(self, text=YAML, **files):
        """An employee placed in the project, with a folder: `text` is its employee.yaml."""
        self.n += 1
        emp, _ = self.lab.world.staff(f"지수{self.n}")
        folder = employees.folder(self.cfg.studio_dir, emp)
        folder.mkdir(parents=True, exist_ok=True)
        for name, body in {**FILES, "employee.yaml": text, **files}.items():
            (folder / name).write_bytes(body.encode("utf-8"))
        return emp

    def task(self, emp=None, relay="demo", stage_models=None):  # noqa: A002
        """A backlog task that has asked to start: T3 is its next step. Its assignee is hired for it."""
        row = self.m.create_task(OWNER, "backlog", self.pid, assignee_id=emp or self.hire(), relay=relay,
                                 stage_models=stage_models, **TEXT).task
        self.m.request_start(row["id"], OWNER)
        return self.store.get_task(row["id"])

    def start(self, task):
        """T3, as the queue will do it: (the task after it, the run it made)."""
        row = self.m.transition(task["id"], "working", SYSTEM).task
        return row, self.engine.load(row["run_id"])

    def refused(self, task):
        """T3 that makes no run: the task stays in backlog, out of the line, and its `run` event keeps why."""
        with pytest.raises(TransitionError) as e:
            self.m.transition(task["id"], "working", SYSTEM)
        assert getattr(e.value, "code", "precondition_failed") == "precondition_failed"
        row = self.store.get_task(task["id"])
        assert (row["status"], row["queued_at"], row["run_id"]) == ("backlog", None, None)
        events = ok(self.owner.get(f"/studio/tasks/{task['id']}/events"))["items"]
        [event] = [item for item in events if item["kind"] == "run"]
        assert event["detail"]["phase"] == "create"
        return event["detail"]["error"]

    def routed(self, run):
        [event] = [item for item in self.engine.history.events(run.id) if item["kind"] == "relay_routed"]
        return event["detail"]


@pytest.fixture
def shop(lab, monkeypatch, tmp_path):  # noqa: F811
    return Shop(lab, monkeypatch, tmp_path)


# --- the values: relay -> work -> override -> cap -> create -----------------------------------------------------
def test_the_codes_are_the_six_of_9_5_in_the_order_of_the_steps():
    line = next(text for text in DOC.read_text(encoding="utf-8").splitlines()
                if text.startswith("- `failed[]` 의 `unknown_stage`"))
    assert tuple(re.findall(r"`([a-z_]+)`", line.split(" 은 ", 1)[0])) == work.CODES


def test_the_employees_file_and_the_override_reach_the_run(shop):
    emp = shop.hire()
    policy = Path(shop.cfg.studio_dir) / "projects" / shop.pid / "POLICY.md"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("main 에 직접 올리지 않는다.\n기획 문서 변경은 결재\n", encoding="utf-8")
    task = shop.task(emp, stage_models={"build": {"model": "opus", "effort": "high"}, "review": {}})
    assert shop.fx.check_start(task) == []
    row, run = shop.start(task)
    assert (row["status"], row["run_id"], run.status) == ("working", run.id, "pending")
    assert run.stage_models == {
        "scout": {"provider": "claude", "model": "sonnet", "effort": "medium"},  # `*` of the file
        "plan": {"provider": "claude", "model": "opus", "effort": "high"},       # its own key, lowered to the cap
        "build": {"provider": "claude", "model": "opus", "effort": "high"}}      # the override; `review: {}` = the relay's
    assert (run.model_cap, run.effort_cap) == ("opus", "high")
    assert (run.auto_apply, run.approval, run.mcp, run.mcp_only, run.repo) == (False, "ai", ["studio"], None, "game")
    assert Path(run.relay).name == "demo.yaml" and run.workspace_mode == "none"  # no stage of demo changes a file
    goal = run.baton.goal
    assert goal.startswith("작업: 2단 점프")
    for text in ("조작감", "공중에서 한 번 더", "테스트 통과", "나는 지수다", "모르면 묻는다", "점프 높이는 300",
                 "main 에 직접 올리지 않는다", "기획 문서 변경은 결재", "===== 결재 규칙 =====", "ask_owner", "kind=approval"):
        assert text in goal
    shop.engine.advance(run.id)  # the mock runner is handed what the run keeps
    assert [(call.stage, call.model, call.effort) for call in shop.lab.runner.calls[:2]] == [
        ("scout", "sonnet", "medium"), ("plan", "opus", "high")]


def test_a_run_started_after_the_guide_changed_carries_the_new_guide(shop):
    emp = shop.hire()
    first = shop.start(shop.task(emp))[1].baton.goal
    assert "모르면 묻는다" in first
    (employees.folder(shop.cfg.studio_dir, emp) / "GUIDE.md").write_bytes(
        "# 지침\n새 기준: 점프는 묻고 한다\n".encode("utf-8"))
    later = shop.start(shop.task(emp))[1].baton.goal  # read from the disk at every start (work.py `_goal`)
    assert "새 기준: 점프는 묻고 한다" in later and "모르면 묻는다" not in later


def test_a_task_run_in_a_repo_without_project_context_still_has_studio(shop):
    shop.engine.repos = RepoRegistry({"game": {"path": str(shop.lab.tmp / "project"), "project_context": False}})
    shop.engine.mcp_registry["studio"] = {"command": "py", "args": ["studio.py"]}
    shop.engine.mcp_env["studio"] = api.mcp_env  # as server.py registers it (§8.2)
    relay(shop.relays, "look", scout={"tools": ["Read", "Grep"]})  # demo's stages have no tools: no ToolSearch there
    row, run = shop.start(shop.task(relay="look"))
    assert (run.repo, run.mcp, run.mcp_only) == ("game", ["studio"], None)
    assert shop.engine.project_context(run) is None
    shop.engine.advance(run.id)
    call = shop.lab.runner.calls[0]
    assert call.mcp_servers == run.mcp and "ToolSearch" in call.tools  # §9.5: what it keeps is attached
    assert "project_context" not in shop.lab.events(run)
    claims = sat.verify(call.mcp_overrides["studio"]["env"]["STUDIO_ACTOR_TOKEN"])  # the task's run is in the ledger
    assert (claims["run"], claims["emp"], claims["task"]) == (run.id, row["assignee_id"], row["id"])
    assert claims["mode"] == "work"


def test_the_relay_is_the_tasks_then_the_employees_and_one_that_is_not_there_makes_no_run(shop):
    theirs = PLAIN + "relay: edit\n"
    assert Path(shop.start(shop.task(shop.hire(theirs), relay=None))[1].relay).name == "edit.yaml"
    assert Path(shop.start(shop.task(shop.hire(theirs), relay="demo"))[1].relay).name == "demo.yaml"
    nowhere = shop.task(shop.hire(PLAIN + "relay: nowhere\n"), relay=None)
    assert shop.fx.check_start(nowhere) == []  # not a start's to refuse (§9.5 의 1): T3 says so
    assert "nowhere" in shop.refused(nowhere)


def test_work_is_spread_over_the_stages_and_an_override_replaces_a_slot_whole(shop):
    stages = RelaySpec.load(shop.relays / "demo.yaml")[0].stages
    checks, every = employees.context(shop.cfg), {"model": "sonnet", "effort": "medium"}
    listed, free = work.models(CATALOG, checks), {"model": None, "effort": None}
    assert work.spread({"*": every, "plan": {"model": "opus"}, "build": {}, "ghost": {"model": "opus"}}, STAGES) == {
        "scout": every, "plan": {"model": "opus"}, "review": every}  # `build: {}` is the relay's, not `*`
    assert work.spread(None, STAGES) == {} and work.spread({"plan": {"model": ""}}, STAGES) == {}
    slots, problems = work.settle(stages, {"*": every}, {"plan": {"effort": "low"}, "scout": {}}, free, checks, listed)
    assert problems == []
    assert list(slots.items()) == [("plan", {"effort": "low"}), ("build", every), ("review", every)]  # not merged
    assert work.settle(stages, None, None, free, checks, listed) == ({}, [])


def test_an_override_over_the_cap_is_refused_and_a_slot_of_the_file_is_lowered(shop, monkeypatch):
    over = shop.task(stage_models={"build": {"model": "fable", "effort": "max"}})  # the file's cap: opus · high
    assert [(p["code"], p["stage"]) for p in shop.fx.check_start(over)] == [
        ("model_over_cap", "build"), ("effort_over_cap", "build")]
    assert codes(shop.fx.check_start(over, {"build": {"model": "opus", "effort": "xhigh"}})) == ["effort_over_cap"]
    assert shop.fx.check_start(over, {}) == []  # the override a request brings is looked at, not the stored one
    error = shop.refused(over)
    assert "model_over_cap" in error and "effort_over_cap" in error
    # neither the file nor the part names a cap: the config's is the last one (§9.5 의 6)
    monkeypatch.setattr(shop.cfg, "studio_model_cap", "sonnet")
    monkeypatch.setattr(shop.cfg, "studio_effort_cap", "low")
    loose = PLAIN + 'work: {"*": {model: opus, effort: high}}\n'
    _, run = shop.start(shop.task(shop.hire(loose)))
    assert set(run.stage_models) == set(STAGES)
    assert all(slot == {"provider": "claude", "model": "sonnet", "effort": "low"} for slot in run.stage_models.values())
    assert (run.model_cap, run.effort_cap) == ("sonnet", "low")
    with pytest.raises(work.Refused) as e:
        shop.fx.create_run(shop.task(shop.hire(loose), stage_models={"plan": {"model": "opus"}}))
    assert e.value.codes == ["model_over_cap"]


def test_a_stage_that_would_not_run_on_claude_code_is_refused(shop):
    slot = shop.task(stage_models={"build": {"provider": "codex"}})  # the override names another AI
    assert [(p["code"], p["stage"]) for p in shop.fx.check_start(slot)] == [("provider_unguarded", "build")]
    assert "provider_unguarded" in shop.refused(slot)
    mock = shop.task(relay="mockdemo")  # the relay file itself does
    assert [(p["code"], p["stage"]) for p in shop.fx.check_start(mock)] == [("provider_unguarded", s) for s in STAGES]
    assert "provider_unguarded" in shop.refused(mock)
    file = shop.task(shop.hire(PLAIN + "work: {build: {provider: nobody}}\n"))  # written by hand, past §9.4
    with pytest.raises(work.Refused) as e:
        shop.fx.create_run(file)
    assert e.value.codes == ["provider_unguarded"] and [p["stage"] for p in e.value.problems] == ["build"]


def test_a_stage_the_relay_has_not_and_a_value_nobody_offers_are_refused(shop):
    task = shop.task()

    def found(stage_models):
        return [(p["code"], p["stage"]) for p in shop.fx.check_start(task, stage_models)]

    assert found({"ghost": {"model": "opus"}, "*": {"model": "sonnet"}}) == [
        ("unknown_stage", "ghost"), ("unknown_stage", "*")]
    assert found({"build": {"model": "gpt-5", "effort": "high"}}) == [("model_unavailable", "build")]
    assert found({"build": {"provider": "anthropic_api", "model": "opus"}}) == [
        ("provider_unguarded", "build"), ("model_unavailable", "build")]  # /studio/models has no such entry
    assert found({"scout": {"effort": "high"}}) == [("effort_unsupported", "scout")]  # the relay's haiku takes none
    assert found({"build": {"model": "haiku", "effort": "low"}, "plan": {"effort": "ultra"}}) == [
        ("effort_unsupported", "build"), ("effort_unsupported", "plan")]
    assert found({"build": {"model": "opus"}, "scout": {"model": "haiku"}, "review": None}) == []  # benched, but listed
    stored = shop.task(stage_models={"ghost": {"model": "opus"}})
    with pytest.raises(work.Refused) as e:
        shop.fx.create_run(stored)
    assert e.value.codes == ["unknown_stage"]


def test_an_employee_whose_file_cannot_be_read_gets_no_run(shop):
    emp, _ = shop.lab.world.staff("폴더없음")  # no folder: its employee.yaml is not there (§9.6)
    task = shop.task(emp)
    assert codes(shop.fx.check_start(task)) == ["yaml_invalid"]
    assert "yaml_invalid" in shop.refused(task)


def test_the_runs_of_a_task_go_under_one_session_and_a_start_that_failed_is_closed(shop):
    row, run = shop.start(shop.task())
    [room] = shop.store.list_rooms(task_id=row["id"])
    assert shop.store.get_room_member(room["id"], row["assignee_id"])["session_id"] == run.session_id
    again = shop.engine.load(shop.fx.create_run(row))  # the task's next run (T8 · §4.7)
    assert again.id != run.id and again.session_id == run.session_id
    shop.fx.cancel_run(again.id)  # T3's transaction failed after the run was made (§4.5)
    assert shop.engine.load(again.id).status == "cancelled"
    shop.fx.cancel_run(again.id)  # closed already: nothing to do, nothing raised


# --- `auto`: picked first, among the relays of Claude Code -------------------------------------------------------
def test_auto_is_picked_at_the_start_among_the_relays_of_claude_code(shop):
    task = shop.task(relay="auto", stage_models={"build": {"model": "opus"}})
    assert shop.fx.check_start(task) == []
    _, run = shop.start(task)
    assert Path(run.relay).name == "edit.yaml" and run.stage_models["build"] == {"provider": "claude", "model": "opus"}
    picked = shop.routed(run)
    assert (picked["relay"], picked["router"], picked["reason"]) == ("edit", "auto", "후보가 하나뿐")  # no mockedit
    # the stages are known only once a relay is picked: a start looks at the slot itself, T3 at the rest
    ghost = shop.task(relay="auto", stage_models={"ghost": {"model": "opus"}, "build": {"provider": "codex"}})
    assert [(p["code"], p["stage"]) for p in shop.fx.check_start(ghost)] == [("provider_unguarded", "build")]
    with pytest.raises(work.Refused) as e:
        shop.fx.create_run(ghost)
    assert e.value.codes == ["unknown_stage", "provider_unguarded"]
    (shop.relays / "edit.yaml").unlink()  # nothing of Claude Code is left to pick
    assert "릴레이를 고르지 못했다" in shop.refused(shop.task(relay="auto"))


def test_route_picks_a_relay_without_making_a_run_and_create_keeps_the_record(shop, tmp_path):
    engine, folder, auto = shop.engine, tmp_path / "project", shop.relays / "auto.yaml"
    assert engine.route(auto, GOAL, folder, needs_writes=True)["relay"] == "mockedit"  # two left, no router AI: fallback
    picked = engine.route(auto, GOAL, folder, needs_writes=True, exclude=["mockedit"])
    assert (picked["relay"], Path(picked["path"]).name, picked["from"]) == ("edit", "edit.yaml", "auto")
    assert engine.route(auto, GOAL, repo="game", needs_writes=True, exclude=["mockedit"])["relay"] == "edit"
    for refused in (lambda: engine.route(shop.relays / "demo.yaml", GOAL, folder),          # no router in it
                    lambda: engine.route(auto, GOAL, needs_writes=True),                    # no folder
                    lambda: engine.route(auto, GOAL, folder, needs_writes=True, exclude=["mockedit", "edit"])):
        with pytest.raises(ValueError):
            refused()
    run = engine.create(picked["path"], GOAL, folder, workspace="none", routed=picked)
    assert {"path", "usage"} <= set(picked)  # the caller's dict is left as it was
    assert Path(run.relay).name == "edit.yaml"
    assert (shop.routed(run)["relay"], shop.routed(run)["router"]) == ("edit", "auto")
    with pytest.raises(ValueError):
        engine.create(auto, GOAL, folder, workspace="none", routed=picked)  # routed twice


# --- a project that holds the server's code: a copy, or no run -------------------------------------------------
def test_pin_copy_refuses_a_run_that_would_write_in_the_folder_itself(shop, tmp_path):
    engine, folder = shop.engine, tmp_path / "project"
    edit, demo = shop.relays / "edit.yaml", shop.relays / "demo.yaml"
    for mode in ("inplace", "none"):
        with pytest.raises(ValueError, match="복사본"):
            engine.create(edit, GOAL, folder, workspace=mode, pin_copy=True)
        assert engine.create(edit, GOAL, folder, workspace=mode).workspace_mode == mode  # not pinned: as before
    with pytest.raises(ValueError, match="unreal"):  # a live MCP would switch the copy to the folder itself
        engine.create(edit, GOAL, folder, workspace="copy", mcp=["unreal"], pin_copy=True)
    assert engine.create(edit, GOAL, folder, workspace="copy", mcp=[], mcp_only=[], pin_copy=True).workspace_mode == "copy"
    assert engine.create(demo, GOAL, folder, pin_copy=True).workspace_mode == "none"  # nothing writes: no copy needed
    with pytest.raises(ValueError, match="복사본"):
        engine.create(demo, GOAL, folder, workspace="inplace", pin_copy=True)


def test_a_project_that_holds_the_servers_code_gets_no_run_in_its_own_folder(shop, tmp_path):
    assert work.holds_server_code(ROOT) and work.holds_server_code(ROOT.parent)  # the root and what is above it
    assert work.holds_server_code(str(ROOT / "relay_agent" / "studio")) and work.holds_server_code(str(ROOT).upper())
    assert work.holds_server_code(tmp_path, ROOT / "relay_agent")  # one of the paths is enough
    assert not work.holds_server_code(tmp_path, ROOT / "relays", "", None)
    assert work.holds_server_code(tmp_path / "srv" / "relay_agent" / "x", root=tmp_path / "srv")
    shop.engine.repos = RepoRegistry({"game": {"path": str(tmp_path / "project"), "workspace": "inplace"}})
    pinned = work.WorkEffects(shop.engine, shop.cfg, api.get_store, catalog=lambda: CATALOG,
                              server_root=tmp_path / "project")  # the project's folder is the server's root
    with pytest.raises(work.Refused, match="복사본"):
        pinned.create_run(shop.task(relay="edit"))
    assert shop.engine.load(pinned.create_run(shop.task(relay="demo"))).workspace_mode == "none"
    assert shop.engine.load(shop.fx.create_run(shop.task(relay="edit"))).workspace_mode == "inplace"  # another project


# --- GET /studio/models and the config's cap ---------------------------------------------------------------------
def test_studio_models_lists_what_claude_code_offers_and_nothing_else(shop, monkeypatch):
    monkeypatch.setattr(api, "_models", lambda: CATALOG)
    listed = ok(shop.owner.get("/studio/models"))
    assert listed == [CATALOG[0]]
    assert [set(item) for item in listed] == [{"provider", "label", "tools", "models"}]
    assert all(set(model) == {"model", "tier", "exhausted_until", "efforts"} for model in listed[0]["models"])
    assert listed[0]["models"][1]["exhausted_until"] and listed[0]["models"][3]["efforts"] == []  # benched; haiku
    monkeypatch.setattr(api, "_models", None)  # no engine connected: the config's own registry is asked
    checks = employees.context(shop.cfg)
    assert all(checks.guarded(item["provider"]) for item in ok(shop.owner.get("/studio/models")))
    assert "get" in api.openapi_schema()["paths"]["/studio/models"]


def test_the_configs_cap_is_the_last_one_of_a_chat_turn_too(lab, monkeypatch):  # noqa: F811
    cfg = lab.server.cfg
    assert employees.config_cap(cfg) == {"model": None, "effort": None}
    monkeypatch.setattr(cfg, "studio_model_cap", "sonnet")
    monkeypatch.setattr(cfg, "studio_effort_cap", "medium")
    assert employees.config_cap(cfg) == {"model": "sonnet", "effort": "medium"}
    emp, _ = lab.world.staff("채팅")
    folder = employees.folder(cfg.studio_dir, emp)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "employee.yaml").write_bytes(b"version: 1\ndepartment: program\nchat: {model: opus, effort: high}\n")
    values = chat.ChatTurns(lab.engine, cfg)._values(lab.store, lab.store.get_employee(emp))
    assert (values["slot"], values["model_cap"], values["effort_cap"]) == (
        {"model": "sonnet", "effort": "medium"}, "sonnet", "medium")


def test_a_task_run_goal_carries_the_harness_section_after_the_policy(shop, monkeypatch):
    from test_studio_harness_root import fake_harness
    emp = shop.hire()
    assert "===== 하네스" not in shop.start(shop.task(emp))[1].baton.goal
    monkeypatch.setattr(shop.cfg, "studio_harness_root", str(fake_harness(shop.lab.tmp)))
    goal = shop.start(shop.task(emp))[1].baton.goal
    assert "===== 하네스 =====\n행동하기 전에 읽는다." in goal and "- b — b 을 할 때" in goal
    assert goal.index("===== GUIDE.md") < goal.index("===== 하네스") < goal.index("===== 결재 규칙")
