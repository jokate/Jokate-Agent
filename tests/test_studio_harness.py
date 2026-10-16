"""The harness and the registration of a new UE project (M-5): the two command lines and their order, the
repository as the server sees it after a reload, `.git` and the two git files, the `projects` row, and what is
refused before anything is made (§6.4, §3.3, §11.1). game-harness and MNYS are not read: the harness is a folder
made here with two batch files of a few lines, and the settings are files of the test's own — the ones of this
repository are never written."""
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from relay_agent import journal
from relay_agent.config import ROOT, Config
from relay_agent.repos import RepoRegistry, save_local_repo
from relay_agent.studio import creator, harness, importer, plugins, review
from relay_agent.studio.store import StudioStore

DESIGN = Path(__file__).resolve().parent.parent / "design" / "studio"
BOOTSTRAP_BAT = ("@echo off\r\n"
                 "echo bootstrap %*\r\n"
                 "echo cwd %CD%\r\n"
                 'echo rules> "%~1\\CLAUDE.local.md"\r\n'
                 "exit /b 0\r\n")
INDEX_BAT = ("@echo off\r\n"
             "echo index %*\r\n"
             'if not exist "%~1\\CLAUDE.local.md" exit /b 9\r\n'
             'echo made> "%~1\\index.db"\r\n'
             "exit /b 0\r\n")


def project(tmp_path) -> creator.Spec:
    """A project as the `folder` step leaves it (see tests/test_studio_plugins.py)."""
    parent = tmp_path / "Projects"
    parent.mkdir()
    spec = creator.Spec("Sample", "5.7", tmp_path / "UE_5.7", parent)
    assert creator.folder(spec).ok
    return spec


def game_harness(tmp_path, *, bootstrap=BOOTSTRAP_BAT, index=INDEX_BAT, name="game-harness") -> Path:
    """The folder `studio_harness_root` names, with the two batch files (one left out when None)."""
    home = tmp_path / name
    home.mkdir()
    for name, text in ((harness.BOOTSTRAP, bootstrap), (harness.INDEX_BUILD, index)):
        if text is not None:
            (home / name).write_bytes(text.encode("ascii"))
    return home


class Runner:
    """A `run` that starts nothing: it keeps what it was asked, does what the test gives it for that call and
    answers with the code given for it (0 when none is)."""

    def __init__(self, *does, codes=()):
        self.calls, self.does, self.codes = [], list(does), list(codes)

    def __call__(self, argv, *, cwd, log):
        index = len(self.calls)
        self.calls.append((list(argv), Path(cwd)))
        log(f"ran {index}")
        if index < len(self.does) and self.does[index] is not None:
            self.does[index]()
        return self.codes[index] if index < len(self.codes) else 0


class Server:
    """What the server keeps of its repositories, replaced as `server._reload_repos` replaces it — read from a
    settings file of the test's own."""

    def __init__(self, tmp_path):
        self.config, self.local = tmp_path / "relay.config.yaml", tmp_path / "relay.config.local.yaml"
        self.repos = RepoRegistry(Config.load(self.config).repos)

    def reload(self) -> RepoRegistry:
        self.repos = RepoRegistry(Config.load(self.config).repos)
        return self.repos


@pytest.fixture
def made(tmp_path):
    """A project, a server and a store. The settings of this repository are the same after the test."""
    global_file = ROOT / "relay.config.local.yaml"
    before = global_file.read_bytes() if global_file.exists() else None
    store = StudioStore(tmp_path / "studio.sqlite")
    yield project(tmp_path), Server(tmp_path), store
    store.close()
    assert (global_file.read_bytes() if global_file.exists() else None) == before


def fake_git(spec) -> Runner:
    return Runner(lambda: (spec.root / ".git").mkdir())


def untouched(spec) -> bool:
    return not any((spec.root / name).exists() for name in (".git", ".gitignore", ".gitattributes"))


def git(folder, *args) -> str:
    """As tests/test_studio_decide.py has it: git in that folder, which has to end with 0, and what it said."""
    done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return done.stdout.decode("utf-8", "replace").strip()


def events(store) -> list[tuple]:
    rows = store._rows("SELECT type, project_id FROM stream_events WHERE type = 'project.updated'")
    return [(row["type"], row["project_id"]) for row in rows]


