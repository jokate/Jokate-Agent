"""S-10 (2/2): the guard on the server's side — `POST /studio/guard/check` and its four branches (§6.4), the
one-time grant (§10.5), and how the guard is hung on a studio run and on no other (§10.1 · §11.4). The hook's own
audit log (§10.7) is in tests/test_studio_guard.py. The engine's runner is a recorder: no AI is started."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay_agent import studio_guard
from relay_agent.campaign import CampaignRunner
from relay_agent.config import ROOT, Config
from relay_agent.pipeline import CampaignSpec
from relay_agent.runners import ClaudeCliRunner, StageCall
from relay_agent.studio import TaskMachine, api, guard, tools
from relay_agent.studio.inbox import grant_key
from test_mcp_attach import RELAY, setup as engine_setup
from test_studio_api_tasks import TOKEN as SHARED  # the shared token of the `world` fixture's server
from test_studio_api_tasks import refused, world  # noqa: F401 - `world` is that file's fixture, `desk` stands on it
from test_studio_tools import bearer, desk  # noqa: F401 - `desk` is that file's fixture, used here too

PUSH = dict(tool="Bash", command="git push origin main", rule_id="git_risky")
PROFILE = ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "mcp__studio"]


def ask(desk, token, **over):  # noqa: F811
    """A question of the hook that got past the authentication: always 200 and the four keys of §6.4."""
    r = desk.http.post("/studio/guard/check", json={**PUSH, **over}, headers=bearer(token))
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"decision", "message", "inbox_id", "end_turn"} and out["message"], out
    assert out["decision"] in ("allow", "deny") and out["message"].endswith(tools.END_TURN) == out["end_turn"], out
    return out


def real(path):
    return studio_guard._resolve(str(Path(path)))


class Cut:
    """Stands in for studio/cutoff.py: what `api.answered` was told."""

    def __init__(self):
        self.calls = []

    def after(self, rule, task_id, *, caller_run_id, inbox_id):
        self.calls.append((rule, task_id, caller_run_id, inbox_id))
        return "timed"


# --- POST /studio/guard/check: the four branches (§6.4) ---------------------------------------------------------
def test_the_four_branches_of_guard_check(desk, monkeypatch):  # noqa: F811
    cut, live = Cut(), api._liveness
    monkeypatch.setattr(api, "_cutoff", cut)
    monkeypatch.setattr(api, "_liveness", lambda run_id: {**live(run_id), "stage": "build"})
    assert set(guard.REASONS) == set(studio_guard.ASK_RULES)
    store, task = desk.store, desk.working()
    token, events = desk.token(task), len(desk.w.events(task["id"]))

    # 4: a turn that is no task run (a chat turn, a read turn of that task's room), and a rule that is not asked
    for other in (desk.turn(desk.w.emp), desk.turn(desk.w.mate, task=task)):
        out = ask(desk, other)
        assert (out["decision"], out["inbox_id"], out["end_turn"]) == ("deny", None, False)
    for rule_id in ("system_delete", "unparsed", "guard_error", ""):
        out = ask(desk, token, rule_id=rule_id)
        assert (out["decision"], out["inbox_id"], out["end_turn"]) == ("deny", None, False)
    assert desk.untouched(task) and len(desk.w.events(task["id"])) == events and cut.calls == []

    # 2: the task run of a working task — the system's T4 files a risky_command and the run is to end
    out = ask(desk, token)
    item = store.get_inbox_item(out["inbox_id"])
    assert (out["decision"], out["end_turn"]) == ("deny", True) and guard.REASONS["git_risky"] in out["message"]
    assert (item["kind"], item["status"], item["task_id"], item["run_id"]) == (
        "risky_command", "pending", task["id"], task["run_id"])
    assert {key: item["payload"].get(key) for key in ("command", "tool", "rule_id", "reason", "stage")} == {
        "command": PUSH["command"], "tool": "Bash", "rule_id": "git_risky", "reason": guard.REASONS["git_risky"],
        "stage": "build"}
    assert store.get_task(task["id"])["status"] == "blocked"
    assert cut.calls == [("T4", task["id"], task["run_id"], item["id"])]  # the same way in as ask_owner's (§8.3)

    # 3: the run is still going while the task waits — a `guard` row, no item, and the turn is to end
    out = ask(desk, token, command="git push --force")
    assert (out["decision"], out["inbox_id"], out["end_turn"]) == ("deny", None, True)
    row = desk.w.events(task["id"])[-1]
    assert (row["kind"], row["actor_kind"], row["to_status"]) == ("guard", "system", None) and "git_risky" in row["reason"]
    assert row["detail"] == {"run_id": task["run_id"], "tool": "Bash", "command": "git push --force",
                             "rule_id": "git_risky"}
    assert len(store.list_inbox(task_id=task["id"])) == 1 and len(cut.calls) == 1
    review = desk.working("review")  # in review the run finishes its stages: refused, and the turn goes on
    out = ask(desk, desk.token(review))
    assert (out["decision"], out["inbox_id"], out["end_turn"]) == ("deny", None, False)
    assert desk.w.events(review["id"])[-1]["kind"] == "guard" and len(store.list_inbox(task_id=review["id"])) == 1

    # 4 again: a task run whose task is in none of the three states
    done = desk.working("done")
    events = len(desk.w.events(done["id"]))
    out = ask(desk, desk.token(done))
    assert (out["decision"], out["inbox_id"], out["end_turn"]) == ("deny", None, False)
    assert len(desk.w.events(done["id"])) == events and len(cut.calls) == 1

    # who may ask, and what must be sent
    refused(desk.http.post("/studio/guard/check", json=PUSH), 401, "actor_invalid")
    refused(desk.w.owner.post("/studio/guard/check", json=PUSH), 403, "actor_not_allowed")
    for body in ({"tool": "Bash", "command": "git push"}, {**PUSH, "cwd": "D:/game"}):
        assert desk.http.post("/studio/guard/check", json=body, headers=bearer(token)).status_code == 422


# --- the one-time grant (§10.5) -----------------------------------------------------------------------------------
def test_a_grant_is_used_once_by_the_task_run_and_outlives_a_restart(desk):  # noqa: F811
    task = desk.working()
    desk.turn(desk.w.mate, task=task)
    reading = desk.tracked[-1]
    first = ask(desk, desk.token(task))["inbox_id"]
    api.run_inbox().decide(first, "approve")  # T5: the task works again and the grant of that command is left

    def grant(inbox_id=first):
        return api.get_store().get_inbox_item(inbox_id)["decision_data"]["grant"]

    assert grant() == {"key": grant_key("Bash", PUSH["command"]), "used_at": None, "used_run_id": None, "void": False}
    api.close_store()  # a restart: the grant is in the DB, not in the server's memory
    store = api.get_store()
    assert store is not desk.store and store.get_task(task["id"])["status"] == "working"

    assert ask(desk, api.actor_token(reading))["decision"] == "deny"  # (나) a read turn is not the task run
    assert ask(desk, desk.token(task), command="git push origin dev")["inbox_id"]  # another command: asked anew
    api.run_inbox().decide(store.list_inbox(task_id=task["id"], status="pending")[0]["id"], "reject")
    assert grant()["used_at"] is None and store.get_task(task["id"])["status"] == "working"

    with store.transaction() as tx:  # (가) the run ended and a new run took the task: the grant is the task's
        tx.exec("UPDATE tasks SET run_id = 'run_next' WHERE id = ?", (task["id"],))
    desk.runs["run_next"] = "running"
    token = api.actor_token("run_next")
    out = ask(desk, token, command="  git   push\torigin main\n")  # the same key: only the whitespace differs
    assert (out["decision"], out["inbox_id"], out["end_turn"]) == ("allow", None, False)
    assert grant()["used_run_id"] == "run_next" and grant()["used_at"] and grant()["void"] is False
    assert store.get_task(task["id"])["status"] == "working"

    again = ask(desk, token)  # (다) used once: the same command is the owner's to decide again
    assert again["decision"] == "deny" and again["inbox_id"] not in (None, first)
    inbox = api.run_inbox()
    inbox.decide(again["inbox_id"], "approve")
    with store.transaction() as tx:  # what T9 · T11 do to the grants that were not used
        TaskMachine._void_grants(tx, task["id"])
    assert grant(again["inbox_id"])["void"] is True and grant()["void"] is False  # a used one stays as it is
    assert inbox.use_grant(task["id"], grant_key("Bash", PUSH["command"]), "run_next") is None
    assert ask(desk, token)["inbox_id"] not in (None, first, again["inbox_id"])


# --- what a studio run's stage is given (§10.1 · §10.2 · §11.4) ---------------------------------------------------
def test_guard_stage_gives_a_studio_run_the_profile_the_hook_and_its_material(desk, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv("KATAE_TOKEN", raising=False)  # with it the env has one key more: its own test below
    monkeypatch.setattr(api._cfg, "studio_fcm_key", "", raising=False)  # whatever this PC's settings name
    task = desk.working()
    desk.turn(desk.w.emp)
    chat = desk.tracked[-1]
    run_dir, cwd = tmp_path / "runs" / task["run_id"], tmp_path / "work"
    cwd.mkdir()
    assert api.guard_stage("20261011-000000-abcdef", "build", run_dir=tmp_path / "other", cwd=cwd) is None
    assert not (tmp_path / "other").exists()  # a dashboard · CLI run: nothing is given and nothing is written

    out = api.guard_stage(task["run_id"], "build", run_dir=run_dir, cwd=cwd)
    assert set(out) == {"allowed_tools", "settings_path", "env", "claude_only"} and out["claude_only"] is True
    assert out["allowed_tools"] == PROFILE and guard.profile("read") == guard.profile("chat") == ["mcp__studio"]
    assert out["settings_path"] == run_dir / "studio_settings.json"
    assert set(out["env"]) == {"STUDIO_URL", "STUDIO_ACTOR_TOKEN"} and out["env"]["STUDIO_URL"] == api._cfg.server_url
    claims = studio_guard.claims(out["env"]["STUDIO_ACTOR_TOKEN"])
    assert (claims["run"], claims["task"], claims["mode"]) == (task["run_id"], task["id"], "work")

    ctx = studio_guard.load_ctx(str(run_dir / "guard_ctx.json"))  # the hook's own reader takes it
    assert (ctx["run_id"], ctx["server_pid"], ctx["server_url"]) == (task["run_id"], os.getpid(), api._cfg.server_url)
    assert ctx["server_root"] == real(ROOT) and ctx["runs_dir"] == real(tmp_path / "runs")  # where the audit log goes
    assert ctx["protected"] == [real(p) for p in (  # §10.8: an employee changes none of these
        ROOT / "relay_agent", ROOT / "relay.config.yaml", ROOT / "relay.config.local.yaml",
        tmp_path / "runs" / "studio", run_dir / "guard_ctx.json", run_dir / "studio_settings.json",
        run_dir / "hook-settings.json")]
    assert ctx["delete_roots"] == [real("D:/game"), real("D:/tool"), real(cwd), real(tempfile.gettempdir())]
    assert ctx["secrets"] == studio_guard.default_secrets(dict(os.environ), real(ROOT)) and ctx["known_hosts"] == []
    assert "sat1." not in (run_dir / "guard_ctx.json").read_text(encoding="utf-8")  # no token on disk

    line = f'"{sys.executable}" "{Path(studio_guard.__file__).resolve()}" "{run_dir / "guard_ctx.json"}"'
    hook = {"matcher": "*", "hooks": [{"type": "command", "command": line}]}
    assert json.loads(out["settings_path"].read_text(encoding="utf-8")) == {"hooks": {"PreToolUse": [hook]}}

    # a chat turn in a JournalWorkspace: no shell or edit approved ahead, and the backup hook stays, after the guard
    backup = {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": "py backup_hook.py"}]}
    journal = tmp_path / "hook_settings.json"
    journal.write_text(json.dumps({"hooks": {"PreToolUse": [backup]}}), encoding="utf-8")
    (run_dir / "guard_ctx.json").write_text("{}", encoding="utf-8")  # whatever became of it: written again
    out = api.guard_stage(chat, "chat", run_dir=run_dir, cwd=cwd, settings=journal)
    assert out["allowed_tools"] == ["mcp__studio"] and studio_guard.claims(out["env"]["STUDIO_ACTOR_TOKEN"])["mode"] == "chat"
    assert json.loads(out["settings_path"].read_text(encoding="utf-8"))["hooks"]["PreToolUse"] == [hook, backup]
    assert json.loads(journal.read_text(encoding="utf-8")) == {"hooks": {"PreToolUse": [backup]}}
    ctx = studio_guard.load_ctx(str(run_dir / "guard_ctx.json"))
    assert ctx["run_id"] == chat and ctx["protected"][-2:] == [real(run_dir / "hook-settings.json"), real(journal)]

    # §5.9: the push's key file is a secret wherever the owner keeps it, from the next stage on
    usual = studio_guard.default_secrets(dict(os.environ), real(ROOT))
    for named, file in ((str(tmp_path / "keys" / "fcm.json"), tmp_path / "keys" / "fcm.json"),
                        ("keys/fcm.json", ROOT / "keys" / "fcm.json"), ("  ", None)):
        monkeypatch.setattr(api._cfg, "studio_fcm_key", named)
        api.guard_stage(task["run_id"], "build", run_dir=run_dir, cwd=cwd)
        ctx = studio_guard.load_ctx(str(run_dir / "guard_ctx.json"))
        assert ctx["secrets"] == usual + ([real(file)] if file else []), named


def test_known_hosts_are_the_git_remotes_and_the_configured_ones(tmp_path):
    repo = tmp_path / "game"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://kim@GitHub.com/kim/game.git\n\tpushurl = git@gitlab.example.com:kim/game.git\n'
        '[remote "disk"]\n\turl = D:\\mirror\\game.git\n[remote "lan"]\n\turl = ssh://git@10.0.0.5:2222/game.git\n',
        encoding="utf-8")
    cfg = type("Cfg", (), {"runs_dir": tmp_path / "runs", "server_url": "http://127.0.0.1:8020",
                           "studio_known_hosts": ["Files.Example.com", "github.com", ""]})()
    path = guard.write_ctx(tmp_path / "runs" / "r1", cfg, "r1", tmp_path, [{"path": str(repo)}, {"path": str(tmp_path / "none")}])
    assert studio_guard.load_ctx(str(path))["known_hosts"] == ["github.com", "gitlab.example.com", "10.0.0.5",
                                                              "files.example.com"]


def shape(call):
    """What the guard could change in a call. The settings file is the run's own, so only its name is compared."""
    return (call.tools, call.mcp_servers, call.allowed_tools, call.settings_path and Path(call.settings_path).name,
            call.env, call.claude_only, call.permission_mode)


