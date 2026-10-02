"""A broken copy-mode snapshot store (runs/shadow/<hash>.git) is found, reset, and rebuilt on its own."""
from pathlib import Path

from relay_agent.cli import cache_command
from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.runners import MockRunner
from relay_agent.usage import UsageStore
from relay_agent.workspace import check_shadow


class Writer(MockRunner):
    """Edits the workspace it runs in, like a build stage would."""

    def run(self, call):
        (Path(call.cwd) / "a.txt").write_text("changed\n", encoding="utf-8")
        return super().run(call)


def setup(tmp_path, auto_apply=False):
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text(f"name: r\nworkspace: copy\nauto_apply: {str(auto_apply).lower()}\nstages:\n"
                     "  - {name: build, provider: mock, prompt: role.md, tools: [Edit]}\n", encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: Writer())
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("original\n", encoding="utf-8")
    return engine, relay, project


def break_store(engine):
    (store,) = (engine.runs_dir / "shadow").glob("*.git")
    for obj in (store / "objects").glob("??/*"):
        obj.chmod(0o644)
        obj.write_bytes(b"")  # e.g. a disk error or a killed process left empty object files
    return store


def test_a_broken_store_is_reported_and_reset_keeps_the_runs_patch(tmp_path):
    engine, relay, project = setup(tmp_path)
    run = engine.advance(engine.create(relay, "g", project).id)
    assert run.changes_status == "ready"  # undecided: the work is only in the copy
    store = break_store(engine)
    assert check_shadow(store)
    report = cache_command(engine, "check", None)
    assert "손상" in report[0] and "relay cache reset" in report[-1]

    result = engine.reset_snapshot_store(str(project))  # by folder; a repo name or store file works too
    assert not store.exists() and result["runs_cleaned"] == [run.id] and result["patch_kept"] == [run.id]
    run = engine.apply_changes(run.id)  # the kept patch still applies
    assert (project / "a.txt").read_text(encoding="utf-8") == "changed\n"


def test_reset_is_refused_while_a_run_uses_the_store(tmp_path):
    engine, relay, project = setup(tmp_path)
    run = engine.advance(engine.create(relay, "g", project).id)
    run.status = "awaiting_approval"
    engine.save(run)
    try:
        engine.reset_snapshot_store(str(project))
    except ValueError as e:
        assert run.id in str(e)
    else:
        raise AssertionError("reset must be refused")


def test_a_new_run_rebuilds_a_broken_store_by_itself(tmp_path):
    engine, relay, project = setup(tmp_path, auto_apply=True)
    first = engine.advance(engine.create(relay, "g", project).id)
    assert first.changes_status == "applied"
    (project / "a.txt").write_text("original\n", encoding="utf-8")  # same content as the stored blob
    break_store(engine)
    run = engine.advance(engine.create(relay, "g", project).id)
    assert run.status == "done" and run.changes_status == "applied", run.error
    assert "snapshot_store_rebuilt" in [e["kind"] for e in engine.history.events(run.id)]
    (store,) = (engine.runs_dir / "shadow").glob("*.git")
    assert check_shadow(store) is None


def test_reset_never_deletes_anything_outside_the_store_folder(tmp_path):
    engine, relay, project = setup(tmp_path)
    for target in (str(project), str(tmp_path), "../project", "Game"):
        try:
            engine.reset_snapshot_store(target)  # no copy run yet: there is no store for any of these
        except ValueError:
            pass
    assert (project / "a.txt").read_text(encoding="utf-8") == "original\n" and tmp_path.exists()
    try:
        engine.reset_snapshot_store(project)  # a Path is taken as a store and must live in runs/shadow
    except ValueError as e:
        assert "스냅샷 저장소가 아닙니다" in str(e)
    assert project.exists()
