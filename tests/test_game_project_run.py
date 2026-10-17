"""A game project's run gets the user's harness skills and the index commands pre-approved; any other run is as it was."""
from relay_agent.history import HistoryStore
from relay_agent.pipeline import RelayEngine
from relay_agent.projctx import INDEX_COMMANDS, game_root, harness_commands, index_allowed
from relay_agent.repos import RepoRegistry
from relay_agent.runners import ClaudeCliRunner
from relay_agent.usage import UsageStore
from test_mcp_attach import Recorder

BASH_RELAY = ("name: r\nworkspace: none\nstages:\n"
              "  - {name: build, provider: mock, prompt: role.md, tools: [Read, Bash], isolate: true}\n")


def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    monkeypatch.setattr("relay_agent.projctx.Path.home", staticmethod(lambda: h))
    return h


def test_game_root_finds_unreal_and_unity_projects_walking_up(tmp_path, monkeypatch):
    home(tmp_path, monkeypatch)
    ue = tmp_path / "UE"
    (ue / "Source" / "Mod").mkdir(parents=True)
    (ue / "My.uproject").write_text("{}", encoding="utf-8")
    unity = tmp_path / "Unity"
    (unity / "ProjectSettings").mkdir(parents=True)
    (unity / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000", encoding="utf-8")
    (unity / "Assets").mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "ProjectSettings").mkdir()  # the folder alone is not a marker
    assert game_root(ue / "Source" / "Mod") == ue.resolve()
    assert game_root(unity / "Assets") == unity.resolve()
    assert game_root(plain) is None and game_root(tmp_path / "missing") is None


def test_index_allowed_has_both_rule_forms_and_cd():
    rules = index_allowed()
    for c in INDEX_COMMANDS:
        assert f"Bash({c})" in rules and f"Bash({c}:*)" in rules
    assert rules[-1] == "Bash(cd:*)" and f"Bash({INDEX_COMMANDS[0]}:*)" in rules


def run_in(tmp_path, monkeypatch, folder):
    home(tmp_path, monkeypatch)
    (tmp_path / "role.md").write_text("r", encoding="utf-8")
    relay = tmp_path / "r.yaml"
    relay.write_text(BASH_RELAY, encoding="utf-8")
    runner = Recorder()
    engine = RelayEngine(tmp_path / "runs", UsageStore(tmp_path / "u.sqlite"), HistoryStore(tmp_path / "h.sqlite"),
                         runner_factory=lambda _: runner, repos=RepoRegistry({"p": {"path": str(folder)}}))
    engine.advance(engine.create(relay, "g", repo="p").id)
    call = runner.seen[-1]
    return call, ClaudeCliRunner(exe="claude").build_args(call, None)


def test_a_game_project_run_gets_skill_and_index_commands_and_a_plain_one_is_as_it_was(tmp_path, monkeypatch):
    game = tmp_path / "Game"
    game.mkdir()
    (game / "Game.uproject").write_text("{}", encoding="utf-8")  # no CLAUDE.md / .claude: no project context
    call, args = run_in(tmp_path, monkeypatch, game)
    assert "Skill" in call.tools and "Bash" in call.tools and "Skill" in call.allowed_tools
    assert f"Bash({INDEX_COMMANDS[0]}:*)" in call.allowed_tools and f"Bash({INDEX_COMMANDS[-1]})" in call.allowed_tools
    assert not call.isolate
    assert "--safe-mode" not in args and "--disable-slash-commands" not in args

    # the common case: instructions name a command (ctx.allowed_bash() is not empty) but there are no .claude/skills
    doc = tmp_path / "Doc"
    doc.mkdir()
    (doc / "Doc.uproject").write_text("{}", encoding="utf-8")
    (doc / "CLAUDE.md").write_text("조회: `python Tools/x.py --q`", encoding="utf-8")
    call, args = run_in(tmp_path, monkeypatch, doc)
    assert "Skill" in call.tools and "Bash" in call.tools and "Skill" in call.allowed_tools
    assert f"Bash({INDEX_COMMANDS[0]}:*)" in call.allowed_tools and "Bash(python Tools/x.py:*)" in call.allowed_tools
    assert "--safe-mode" not in args and "--disable-slash-commands" not in args

    plain = tmp_path / "Plain"
    plain.mkdir()
    (plain / "a.txt").write_text("x", encoding="utf-8")
    call, args = run_in(tmp_path, monkeypatch, plain)
    assert "Skill" not in call.tools and "Skill" not in call.allowed_tools
    assert not any(a.startswith("Bash(python Tools") for a in call.allowed_tools)
    assert call.isolate and "--safe-mode" in args


def fake_harness(tmp_path):
    h = tmp_path / "harness"
    (h / "skills" / "a").mkdir(parents=True)
    (h / "skills" / "b").mkdir(parents=True)
    (h / "skills" / "a" / "SKILL.md").write_text("조회: `python Tools/real_idx.py --sym X`", encoding="utf-8")
    (h / "skills" / "b" / "SKILL.md").write_text(f"`{INDEX_COMMANDS[0]}` 와 `python Tools/real_idx.py`", encoding="utf-8")
    return h


def test_index_allowed_adds_the_commands_the_harness_names(tmp_path):
    h = fake_harness(tmp_path)
    rules = index_allowed(h)
    assert "Bash(python Tools/real_idx.py:*)" in rules and "Bash(python Tools/real_idx.py)" in rules
    assert len(rules) == len(set(rules)) and rules[-1] == "Bash(cd:*)"
    assert harness_commands(h) == ["python Tools/real_idx.py", INDEX_COMMANDS[0]]
    assert index_allowed(None) == index_allowed()
    assert index_allowed(tmp_path / "missing") == index_allowed() and harness_commands(tmp_path / "missing") == []


def test_a_game_run_pre_approves_the_commands_of_the_resolved_harness(tmp_path, monkeypatch):
    h = fake_harness(tmp_path)
    monkeypatch.setattr("relay_agent.pipeline.resolve_root", lambda setting, home=None: h)
    game = tmp_path / "Game"
    game.mkdir()
    (game / "Game.uproject").write_text("{}", encoding="utf-8")
    call, _ = run_in(tmp_path, monkeypatch, game)
    assert "Bash(python Tools/real_idx.py:*)" in call.allowed_tools
    plain = tmp_path / "Plain"
    plain.mkdir()
    (plain / "a.txt").write_text("x", encoding="utf-8")
    call, _ = run_in(tmp_path, monkeypatch, plain)
    assert "Bash(python Tools/real_idx.py:*)" not in call.allowed_tools
