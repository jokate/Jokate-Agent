"""Disk growth guards: shared snapshots, size limit, skipped big files, cleanup of no-change runs."""
import pytest

from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.runners import MockRunner
from relay_agent.usage import UsageStore
from relay_agent.workspace import Workspace, WorkspaceError, dir_size


def project(tmp_path, files=20, size=200_000):
    src = tmp_path / "proj"
    (src / "src").mkdir(parents=True)
    for i in range(files):
        (src / "src" / f"f{i}.txt").write_bytes(bytes([65 + i % 26]) * size)
    return src


def test_repeated_runs_share_objects_instead_of_full_copies(tmp_path):
    src = project(tmp_path)  # ~4 MB of files
    stores = tmp_path / "shadow"
    first = Workspace(tmp_path / "runs" / "r1", src, "inplace", shadow_root=stores)
    first.prepare()
    after_one = dir_size(stores)
    (src / "src" / "f0.txt").write_bytes(b"changed")
    for i in range(2, 6):
        Workspace(tmp_path / "runs" / f"r{i}", src, "inplace", shadow_root=stores).prepare()
    after_five = dir_size(stores)
    assert after_five < after_one * 1.5  # five snapshots cost about one
    assert len(list(stores.glob("*.git"))) == 1


def test_size_guard_refuses_before_writing_and_names_big_folders(tmp_path):
    src = project(tmp_path, files=10, size=300_000)  # ~3 MB
    (src / "assets").mkdir()
    (src / "assets" / "big.bin").write_bytes(b"x" * 1_500_000)
    ws = Workspace(tmp_path / "runs" / "r1", src, "copy", shadow_root=tmp_path / "shadow", max_snapshot_mb=2)
    with pytest.raises(WorkspaceError, match="src") as err:
        ws.prepare()
    assert "한도" in str(err.value)
    assert not ws.copy_dir.exists()


def test_oversized_single_files_are_skipped_and_reported(tmp_path):
    src = project(tmp_path, files=2, size=10)
    (src / "movie.raw").write_bytes(b"x" * 3_000_000)
    ws = Workspace(tmp_path / "runs" / "r1", src, "copy", shadow_root=tmp_path / "shadow", max_file_mb=1)
    info = ws.prepare()
    assert info["skipped_large"] and "movie.raw" in info["skipped_large"][0]
    assert not (ws.path / "movie.raw").exists() and (ws.path / "src" / "f0.txt").exists()


def test_run_without_changes_leaves_no_copy_behind(tmp_path):
    src = project(tmp_path, files=3, size=100)
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text("name: r\nworkspace: copy\nstages:\n  - {name: build, provider: mock, prompt: role.md, tools: [Edit]}\n",
                     encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: MockRunner())
    run = engine.advance(engine.create(relay, "look only", src).id)
    assert run.status == "done" and run.changes_status == "none" and run.workspace_cleaned
    assert not (engine.runs_dir / run.id / "workspace").exists()
    report = engine.disk_usage()
    assert report["working_copies_mb"] == 0
