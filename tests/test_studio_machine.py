"""This computer's diagnosis (M-1): GET /studio/machine (§6.4) and what it is put together from — the engines in
the registry, the running editors, the disk, the checks of `relay doctor`. The registry is a fake that can only
be read; no PowerShell and no `claude` is run."""
import json
import platform
import re
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from relay_agent.studio import api, machine
from relay_agent.studio.schema import MIGRATIONS

DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
TOKEN = "shared-Token-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
KEYS = ["name", "server_version", "schema_version", "now", "utc_offset_minutes", "doctor", "engines", "disk",
        "providers", "editors", "limits"]
BRIEF = ["name", "server_version", "schema_version", "utc_offset_minutes"]
W64, W32 = 0x0100, 0x0200
FOUR = {"5.7": "F:\\Epic Games\\UE_5.7", "4.27": "C:\\Program Files\\Epic Games\\UE_4.27",
        "5.6": "E:\\Unreal\\UE_5.6", "5.1": "D:\\Epic\\UE_5.1"}  # as a launcher wrote them: not in order
IN_ORDER = ["4.27", "5.1", "5.6", "5.7"]
EDITOR = '"F:\\Epic Games\\UE_5.7\\Engine\\Binaries\\Win64\\UnrealEditor.exe" "D:\\Game\\Game.uproject" -log'


class Key:
    """An open key of the fake registry: its sub keys, or its values."""

    def __init__(self, node):
        self.node, self.open = node, True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.open = False


class Registry:
    """Stands in for `winreg`, with the three calls that read and none that writes — a CreateKey or a
    SetValueEx would be an AttributeError. `views` is {view flag: {version: {value name: value}}}; a view that
    is not given has no `Unreal Engine` key."""
    HKEY_LOCAL_MACHINE, KEY_READ, KEY_WOW64_64KEY, KEY_WOW64_32KEY = "HKLM", 0x20019, W64, W32

    def __init__(self, views):
        self.views, self.opened, self.keys = views, [], []

    def OpenKey(self, key, sub_key, reserved=0, access=0x20019):  # noqa: N802 — winreg's names
        assert reserved == 0 and access & ~(W64 | W32) == self.KEY_READ  # read access, nothing more
        view = access & (W64 | W32)
        self.opened.append((sub_key, view))
        if isinstance(key, Key):
            assert key.open
            node = key.node.get(sub_key)
        else:
            assert key == self.HKEY_LOCAL_MACHINE
            node = self.views.get(view) if sub_key == machine.ENGINE_KEY else None
        if node is None:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        self.keys.append(Key(node))
        return self.keys[-1]

    def EnumKey(self, key, index):  # noqa: N802
        names = list(key.node)
        if index >= len(names):
            raise OSError(259, "No more data is available")
        return names[index]

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in key.node:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return key.node[name], 1


def installed(folders):
    return {version: {"InstalledDirectory": folder} for version, folder in folders.items()}


def answer(*rows):
    """What PowerShell prints for these processes: one object bare, several as an array."""
    data = [{"ProcessId": pid, "Name": name, "CommandLine": cmdline} for pid, name, cmdline in rows]
    return json.dumps(data[0] if len(data) == 1 else data, ensure_ascii=False)


def fakes(**over):
    parts = dict(engines=lambda: dict(FOUR),
                 processes=lambda: [{"pid": 4321, "name": "UnrealEditor.exe", "cmdline": EDITOR}],
                 healthy=lambda port: port == 8101,
                 usage=lambda folder: SimpleNamespace(total=1000, used=400, free=600),
                 doctor=lambda: [{"id": "git", "ok": True, "what": "git", "hint": ""}],
                 providers=lambda: [{"name": "claude", "available": True, "limits": []}],
                 version=lambda: "0123456789ab")
    return machine.Sources(**{**parts, **over})


def documented_keys():
    row = re.search(r"^\| `GET /studio/machine` \| — \| `\{([^}]+)\}` \|", DOC.read_text(encoding="utf-8"), re.M)
    return [key.strip().removesuffix("[]") for key in row.group(1).split(",")]


@pytest.fixture
def served(tmp_path, monkeypatch):
    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    api.close_store()
    yield server
    api.close_store()


