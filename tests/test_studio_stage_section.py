"""T1: the '빌드·검증' section of a task run, given again at every stage (`prompt_append` of `api.guard_stage`, which
the engine appends to the prompt of the stage; §9.1 · §11.4). Mock only: no editor, no AI."""
from relay_agent.studio import api
from test_mcp_attach import setup as engine_setup
from test_studio_api_tasks import world  # noqa: F401 - `world` is that file's fixture, `desk` stands on it
from test_studio_tools import desk  # noqa: F401 - `desk` is that file's fixture

HEAD = "===== 빌드·검증 ====="


class SectionStub:
    """What `api._effects` is here: the one method guard_stage asks for."""
    def __init__(self, give):
        self.give, self.asked = give, []

    def stage_section(self, store, project_id):
        self.asked.append(project_id)
        if isinstance(self.give, Exception):
            raise self.give
        return self.give


def test_guard_stage_adds_the_build_section_to_a_task_run_only(desk, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv("KATAE_TOKEN", raising=False)
    task = desk.working()
    desk.turn(desk.w.emp)
    chat = desk.tracked[-1]
    cwd = tmp_path / "work"
    cwd.mkdir()
    keys = {"allowed_tools", "settings_path", "env", "claude_only", "skills"}
    stub = SectionStub(f"{HEAD}\nX")
    monkeypatch.setattr(api, "_effects", stub)
    out = api.guard_stage(task["run_id"], "build", run_dir=tmp_path / "r1", cwd=cwd)
    assert out["prompt_append"] == f"{HEAD}\nX" and set(out) == keys | {"prompt_append"}
    assert stub.asked == [api.run_head(desk.store, task["run_id"])["project_id"]]
    stub.give = f"{HEAD}\nY"  # asked again at every stage
    assert api.guard_stage(task["run_id"], "review", run_dir=tmp_path / "r1", cwd=cwd)["prompt_append"].endswith("Y")
    stub.give = None  # not an Unreal project
    assert set(api.guard_stage(task["run_id"], "build", run_dir=tmp_path / "r1", cwd=cwd)) == keys
    stub.give = f"{HEAD}\nX"  # a chat turn is not a task run: not even asked
    stub.asked.clear()
    assert set(api.guard_stage(chat, "chat", run_dir=tmp_path / "r2", cwd=cwd)) == keys and stub.asked == []
    stub.give = RuntimeError("에디터를 못 읽는다")  # a section that fails is a stage without it
    assert set(api.guard_stage(task["run_id"], "build", run_dir=tmp_path / "r1", cwd=cwd)) == keys


def test_the_engine_gives_every_stage_the_section_of_its_moment_once(tmp_path, monkeypatch):
    engine, relay, runner, _ = engine_setup(tmp_path, monkeypatch)
    given = {"text": f"{HEAD}\n에디터: 켜져 있다"}

    def guard_of(run_id, stage, **kw):
        out = {"claude_only": True}
        if given["text"]:
            out["prompt_append"] = given["text"]
        return out

    engine.guard_stage = guard_of

    def resumed(goal, sid):
        run = engine.create(relay, goal, repo="game")
        run.stage_sessions["build"] = sid
        engine.save(run)
        engine.advance(run.id)
        return runner.seen[-1]

    # a first stage: the section follows the prompt, once
    engine.advance(engine.create(relay, "목표", repo="game").id)
    assert runner.seen[-1].prompt.count(HEAD) == 1 and runner.seen[-1].prompt.endswith("켜져 있다")

    # (a) a run that goes on: the resume branch replaces the prompt, and the section comes after that
    call = resumed("목표", "sid-1")
    assert call.prompt.startswith("[재개]") and call.prompt.count(HEAD) == 1 and call.prompt.endswith("켜져 있다")
    assert call.fresh_prompt and call.fresh_prompt.count(HEAD) == 1  # what a failed resume falls back to

    # (b) the editor changed between the stages: the next stage reads the new state
    given["text"] = f"{HEAD}\n에디터: 꺼져 있다"
    call = resumed("목표", "sid-2")
    assert call.prompt.endswith("꺼져 있다") and "켜져 있다" not in call.prompt + call.fresh_prompt

    # (c) a goal that already carries a section (a run made before this) gets no second one in its first prompt
    old = f"목표\n\n{HEAD}\n옛 절"
    engine.advance(engine.create(relay, old, repo="game").id)
    assert runner.seen[-1].prompt.count(HEAD) == 1 and "옛 절" in runner.seen[-1].prompt
    assert "꺼져 있다" not in runner.seen[-1].prompt
    call = resumed(old, "sid-3")  # ...and from its next stage on it gets the new one
    assert call.prompt.startswith("[재개]") and call.prompt.endswith("꺼져 있다")
    assert call.fresh_prompt.count(HEAD) == 1 and "옛 절" in call.fresh_prompt  # the goal's own is not touched

    # (d) no section from the guard (not an Unreal task run): the prompt is as it was
    given["text"] = ""
    engine.advance(engine.create(relay, "목표", repo="game").id)
    assert HEAD not in runner.seen[-1].prompt
    assert HEAD not in resumed("목표", "sid-4").prompt


def test_the_engine_gives_a_redo_stage_the_section_too(tmp_path, monkeypatch):
    """The '[재작업]' branch (a stage sent back by a reviewer) passes the same attach point as '[재개]'."""
    from relay_agent.pipeline import StageRecord

    engine, relay, runner, _ = engine_setup(tmp_path, monkeypatch)
    given = {"text": f"{HEAD}\n에디터: 켜져 있다"}

    def guard_of(run_id, stage, **kw):
        out = {"claude_only": True}
        if given["text"]:
            out["prompt_append"] = given["text"]
        return out

    engine.guard_stage = guard_of

    def redone(goal):
        run = engine.create(relay, goal, repo="game")
        run.approval = "auto"
        run.history.append(StageRecord(stage="build", at="2026-10-20T00:00:00", verdict="retry", runner="claude",
                                       model="m", total_input=0, output_tokens=0, cost_usd=0.0))
        run.done_sessions["build"] = "sid-redo"  # stage_sessions stays empty: not an interrupted stage
        engine.save(run)
        engine.advance(run.id)
        return runner.seen[-1]

    call = redone("목표")
    assert call.prompt.startswith("[재작업]") and not call.prompt.startswith("[재개]")
    assert call.prompt.count(HEAD) == 1 and call.prompt.endswith("켜져 있다")
    assert call.fresh_prompt and call.fresh_prompt.count(HEAD) == 1

    given["text"] = ""  # no section from the guard: the redo prompt is as it was
    call = redone("목표")
    assert call.prompt.startswith("[재작업]") and HEAD not in call.prompt
