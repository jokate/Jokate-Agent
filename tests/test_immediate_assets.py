"""Immediate apply by default, no caching of assets, Engine folder hook-only, MCP forces in-place."""
import subprocess
from pathlib import Path

from relay_agent import backup_hook
from relay_agent.history import HistoryStore
from relay_agent.journal import GitVcs, JournalWorkspace
from relay_agent.pipeline import RelayEngine
from relay_agent.runners import MockRunner
from relay_agent.usage import UsageStore

BIG_ASSET = b"UASSET\0" + b"\1" * 300_000


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=repo, check=True, capture_output=True)


def ue_project(tmp_path) -> Path:
    root = tmp_path / "Game"
    for d in ("Source", "Content", "Engine/Source/Runtime"):
        (root / d).mkdir(parents=True)
    (root / "Source" / "Hero.cpp").write_bytes(b"int hp = 100;\n")
    (root / "Content" / "Hero.uasset").write_bytes(BIG_ASSET)
    (root / "Engine" / "Source" / "Runtime" / "Core.cpp").write_bytes(b"// engine\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def test_uncommitted_assets_are_never_cached_and_engine_is_not_scanned(tmp_path):
    root = ue_project(tmp_path)
    (root / "Content" / "Hero.uasset").write_bytes(BIG_ASSET + b"wip")   # asset edited before the run
    (root / "Source" / "Hero.cpp").write_bytes(b"int hp = 110;\n")       # code edited before the run
    ws = JournalWorkspace(tmp_path / "run", root)
    info = ws.prepare()
    assert info["uncommitted_assets_not_cached"] == 1 and info["backup_mb"] < 0.01  # only Hero.cpp copied
    assert not (tmp_path / "run" / "baseline" / "Content").exists()
    assert info["hook_only"] == ["Engine"]

    (root / "Content" / "Hero.uasset").write_bytes(b"MCP saved")               # Unreal MCP save
    (root / "Engine" / "Source" / "Runtime" / "Core.cpp").write_bytes(b"x")    # Bash in Engine: not scanned
    backup_hook.backup(ws.run_dir, root, str(root / "Engine" / "Source" / "Runtime" / "Stats.cpp"))
    (root / "Engine" / "Source" / "Runtime" / "Stats.cpp").write_bytes(b"new engine file\n")  # AI Write in Engine

    summary = ws.collect()
    paths = {b["path"]: b for b in summary["binary"]}
    assert paths["Content/Hero.uasset"]["asset"] and not paths["Content/Hero.uasset"]["restorable"]
    assert "Engine/Source/Runtime/Core.cpp" not in summary["stat"]              # untracked zone, by design
    assert "Engine/Source/Runtime/Stats.cpp" in ws.patch_path.read_text(encoding="utf-8")  # hook-tracked

    result = ws.rollback()
    assert not (root / "Engine" / "Source" / "Runtime" / "Stats.cpp").exists()
    assert (root / "Source" / "Hero.cpp").read_bytes() == b"int hp = 110;\n"
    assert result["not_restored"] == ["Content/Hero.uasset"]


def test_git_lfs_pointer_is_never_written_back_over_an_asset(tmp_path):
    root = tmp_path / "Lfs"
    (root / "Content").mkdir(parents=True)
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 300000\n"
    (root / "Content" / "Map.umap").write_bytes(pointer)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "lfs")
    (root / "Content" / "Map.umap").write_bytes(BIG_ASSET)  # the real (smudged) asset in the working tree
    ws = JournalWorkspace(tmp_path / "run", root, vcs=GitVcs(root, ""))
    ws.prepare()
    (root / "Content" / "Map.umap").write_bytes(b"MCP edit")
    result = ws.rollback()
    assert (root / "Content" / "Map.umap").read_bytes() == b"MCP edit"  # left alone, not replaced by the pointer
    assert result["not_restored"] == ["Content/Map.umap"]


def engine_with(tmp_path, relay_yaml, runner):
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text(relay_yaml, encoding="utf-8")
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: runner)
    return engine, relay


