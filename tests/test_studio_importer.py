"""Bringing in a project that is already there (M-6): POST /studio/projects (§6.4, §3.3) and what it reads from
the folder (studio/importer.py). Every project is a fake one in a temporary folder, the repositories are a
registry made here and the engines a list made here — no real project, registry or config file is touched."""
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay_agent.repos import RepoRegistry
from relay_agent.studio import api, importer, machine
from relay_agent.studio.store import now

DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
TOKEN = "shared-Token-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ROW = {"id", "name", "path", "engine_version", "mcp_port", "repo", "created_at", "updated_at", "archived_at"}
GUID = "{4F0C2A51-4B6E-9C1D-7E33-0A1B2C3D4E5F}"  # what a .uproject of an engine built from source says
INI = """[/Script/EngineSettings.GameMapsSettings]
GameDefaultMap=/Game/Maps/Main.Main
+ActiveGameNameRedirects=(OldGameName="TP_Blank",NewGameName="/Script/Game")
+ActiveGameNameRedirects=(OldGameName="/Script/TP_Blank",NewGameName="/Script/Game")

[/Script/Other.Settings]
Port=1234
"""


def fake_project(folder, name="Game", *, association="5.7", ini=55557, listed=None, claude=True, docs=True):
    """A UE project as the editor leaves one. `ini` · `listed`: the port in DefaultEngine.ini · in .mcp.json
    (None = not written there, and no .mcp.json at all)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.uproject").write_text(
        json.dumps({"FileVersion": 3, "EngineAssociation": association, "Modules": [{"Name": name}]}, indent="\t"),
        encoding="utf-8")
    (folder / "Config").mkdir()
    text = INI + (f"\n[UnrealMCP]\nPort={ini}\n" if ini is not None else "")
    (folder / "Config" / "DefaultEngine.ini").write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    (folder / "Content").mkdir()
    (folder / "Content" / "Main.umap").write_bytes(b"\xc1\x83\x2a\x9e" + bytes(range(64)))
    (folder / "Source").mkdir()
    (folder / "Source" / f"{name}.Target.cs").write_text("// target\n", encoding="utf-8")
    if listed is not None:
        servers = {"docs": {"command": "uv", "args": ["run", "docs"]},
                   "unreal": {"command": "uv", "args": ["run", "unreal"], "env": {"UNREAL_MCP_PORT": listed}}}
        (folder / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    if claude:
        (folder / "CLAUDE.md").write_text("# 규칙\n", encoding="utf-8")
    if docs:
        (folder / "Docs").mkdir()
        (folder / "Docs" / "README.md").write_text("문서\n", encoding="utf-8")
    return folder


def snapshot(folder):
    """Everything in `folder`, `.git` too: {relative path: (sha256 of a file | None for a folder, when it was
    last written)}. The same before and after = nothing was made, removed or written again."""
    seen = {".": (None, folder.stat().st_mtime_ns)}
    for p in folder.rglob("*"):
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        seen[p.relative_to(folder).as_posix()] = (digest, p.stat().st_mtime_ns)
    return seen


def git(folder, *args):
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=folder, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


def refused(r, status, code):
    assert (r.status_code, r.json().get("code")) == (status, code), r.text
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]
    return r.json()


def row_of(body):
    return {key: value for key, value in body.items() if key != "detected"}


class Studio:
    """The server app on a studio DB of its own. `root` · `second` are the folders of the two registered
    repositories (`game` · `second`), `outside` a folder no repository holds."""

    def __init__(self, server, tmp_path):
        self.server = server
        self.owner = TestClient(server.app, headers=AUTH)
        self.store = api.get_store()
        self.root, self.second, self.outside = tmp_path / "repo", tmp_path / "second", tmp_path / "outside"

    def bring(self, path, **body):
        return self.owner.post("/studio/projects", json={"path": str(path), **body})

    def rows(self):
        return self.store.list_projects(include_archived=True)

    def events(self):
        return [e for e in self.store.list_stream_events() if e["type"] == "project.updated"]


@pytest.fixture
def studio(tmp_path, monkeypatch):
    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    for name in ("repo", "second", "outside", "UE_5.7"):
        (tmp_path / name).mkdir()
    # the repositories: the server's registry object is replaced (as `_reload_repos` does), no config file is read
    monkeypatch.setattr(server.engine, "repos", RepoRegistry({"game": {"path": str(tmp_path / "repo")},
                                                              "second": {"path": str(tmp_path / "second")}}))
    # the engines: 5.7 is installed; the registry still names 5.6, whose folder is gone
    found = {"5.7": str(tmp_path / "UE_5.7"), "5.6": str(tmp_path / "UE_5.6")}
    monkeypatch.setattr(api, "_machine", machine.Sources(engines=lambda: dict(found)))
    api.close_store()
    yield Studio(server, tmp_path)
    api.close_store()


# --- the folder is read and left as it was ------------------------------------------------------------------
def test_import_registers_the_folder_and_writes_nothing_in_it(studio):
    folder = fake_project(studio.root, listed="55558")
    before = snapshot(folder)
    r = studio.bring(folder)
    assert r.status_code == 201, r.text
    body, row = r.json(), row_of(r.json())
    assert set(row) == ROW and row == studio.store.get_project(row["id"])
    assert (row["name"], row["path"], row["repo"]) == ("Game", str(folder.resolve()), "game")
    assert (row["engine_version"], row["mcp_port"], row["archived_at"]) == ("5.7", 55557, None)
    assert body["detected"] == {
        "uproject": "Game.uproject", "engine": {"association": "5.7", "installed": True},
        "mcp": {"port": 55557, "ini": 55557, "mcp_json": 55558, "in_use_by": None},  # the ini before .mcp.json
        "harness": {"claude_md": True, "docs": True}, "git": False, "missing": []}
    assert snapshot(folder) == before  # the list of files, what is in them, when they were written
    events = studio.events()
    assert len(events) == 1 and events[0]["data"] == {"project": row} and events[0]["project_id"] == row["id"]
    assert studio.owner.get("/studio/projects").json()["items"] == [row]
    assert studio.owner.get(f"/studio/projects/{row['id']}").json()["name"] == "Game"


def test_import_in_a_git_repository_leaves_git_status_clean(studio):
    if shutil.which("git") is None:
        pytest.skip("git 이 없다")
    folder = fake_project(studio.root / "Game", claude=False)
    git(studio.root, "init", "-q")
    git(studio.root, "add", "-A")
    git(studio.root, "commit", "-q", "-m", "init")
    assert git(studio.root, "status", "--porcelain") == ""
    before = snapshot(studio.root)  # .git too: its index, its refs, its objects
    r = studio.bring(folder)
    assert r.status_code == 201, r.text
    assert snapshot(studio.root) == before
    assert git(studio.root, "status", "--porcelain") == ""
    assert r.json()["path"] == str(folder.resolve()) and r.json()["repo"] == "game"
    assert r.json()["detected"]["git"] is True  # the .git of the repository's root, one folder up
    assert r.json()["detected"]["missing"] == ["harness"]


def test_what_is_missing_is_reported_and_not_filled_in(studio):
    folder = fake_project(studio.root, "Mnys", association=GUID, ini=None, claude=False, docs=False)
    before = snapshot(folder)
    r = studio.bring(folder)
    body = r.json()
    assert r.status_code == 201 and (body["name"], body["engine_version"], body["mcp_port"]) == ("Mnys", "", None)
    assert body["detected"] == {
        "uproject": "Mnys.uproject", "engine": {"association": GUID, "installed": False},
        "mcp": {"port": None, "ini": None, "mcp_json": None, "in_use_by": None},
        "harness": {"claude_md": False, "docs": False}, "git": False, "missing": ["mcp", "harness"]}
    assert snapshot(folder) == before
    assert not any((folder / name).exists() for name in ("CLAUDE.md", "Docs", ".mcp.json", ".git"))


def test_the_port_falls_back_to_mcp_json_and_an_engine_that_is_gone_keeps_its_version(studio):
    folder = fake_project(studio.root, association="5.6", ini=None, listed=55560, docs=False)
    body = studio.bring(folder, name="  내 게임  ").json()
    assert (body["name"], body["engine_version"], body["mcp_port"]) == ("내 게임", "5.6", 55560)
    found = body["detected"]
    assert found["engine"] == {"association": "5.6", "installed": False}  # the registry names it, its folder is gone
    assert found["mcp"] == {"port": 55560, "ini": None, "mcp_json": 55560, "in_use_by": None}
    assert found["harness"] == {"claude_md": True, "docs": False} and found["missing"] == ["harness"]


def test_engines_that_cannot_be_read_do_not_stop_an_import(studio, monkeypatch):
    def broken():
        raise OSError("registry")

    monkeypatch.setattr(api, "_machine", machine.Sources(engines=broken))
    body = studio.bring(fake_project(studio.root, association="5.5")).json()
    assert body["engine_version"] == "5.5" and body["detected"]["engine"] == {"association": "5.5", "installed": False}


def test_a_port_another_project_holds_is_left_empty_and_named(studio):
    holder = studio.store.create_project("먼저", "D:/first", "first", mcp_port=55557)
    r = studio.bring(fake_project(studio.root))
    body = r.json()
    assert r.status_code == 201 and body["mcp_port"] is None
    assert studio.store.get_project(body["id"])["mcp_port"] is None
    assert body["detected"]["mcp"] == {"port": 55557, "ini": 55557, "mcp_json": None, "in_use_by": holder["id"]}
    assert body["detected"]["missing"] == []  # the project names its port; it is this studio that cannot take it yet
    assert studio.events()[-1]["data"] == {"project": row_of(body)}  # the event carries the row, not `detected`
    studio.store.update_project(holder["id"], archived_at=now())
    free = studio.bring(fake_project(studio.second, "Second")).json()  # an archived project frees its port
    assert free["mcp_port"] == 55557 and free["detected"]["mcp"]["in_use_by"] is None and free["repo"] == "second"


# --- refusals -----------------------------------------------------------------------------------------------
def test_the_same_folder_name_or_repository_is_409(studio):
    folder = fake_project(studio.root)
    first = row_of(studio.bring(folder).json())
    body = refused(studio.bring(folder), 409, "conflict")
    assert body["field"] == "path" and "보관" not in body["detail"]
    assert refused(studio.bring(folder.as_posix() + "/", name="다른 이름"), 409, "conflict")["field"] == "path"
    inner = fake_project(studio.root / "Inner", "Inner")
    assert refused(studio.bring(inner, name="Game"), 409, "conflict")["field"] == "name"
    assert refused(studio.bring(inner), 409, "conflict")["field"] == "repo"  # one repository, one project (§3.3)
    other = fake_project(studio.second, "Game")
    assert refused(studio.bring(other), 409, "conflict")["field"] == "name"
    assert studio.rows() == [first] and len(studio.events()) == 1
    studio.store.update_project(first["id"], archived_at=now())
    for path, field in ((folder, "path"), (other, "name"), (inner, "repo")):  # an archived one keeps what it has,
        body = refused(studio.bring(path), 409, "conflict")  # and the refusal says it is one: the list does not show it
        assert body["field"] == field and "보관된 프로젝트" in body["detail"]
    r = studio.bring(other, name="Second")
    assert r.status_code == 201 and (r.json()["repo"], r.json()["mcp_port"]) == ("second", 55557)
    assert len(studio.rows()) == 2 and len(studio.events()) == 2


def test_a_folder_that_cannot_be_brought_in_is_precondition_failed(studio):
    inside = fake_project(studio.root / "Game")
    there = fake_project(studio.outside)  # a real project, in no registered repository
    said = []
    for path in (there, studio.outside / "gone"):  # there or not: one answer for a folder outside the repositories
        body = refused(studio.bring(path), 409, "precondition_failed")
        assert body["failed"] == [] and "relay repo add" in body["detail"]
        said.append(body["detail"].replace(str(path.resolve()), "<path>"))
    assert said[0] == said[1] and "<path>" in said[0]
    assert "relay repo add" in refused(studio.bring(inside, repo="nowhere"), 409, "precondition_failed")["detail"]
    assert "'game'" in refused(studio.bring(inside, repo="second"), 409, "precondition_failed")["detail"]
    refused(studio.bring(there, repo="game"), 409, "precondition_failed")
    assert "폴더가 없습니다" in refused(studio.bring(studio.root / "Gone"), 409, "precondition_failed")["detail"]
    (studio.root / "Empty").mkdir()
    assert ".uproject" in refused(studio.bring(studio.root / "Empty"), 409, "precondition_failed")["detail"]
    detail = refused(studio.bring(inside / "Game.uproject"), 409, "precondition_failed")["detail"]
    assert "파일입니다" in detail and "폴더가 없습니다" not in detail  # the folder is asked for, not the file — said so
    refused(studio.bring("Game"), 409, "precondition_failed")  # not an absolute path
    assert refused(studio.bring("  "), 422, "missing_fields")["fields"] == ["path"]
    assert refused(studio.bring(inside, name=" ", repo=""), 422, "missing_fields")["fields"] == ["name", "repo"]
    assert studio.owner.post("/studio/projects", json={"path": str(inside), "mcp_port": 1}).status_code == 422
    assert studio.owner.post("/studio/projects", json={"name": "x"}).status_code == 422
    assert studio.rows() == [] and studio.events() == []
    assert studio.bring(inside, repo="game", name=None).status_code == 201  # the repository it lies in, named


def test_import_needs_the_owner(studio):
    folder = fake_project(studio.root)
    r = TestClient(studio.server.app).post("/studio/projects", json={"path": str(folder)})
    assert (r.status_code, r.json()["code"]) == (401, "unauthorized") and studio.rows() == []


def test_the_server_hands_over_the_repositories_it_has_now(studio, monkeypatch):
    assert api._repos() is studio.server.engine.repos
    folder = fake_project(studio.root)
    monkeypatch.setattr(studio.server.engine, "repos", RepoRegistry({}))  # what `_reload_repos` does
    assert api._repos() is studio.server.engine.repos
    refused(studio.bring(folder), 409, "precondition_failed")  # that folder is in no repository any more
    monkeypatch.setattr(api, "_repos", None)  # no registry at all: the same refusal, not an error
    refused(studio.bring(folder), 409, "precondition_failed")
    refused(studio.bring(folder, repo="game"), 409, "precondition_failed")
    assert studio.rows() == []


# --- what is read from the folder (studio/importer.py) ------------------------------------------------------
def test_the_module_has_nothing_to_write_or_run_with():
    assert not {"subprocess", "os", "shutil", "tempfile"} & set(vars(importer))
    source = Path(importer.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b(write_text|write_bytes|mkdir|unlink|rename|replace\(|touch|open)\b", source)


def test_the_uproject_and_its_engine_are_read(tmp_path):
    assert importer.find_uproject(tmp_path) is None and importer.find_uproject(tmp_path / "gone") is None
    (tmp_path / "Zeta.uproject").write_text('{"EngineAssociation": "5.7"}', encoding="utf-8")
    (tmp_path / "Beta.UPROJECT").write_bytes(b"\xef\xbb\xbf" + b'{"EngineAssociation": " 5.6 "}')  # with a BOM
    (tmp_path / "Alpha.uproject").mkdir()  # a folder of that name is not a project file
    (tmp_path / "Game.uproject.bak").write_text("{}", encoding="utf-8")
    assert importer.find_uproject(tmp_path).name == "Beta.UPROJECT"  # the first by name
    assert importer.engine_association(tmp_path / "Beta.UPROJECT") == "5.6"
    assert importer.engine_association(tmp_path / "Zeta.uproject") == "5.7"
    for text in ("{", "[]", '{"EngineAssociation": 5.7}', "{}", "\xff\xfe"):
        (tmp_path / "Zeta.uproject").write_text(text, encoding="latin-1")
        assert importer.engine_association(tmp_path / "Zeta.uproject") == ""
    assert importer.engine_association(tmp_path / "gone.uproject") == ""
    versions = [importer.engine_version_of(a) for a in ("5.7", "4.27", "5.10", "", GUID, "5", "five", "5.7-src")]
    assert versions == ["5.7", "4.27", "5.10", "", "", "", "", ""]


def test_ini_port_is_the_port_of_the_unrealmcp_section(tmp_path):
    def port(text, encoding="utf-8"):
        (tmp_path / "Config").mkdir(exist_ok=True)
        (tmp_path / "Config" / "DefaultEngine.ini").write_bytes(text.encode(encoding))
        return importer.ini_port(tmp_path)

    assert importer.ini_port(tmp_path) is None  # no file
    assert port(INI) is None  # the `Port` of another section is not ours
    assert port(INI + "[UnrealMCP]\nPort=55557\n") == 55557
    assert port("[unrealmcp]\r\n; Port=1\r\n  port = 55558  \r\n[Next]\r\nPort=2\r\n") == 55558
    assert port('[UnrealMCP]\n+Port=1\nHost=127.0.0.1\nPort="55559"\n') == 55559  # `+Port` is an array line
    assert port("[UnrealMCP]\nPort=1\nPort=2\n") == 2  # twice: the last one, as the engine reads it
    assert port("[UnrealMCP]\nPort=55557\nPort=abc\n") is None  # the last one is no port: the one before is not taken
    assert port("[UnrealMCP]\nPort=abc\nPort=55557\n") == 55557
    assert port("[UnrealMCP]\nPort=55557\n", "utf-16") == 55557  # UTF-16 with its mark
    assert port("\ufeff[UnrealMCP]\nPort=55557\n") == 55557  # UTF-8 with its mark
    for bad in ("0", "65536", "abc", "", "55557.5", "-1", "123456"):
        assert port(f"[UnrealMCP]\nPort={bad}\n") is None
    folder = fake_project(tmp_path / "P", ini="abc", listed=55558)  # an ini whose `Port` is no port: .mcp.json's
    assert importer.detect(folder, [])["mcp"] == {"port": 55558, "ini": None, "mcp_json": 55558, "in_use_by": None}


def test_mcp_json_port_is_the_first_server_that_names_one(tmp_path):
    def env(value):
        return {"command": "x", "env": {"UNREAL_MCP_PORT": value}}

    def port(data):
        (tmp_path / ".mcp.json").write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return importer.mcp_json_port(tmp_path)

    assert importer.mcp_json_port(tmp_path) is None  # no file
    assert port({"mcpServers": {"a": {"command": "x"}, "b": env("55557"), "c": env(1)}}) == 55557
    assert port({"mcpServers": {"b": env(55558)}}) == 55558
    assert port({"mcpServers": {"a": env("many"), "b": env(55557)}}) is None  # the first that names one, port or not
    for bad in ("{", "[]", "null", {"mcpServers": []}, {"mcpServers": {"a": "x"}}, {"mcpServers": {"a": {"env": []}}},
                {"mcpServers": {"a": env("0")}}, {"mcpServers": {"a": env(True)}}, {"mcpServers": {"a": env(70000)}},
                {"mcpServers": {"a": env("many")}}, {"mcpServers": {"a": env(55557.5)}}):
        assert port(bad) is None


def test_git_is_looked_for_up_to_the_repository_root_and_no_further(tmp_path):
    top = tmp_path / "top"
    folder = top / "a" / "b"
    folder.mkdir(parents=True)
    (tmp_path / ".git").mkdir()  # above the root: somebody else's
    assert importer.in_git(folder, top) is False and importer.in_git(top, top) is False
    assert importer.in_git(folder, tmp_path / "elsewhere") is False  # a root the folder is not under: itself only
    (top / ".git").write_text("gitdir: ../elsewhere\n", encoding="utf-8")  # a worktree's .git is a file
    assert importer.in_git(folder, top) is True and importer.in_git(top, top) is True
    assert importer.in_git(folder) is False  # no root given: the folder itself only
    (folder / ".git").mkdir()
    assert importer.in_git(folder) is True


def test_detect_reads_a_folder_without_a_project_too(tmp_path):
    assert importer.detect(tmp_path, [{"version": "5.7", "path": "x", "exists": True}]) == {
        "uproject": None, "engine": {"association": "", "installed": False},
        "mcp": {"port": None, "ini": None, "mcp_json": None, "in_use_by": None},
        "harness": {"claude_md": False, "docs": False}, "git": False, "missing": ["mcp", "harness"]}
    (tmp_path / "CLAUDE.md").mkdir()  # a folder of that name is not the file
    (tmp_path / "Docs").write_text("x", encoding="utf-8")  # nor a file the folder
    assert importer.detect(tmp_path, [])["harness"] == {"claude_md": False, "docs": False}


def test_any_rules_file_a_relay_run_reads_counts_as_the_rules_of_the_harness(tmp_path):
    for index, name in enumerate(("CLAUDE.local.md", ".claude/CLAUDE.md")):  # what bootstrap.bat may leave (M-5)
        folder = tmp_path / f"project{index}"
        file = folder.joinpath(*name.split("/"))
        file.mkdir(parents=True)  # a folder of that name is not the file
        assert importer.rules(folder) == [] and importer.detect(folder, [])["harness"]["claude_md"] is False
        file.rmdir()
        file.write_text("rules", encoding="utf-8")
        found = importer.detect(folder, [])
        assert importer.rules(folder) == [name]
        assert found["harness"] == {"claude_md": True, "docs": False}
        assert found["missing"] == ["mcp", "harness"]  # the Docs folder is still asked for
        (folder / "Docs").mkdir()
        assert importer.detect(folder, [])["missing"] == ["mcp"]


# --- the document -------------------------------------------------------------------------------------------
def documented(name):
    """The keys the §6.4 paragraph of M-6 gives for `name`: "`name` 는 `{a, b[]}`" -> ['a', 'b']."""
    text = DOC.read_text(encoding="utf-8")
    note = next(line for line in text.splitlines() if line.startswith("- `POST /studio/projects` (M-6"))
    found = re.search(rf"`{name}` [은는] `\{{([^}}]+)\}}`", note)
    return [key.strip().removesuffix("[]") for key in found.group(1).split(",")]


def test_the_document_names_the_file_and_the_shape_of_detected(studio):
    text = DOC.read_text(encoding="utf-8")
    assert re.search(r"^\| `POST /studio/projects` \| [^|]+ \| 201 행 \+ `\{detected\}` \| "
                     r"`conflict` · `precondition_failed` \| M-6 \|$", text, re.M)
    note = next(line for line in text.splitlines() if line.startswith("- `POST /studio/projects` (M-6"))
    assert "`studio/importer.py`" in note and "`missing_fields`" in note
    assert all(f"`{field}`" in note for field in ("path", "name", "repo", "detected.mcp.in_use_by", "detected.missing"))
    detected = studio.bring(fake_project(studio.root)).json()["detected"]
    assert documented("detected") == list(detected)
    for part in ("engine", "mcp", "harness"):
        assert documented(part) == list(detected[part])
