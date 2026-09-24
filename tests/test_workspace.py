from pathlib import Path

import pytest

from relay_agent.workspace import Workspace, WorkspaceError


def make_project(root: Path) -> Path:
    src = root / "proj"
    (src / "app").mkdir(parents=True)
    (src / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (src / "README.md").write_text("# proj\n", encoding="utf-8")
    (src / "Content").mkdir()
    (src / "Content" / "Hero.uasset").write_bytes(b"\x00" * 64)
    (src / "node_modules").mkdir()
    (src / "node_modules" / "big.js").write_text("x", encoding="utf-8")
    return src


def test_copy_mode_returns_patch_and_applies(tmp_path):
    src = make_project(tmp_path)
    ws = Workspace(tmp_path / "run", src, "copy")
    info = ws.prepare()
    assert info["files"] == 2  # heavy/generated folders excluded
    assert (ws.path / "app" / "main.py").exists() and not (ws.path / "Content").exists()

    (ws.path / "app" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (ws.path / "app" / "new.py").write_text("x = 1\n", encoding="utf-8")
    summary = ws.collect()
    assert summary["files"] == 2 and summary["insertions"] == 2
    assert src.joinpath("app", "main.py").read_text(encoding="utf-8") == "print('hi')\n"  # original untouched

    ws.apply()
    assert src.joinpath("app", "main.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert src.joinpath("app", "new.py").exists()
    assert not ws.copy_dir.exists() and ws.patch_path.exists()


def test_copy_mode_apply_refuses_when_original_changed_the_same_lines(tmp_path):
    src = make_project(tmp_path)
    ws = Workspace(tmp_path / "run", src, "copy")
    ws.prepare()
    (ws.path / "README.md").write_text("# agent\n", encoding="utf-8")
    (ws.path / "app" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (src / "README.md").write_text("# me meanwhile\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="스냅샷 이후.*README.md"):
        ws.apply()
    # all or nothing: the non-conflicting file is not applied either, and the work is kept for a decision
    assert src.joinpath("README.md").read_text(encoding="utf-8") == "# me meanwhile\n"
    assert src.joinpath("app", "main.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert ws.copy_dir.exists() and ws.prepared


LINES = "".join(f"line {i}\n" for i in range(1, 21))


def test_copy_mode_apply_merges_when_original_changed_elsewhere(tmp_path):
    src = make_project(tmp_path)
    (src / "app" / "game.py").write_text(LINES, encoding="utf-8")
    (src / "logo.png").write_bytes(b"\x89PNG" + b"\x00" * 32)
    ws = Workspace(tmp_path / "run", src, "copy")
    ws.prepare()
    # the AI: edits line 10 of game.py, adds a file, deletes README
    (ws.path / "app" / "game.py").write_text(LINES.replace("line 10\n", "line 10 (ai)\n"), encoding="utf-8")
    (ws.path / "app" / "enemy.py").write_text("hp = 3\n", encoding="utf-8")
    (ws.path / "README.md").unlink()
    # the user, meanwhile: line 12 of the same file (inside the patch's context, so a plain apply fails),
    # another file, a binary and a new file
    (src / "app" / "game.py").write_text(LINES.replace("line 12\n", "line 12 (me)\n"), encoding="utf-8")
    (src / "app" / "main.py").write_text("print('me')\n", encoding="utf-8")
    (src / "logo.png").write_bytes(b"\x89PNG" + b"\x01" * 40)
    (src / "notes.txt").write_text("todo\n", encoding="utf-8")

    applied = ws.apply()

    assert Path(applied).name == "merged.patch"
    game = src.joinpath("app", "game.py").read_text(encoding="utf-8")
    assert game == LINES.replace("line 10\n", "line 10 (ai)\n").replace("line 12\n", "line 12 (me)\n")
    assert src.joinpath("app", "enemy.py").read_text(encoding="utf-8") == "hp = 3\n"
    assert not src.joinpath("README.md").exists()
    assert src.joinpath("app", "main.py").read_text(encoding="utf-8") == "print('me')\n"
    assert src.joinpath("logo.png").read_bytes() == b"\x89PNG" + b"\x01" * 40
    assert src.joinpath("notes.txt").read_text(encoding="utf-8") == "todo\n"
    assert not ws.copy_dir.exists() and ws.patch_path.exists()


def test_copy_mode_apply_when_original_already_has_the_change(tmp_path):
    src = make_project(tmp_path)
    ws = Workspace(tmp_path / "run", src, "copy")
    ws.prepare()
    (ws.path / "README.md").write_text("# same\n", encoding="utf-8")
    (src / "README.md").write_text("# same\n", encoding="utf-8")
    assert Path(ws.apply()).name == "merged.patch"
    assert src.joinpath("README.md").read_text(encoding="utf-8") == "# same\n"


def test_inplace_mode_rollback_restores_and_removes_new_files(tmp_path):
    src = make_project(tmp_path)
    ws = Workspace(tmp_path / "run", src, "inplace")
    ws.prepare()
    assert ws.path == src
    (src / "app" / "main.py").write_text("broken\n", encoding="utf-8")
    (src / "app" / "junk.py").write_text("junk\n", encoding="utf-8")
    (src / "README.md").unlink()

    summary = ws.rollback()
    assert summary["files"] == 3
    assert src.joinpath("app", "main.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert src.joinpath("README.md").exists() and not src.joinpath("app", "junk.py").exists()
    assert src.joinpath("Content", "Hero.uasset").exists()  # excluded content never touched


def test_snapshot_respects_gitignore_unless_opted_in(tmp_path):
    import subprocess

    src = make_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    (src / ".gitignore").write_text("app/\n", encoding="utf-8")
    ws = Workspace(tmp_path / "run", src, "copy")
    ws.prepare()
    assert not (ws.path / "app" / "main.py").exists() and (ws.path / "README.md").exists()
    assert not (src / ".git" / "refs" / "heads" / "master").exists()  # target repo's git untouched

    opted = Workspace(tmp_path / "run2", src, "copy", include_ignored=True)
    opted.prepare()
    assert (opted.path / "app" / "main.py").exists()


def test_interrupted_prepare_is_redone(tmp_path):
    src = make_project(tmp_path)
    ws = Workspace(tmp_path / "run", src, "copy")
    ws.git_dir.mkdir(parents=True)  # a prepare() killed right after `git init`
    assert not ws.prepared
    ws.prepare()
    assert ws.prepared and (ws.path / "app" / "main.py").exists()


def test_unexpected_error_does_not_leave_run_running(tmp_path):
    from relay_agent.history import HistoryStore
    from relay_agent.pipeline import RelayEngine
    from relay_agent.runners import MockRunner
    from relay_agent.usage import UsageStore

    class Boom(MockRunner):
        def run(self, call):
            raise KeyError("boom")

    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text("name: r\nstages:\n  - {name: a, provider: mock, prompt: role.md}\n", encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: Boom())
    run = engine.advance(engine.create(relay, "g", tmp_path).id)
    assert run.status == "failed" and "KeyError" in run.error


def test_patch_keeps_mixed_line_endings(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "mixed.py").write_bytes(b"a = 1\nb = 2\r\nc = 3\n")
    ws = Workspace(tmp_path / "run", src, "copy")
    ws.prepare()
    (ws.path / "mixed.py").write_bytes(b"a = 1\nb = 20\r\nc = 3\n")
    ws.apply()
    assert (src / "mixed.py").read_bytes() == b"a = 1\nb = 20\r\nc = 3\n"