# --- installed engines (the registry) -----------------------------------------------------------------------
def test_four_engines_on_four_drives_are_read_and_listed_in_version_order():
    reg = Registry({W64: installed(FOUR)})
    found = machine.read_engine_registry(reg, windows=True)
    assert found == FOUR
    rows = machine.engines(lambda: found)
    assert [(e["version"], e["path"]) for e in rows] == [
        ("4.27", "C:\\Program Files\\Epic Games\\UE_4.27"), ("5.1", "D:\\Epic\\UE_5.1"),
        ("5.6", "E:\\Unreal\\UE_5.6"), ("5.7", "F:\\Epic Games\\UE_5.7")]
    assert all(list(e) == ["version", "path", "exists"] and isinstance(e["exists"], bool) for e in rows)
    assert len({e["path"][0] for e in rows}) == 4  # four drives
    assert len(reg.keys) == 5 and not any(key.open for key in reg.keys)  # every key opened was closed again


def test_both_views_are_read_and_a_key_without_a_folder_is_skipped():
    reg = Registry({W64: {"5.7": {"InstalledDirectory": "F:\\UE_5.7"}, "5.6": {}, "5.5": {"InstalledDirectory": "  "}},
                    W32: installed({"5.7": "C:\\old\\UE_5.7", "4.27": "C:\\UE_4.27"})})
    assert machine.read_engine_registry(reg, windows=True) == {"5.7": "F:\\UE_5.7", "4.27": "C:\\UE_4.27"}
    assert [view for sub_key, view in reg.opened if sub_key == machine.ENGINE_KEY] == [W64, W32]
    only32 = Registry({W32: installed(FOUR)})
    assert machine.read_engine_registry(only32, windows=True) == FOUR


def test_no_key_or_not_windows_is_an_empty_list(monkeypatch):
    bare = Registry({})
    assert machine.read_engine_registry(bare, windows=True) == {} and len(bare.opened) == 2  # both views asked
    assert machine.engines(lambda: machine.read_engine_registry(bare, windows=True)) == []
    untouched = Registry({W64: installed(FOUR)})
    assert machine.read_engine_registry(untouched, windows=False) == {} and untouched.opened == []
    monkeypatch.setattr(machine, "is_windows", lambda: False)  # what `windows` is when nobody says
    assert machine.read_engine_registry(untouched) == {} and untouched.opened == []
    assert machine.read_engine_registry() == {}  # winreg is not even imported
    ran = []
    assert machine.editor_processes(run=lambda *a, **kw: ran.append(a)) == [] and ran == []


def test_exists_says_whether_the_folder_is_there_and_versions_sort_as_numbers(tmp_path):
    here = tmp_path / "UE_5.10"
    here.mkdir()
    rows = machine.engines(lambda: {"5.10": str(here), "source": str(here), "5.7": str(tmp_path / "gone"),
                                    "4.27": str(tmp_path)})
    assert [(e["version"], e["exists"]) for e in rows] == [("4.27", True), ("5.7", False), ("5.10", True),
                                                           ("source", True)]


def test_the_real_registry_is_read_without_an_error():
    found = machine.read_engine_registry()  # read only; {} where there is no Windows or no engine
    assert isinstance(found, dict)
    assert all(isinstance(version, str) and isinstance(folder, str) and folder for version, folder in found.items())


# --- running editors ----------------------------------------------------------------------------------------
def test_the_process_list_is_read_from_what_powershell_prints():
    assert machine.parse_processes("") == [] and machine.parse_processes(" \r\n") == []
    assert machine.parse_processes(answer((4321, "UnrealEditor.exe", EDITOR))) == [
        {"pid": 4321, "name": "UnrealEditor.exe", "cmdline": EDITOR}]
    many = machine.parse_processes(answer((4321, "UnrealEditor.exe", EDITOR), (51, "UE4Editor.exe", None),
                                          (50, "UnrealEditor-Cmd.exe", "UnrealEditor-Cmd.exe x.uproject -run=cook")))
    assert [(p["pid"], p["name"], p["cmdline"]) for p in many] == [(4321, "UnrealEditor.exe", EDITOR),
                                                                    (51, "UE4Editor.exe", "")]


