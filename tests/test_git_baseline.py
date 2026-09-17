"""In-place work on a git repo without snapshotting it."""
import subprocess
from pathlib import Path

from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.runners import MockRunner
from relay_agent.usage import UsageStore
from relay_agent.workspace import GitBaselineWorkspace, dir_size, git_toplevel


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                          cwd=repo, check=True, capture_output=True).stdout


def make_repo(tmp_path) -> Path:
    repo = tmp_path / "Game"
    (repo / "Source").mkdir(parents=True)
    (repo / "Content").mkdir()
    (repo / "Source" / "Hero.cpp").write_bytes(b"int hp = 100;\n")
    (repo / "Source" / "Old.cpp").write_bytes(b"// legacy\n")
    (repo / "Config.ini").write_bytes(b"[game]\nspeed=1\n")
    (repo / "Content" / "Map.umap").write_bytes(b"\0" * 2_000_000)  # big asset, committed
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    # uncommitted work already present when the run starts
    (repo / "Config.ini").write_bytes(b"[game]\nspeed=2\n")
    (repo / "Source" / "Draft.cpp").write_bytes(b"// wip\n")
    return repo


def test_baseline_saves_only_uncommitted_files_and_round_trips(tmp_path):
    repo = make_repo(tmp_path)
    git_dir_size = dir_size(repo / ".git")
    index_before = (repo / ".git" / "index").read_bytes()
    ws = GitBaselineWorkspace(tmp_path / "run", repo, git_toplevel(repo))
    info = ws.prepare()
    assert info["saved_files"] == 2 and info["baseline_mb"] < 0.01  # Config.ini + Draft.cpp, not the 2MB map

    # the relay edits
    (repo / "Source" / "Hero.cpp").write_bytes(b"int hp = 120;\n")
    (repo / "Config.ini").write_bytes(b"[game]\nspeed=3\n")
    (repo / "Source" / "Old.cpp").unlink()
    (repo / "Source" / "Draft.cpp").unlink()
    (repo / "Source" / "New.cpp").write_bytes(b"// new\n")

    summary = ws.collect()
    assert summary["files"] == 5
    patch = ws.patch_path.read_text(encoding="utf-8")
    assert "+int hp = 120;" in patch and "-speed=2" in patch and "+speed=3" in patch  # vs pre-run state, not HEAD
    assert "diff --git a/Source/New.cpp b/Source/New.cpp" in patch

    ws.rollback()
    assert (repo / "Source" / "Hero.cpp").read_bytes() == b"int hp = 100;\n"
    assert (repo / "Config.ini").read_bytes() == b"[game]\nspeed=2\n"  # user's uncommitted edit restored
    assert (repo / "Source" / "Draft.cpp").read_bytes() == b"// wip\n"
    assert (repo / "Source" / "Old.cpp").exists() and not (repo / "Source" / "New.cpp").exists()
    # the repository itself was never written to
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert dir_size(repo / ".git") == git_dir_size
    assert not (tmp_path / "run" / "baseline").exists()


def test_patch_applies_cleanly_on_the_pre_run_state(tmp_path):
    repo = make_repo(tmp_path)
    ws = GitBaselineWorkspace(tmp_path / "run", repo, git_toplevel(repo))
    ws.prepare()
    (repo / "Source" / "Hero.cpp").write_bytes(b"int hp = 150;\n")
    (repo / "Source" / "New.cpp").write_bytes(b"// new\n")
    ws.collect()
    patch = ws.patch_path
    ws.rollback()
    check = subprocess.run(["git", "apply", "--check", str(patch)], cwd=repo, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_engine_uses_git_baseline_for_inplace_git_repos(tmp_path):
    repo = make_repo(tmp_path)

    class Editor(MockRunner):
        def run(self, call):
            (call.cwd / "Source" / "Hero.cpp").write_bytes(b"int hp = 999;\n")
            return super().run(call)

    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text("name: r\nworkspace: inplace\nstages:\n  - {name: build, provider: mock, prompt: role.md, tools: [Edit]}\n",
                     encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: Editor())
    run = engine.advance(engine.create(relay, "buff hero", repo).id)
    assert run.status == "done" and run.changes["files"] == 1
    assert not (engine.runs_dir / "shadow").exists()  # no snapshot store at all
    engine.rollback_changes(run.id)
    assert (repo / "Source" / "Hero.cpp").read_bytes() == b"int hp = 100;\n"