# --- the setting and the shape of a step --------------------------------------------------------------------
def test_the_harness_folder_is_a_setting_read_from_the_local_file(tmp_path):
    assert Config().studio_harness_root == ""
    config = tmp_path / "relay.config.yaml"
    assert Config.load(config).studio_harness_root == ""
    config.with_name("relay.config.local.yaml").write_text(
        yaml.safe_dump({"studio_harness_root": "D:/game-harness"}), encoding="utf-8")
    assert Config.load(config).studio_harness_root == "D:/game-harness"


def test_a_step_is_not_skipped_unless_it_says_so():
    assert creator.StepResult("build").skipped is False and plugins.PluginsResult("plugins").skipped is False
    result = harness.RegisterResult("register")
    assert result.ok and not result.skipped and result.repo == "" and result.project is None


# --- the harness --------------------------------------------------------------------------------------------
def test_the_two_commands_are_the_batch_files_with_the_project_folder(tmp_path):
    spec, home = project(tmp_path), game_harness(tmp_path)
    assert harness.commands(spec, home) == [[str(home / "bootstrap.bat"), str(spec.root)],
                                            [str(home / "index_build.bat"), str(spec.root)]]
    assert harness.locate(f"  {home}  ") == home


def test_harness_runs_bootstrap_then_the_index_in_the_harness_folder(tmp_path):
    spec, home, lines = project(tmp_path), game_harness(tmp_path), []
    run = Runner(lambda: (spec.root / "CLAUDE.local.md").write_text("rules", encoding="utf-8"),
                 lambda: (spec.root / "index.db").write_text("made", encoding="utf-8"))
    result = harness.harness(spec, str(home), run=run, log=lines.append)
    first, second = harness.commands(spec, home)
    assert result.ok and not result.skipped and result.step == "harness"
    assert run.calls == [(first, home), (second, home)]
    assert result.created == sorted([str(spec.root / "CLAUDE.local.md"), str(spec.root / "index.db")])
    assert lines[:4] == ["> " + creator.command_line(first), "ran 0", "> " + creator.command_line(second), "ran 1"]
    assert "CLAUDE.local.md" in lines[4] and result.tail == lines


def test_the_index_is_not_built_when_bootstrap_fails(tmp_path):
    spec, home = project(tmp_path), game_harness(tmp_path)
    run = Runner(lambda: (spec.root / "Half").mkdir(), codes=[3])
    result = harness.harness(spec, home, run=run)
    assert not result.ok and "하네스 설치" in result.error and "exit 3" in result.error and len(run.calls) == 1
    assert result.created == [str(spec.root / "Half")] and result.tail[-1] == result.error

    def refuse():
        raise OSError("not a program")

    unstarted = harness.harness(spec, home, run=Runner(refuse))
    assert not unstarted.ok and "시작하지 못했다" in unstarted.error
    late = Runner(lambda: (spec.root / "CLAUDE.md").write_text("rules", encoding="utf-8"), codes=[0, 5])
    second = harness.harness(spec, home, run=late)
    assert not second.ok and "인덱스 빌드" in second.error and "exit 5" in second.error and len(late.calls) == 2


def test_a_bootstrap_that_leaves_no_instruction_file_fails(tmp_path):
    spec, home = project(tmp_path), game_harness(tmp_path)
    run = Runner()
    result = harness.harness(spec, home, run=run)
    assert not result.ok and "규칙 파일" in result.error and "CLAUDE.local.md" in result.error
    assert len(run.calls) == 2 and result.created == []
    for name in (".claude/CLAUDE.md", "CLAUDE.md"):  # any of the files a relay run reads will do
        file = spec.root.joinpath(*name.split("/"))
        file.parent.mkdir(exist_ok=True)
        file.write_text("rules", encoding="utf-8")
        assert harness.harness(spec, home, run=Runner()).ok and harness.rules(spec.root)[-1] == name
        file.unlink()


def test_a_harness_turned_off_is_skipped_and_runs_nothing(tmp_path):
    spec, run, lines = project(tmp_path), Runner(), []
    result = harness.harness(spec, "", enabled=False, run=run, log=lines.append)
    assert result.ok and result.skipped and run.calls == [] and result.created == []
    assert len(lines) == 1 and result.tail == lines