def test_editor_processes_asks_powershell_once_and_gives_up_quietly(monkeypatch):
    monkeypatch.setattr(machine, "is_windows", lambda: True)
    asked = []

    def run(command, **kw):
        asked.append((command, kw))
        return SimpleNamespace(stdout=("\ufeff" + answer((7, "UnrealEditor.exe", EDITOR))).encode("utf-8"))

    assert machine.editor_processes(run=run) == [{"pid": 7, "name": "UnrealEditor.exe", "cmdline": EDITOR}]
    (command, kw), = asked
    assert command[0] == "powershell" and command[-1] == machine.EDITOR_QUERY
    assert "Win32_Process" in machine.EDITOR_QUERY and '"' not in machine.EDITOR_QUERY
    assert kw["timeout"] == machine.QUERY_TIMEOUT and kw["capture_output"] is True

    def slow(command, **kw):
        raise subprocess.TimeoutExpired(command, kw["timeout"])

    def missing(command, **kw):
        raise FileNotFoundError(command[0])

    assert machine.editor_processes(run=slow) == [] and machine.editor_processes(run=missing) == []
    assert machine.editor_processes(run=lambda *a, **kw: SimpleNamespace(stdout=b"not json")) == []
    assert machine.editor_processes(run=lambda *a, **kw: SimpleNamespace(stdout=b"")) == []


def test_the_uproject_is_found_in_a_command_line():
    assert machine.uproject_of(EDITOR) == "D:\\Game\\Game.uproject"
    assert machine.uproject_of("UnrealEditor.exe D:/Game/Game.uproject -game") == "D:/Game/Game.uproject"
    assert machine.uproject_of('"C:\\UE 5\\UnrealEditor.exe" "D:\\My Game\\My Game.uproject"') == (
        "D:\\My Game\\My Game.uproject")
    assert machine.uproject_of('"C:\\UE\\UnrealEditor.exe"') is None and machine.uproject_of("") is None


def test_an_editor_belongs_to_the_project_whose_folder_holds_its_uproject():
    projects = [{"id": "prj_a", "path": "D:/game", "mcp_port": 8101},
                {"id": "prj_b", "path": "D:/game/tools/", "mcp_port": 8102},
                {"id": "prj_c", "path": "E:\\Other", "mcp_port": None}]
    processes = [{"pid": 30, "name": "UnrealEditor.exe", "cmdline": '"x.exe" "D:\\Game\\Tools\\T.uproject"'},
                 {"pid": 10, "name": "UnrealEditor.exe", "cmdline": EDITOR},
                 {"pid": 20, "name": "UnrealEditor.exe", "cmdline": '"x.exe" "D:\\gamer\\G.uproject"'},
                 {"pid": 40, "name": "UE4Editor.exe", "cmdline": '"x.exe"'},
                 {"pid": 50, "name": "UnrealEditor.exe", "cmdline": "x.exe e:/other/O.uproject"}]
    probed = []
    rows = machine.editors(processes, projects, lambda port: probed.append(port) or port == 8101)
    assert all(list(row) == ["project_id", "pid", "mcp_port", "healthy", "uproject"] for row in rows)
    assert [(r["pid"], r["project_id"], r["mcp_port"], r["healthy"]) for r in rows] == [
        (10, "prj_a", 8101, True), (20, None, None, False), (30, "prj_b", 8102, False), (40, None, None, False),
        (50, "prj_c", None, False)]
    assert sorted(probed) == [8101, 8102]  # only a port a project has is asked
    assert rows[0]["uproject"] == "D:\\Game\\Game.uproject" and rows[3]["uproject"] is None


def test_port_open_says_whether_something_listens():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert machine.port_open(port) is True
    assert machine.port_open(port) is False and machine.port_open(None) is False


# --- disk · limits ------------------------------------------------------------------------------------------
def test_disk_is_asked_of_the_nearest_folder_that_exists(tmp_path):
    asked = []

    def usage(folder):
        asked.append(Path(folder))
        return SimpleNamespace(total=1000, used=400, free=600)

    def broken(folder):
        raise OSError("drive gone")

    assert machine.disk(tmp_path / "runs" / "not" / "yet", usage) == {
        "path": tmp_path.as_posix(), "free_bytes": 600, "total_bytes": 1000}
    assert asked == [tmp_path]
    assert machine.disk(tmp_path, broken) == {"path": tmp_path.as_posix(), "free_bytes": None, "total_bytes": None}
    real = machine.disk(tmp_path)
    assert 0 < real["free_bytes"] <= real["total_bytes"]