def test_live_mcp_forces_inplace_even_if_copy_was_asked(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.txt").write_text("a", encoding="utf-8")
    engine, relay = engine_with(tmp_path, """
name: r
workspace: copy
stages:
  - {name: build, provider: mock, prompt: role.md, tools: [Edit], mcp: [unreal]}
""", MockRunner())
    run = engine.create(relay, "move actor", project)
    assert run.workspace_mode == "inplace"
    assert "workspace_forced" in [e["kind"] for e in engine.history.events(run.id)]
    read_only = engine.create(relay.with_name("r.yaml"), "g", project, workspace="copy")
    assert read_only.workspace_mode == "inplace"  # explicit copy is still unsafe with a live MCP


def test_copy_mode_auto_applies_when_done(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.txt").write_text("old\n", encoding="utf-8")

    class Editor(MockRunner):
        def run(self, call):
            (call.cwd / "a.txt").write_text("new\n", encoding="utf-8")
            return super().run(call)

    engine, relay = engine_with(tmp_path, """
name: r
workspace: copy
auto_apply: true
stages:
  - {name: build, provider: mock, prompt: role.md, tools: [Edit]}
""", Editor())
    run = engine.advance(engine.create(relay, "g", project).id)
    assert run.changes_status == "applied" and (project / "a.txt").read_text(encoding="utf-8") == "new\n"
    assert "changes_auto_applied" in [e["kind"] for e in engine.history.events(run.id)]


def test_shipped_relays_apply_immediately():
    import yaml

    root = Path(__file__).resolve().parent.parent / "relays"
    for name in ("default", "quick", "quick-fable", "game-cycle", "doc-write", "skill-make", "editor-task"):
        data = yaml.safe_load((root / f"{name}.yaml").read_text(encoding="utf-8"))
        assert data["workspace"] == "inplace" and data["auto_apply"] is True


def test_game_cycle_relay_sends_playtest_failures_back_to_build():
    from relay_agent.pipeline import RelaySpec

    root = Path(__file__).resolve().parent.parent / "relays"
    spec, base = RelaySpec.load(root / "game-cycle.yaml")
    stages = {s.name: s for s in spec.stages}
    assert list(stages) == ["design", "build", "playtest"]
    assert stages["playtest"].on_retry == "build" and not stages["playtest"].writes and stages["build"].writes
    assert all((base / s.prompt).is_file() for s in spec.stages)


def test_document_relays_write_without_bash_and_send_review_back():
    from relay_agent.pipeline import RelaySpec

    root = Path(__file__).resolve().parent.parent / "relays"
    for name, order in (("doc-write", ["outline", "write", "review"]), ("skill-make", ["write", "review"])):
        spec, base = RelaySpec.load(root / f"{name}.yaml")
        stages = {s.name: s for s in spec.stages}
        assert list(stages) == order
        assert stages["write"].writes and not stages["review"].writes and stages["review"].on_retry == "write"
        # documents have no verification command: no Bash, so nothing can stop at "requires approval"
        assert all("Bash" not in (s.tools or []) for s in spec.stages)
        assert all((base / s.prompt).is_file() for s in spec.stages)


def test_an_mcp_picked_for_the_run_forces_inplace_too(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.txt").write_text("a", encoding="utf-8")
    engine, relay = engine_with(tmp_path, """
name: r
workspace: copy
stages:
  - {name: build, provider: mock, prompt: role.md, tools: [Edit], mcp: [digest]}
""", MockRunner())
    assert engine.create(relay, "code only", project).workspace_mode == "copy"  # digest is read-only
    run = engine.create(relay, "move actor", project, mcp=["unreal"])  # the run's pick (dashboard 🔌 / --mcp)
    assert run.workspace_mode == "inplace"
    assert "workspace_forced" in [e["kind"] for e in engine.history.events(run.id)]


def test_build_folder_names_inside_content_are_tracked(tmp_path):
    root = tmp_path / "Game"
    for d in ("Content/UI/Temp", "Content/Library", "Temp", "Saved", "Plugins/Fx/Binaries", "Plugins/Fx/Content/Logs"):
        (root / d).mkdir(parents=True)
        (root / d / "x.uasset").write_bytes(b"A")
    ws = JournalWorkspace(tmp_path / "run", root, vcs=None)
    ws.prepare()
    for d in ("Content/UI/Temp", "Content/Library", "Temp", "Saved", "Plugins/Fx/Binaries", "Plugins/Fx/Content/Logs"):
        (root / d / "x.uasset").write_bytes(b"MCP saved")
    changed = {b["path"] for b in ws.collect()["binary"]}
    assert changed == {"Content/UI/Temp/x.uasset", "Content/Library/x.uasset", "Plugins/Fx/Content/Logs/x.uasset"}


def test_mcp_tool_names_that_change_things():
    from relay_agent.pipeline import mcp_tool_changes

    assert all(mcp_tool_changes(t) for t in ("create_blueprint", "set_actor_property", "SpawnActor", "delete_asset"))
    assert not any(mcp_tool_changes(t) for t in ("get_actors_in_level", "list_assets", "FindActorByName", "search"))


class McpRunner(MockRunner):
    """Calls an editor MCP tool that changes something; `save` writes a project file like an editor save."""

    def __init__(self, save=None):
        super().__init__()
        self.save = save

    def run(self, call):
        if call.stage == "build":
            call.emit("mcp_call", {"server": "unreal", "tool": "get_actors", "target": ""})
            call.emit("mcp_call", {"server": "unreal", "tool": "set_actor_property", "target": ""})
            if self.save:
                self.save()
        return super().run(call)


MCP_RELAY = """
name: r
workspace: inplace
stages:
  - {name: build, provider: mock, prompt: role.md, tools: [Edit]}
  - {name: review, provider: mock, prompt: role.md, tools: [Read]}
"""


def test_mcp_changes_that_were_never_saved_are_flagged_for_the_next_stage(tmp_path):
    root = ue_project(tmp_path)
    runner = McpRunner()
    engine, relay = engine_with(tmp_path, MCP_RELAY, runner)
    run = engine.advance(engine.create(relay, "move actor", root).id)
    unsaved = [e for e in engine.history.events(run.id) if e["kind"] == "mcp_unsaved"]
    assert len(unsaved) == 1 and unsaved[0]["stage"] == "build" and unsaved[0]["detail"]["tools"] == ["set_actor_property"]
    assert "[MCP 저장 확인]" in runner.calls[1].prompt  # the reviewer is told to check the save


def test_mcp_changes_saved_to_disk_are_not_flagged(tmp_path):
    root = ue_project(tmp_path)
    runner = McpRunner(save=lambda: (root / "Content" / "Hero.uasset").write_bytes(BIG_ASSET + b"moved"))
    engine, relay = engine_with(tmp_path, MCP_RELAY, runner)
    run = engine.advance(engine.create(relay, "move actor", root).id)
    assert "mcp_unsaved" not in [e["kind"] for e in engine.history.events(run.id)]
    assert [b["path"] for b in run.changes["binary"]] == ["Content/Hero.uasset"]


def test_a_code_edit_does_not_hide_an_unsaved_mcp_change(tmp_path):
    root = ue_project(tmp_path)

    def edit_code():  # what the Edit tool does: the pre-edit hook records the file, then it is written
        backup_hook.backup(tmp_path / "runs" / run_id[0], root, str(root / "Source" / "Hero.cpp"))
        (root / "Source" / "Hero.cpp").write_bytes(b"int hp = 120;\n")

    run_id = []
    runner = McpRunner(save=edit_code)
    engine, relay = engine_with(tmp_path, MCP_RELAY, runner)
    run_id.append(engine.create(relay, "move actor", root).id)
    run = engine.advance(run_id[0])
    assert "mcp_unsaved" in [e["kind"] for e in engine.history.events(run.id)]


def test_editor_task_relay_edits_only_through_mcp_and_verify_sends_back():
    from relay_agent.pipeline import RelaySpec

    root = Path(__file__).resolve().parent.parent / "relays"
    spec, base = RelaySpec.load(root / "editor-task.yaml")
    stages = {s.name: s for s in spec.stages}
    assert list(stages) == ["edit", "verify"]
    assert stages["edit"].edits_via_mcp and stages["edit"].writes and not stages["verify"].writes
    assert not {"Edit", "Write", "Bash"} & {t for s in spec.stages for t in s.tools}  # changes only via the MCP
    assert stages["verify"].on_retry == "edit" and all((base / s.prompt).is_file() for s in spec.stages)
