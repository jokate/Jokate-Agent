"""In-place tracking without snapshots: journal + pre-edit backups + VCS pristine (git / SVN / none)."""
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from relay_agent import backup_hook
from relay_agent.history import HistoryStore
from relay_agent.journal import JournalWorkspace, NoVcs, SvnVcs
from relay_agent.pipeline import RelayEngine
from relay_agent.runners import ClaudeCliRunner, MockRunner, StageCall
from relay_agent.usage import UsageStore
from relay_agent.workspace import dir_size

ASSET = b"UASSET\0" + bytes(range(256)) * 400  # ~100KB binary


def game_files(root: Path) -> None:
    (root / "Source").mkdir(parents=True)
    (root / "Content" / "Maps").mkdir(parents=True)
    (root / "Intermediate").mkdir()
    (root / "Source" / "Hero.cpp").write_bytes(b"int hp = 100;\n")
    (root / "Source" / "Old.cpp").write_bytes(b"// legacy\n")
    (root / "Config.ini").write_bytes(b"[game]\nspeed=1\n")
    (root / "Content" / "Maps" / "Arena.umap").write_bytes(ASSET)
    (root / "Intermediate" / "cache.bin").write_bytes(b"x" * 1000)


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=repo, check=True, capture_output=True)


def hook_edit(ws: JournalWorkspace, rel: str, data: bytes | None):
    """What Claude Code does: run the PreToolUse hook, then the edit."""
    backup_hook.backup(ws.run_dir, ws.source, str(ws.source / rel))
    target = ws.source / rel
    if data is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def test_git_project_assets_and_code_round_trip_without_snapshot(tmp_path):
    repo = tmp_path / "Game"
    game_files(repo)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    (repo / "Config.ini").write_bytes(b"[game]\nspeed=2\n")  # uncommitted before the run
    git_size, index_before = dir_size(repo / ".git"), (repo / ".git" / "index").read_bytes()

    ws = JournalWorkspace(tmp_path / "run", repo)
    info = ws.prepare()
    assert info["vcs"] == "git" and info["uncommitted_at_start"] == 1
    assert info["backup_mb"] < 0.01  # only Config.ini was copied — not the project

    hook_edit(ws, "Source/Hero.cpp", b"int hp = 120;\n")          # AI edit tool (hook backs up)
    (repo / "Content" / "Maps" / "Arena.umap").write_bytes(ASSET + b"moved actor")  # Unreal editor via MCP (no hook)
    (repo / "Source" / "Old.cpp").unlink()                        # Bash rm (no hook)
    (repo / "Config.ini").write_bytes(b"[game]\nspeed=3\n")       # Bash edit of an uncommitted file
    hook_edit(ws, "Source/New.cpp", b"// new\n")
    (repo / "Intermediate" / "cache.bin").write_bytes(b"y")       # build noise: not tracked

    summary = ws.collect()
    patch = ws.patch_path.read_text(encoding="utf-8")
    assert "+int hp = 120;" in patch and "-speed=2" in patch and "diff --git a/Source/New.cpp" in patch
    assert "deleted file mode" in patch
    assets = {b["path"]: b for b in summary["binary"]}
    assert assets["Content/Maps/Arena.umap"]["restorable"] and assets["Content/Maps/Arena.umap"]["source"] == "pristine"
    assert not any("Intermediate" in line for line in summary["stat"].splitlines())

    result = ws.rollback()
    assert result["not_restored"] == []
    assert (repo / "Source" / "Hero.cpp").read_bytes() == b"int hp = 100;\n"
    assert (repo / "Content" / "Maps" / "Arena.umap").read_bytes() == ASSET  # asset restored from git
    assert (repo / "Source" / "Old.cpp").exists() and not (repo / "Source" / "New.cpp").exists()
    assert (repo / "Config.ini").read_bytes() == b"[game]\nspeed=2\n"  # user's uncommitted edit kept
    assert (repo / ".git" / "index").read_bytes() == index_before and dir_size(repo / ".git") == git_size