def test_limits_come_from_the_config(tmp_path):
    cfg = SimpleNamespace(runs_dir=tmp_path, studio_max_runs=2, studio_max_exchanges=20, studio_group_max_members=8,
                          studio_group_max_exchanges=6)
    assert machine.report(cfg, [], fakes())["limits"] == {"max_concurrent_runs": 2, "max_exchanges": 20,
                                                          "group_max_members": 8, "group_max_exchanges": 6}
    cfg.studio_max_exchanges = 7  # `Config.studio_max_exchanges` (§3.16 · §4.6), read at every report
    cfg.studio_group_max_members = 5  # `Config.studio_group_max_members` (§6.4): what the app holds an invitation to
    cfg.studio_group_max_exchanges = 3  # `Config.studio_group_max_exchanges` (§4.6): the n of the app's n/limit
    assert machine.report(cfg, [], fakes())["limits"] == {"max_concurrent_runs": 2, "max_exchanges": 7,
                                                          "group_max_members": 5, "group_max_exchanges": 3}


# --- GET /studio/machine ------------------------------------------------------------------------------------
def test_get_machine_answers_the_eleven_fields_of_the_document(served, monkeypatch):
    project = api.get_store().create_project("게임", "D:/game", "game", engine_version="5.7", mcp_port=8101)
    monkeypatch.setattr(api, "_machine", fakes())
    r = TestClient(served.app, headers=AUTH).get("/studio/machine")
    assert r.status_code == 200, r.text
    body = r.json()
    assert list(body) == KEYS == documented_keys() and len(KEYS) == 11
    assert body["name"] == platform.node() and body["server_version"] == "0123456789ab"
    assert body["schema_version"] == MIGRATIONS[-1][0] and datetime.fromisoformat(body["now"])
    assert body["utc_offset_minutes"] == machine.utc_offset_minutes() and abs(body["utc_offset_minutes"]) <= 14 * 60
    assert [e["version"] for e in body["engines"]] == IN_ORDER
    assert [e["path"] for e in body["engines"]] == [FOUR[version] for version in IN_ORDER]
    assert body["doctor"] == [{"id": "git", "ok": True, "what": "git", "hint": ""}]
    assert body["providers"] == [{"name": "claude", "available": True, "limits": []}]
    assert body["editors"] == [{"project_id": project["id"], "pid": 4321, "mcp_port": 8101, "healthy": True,
                                "uproject": "D:\\Game\\Game.uproject"}]
    assert list(body["disk"]) == ["path", "free_bytes", "total_bytes"]
    assert body["disk"]["free_bytes"] == 600 and body["disk"]["total_bytes"] == 1000
    assert body["limits"] == {"max_concurrent_runs": served.cfg.studio_max_runs,
                              "max_exchanges": served.cfg.studio_max_exchanges,
                              "group_max_members": served.cfg.studio_group_max_members,
                              "group_max_exchanges": served.cfg.studio_group_max_exchanges}
    assert served.cfg.studio_group_max_exchanges == 6  # the default (§4.6)
    assert TestClient(served.app).get("/studio/machine").status_code == 401  # the owner's path, like the rest


def test_a_source_that_fails_empties_only_its_own_field(served, monkeypatch):
    def broken(*args):
        raise RuntimeError("읽지 못함")

    owner = TestClient(served.app, headers=AUTH)
    monkeypatch.setattr(api, "_machine", fakes(doctor=broken, engines=broken, providers=broken, version=broken))
    body = owner.get("/studio/machine").json()
    assert list(body) == KEYS and body["doctor"] == body["engines"] == body["providers"] == []
    assert body["server_version"] == "" and len(body["editors"]) == 1 and body["disk"]["free_bytes"] == 600
    monkeypatch.setattr(api, "_machine", fakes(processes=broken, usage=broken))
    body = owner.get("/studio/machine").json()
    assert body["editors"] == [] and body["disk"]["free_bytes"] is None and body["disk"]["total_bytes"] is None
    assert len(body["engines"]) == 4 and len(body["doctor"]) == 1
    monkeypatch.setattr(api, "_machine", machine.Sources(engines=dict, processes=list, usage=fakes().usage))
    body = owner.get("/studio/machine").json()  # nobody gave a doctor, the providers or a version
    assert list(body) == KEYS and body["doctor"] == [] and body["providers"] == [] and body["server_version"] == ""


def test_the_server_hands_over_its_doctor_providers_and_code_version(served):
    src = api._machine
    assert src.version() == served.RUNNING_CODE and re.fullmatch(r"[0-9a-f]{12}", src.version())
    assert src.providers is served.list_providers and src.doctor is served._doctor_checks
    assert src.engines is machine.read_engine_registry and src.processes is machine.editor_processes
    assert src.healthy is machine.health_check  # M-7: the MCP health_check, not a bare connection
    brief = api.machine_brief()  # what the stream's hello carries (§7.1)
    assert list(brief) == BRIEF and brief == {
        "name": platform.node(), "server_version": served.RUNNING_CODE, "schema_version": MIGRATIONS[-1][0],
        "utc_offset_minutes": machine.utc_offset_minutes()}


