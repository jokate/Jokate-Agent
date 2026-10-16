"""S-11 ①: the per-run record of skills, MCP calls and guard warnings (relay_agent/studio/runlog.py)."""

import json
import threading
from datetime import datetime
from pathlib import Path

import pytest

from relay_agent import studio_guard
from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.repos import RepoRegistry
from relay_agent.runners import MockRunner
from relay_agent.studio import runlog, runs
from relay_agent.usage import UsageStore

DEMO = Path(__file__).resolve().parent.parent / "relays" / "demo.yaml"


# --- skill_name -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("target,name", [
    ('{"skill": "game-review", "args": "x"}', "game-review"),
    ('{"skill":"qa-pass","args":"' + "z" * 150, "qa-pass"),  # cut at 200 characters, name intact
    ('{"skill": "cut-in-the-mid', "cut-in-the-mid"),
    ("balance-check", "balance-check"),
    ("/balance-check 3 args", "balance-check"),
    ('{"args": "no name"}', ""),
    ("", ""),
])
def test_skill_name(target, name):
    assert runlog.skill_name(target) == name


# --- collect --------------------------------------------------------------------------------------------------------
def ev(kind, stage, **detail):
    return {"id": 1, "at": "2026-10-14T10:00:00", "stage": stage, "kind": kind, "detail": detail}


def test_collect_pairs_mcp_and_filters_audit():
    events = [
        ev("tool_use", "a", tool="Read", target="x"),
        ev("tool_use", "a", tool="Skill", target='{"skill": "s1"}'),
        ev("mcp_call", "a", server="mnys", tool="query", target="q1"),
        ev("mcp_call", "a", server="mnys", tool="query", target="q2"),
        ev("mcp_result", "a", server="mnys", tool="query", chars=10, is_error=False),
        ev("mcp_result", "a", server="mnys", tool="query", chars=5, is_error=True),
        ev("mcp_call", "b", server="mnys", tool="query", target="q3"),
    ]
    audit = [
        {"run_id": "R1", "tool": "mcp__mnys__query", "verdict": "ask", "rule_id": "r", "outcome": "pending",
         "command": "c" * 300, "at": "t", "server": {"inbox_id": "inb_1"}},
        {"run_id": "R1", "tool": "Bash", "verdict": "allow", "rule_id": None, "outcome": "allowed", "command": "ls"},
        {"run_id": "R1", "tool": "Bash", "verdict": "deny", "rule_id": "d", "outcome": "refused", "command": "rm"},
        {"run_id": "R2", "tool": "Bash", "verdict": "deny", "rule_id": "d", "outcome": "refused", "command": "rm"},
    ]
    rec = runlog.collect("R1", events, audit)
    assert [s["name"] for s in rec["skills"]] == ["s1"]
    assert [(m["target"], m["chars"], m["is_error"]) for m in rec["mcp"]] == [
        ("q1", 10, False), ("q2", 5, True), ("q3", None, None)]
    assert [(g["tool"], g["mcp"], g["inbox_id"]) for g in rec["guards"]] == [
        ("mcp__mnys__query", True, "inb_1"), ("Bash", False, None)]
    assert len(rec["guards"][0]["command"]) == runlog.COMMAND_CHARS
    assert rec["counts"] == {"skills": 1, "mcp_calls": 3, "mcp_errors": 1,
                             "guard_judged": 3, "guard_allowed": 1, "guard_warnings": 2}


# --- a mock run -----------------------------------------------------------------------------------------------------
class Skilled(MockRunner):
    """Calls a skill and an MCP tool in every stage."""

    def run(self, call):
        call.emit("tool_use", {"tool": "Skill", "target": json.dumps({"skill": "game-review", "args": "x"})})
        call.emit("mcp_call", {"server": "mnys", "tool": "query", "target": "q"})
        call.emit("mcp_result", {"server": "mnys", "tool": "query", "chars": 42, "is_error": False})
        return super().run(call)