def test_a_harness_folder_that_is_not_there_fails_before_anything_is_run(tmp_path):
    spec, run = project(tmp_path), Runner()
    for root, word in (("", "studio_harness_root"), (None, "studio_harness_root"),
                       ("game-harness", "절대 경로"), (tmp_path / "nowhere", "nowhere")):
        result = harness.harness(spec, root, run=run)
        assert not result.ok and word in result.error and not result.skipped, root
        assert result.created == [] and result.tail == [result.error]
    for missing, gone in (("bootstrap", harness.BOOTSTRAP), ("index", harness.INDEX_BUILD)):
        (tmp_path / missing).mkdir()
        home = game_harness(tmp_path / missing, **{missing: None})
        result = harness.harness(spec, home, run=run)
        assert not result.ok and str(home / gone) in result.error and result.created == []
    home = game_harness(tmp_path)
    absent = creator.Spec("Absent", "5.7", spec.engine_dir, spec.parent_dir)
    result = harness.harness(absent, home, run=run)
    assert not result.ok and str(absent.root) in result.error
    assert run.calls == []


@pytest.mark.skipif(os.name != "nt", reason="the harness is two Windows batch files")
def test_the_two_batch_files_are_really_run_in_order(tmp_path):
    spec, home, lines = project(tmp_path), game_harness(tmp_path), []
    result = harness.harness(spec, home, log=lines.append)
    assert result.ok, result.error
    said = [line for line in lines if line.startswith(("bootstrap ", "index "))]
    assert [line.split(" ")[0] for line in said] == ["bootstrap", "index"]
    assert all(str(spec.root) in line for line in said)
    cwd = next(line[4:] for line in lines if line.startswith("cwd "))
    assert os.path.samefile(cwd, home)
    assert (spec.root / "CLAUDE.local.md").is_file() and (spec.root / "index.db").is_file()
    assert result.created == sorted([str(spec.root / "CLAUDE.local.md"), str(spec.root / "index.db")])
    (home / harness.INDEX_BUILD).write_bytes(b"@echo off\r\necho no index\r\nexit /b 4\r\n")
    failed = harness.harness(spec, home, log=lines.append)
    assert not failed.ok and "인덱스 빌드" in failed.error and "exit 4" in failed.error
    assert "no index" in failed.tail


@pytest.mark.skipif(os.name != "nt", reason="the harness is two Windows batch files")
def test_a_harness_folder_with_a_space_in_its_path_is_really_run(tmp_path):
    spec, home, lines = project(tmp_path), game_harness(tmp_path, name="game harness"), []
    assert " " in str(home) and " " not in str(spec.root) and harness.locate(home) == home
    result = harness.harness(spec, home, log=lines.append)  # the batch file itself is argv[0]: no `cmd /c`
    assert result.ok, (result.error, lines)
    said = [line for line in lines if line.startswith(("bootstrap ", "index "))]
    assert [line.split(" ")[0] for line in said] == ["bootstrap", "index"]
    assert all(str(spec.root) in line for line in said)
    cwd = next(line[4:] for line in lines if line.startswith("cwd "))
    assert os.path.samefile(cwd, home)
    assert (spec.root / "CLAUDE.local.md").is_file() and (spec.root / "index.db").is_file()
    # what the step passed, the importer sees as the rules of the harness too (`importer.rules`)
    assert importer.detect(spec.root, [])["harness"]["claude_md"] is True