def test_the_engine_guards_studio_runs_and_leaves_every_other_run_as_it_was(desk, tmp_path, monkeypatch):  # noqa: F811
    engine, relay, runner, game = engine_setup(tmp_path, monkeypatch)
    monkeypatch.delenv("KATAE_TOKEN", raising=False)
    relays = tmp_path / "relays"
    relays.mkdir()
    (relays / "role.md").write_text("r", encoding="utf-8")
    (relays / "planner.yaml").write_text(
        "name: planner\nhidden: true\nstages:\n  - {name: plan, provider: mock, prompt: role.md}\n", encoding="utf-8")
    (relays / "task.yaml").write_text(
        "name: task\nstages:\n  - {name: build, provider: mock, prompt: role.md}\n", encoding="utf-8")
    campaigns = CampaignRunner(engine, relays, sleep=lambda seconds: None)
    campaigns.spawn = lambda cid: None

    def dashboard():  # a dashboard · CLI run: the engine's own create and advance
        engine.advance(engine.create(relay, "g", repo="game").id)
        return shape(runner.seen[-1])

    def campaign():  # the planning run of a campaign (relay_agent/campaign.py)
        seen = len(runner.seen)
        _, plan_run = campaigns.start("로드맵", game, spec=CampaignSpec(planner="planner", task_relay="task"))
        engine.advance(plan_run.id)
        assert len(runner.seen) == seen + 1
        return shape(runner.seen[-1])

    before = dashboard(), campaign()
    assert before[0][5] is False and before[1][5] is False
    engine.guard_stage = api.guard_stage  # what server.py registers
    assert (dashboard(), campaign()) == before  # not in studio's ledger: the call is the one it was
    assert not list((tmp_path / "runs").glob("*/guard_ctx.json"))

    desk.turn(desk.w.emp)  # a chat turn's run: tracked under the id the engine gave it
    turn = engine.create(relay, "g", repo="game")
    api.track_run(turn.id, mode="chat", employee_id=desk.w.emp, room_id=desk.dms[desk.w.emp])
    desk.tracked.append(turn.id)
    engine.advance(turn.id)
    call, run_dir = runner.seen[-1], engine._dir(turn.id)
    assert call.claude_only and call.settings_path == run_dir / "studio_settings.json"
    assert call.allowed_tools == before[0][2] + ["mcp__studio"] and call.tools == before[0][0]  # no tool is added
    assert call.env == {**before[0][4], "STUDIO_URL": api._cfg.server_url, "STUDIO_ACTOR_TOKEN": call.env["STUDIO_ACTOR_TOKEN"]}
    assert studio_guard.claims(call.env["STUDIO_ACTOR_TOKEN"])["run"] == turn.id
    assert studio_guard.load_ctx(str(run_dir / "guard_ctx.json"))["run_id"] == turn.id
    assert real(run_dir / "guard_ctx.json") in studio_guard.load_ctx(str(run_dir / "guard_ctx.json"))["protected"]
    args = ClaudeCliRunner(exe="claude").build_args(call, None)
    assert args[args.index("--settings") + 1] == str(call.settings_path) and "--safe-mode" not in args

    task, work = desk.working(), engine.create(relay, "g", repo="game")  # a task run: tasks.run_id is the ledger
    with desk.store.transaction() as tx:
        tx.exec("UPDATE tasks SET run_id = ? WHERE id = ?", (work.id, task["id"]))
    engine.advance(work.id)
    assert runner.seen[-1].allowed_tools == before[0][2] + PROFILE and runner.seen[-1].claude_only

    # closed side: a callback that cannot tell fails the stage — the run does not go on without its guard
    def broken(run_id, stage, **given):
        raise RuntimeError("DB 가 닫혔다")

    engine.guard_stage, seen = broken, len(runner.seen)
    failed = engine.advance(engine.create(relay, "g", repo="game").id)
    assert failed.status == "failed" and "가드" in failed.error and len(runner.seen) == seen


