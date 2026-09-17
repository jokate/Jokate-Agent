"""Regression tests for the independent review findings."""
import subprocess
import threading
import time
from pathlib import Path

import pytest

from relay_agent import cc_import
from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.providers import looks_like_quota
from relay_agent.runners import MockRunner
from relay_agent.usage import UsageStore
from relay_agent.workspace import Workspace, WorkspaceError


def make_engine(tmp_path, yaml_text, runner):
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text(yaml_text, encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: runner)
    return engine, relay


def test_changes_cannot_be_applied_while_relay_is_paused(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.txt").write_text("a", encoding="utf-8")

    class Editor(MockRunner):
        def run(self, call):
            (call.cwd / "a.txt").write_text("b", encoding="utf-8")
            return super().run(call)

    engine, relay = make_engine(tmp_path, """
name: r
workspace: copy
stages:
  - {name: plan, provider: mock, prompt: role.md, tools: [Edit], gate: human}
  - {name: build, provider: mock, prompt: role.md, tools: [Edit]}
""", Editor())
    run = engine.advance(engine.create(relay, "g", project).id)
    assert run.status == "awaiting_approval"
    with pytest.raises(ValueError, match="끝난 뒤"):
        engine.apply_changes(run.id)
    engine.approve(run.id)
    assert engine.advance(run.id).status == "done"  # workspace still intact for the next stage


@pytest.mark.parametrize("text,expected", [
    ("Claude AI usage limit reached|1789634400", True),
    ("You've hit your limit · resets 3pm", True),
    ("Error: 429 Too Many Requests", True),
    ("fixed billing module; see L429 and quota.py", False),
    ("processed 14290 tokens", False),
])
def test_quota_detection_is_specific(text, expected):
    assert looks_like_quota(text) is expected


def test_cancel_before_thread_starts_is_not_revived(tmp_path):
    engine, relay = make_engine(tmp_path, "name: r\nstages:\n  - {name: a, provider: mock, prompt: role.md}\n", MockRunner())
    run = engine.create(relay, "g", tmp_path)
    engine.cancel(run.id)  # no thread owns it yet
    assert engine.advance(run.id).status == "cancelled"  # a late background start does nothing
    assert engine.advance(run.id, resume=True).status == "done"  # explicit resume does


def test_cancel_between_stages_is_honoured(tmp_path):
    engine, relay = make_engine(tmp_path, """
name: r
stages:
  - {name: a, provider: mock, prompt: role.md}
  - {name: b, provider: mock, prompt: role.md}
""", MockRunner(delay_s=0.3))
    run = engine.create(relay, "g", tmp_path)
    t = threading.Thread(target=engine.advance, args=(run.id,))
    t.start()
    time.sleep(0.1)
    engine.cancel(run.id)
    t.join(5)
    final = engine.load(run.id)
    assert final.status == "cancelled" and [h.stage for h in final.history] in ([], ["a"])


def test_excludes_apply_at_any_depth_and_nested_repos_are_refused(tmp_path):
    src = tmp_path / "proj"
    (src / "web" / "node_modules" / "x").mkdir(parents=True)
    (src / "web" / "node_modules" / "x" / "big.js").write_text("x", encoding="utf-8")
    (src / "web" / "app.js").write_text("y", encoding="utf-8")
    ws = Workspace(tmp_path / "run", src, "copy")
    assert ws.prepare()["files"] == 1

    (src / "vendor" / "lib").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=src / "vendor" / "lib", check=True)
    with pytest.raises(WorkspaceError, match="vendor/lib"):
        Workspace(tmp_path / "run2", src, "copy").prepare()


def test_inplace_discard_is_refused(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("a", encoding="utf-8")
    ws = Workspace(tmp_path / "run", src, "inplace")
    ws.prepare()
    with pytest.raises(WorkspaceError):
        ws.discard()


def test_cwd_slug_matches_claude_code_and_blocks_traversal(tmp_path, monkeypatch):
    assert cc_import.cwd_slug(r"C:\Users\kkkk4017\.buzz") == "C--Users-kkkk4017--buzz"
    monkeypatch.setattr(cc_import, "PROJECTS_DIR", tmp_path / "projects")
    (tmp_path / "projects" / "p").mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "x.jsonl").write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        cc_import.list_sessions(r"..\secret")


def test_session_context_lines_stay_short(tmp_path):
    h = HistoryStore(tmp_path / "h.sqlite")
    sid = h.create_session("s", str(tmp_path))["id"]
    h.add_turn(sid, "질문 " * 800, "claude-code", "cc-1")
    h.update_turn("cc-1", "imported", "답 " * 800)
    (line,) = h.session_context(sid)
    assert len(line) < 450


def test_damaged_run_file_does_not_break_listing(tmp_path):
    engine, relay = make_engine(tmp_path, "name: r\nstages:\n  - {name: a, provider: mock, prompt: role.md}\n", MockRunner())
    good = engine.create(relay, "g", tmp_path)
    bad = tmp_path / "runs" / "broken"
    bad.mkdir()
    (bad / "run.json").write_text("{not json", encoding="utf-8")
    assert [r.id for r in engine.list_runs()] == [good.id]
    assert engine.recover_interrupted() == []