# --- the registration ---------------------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_register_makes_the_repository_git_and_the_row(made):
    (spec, server, store), lines = made, []
    plugins.write_ini_port(spec.root, 55560)
    held, root = server.repos, spec.root.resolve()
    result = harness.register(spec, store, local_file=server.local, reload=server.reload, log=lines.append)
    assert result.ok, result.error
    assert result.step == "register" and result.repo == "Sample" and result.tail == lines[-creator.TAIL_LINES:]
    # the repository: in the registry the server holds now, without a restart (§11.1)
    assert "Sample" not in held.repos and server.repos is not held
    repo = server.repos.get("Sample")
    assert (repo.workspace, repo.docs, repo.mcp) == ("inplace", "Docs", ["unreal"])
    assert repo.resolved == root and server.repos.match(spec.uproject).name == "Sample"
    assert yaml.safe_load(server.local.read_text(encoding="utf-8")) == {"repos": {"Sample": {
        "path": root.as_posix(), "workspace": "inplace", "docs": "Docs", "mcp": ["unreal"]}}}
    assert not server.config.exists()
    # git: really made, and the two files as the defaults are
    assert (root / ".git" / "HEAD").is_file() and "> git init" in lines
    for name, default in harness.GIT_DEFAULTS:
        assert (root / name).read_bytes() == default.read_bytes()
    assert result.created == [str(root / ".git"), str(root / ".gitignore"), str(root / ".gitattributes")]
    # the row
    row = result.project
    assert store.list_projects() == [row] and events(store) == [("project.updated", row["id"])]
    assert (row["name"], row["path"], row["repo"], row["mcp_port"], row["engine_version"]) == (
        "Sample", str(root), "Sample", 55560, "5.7")
    assert importer.detect(root, [], top=root)["git"] is True


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_registered_project_has_no_commit_until_the_first_pass_makes_it(made, monkeypatch):
    spec, server, store = made
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.delenv(name, raising=False)
    plugins.write_ini_port(spec.root, 55563)
    root = spec.root.resolve()
    result = harness.register(spec, store, local_file=server.local, reload=server.reload)
    assert result.ok, result.error
    assert isinstance(journal.detect_vcs(root), journal.NoVcs)  # a git repository without HEAD: its first run
    (root / ".mcp.json").write_text("{}", encoding="utf-8")
    (root / "Saved").mkdir(exist_ok=True)
    (root / "Saved" / "x.log").write_text("log\n", encoding="utf-8")
    git(root, "config", "user.name", "")  # git refuses a commit without a name
    with pytest.raises(review.CommitError):
        review.commit(root, "첫 커밋")
    assert git(root, "ls-files") == ""  # nothing of the pass that failed stays staged
    for name, default in harness.GIT_DEFAULTS:
        assert (root / name).read_bytes() == default.read_bytes()
    for key, value in (("user.name", "owner"), ("user.email", "owner@example.invalid"),
                       ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
        git(root, "config", key, value)
    sha = review.commit(root, "첫 커밋")
    assert sha and sha == git(root, "rev-parse", "HEAD") and git(root, "rev-list", "--count", "--all") == "1"
    tracked = git(root, "ls-files").splitlines()
    assert {".gitignore", ".gitattributes", "Sample.uproject"} <= set(tracked)
    assert ".mcp.json" not in tracked and not any(path.startswith("Saved/") for path in tracked)  # ignored
    assert isinstance(journal.detect_vcs(root), journal.GitVcs)


def test_register_asks_for_git_init_in_the_project_folder(made):
    spec, server, store = made
    plugins.write_ini_port(spec.root, 55561)
    run = fake_git(spec)
    result = harness.register(spec, store, local_file=server.local, reload=server.reload, mcp=("unreal_two",),
                              run=run)
    assert result.ok, result.error
    assert run.calls == [(["git", "init"], spec.root.resolve())]
    assert server.repos.get("Sample").mcp == ["unreal_two"] and result.project["mcp_port"] == 55561


def test_a_project_without_an_mcp_port_is_registered_without_the_mcp_server(made):
    spec, server, store = made
    result = harness.register(spec, store, local_file=server.local, reload=server.reload, run=fake_git(spec))
    assert result.ok, result.error
    repo = server.repos.get("Sample")
    assert (repo.workspace, repo.docs, repo.mcp) == ("inplace", "Docs", [])
    assert "mcp" not in yaml.safe_load(server.local.read_text(encoding="utf-8"))["repos"]["Sample"]
    assert result.project["mcp_port"] is None and any("mcp 를 넣지 않는다" in line for line in result.tail)


def test_a_git_repository_and_files_that_are_there_are_left_as_they_are(made):
    spec, server, store = made
    (spec.root / ".git").mkdir()
    (spec.root / ".gitignore").write_bytes(b"mine\n")
    run = Runner()
    result = harness.register(spec, store, local_file=server.local, reload=server.reload, run=run)
    assert result.ok and run.calls == [] and (spec.root / ".gitignore").read_bytes() == b"mine\n"
    assert result.created == [str(spec.root.resolve() / ".gitattributes")]


def test_a_repository_of_that_name_or_folder_is_not_written_over(made, tmp_path):
    spec, server, store = made
    other, run = tmp_path / "Elsewhere", Runner()
    other.mkdir()
    save_local_repo(server.local, "Sample", {"path": other.as_posix(), "workspace": "copy"})
    result = harness.register(spec, store, local_file=server.local, reload=server.reload, run=run)
    assert not result.ok and "'Sample'" in result.error and result.repo == "" and result.project is None
    assert server.reload().get("Sample").workspace == "copy" and server.repos.get("Sample").resolved == other.resolve()
    save_local_repo(server.local, "Sample", None, remove=True)
    save_local_repo(server.local, "Mine", {"path": spec.root.as_posix()})
    result = harness.register(spec, store, local_file=server.local, reload=server.reload, run=run)
    assert not result.ok and "'Mine'" in result.error and result.repo == ""
    assert "Sample" not in server.reload().repos
    assert run.calls == [] and untouched(spec) and result.created == [] and store.list_projects() == []


def test_a_project_that_clashes_is_refused_before_anything_is_made(made):
    spec, server, store = made
    run = Runner()
    plugins.write_ini_port(spec.root, 55562)
    store.create_project("Other", "X:/Games/Other", "other", mcp_port=55562)
    result = harness.register(spec, store, local_file=server.local, reload=server.reload, run=run)
    assert not result.ok and "55562" in result.error and "'Other'" in result.error
    # each row that follows clashes on a field that comes before the last one's: repo, then name, then path
    for (name, path, repo), word in ((("Two", "X:/Games/Two", "Sample"), "저장소 'Sample'"),
                                     (("Sample", "X:/Games/Three", "three"), "이름 'Sample'"),
                                     (("Four", spec.root.as_posix() + "/", "four"), "이미 프로젝트로 등록")):
        store.create_project(name, path, repo)
        result = harness.register(spec, store, local_file=server.local, reload=server.reload, run=run)
        assert not result.ok and word in result.error and result.repo == "" and result.project is None, name
    assert run.calls == [] and untouched(spec) and not server.local.exists()
    assert len(store.list_projects()) == 4 and events(store) == []


def test_a_folder_without_a_uproject_is_not_registered(made):
    spec, server, store = made
    spec.uproject.unlink()
    result = harness.register(spec, store, local_file=server.local, reload=server.reload, run=Runner())
    assert not result.ok and ".uproject" in result.error and not server.local.exists() and untouched(spec)


def test_a_git_that_fails_stops_before_the_row_and_the_step_can_be_run_again(made):
    spec, server, store = made
    run = Runner(codes=[128])
    result = harness.register(spec, store, local_file=server.local, reload=server.reload, run=run)
    assert not result.ok and "git init" in result.error and "exit 128" in result.error
    assert result.repo == "Sample" and "Sample" in server.repos.repos  # saved: what a cleanup takes out
    assert result.project is None and store.list_projects() == [] and result.created == [] and untouched(spec)

    def refuse():
        raise OSError("git is not here")

    unstarted = harness.register(spec, store, local_file=server.local, reload=server.reload, run=Runner(refuse))
    assert not unstarted.ok and "시작하지 못했다" in unstarted.error and unstarted.repo == "Sample"
    nothing = harness.register(spec, store, local_file=server.local, reload=server.reload, run=Runner())
    assert not nothing.ok and ".git 이 없다" in nothing.error
    again = harness.register(spec, store, local_file=server.local, reload=server.reload, run=fake_git(spec))
    assert again.ok and store.list_projects() == [again.project] and any("있던 항목" in line for line in again.tail)
    twice = harness.register(spec, store, local_file=server.local, reload=server.reload, run=Runner())
    assert not twice.ok and "이미 프로젝트로 등록" in twice.error and len(store.list_projects()) == 1


def test_a_reload_that_does_not_show_the_repository_fails_the_step(made):
    spec, server, store = made
    run = Runner()
    result = harness.register(spec, store, local_file=server.local, reload=lambda: RepoRegistry({}), run=run)
    assert not result.ok and "RepoRegistry" in result.error and result.repo == "Sample"
    assert run.calls == [] and untouched(spec) and store.list_projects() == []


def test_a_reload_that_raises_or_gives_no_registry_fails_the_step_instead_of_raising(made):
    spec, server, store = made
    run = Runner()

    def breaks(at):
        """The server's reload, whose call number `at` raises what a wrong entry of the settings does."""
        calls = []

        def reload():
            calls.append(at)
            if len(calls) == at:
                raise TypeError("'int' object is not iterable")
            return server.reload()
        return reload

    first = harness.register(spec, store, local_file=server.local, reload=breaks(1), run=run)
    assert not first.ok and "TypeError" in first.error and "not iterable" in first.error
    assert first.repo == "" and not server.local.exists() and first.tail[-1] == first.error
    none = harness.register(spec, store, local_file=server.local, reload=lambda: None, run=run)
    assert not none.ok and "RepoRegistry" in none.error and "NoneType" in none.error  # server._reload_repos
    assert none.repo == "" and not server.local.exists()
    second = harness.register(spec, store, local_file=server.local, reload=breaks(2), run=run)
    assert not second.ok and "TypeError" in second.error
    assert second.repo == "Sample" and server.local.is_file()  # saved before the reload: a cleanup takes it out
    assert run.calls == [] and untouched(spec) and store.list_projects() == [] and events(store) == []


# --- what the importer and the screen of projects share -----------------------------------------------------
def test_conflict_names_the_first_of_path_name_and_repo():
    rows = [{"path": "C:/Games/Sample/", "name": "Sample", "repo": "sample", "archived_at": None}]
    assert importer.conflict(rows, name="Sample", folder="c:\\games\\sample", repo="sample")[0] == "path"
    assert importer.conflict(rows, name="Sample", folder="C:/Games/Other", repo="sample")[0] == "name"
    field, detail = importer.conflict(rows, name="Other", folder="C:/Games/Other", repo="sample")
    assert field == "repo" and "'sample'" in detail and "보관된" not in detail
    assert importer.conflict(rows, name="Other", folder="C:/Games/Other", repo="other") is None
    rows[0]["archived_at"] = "2026-10-12T00:00:00Z"
    assert importer.conflict(rows, name="Sample", folder="C:/Games/Other", repo="x")[1].endswith(
        "(보관된 프로젝트입니다)")


# --- the defaults and the documents -------------------------------------------------------------------------
def test_the_git_defaults_are_package_data_and_no_rule_of_this_repository():
    names = [name for name, _ in harness.GIT_DEFAULTS]
    assert names == [".gitignore", ".gitattributes"]
    root = Path(harness.__file__).resolve().parents[2]
    tool = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    package = harness.DEFAULTS.parent
    listed = {p for pattern in tool["package-data"]["relay_agent.studio"] for p in package.glob(pattern)}
    for _, default in harness.GIT_DEFAULTS:
        assert default in listed and not default.name.startswith(".") and default.parent == harness.DEFAULTS
    ignore = harness.GIT_DEFAULTS[0][1].read_text(encoding="utf-8").splitlines()
    for line in ("Binaries/", "Intermediate/", "Saved/", "DerivedDataCache/", ".mcp.json"):
        assert line in ignore, line
    attributes = harness.GIT_DEFAULTS[1][1].read_text(encoding="utf-8")
    assert "*.uasset binary" in attributes.splitlines() and "filter=lfs" not in attributes


def test_the_documents_say_what_the_code_does():
    architecture = (DESIGN / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for word in ("`relay_agent/studio/harness.py`", "`Config.studio_harness_root`", "`bootstrap.bat <프로젝트 폴더>`",
                 "`index_build.bat <프로젝트 폴더>`", "`RegisterResult`", "`importer.conflict`", "`git init`",
                 "`relay_agent/studio/defaults/gitignore`", "`importer.rules`", "`journal.detect_vcs`",
                 "`engine.repos`"):
        assert word in architecture, word
    verification = (DESIGN / "VERIFICATION.md").read_text(encoding="utf-8")
    assert "M-5 릴레이 런이 `CLAUDE.local.md` 스킬 라우팅을 읽는지" in verification
    assert "M-5 game-harness 의 bat 규약" in verification and "M-5 git 템플릿 대조" in verification