def test_a_guarded_stage_runs_on_claude_code_or_not_at_all(tmp_path, monkeypatch):
    engine, relay, runner, _ = engine_setup(tmp_path, monkeypatch)
    other = tmp_path / "other.yaml"  # a stage of another AI: its runner has no PreToolUse hook (§10.8 의 9)
    other.write_text(RELAY.replace("provider: mock", "provider: codex"), encoding="utf-8")
    ledger = set()
    engine.guard_stage = lambda run_id, stage, **given: {"claude_only": True} if run_id in ledger else None
    assert engine.advance(engine.create(other, "g", repo="game").id).status == "done"  # outside studio: as it was
    assert len(runner.seen) == 1 and runner.seen[0].claude_only is False
    inside = engine.create(other, "g", repo="game")
    ledger.add(inside.id)
    ended = engine.advance(inside.id)
    assert ended.status == "failed" and "codex" in ended.error and "가드" in ended.error and len(runner.seen) == 1
    mock = engine.create(relay, "g", repo="game")  # the tests' own runner calls no tool: it passes
    ledger.add(mock.id)
    assert engine.advance(mock.id).status == "done" and runner.seen[-1].claude_only is True


def test_a_guarded_task_run_keeps_the_bash_its_relay_gave(desk, tmp_path, monkeypatch):  # noqa: F811
    """A repo without a verification command loses Bash to Grep · Glob (`pipeline._stage_tools`). A studio task
    run keeps the Bash its relay's stage lists — the profile approves it and the hook judges each command (§10.1,
    owner's decision of 2026-10-14). A chat turn, whose profile has no Bash, a stage its relay gives no Bash and
    every run outside studio stay as they were."""
    engine, relay, runner, _ = engine_setup(tmp_path, monkeypatch)
    monkeypatch.delenv("KATAE_TOKEN", raising=False)
    shell = tmp_path / "shell.yaml"
    shell.write_text("name: s\nworkspace: none\nstages:\n  - {name: build, provider: mock, prompt: role.md, "
                     "tools: [Read, Bash]}\n", encoding="utf-8")
    engine.guard_stage = api.guard_stage  # what server.py registers
    engine.advance(engine.create(shell, "g", repo="game").id)
    outside = runner.seen[-1].tools
    assert "Bash" not in outside and "Grep" in outside  # the repo has no verification command: as it was

    desk.turn(desk.w.emp)  # a chat turn's run: its profile approves no Bash
    turn = engine.create(shell, "g", repo="game")
    api.track_run(turn.id, mode="chat", employee_id=desk.w.emp, room_id=desk.dms[desk.w.emp])
    desk.tracked.append(turn.id)
    engine.advance(turn.id)
    assert runner.seen[-1].tools == outside

    task, work = desk.working(), engine.create(shell, "g", repo="game")  # a task run: tasks.run_id is the ledger
    with desk.store.transaction() as tx:
        tx.exec("UPDATE tasks SET run_id = ? WHERE id = ?", (work.id, task["id"]))
    engine.advance(work.id)
    call = runner.seen[-1]
    assert call.tools == outside + ["Bash"] and "Bash" in call.allowed_tools and call.claude_only
    args = ClaudeCliRunner(exe="claude").build_args(call, None)
    assert "Bash" in args[args.index("--tools") + 1].split(",")

    plain = engine.create(relay, "g", repo="game")  # a stage whose relay gives no Bash gets none
    with desk.store.transaction() as tx:
        tx.exec("UPDATE tasks SET run_id = ? WHERE id = ?", (plain.id, task["id"]))
    engine.advance(plain.id)
    assert "Bash" not in runner.seen[-1].tools and runner.seen[-1].claude_only


