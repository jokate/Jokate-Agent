import os
import threading
import time

from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.runners import MockRunner
from relay_agent.usage import UsageStore


def make(tmp_path, workspace, runner):
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text(f"name: r\nworkspace: {workspace}\nstages:\n"
                     "  - {name: build, provider: mock, prompt: role.md, tools: [Edit]}\n", encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: runner)
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "a.txt").write_text("a", encoding="utf-8")
    return engine, relay, project


def test_second_inplace_run_on_same_folder_waits_its_turn(tmp_path):
    engine, relay, project = make(tmp_path, "inplace", MockRunner(delay_s=1.5))
    first = engine.create(relay, "one", project)
    second = engine.create(relay, "two", project)
    t = threading.Thread(target=engine.advance, args=(first.id,))
    t.start()
    for _ in range(50):
        if engine.load(first.id).status == "running":
            break
        time.sleep(0.05)
    blocked = engine.advance(second.id)
    assert blocked.status == "failed" and first.id in blocked.error
    assert blocked.baton.stop is not None
    t.join(10)
    assert engine.load(first.id).status == "done"
    assert engine.advance(second.id, resume=True).status == "done"  # lock released after the first run
    assert not any((engine.runs_dir / "locks").glob("*.lock"))


def test_stale_lock_from_crashed_run_is_ignored(tmp_path):
    engine, relay, project = make(tmp_path, "inplace", MockRunner())
    ghost = engine.create(relay, "crashed", project)  # never ran: status pending, not running
    lock = engine._folder_lock(str(project))
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(ghost.id, encoding="utf-8")
    run = engine.advance(engine.create(relay, "next", project).id)
    assert run.status == "done"


def test_cleanup_removes_decided_now_and_undecided_after_retention(tmp_path):
    class Editor(MockRunner):
        def run(self, call):
            (call.cwd / "a.txt").write_text("b", encoding="utf-8")
            return super().run(call)

    engine, relay, project = make(tmp_path, "copy", Editor())
    applied = engine.advance(engine.create(relay, "apply me", project).id)
    engine.apply_changes(applied.id)
    (project / "a.txt").write_text("a", encoding="utf-8")
    pending = engine.advance(engine.create(relay, "undecided", project).id)
    assert pending.changes_status == "ready"

    # applying cleans up at once; both runs shared a single snapshot store for this folder
    assert engine.load(applied.id).workspace_cleaned
    assert not (engine.runs_dir / applied.id / "workspace").exists()
    assert len(list((engine.runs_dir / "shadow").glob("*.git"))) == 1

    result = engine.cleanup_workspaces(retention_days=7)
    assert result["cleaned"] == []  # the undecided run is kept within the retention period
    assert (engine.runs_dir / pending.id / "workspace").exists()

    old = time.time() - 8 * 86400
    os.utime(engine.runs_dir / pending.id / "run.json", (old, old))
    assert engine.cleanup_workspaces(retention_days=7)["cleaned"] == [pending.id]
    assert (engine.runs_dir / pending.id / "result.patch").exists()
    assert not list((engine.runs_dir / "shadow").glob("*.git"))  # no runs left -> store deleted

    # the saved patch can still be applied after its workspace is gone
    engine.apply_changes(pending.id)
    assert (project / "a.txt").read_text(encoding="utf-8") == "b"