def test_use_machine_replaces_only_the_parts_it_is_given(monkeypatch):
    monkeypatch.setattr(api, "_machine", machine.Sources())
    api.use_machine(version=lambda: "v1")
    api.use_machine(doctor=list)
    assert api._machine.version() == "v1" and api._machine.doctor is list and api._machine.providers is None
    assert api._machine.engines is machine.read_engine_registry
    with pytest.raises(TypeError):
        api.use_machine(nothing=1)


# --- relay doctor -------------------------------------------------------------------------------------------
def test_doctor_checks_are_the_lines_relay_doctor_prints(served, monkeypatch, capsys):
    from relay_agent import cli

    cfg = served.cfg
    monkeypatch.setattr("shutil.which", lambda name, *a, **kw: None)  # no git, no claude: `claude` is not run
    monkeypatch.setattr(cfg, "serve_host", "0.0.0.0")
    monkeypatch.setattr(cfg, "docs_root", cfg.runs_dir / "no-docs")
    checks = cli.doctor_checks(cfg)
    assert all(list(check) == ["id", "ok", "what", "hint"] for check in checks)
    ids = [check["id"] for check in checks]
    assert ids[:9] == ["python", "git", "claude", "runs_dir", "relays_dir", "docs_root", "auth_token", "serve_host",
                       "remote_networks"]
    assert all(id.startswith("repo:") for id in ids[9:]) and len(set(ids)) == len(ids)
    by = {check["id"]: check for check in checks}
    assert by["python"]["ok"] is True and by["python"]["hint"] == ""  # a hint only where the check failed
    assert by["git"] == {"id": "git", "ok": False, "what": "git", "hint": "git 설치 필요 (작업 공간 스냅샷에 사용)"}
    assert by["docs_root"]["ok"] is None and by["remote_networks"]["ok"] is None  # notes: not counted
    assert by["runs_dir"]["ok"] is True and by["serve_host"]["ok"] is True  # remote, and the fixture's token
    assert json.loads(json.dumps(checks)) == checks  # plain values: the list goes out as JSON
    # the texts themselves, as `relay doctor` has always printed them — a space after each colon
    assert by["auth_token"]["what"] == "auth_token: 설정됨"
    assert by["docs_root"]["what"].startswith(f"docs_root 없음 (선택): {cfg.docs_root} — 문서 질의는")
    assert by["remote_networks"]["what"] == (
        "원격 허용 네트워크: 제한 없음 (토큰만)  — Tailscale 전용으로: relay remote on --tailscale")
    assert by["serve_host"]["what"] == "serve_host: 0.0.0.0 (원격 접속 켜짐)"
    assert by["runs_dir"]["what"] == f"runs_dir: {cfg.runs_dir}" and by["relays_dir"]["what"].startswith("relays_dir: ")
    assert by["python"]["what"].startswith("Python 3.") and by["claude"]["what"] == "Claude Code CLI (없음)"
    assert all(check["what"].startswith(f"repo {check['id'][5:]}: ") for check in checks[9:])

    code = cli.doctor(cfg)
    out = capsys.readouterr().out
    marks = {None: "➖", True: "✅", False: "❌"}
    printed = [line for line in out.split("\nAI providers:")[0].splitlines() if not line.startswith("   → ")]
    assert printed == [f"{marks[check['ok']]} {check['what']}" for check in checks]
    assert "❌ git\n   → git 설치 필요 (작업 공간 스냅샷에 사용)\n" in out
    problems = sum(check["ok"] is False for check in checks)
    assert problems >= 2 and code == 1 and out.rstrip().endswith(f"확인 필요 {problems}건")


# --- the document -------------------------------------------------------------------------------------------
def test_the_document_names_the_file_and_the_shapes():
    text = DOC.read_text(encoding="utf-8")
    assert "| `relay_agent/studio/machine.py` |" in text  # §2.1
    assert "`{name, server_version, schema_version, utc_offset_minutes}`" in text  # §7.1: hello's machine
    assert "`[{version, path, exists}]`" in text and "`[{project_id, pid, mcp_port, healthy, uproject}]`" in text
    assert "`[{id, ok, what, hint}]`" in text and "`{path, free_bytes, total_bytes}`" in text