def make_svn_wc(root: Path) -> None:
    """A working copy in SVN 1.8+ format: wc.db NODES/PRISTINE + .svn/pristine/<xx>/<sha1>.svn-base."""
    svn = root / ".svn"
    (svn / "pristine").mkdir(parents=True)
    conn = sqlite3.connect(svn / "wc.db")
    conn.execute("CREATE TABLE nodes (wc_id INTEGER, local_relpath TEXT, op_depth INTEGER, presence TEXT, kind TEXT, "
                 "checksum TEXT, translated_size INTEGER, last_mod_time INTEGER)")
    conn.execute("CREATE TABLE pristine (checksum TEXT, compression INTEGER, size INTEGER)")
    for path in root.rglob("*"):
        if path.is_file() and ".svn" not in path.parts:
            data = path.read_bytes()
            digest = hashlib.sha1(data).hexdigest()
            (svn / "pristine" / digest[:2]).mkdir(exist_ok=True)
            (svn / "pristine" / digest[:2] / f"{digest}.svn-base").write_bytes(data)
            st = path.stat()
            rel = path.relative_to(root).as_posix()
            conn.execute("INSERT INTO nodes VALUES (1, ?, 0, 'normal', 'file', ?, ?, ?)",
                         (rel, f"$sha1${digest}", st.st_size, st.st_mtime_ns // 1000))
            conn.execute("INSERT INTO pristine VALUES (?, NULL, ?)", (f"$sha1${digest}", len(data)))
    conn.commit()
    conn.close()


def test_svn_working_copy_uses_pristine_store_without_svn_cli(tmp_path):
    wc = tmp_path / "SvnGame"
    game_files(wc)
    make_svn_wc(wc)
    (wc / "Source" / "Draft.cpp").write_bytes(b"// unversioned wip\n")

    ws = JournalWorkspace(tmp_path / "run", wc)
    info = ws.prepare()
    assert info["vcs"] == "svn" and info["uncommitted_at_start"] == 1  # Draft.cpp only (sha1 check keeps others clean)

    (wc / "Content" / "Maps" / "Arena.umap").write_bytes(b"broken")   # editor save, no hook
    (wc / "Source" / "Hero.cpp").write_bytes(b"int hp = 1;\n")        # Bash edit, no hook
    (wc / "Source" / "Draft.cpp").write_bytes(b"// changed wip\n")    # unversioned: backed up at start

    summary = ws.collect()
    assert summary["unrestorable"] == []
    result = ws.rollback()
    assert (wc / "Content" / "Maps" / "Arena.umap").read_bytes() == ASSET
    assert (wc / "Source" / "Hero.cpp").read_bytes() == b"int hp = 100;\n"
    assert (wc / "Source" / "Draft.cpp").read_bytes() == b"// unversioned wip\n"
    assert result["not_restored"] == []


def test_svn_adapter_reads_subfolder_and_compressed_pristines_are_skipped(tmp_path):
    wc = tmp_path / "wc"
    game_files(wc)
    make_svn_wc(wc)
    vcs = SvnVcs(wc, "Source/")
    assert vcs.pristine("Hero.cpp") == b"int hp = 100;\n"
    checksum = vcs.info["Hero.cpp"][0]
    vcs.compression[checksum] = 1
    assert vcs.pristine("Hero.cpp") is None


def test_no_vcs_restores_only_what_the_hook_backed_up(tmp_path):
    root = tmp_path / "Plain"
    game_files(root)
    ws = JournalWorkspace(tmp_path / "run", root, vcs=NoVcs())
    ws.prepare()
    hook_edit(ws, "Source/Hero.cpp", b"int hp = 5;\n")
    (root / "Config.ini").write_bytes(b"changed by bash")
    summary = ws.collect()
    assert summary["unrestorable"] == ["Config.ini"]
    result = ws.rollback()
    assert (root / "Source" / "Hero.cpp").read_bytes() == b"int hp = 100;\n"
    assert result["not_restored"] == ["Config.ini"]


def test_backup_hook_script_contract(tmp_path, monkeypatch):
    root = tmp_path / "p"
    root.mkdir()
    (root / "a.txt").write_bytes(b"orig")
    run = tmp_path / "run"
    run.mkdir()
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(root / "a.txt")}}
    for _ in range(2):  # second call must not overwrite the first backup
        monkeypatch.setattr(sys, "argv", ["hook", str(run), str(root)])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        (root / "a.txt").write_bytes(b"edited")  if _ else None
        try:
            backup_hook.main()
        except SystemExit as e:
            assert e.code == 0
    assert (run / "baseline" / "a.txt").read_bytes() == b"orig"
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    try:
        backup_hook.main()
    except SystemExit as e:
        assert e.code == 0  # never blocks the edit


def test_engine_passes_hook_settings_and_tracks_inplace(tmp_path):
    root = tmp_path / "Game"
    game_files(root)
    seen = {}

    class Editor(MockRunner):
        def run(self, call):
            seen["settings"] = call.settings_path
            (call.cwd / "Content" / "Maps" / "Arena.umap").write_bytes(b"edited asset")
            return super().run(call)

    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text("name: r\nworkspace: inplace\nstages:\n  - {name: build, provider: mock, prompt: role.md, tools: [Edit]}\n",
                     encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: Editor())
    run = engine.advance(engine.create(relay, "move actor", root).id)
    assert seen["settings"] is not None and "PreToolUse" in Path(seen["settings"]).read_text(encoding="utf-8")
    assert run.changes["binary"][0]["path"] == "Content/Maps/Arena.umap"
    assert not (engine.runs_dir / "shadow").exists()

    args = ClaudeCliRunner(exe="claude").build_args(
        StageCall(stage="s", model="sonnet", effort=None, system="", prompt="", cwd=root, isolate=True,
                  settings_path=Path(seen["settings"])), None)
    assert "--settings" in args and "--safe-mode" not in args  # safe-mode would disable the hook