def test_a_stage_with_the_hook_is_never_started_in_safe_mode(tmp_path):
    def args(**over):
        call = StageCall(stage="s", model=None, effort=None, system="s", prompt="p", cwd=tmp_path, isolate=True, **over)
        return ClaudeCliRunner(exe="claude").build_args(call, None)

    assert "--safe-mode" in args()  # as it was: nothing a safe mode would switch off
    guarded = args(settings_path=tmp_path / "studio_settings.json")  # §10.8 의 10: safe mode would switch the hook off
    assert "--safe-mode" not in guarded and guarded[guarded.index("--settings") + 1] == str(tmp_path / "studio_settings.json")


# --- the backup hook's settings file: read from, so protected and read narrowly (§10.8, §11.4 의 2) -------------
BACKUP = {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": "py backup_hook.py"}]}


def test_the_backup_hooks_settings_file_is_protected_whether_the_run_has_one_or_not(tmp_path):
    cfg = type("Cfg", (), {"runs_dir": tmp_path / "runs", "server_url": "http://127.0.0.1:8020",
                           "studio_known_hosts": []})()
    run_dir = tmp_path / "runs" / "r1"
    own = run_dir / "hook-settings.json"  # JournalWorkspace.settings_path (journal.py)
    assert guard.JOURNAL_NAME == own.name

    def refused(ctx, path):  # the hook's own judgement of an Edit of that file
        event = {"tool_name": "Edit", "tool_input": {"file_path": str(path)}, "cwd": str(tmp_path)}
        verdict = studio_guard.judge(event, ctx, dict(os.environ))
        return (verdict.verdict, verdict.rule_id) == ("block", "protected_write")

    ctx = studio_guard.load_ctx(str(guard.write_ctx(run_dir, cfg, "r1", tmp_path, [])))  # no backup hook in this run
    assert real(own) in ctx["protected"] and refused(ctx, own) and not refused(ctx, run_dir / "outputs" / "build.md")
    elsewhere = tmp_path / "ws" / "hook-settings.json"  # the file the engine names, when it is kept somewhere else
    assert not refused(ctx, elsewhere)
    ctx = studio_guard.load_ctx(str(guard.write_ctx(run_dir, cfg, "r1", tmp_path, [], elsewhere)))
    assert real(own) in ctx["protected"] and real(elsewhere) in ctx["protected"]
    assert refused(ctx, own) and refused(ctx, elsewhere)
    same = studio_guard.load_ctx(str(guard.write_ctx(run_dir, cfg, "r1", tmp_path, [], own)))["protected"]
    assert same.count(real(own)) == 1


def test_only_the_pretooluse_entries_of_the_journal_file_are_carried_over(tmp_path):
    journal, ctx_path = tmp_path / "hook-settings.json", tmp_path / "guard_ctx.json"
    journal.write_text(json.dumps({
        "disableAllHooks": True, "permissions": {"allow": ["Bash"], "defaultMode": "bypassPermissions"},
        "env": {"STUDIO_URL": "http://evil.example"},
        "hooks": {"PreToolUse": [BACKUP], "PostToolUse": [BACKUP], "SessionStart": [BACKUP]}}), encoding="utf-8")
    path = guard.write_settings(tmp_path, ctx_path, journal)
    line = f'"{sys.executable}" "{Path(studio_guard.__file__).resolve()}" "{ctx_path}"'
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": line}]}, BACKUP]}}
    assert "disableAllHooks" not in path.read_text(encoding="utf-8")
    for unread in ("{}", '{"hooks": {}}', '{"hooks": {"PreToolUse": {}}}', '{"hooks": []}', "[]", "not json"):
        journal.write_text(unread, encoding="utf-8")  # the backups cannot be told: no settings file is made of it
        with pytest.raises(ValueError):
            guard.write_settings(tmp_path, ctx_path, journal)
    with pytest.raises(OSError):
        guard.write_settings(tmp_path, ctx_path, tmp_path / "none.json")


# --- the shared token (§10.8 의 4) -------------------------------------------------------------------------------
def test_the_shared_token_is_blanked_for_a_studio_run_only_when_the_server_has_it(desk, tmp_path, monkeypatch):  # noqa: F811
    task = desk.working()
    run_dir = tmp_path / "runs" / task["run_id"]
    monkeypatch.delenv("KATAE_TOKEN", raising=False)
    env = api.guard_stage(task["run_id"], "build", run_dir=run_dir, cwd=tmp_path)["env"]
    assert set(env) == {"STUDIO_URL", "STUDIO_ACTOR_TOKEN"}  # no such variable: no key is added
    monkeypatch.setenv("KATAE_TOKEN", "the-owners-token")
    env = api.guard_stage(task["run_id"], "build", run_dir=run_dir, cwd=tmp_path)["env"]
    assert set(env) == {"STUDIO_URL", "STUDIO_ACTOR_TOKEN", "KATAE_TOKEN"} and env["KATAE_TOKEN"] == ""
    assert {**os.environ, **env}["KATAE_TOKEN"] == ""  # what the CLI process is started with (runners.run_process)
    for name in ("guard_ctx.json", "studio_settings.json"):
        assert "the-owners-token" not in (run_dir / name).read_text(encoding="utf-8")
    assert api.guard_stage("20261011-000000-abcdef", "build", run_dir=run_dir, cwd=tmp_path) is None  # as it was


def load_katae(name):
    """mcp_servers/katae.py read anew, as the MCP process reads it when it starts: BASE and TOKEN are those of
    the environment at that moment."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "mcp_servers" / "katae.py")
    katae = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(katae)
    return katae


def test_the_katae_mcp_reaches_this_pc_with_the_token_blanked(world, monkeypatch):  # noqa: F811
    """A studio run's `katae` MCP process inherits the blank `KATAE_TOKEN`: it sends no Authorization header,
    and this PC's server takes a loopback request on its old paths without one (§10.8 의 3 가 · 4)."""
    monkeypatch.setenv("KATAE_TOKEN", "")
    monkeypatch.delenv("KATAE_URL", raising=False)
    katae = load_katae("katae_mcp_blank_token")
    assert katae.TOKEN == ""
    local, sent = TestClient(world.owner.app), []  # this PC, no token of its own

    def request(method, url, *, timeout, headers, **kwargs):
        sent.append(dict(headers))
        return local.request(method, url.removeprefix(katae.BASE), headers=headers, **kwargs)

    monkeypatch.setattr(katae.httpx, "request", request)
    with pytest.raises(RuntimeError, match="^404"):  # past the server's token check: only the path does not exist
        katae._call("GET", "/no-such-path")
    assert sent == [{}]
    assert world.remote.get("/no-such-path").status_code == 401  # another device is asked for the token there


def test_a_katae_mcp_aimed_at_another_pc_is_refused_until_the_token_is_in_its_own_environment(world, monkeypatch):  # noqa: F811
    """`KATAE_URL` names another PC. With the blank token a studio run hands down, that server answers 401 — it
    trusts its own loopback only. With the token in the MCP process's environment the request is past the check:
    that is what the entry's own `env` is for (§10.8 의 4 다). `world.remote` is the other PC of this test: the
    same app, asked from an address that is not loopback."""
    base, sent = "http://other-pc:8020", []

    def request(method, url, *, timeout, headers, **kwargs):
        sent.append(dict(headers))
        return world.remote.request(method, url.removeprefix(base), headers=headers, **kwargs)

    monkeypatch.setenv("KATAE_URL", base)
    monkeypatch.setenv("KATAE_TOKEN", "")
    katae = load_katae("katae_mcp_remote_blank")
    monkeypatch.setattr(katae.httpx, "request", request)
    assert (katae.BASE, katae.TOKEN) == (base, "")
    with pytest.raises(RuntimeError, match="^401"):
        katae._call("GET", "/no-such-path")
    monkeypatch.setenv("KATAE_TOKEN", SHARED)  # the entry's env, once it is the environment of the MCP process
    katae = load_katae("katae_mcp_remote_token")
    assert (katae.BASE, katae.TOKEN) == (base, SHARED)
    with pytest.raises(RuntimeError, match="^404"):  # past the token check: only the path does not exist
        katae._call("GET", "/no-such-path")
    assert sent == [{}, {"Authorization": f"Bearer {SHARED}"}]


def test_a_blank_token_and_no_token_are_not_the_same_to_the_katae_mcp(tmp_path, monkeypatch):
    """`Config.load` takes `os.environ.get("KATAE_TOKEN", cfg.auth_token)` (config.py): a variable that is there
    and blank wins over the file, and the MCP sends no token. Only when there is no such variable — the server was
    not given its token by the environment, so `api.guard_stage` added no key — does the file's token go out."""
    (tmp_path / "relay.config.local.yaml").write_text("auth_token: file-token\n", encoding="utf-8")
    load = Config.load.__func__
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, path=None: load(cls, tmp_path / "relay.config.yaml")))
    monkeypatch.delenv("KATAE_URL", raising=False)
    monkeypatch.setenv("KATAE_TOKEN", "")
    assert load_katae("katae_mcp_blank_over_file").TOKEN == ""
    monkeypatch.delenv("KATAE_TOKEN")
    katae, sent = load_katae("katae_mcp_file_token"), []

    def request(method, url, *, timeout, headers, **kwargs):
        sent.append((url, dict(headers)))
        raise katae.httpx.ConnectError("no server in this test")

    monkeypatch.setattr(katae.httpx, "request", request)
    with pytest.raises(RuntimeError, match="꺼져"):
        katae._call("GET", "/providers")
    assert katae.TOKEN == "file-token"
    assert sent == [(katae.BASE + "/providers", {"Authorization": "Bearer file-token"})]