class Lab:
    def __init__(self, tmp_path):
        (tmp_path / "project").mkdir()
        (tmp_path / "work").mkdir()
        self.tmp = tmp_path
        self.runs_dir = tmp_path / "runs"
        self.engine = RelayEngine(
            self.runs_dir, UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
            runner_factory=lambda _: Skilled(), repos=RepoRegistry({}))
        runs.attach(self.engine, lambda: None)

    def create(self):
        return self.engine.create(DEMO, "목표", self.tmp / "work", workspace="none")

    def audit(self, run_id, tool, verdict, outcome="refused", **extra):
        studio_guard.write_audit(str(self.runs_dir), {
            "v": 1, "at": datetime.now().isoformat(timespec="seconds"), "run_id": run_id, "tool": tool,
            "command": "cmd", "verdict": verdict, "rule_id": "rule_x", "outcome": outcome, **extra})


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


def test_mock_run_leaves_a_record_studio_reads_back(lab):
    run = lab.create()
    lab.audit(run.id, "mcp__mnys__query", "block")
    lab.audit(run.id, "Bash", "allow", "allowed")
    lab.audit("other-run", "Bash", "block")
    lab.engine.advance(run.id)
    assert (lab.runs_dir / "studio" / "runlog" / f"{run.id}.json").is_file()
    rec = runlog.read(lab.runs_dir, run.id)
    assert rec["counts"]["skills"] >= 1
    assert rec["skills"][0]["name"] == "game-review"
    assert rec["mcp"][0]["server"] == "mnys" and rec["mcp"][0]["chars"] == 42
    assert len(rec["guards"]) == 1 and rec["guards"][0]["mcp"] is True and rec["guards"][0]["verdict"] == "block"
    assert rec["counts"]["guard_judged"] == 2 and rec["counts"]["guard_allowed"] == 1
    assert rec["counts"]["skills"] == rec["counts"]["mcp_calls"]


def test_broken_audit_line_and_rewrite_do_not_break_the_record(lab):
    run = lab.create()
    lab.audit(run.id, "mcp__mnys__query", "block")
    audit_file = next((lab.runs_dir / "studio" / "audit").glob("guard-*.jsonl"))
    with audit_file.open("a", encoding="utf-8") as f:
        f.write(f'{{"run_id": "{run.id}", broken\n')
    lab.engine.advance(run.id)  # idle does not raise
    first = runlog.read(lab.runs_dir, run.id)
    assert first and first["counts"]["guard_warnings"] == 1
    again = runlog.record(lab.engine, run.id)
    for rec in (first, again):
        rec.pop("collected_at")
    assert first == again


def test_read_none_cases(lab, tmp_path):
    assert runlog.read(lab.runs_dir, "20260101-000000-nope") is None
    assert runlog.read(lab.runs_dir, "../escape") is None
    with pytest.raises(ValueError):
        runlog.record(lab.engine, "a/b")
    folder = lab.runs_dir / "studio" / "runlog"
    folder.mkdir(parents=True)
    (folder / "broken.json").write_text("{nope", encoding="utf-8")
    (folder / "wrong.json").write_text(json.dumps({"run_id": "other"}), encoding="utf-8")
    assert runlog.read(lab.runs_dir, "broken") is None
    assert runlog.read(lab.runs_dir, "wrong") is None


def test_overlapping_records_leave_one_clean_file(lab, monkeypatch):
    run = lab.create()
    lab.engine.advance(run.id)
    first = runlog.read(lab.runs_dir, run.id)
    fixed = dict(first)
    monkeypatch.setattr(runlog, "snapshot", lambda engine, run_id: dict(fixed))  # sqlite is single-thread
    errors = []

    def work():
        try:
            for _ in range(20):
                runlog.record(lab.engine, run.id)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    folder = lab.runs_dir / "studio" / "runlog"
    assert list(folder.glob("*.tmp")) == []
    assert runlog.read(lab.runs_dir, run.id) == fixed


def test_failed_write_leaves_no_temp_file_and_keeps_old_record(lab, monkeypatch):
    run = lab.create()
    lab.engine.advance(run.id)
    before = runlog.read(lab.runs_dir, run.id)

    def deny(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(runlog.os, "replace", deny)
    with pytest.raises(PermissionError):
        runlog.record(lab.engine, run.id)
    monkeypatch.undo()
    assert list((lab.runs_dir / "studio" / "runlog").glob("*.tmp")) == []
    assert runlog.read(lab.runs_dir, run.id) == before