def test_a_blanked_variable_is_blank_to_the_cli_process_and_to_what_it_starts(monkeypatch):
    """`runners.run_process` starts the CLI with `{**os.environ, **call.env}`, and the CLI starts the shell and
    the MCP servers with the environment it has. A blank value is handed down as a blank value: the child and the
    process the child starts both see "" — there and blank, not gone (the two differ to the katae MCP: the test
    above). Python processes stand in for Claude Code here; what Claude Code itself hands down is a manual check."""
    monkeypatch.setenv("KATAE_TOKEN", "the-owners-token")
    below = "import json, os; print(json.dumps(os.environ.get('KATAE_TOKEN')))"
    child = ("import json, os, subprocess, sys\n"
             f"below = subprocess.run([sys.executable, '-c', {below!r}], capture_output=True, text=True, timeout=60)\n"
             "print(json.dumps({'child': os.environ.get('KATAE_TOKEN'), 'grandchild': json.loads(below.stdout)}))\n")
    done = subprocess.run([sys.executable, "-c", child], env={**os.environ, "KATAE_TOKEN": ""},
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"child": "", "grandchild": ""}, done.stdout


class Started(Exception):
    """Where the stand-in for `runners.run_process` stops: no CLI is started."""


def test_an_mcp_entrys_own_token_is_written_to_the_runs_mcp_config_and_the_inherited_one_is_blank(
        desk, tmp_path, monkeypatch):  # noqa: F811
    """A project's `katae` entry that names another PC and that PC's token in its own `env`. As far as this
    server's code goes: the engine carries the entry with its env, the runner writes it to the run's mcp.json as
    it is and hands only that file to the CLI, and the CLI process is started with the server's own token blank.
    What Claude Code gives the MCP process from there is not seen here (design/studio/VERIFICATION.md)."""
    engine, relay, runner, game = engine_setup(tmp_path, monkeypatch)
    entry = {"command": "python", "args": [str(ROOT / "mcp_servers" / "katae.py")],
             "env": {"KATAE_URL": "http://other-pc:8020", "KATAE_TOKEN": "the-other-pcs-token"}}
    (game / ".mcp.json").write_text(json.dumps({"mcpServers": {"katae": entry}}), encoding="utf-8")
    monkeypatch.setenv("KATAE_TOKEN", "the-owners-token")  # how this server was given its own token
    engine.guard_stage = api.guard_stage  # what server.py registers
    desk.turn(desk.w.emp)
    turn = engine.create(relay, "g", repo="game")
    api.track_run(turn.id, mode="chat", employee_id=desk.w.emp, room_id=desk.dms[desk.w.emp])
    desk.tracked.append(turn.id)
    engine.advance(turn.id)
    assert len(runner.seen) == 1
    call = runner.seen[0]
    assert call.mcp_servers == ["katae"] and call.mcp_overrides["katae"] == entry
    assert call.settings_path and call.env["KATAE_TOKEN"] == ""  # the guarded call of a studio run
    seen = {}

    def run_process(args, call, stdin_text, on_line, **kwargs):  # runners.run_process, up to its Popen
        path = Path(args[args.index("--mcp-config") + 1])
        seen.update(args=args, path=path, written=json.loads(path.read_text(encoding="utf-8")),
                    env={**os.environ, **call.env})  # what Popen is given (runners.py)
        raise Started

    monkeypatch.setattr("relay_agent.runners.run_process", run_process)
    with pytest.raises(Started):
        ClaudeCliRunner(exe="claude")._run_once(call, None)
    assert seen["written"] == {"mcpServers": {"katae": entry}}  # the entry's own token: as the owner wrote it
    assert seen["env"]["KATAE_TOKEN"] == "" and "the-owners-token" not in seen["env"].values()
    assert "--strict-mcp-config" in seen["args"]  # no other MCP config is read: this file is all the run has
    assert seen["path"].name == "mcp.json" and seen["path"].parent.name.startswith("katae-")  # of secrets[] (§10.3)
    assert not seen["path"].exists()  # gone when the CLI process ended
