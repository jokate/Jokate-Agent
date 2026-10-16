"""The plugins of a new UE project (M-4, the first half): the list of the settings, what the making screen is
given for an engine, the copy without `.git` and zip files, and the `.uproject` (§6.4, §7.4). MNYS is not read:
the sources are folders made here, and the project is the skeleton `creator.folder` writes."""
import codecs
import json
import os
import shutil
import stat
import tomllib
from pathlib import Path

import pytest

from relay_agent.config import Config
from relay_agent.studio import creator, importer, plugins

DESIGN = Path(__file__).resolve().parent.parent / "design" / "studio"
ENTRIES = Config().studio_plugins
MCP_FILES = {"UnrealMCP.uplugin": '{"FileVersion": 3}', "Source/UnrealMCP/UnrealMCP.cpp": "// code",
             "Python/server.py": "print(1)", "Resources/Icon128.png": "png", ".gitignore": "Saved/",
             ".git/config": "[core]", "Python/.git": "gitdir: ../.git/modules/python",
             "Old.zip": "PK", "Resources/Pack.ZIP": "PK"}
MCP_COPIED = [".gitignore", "Python/server.py", "Resources/Icon128.png", "Source/UnrealMCP/UnrealMCP.cpp",
              "UnrealMCP.uplugin"]
TAG_FILES = {"YSTagUsage.uplugin": '{"FileVersion": 3}', "Source/YSTagUsage/YSTagUsage.cpp": "// code",
             ".git/HEAD": "ref: refs/heads/main", "YSTagUsage-main.zip": "PK"}
TAG_COPIED = ["Source/YSTagUsage/YSTagUsage.cpp", "YSTagUsage.uplugin"]
MCP_ENTRY = {"Name": "UnrealMCP", "Enabled": True}
TAG_ENTRY = {"Name": "YSTagUsage", "Enabled": True}


def write(folder: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        file = folder.joinpath(*relative.split("/"))
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(text.encode("utf-8"))
    return folder


def mnys(tmp_path, *, mcp=MCP_FILES, tag=TAG_FILES) -> Path:
    """The folder `studio_plugin_root` names, with the two plugins of the settings (one left out when None)."""
    root = tmp_path / "MNYS"
    (root / "Plugins").mkdir(parents=True)
    if mcp is not None:
        write(root / "Plugins" / "Unreal-MCP-main", mcp)
    if tag is not None:
        write(root / "Plugins" / "YSTagUsage", tag)
    return root


def project(tmp_path) -> creator.Spec:
    """A project as the `folder` step leaves it. `check` is passed by: it reads the engines and refuses a temp
    folder with a space in its path, and neither matters to a copy."""
    parent = tmp_path / "Projects"
    parent.mkdir()
    spec = creator.Spec("Sample", "5.7", tmp_path / "UE_5.7", parent)
    assert creator.folder(spec).ok
    return spec


def tree(folder: Path) -> list[str]:
    return sorted(file.relative_to(folder).as_posix() for file in folder.rglob("*") if file.is_file())


def snapshot(folder: Path) -> dict[str, bytes]:
    return {relative: (folder / relative).read_bytes() for relative in tree(folder)}


def refusal(listed, ids) -> creator.Refused:
    with pytest.raises(creator.Refused) as caught:
        plugins.select(listed, ids)
    refused = caught.value
    assert refused.code == "precondition_failed" and refused.field == "plugins" and refused.reason in plugins.REASONS
    return refused


# --- the list -----------------------------------------------------------------------------------------------
def test_the_settings_list_unreal_mcp_and_ys_tag_usage(tmp_path):
    assert Config().studio_plugin_root == ""
    assert [entry["id"] for entry in ENTRIES] == ["unreal_mcp", "ys_tag_usage"]
    root = tmp_path / "MNYS"
    mcp, tag = plugins.catalog(ENTRIES, str(root))
    server = plugins.McpServer("unreal", "uv", ("--directory", "{plugin}/Python", "run", "unreal_mcp_server.py"))
    assert mcp == plugins.Plugin("unreal_mcp", "Unreal MCP", root / "Plugins" / "Unreal-MCP-main", "Unreal-MCP-main", (),
                                 server)
    assert tag.mcp is None  # only the MCP plugin brings a port
    assert tag == plugins.Plugin("ys_tag_usage", "YSTagUsage", root / "Plugins" / "YSTagUsage", "YSTagUsage", ("5.7",))


def test_a_source_is_taken_from_the_root_unless_it_is_absolute(tmp_path):
    elsewhere = tmp_path / "Elsewhere" / "Tool"
    entries = [{"id": "a", "source": "Plugins/A"}, {"id": "b", "source": str(elsewhere), "folder": "Renamed",
                                                    "name": "B", "engines": [5.7, "5.6"]}]
    for no_root in ("", None, "relative/root"):
        a, b = plugins.catalog(entries, no_root)
        assert a.source is None and b.source == elsewhere
    a, b = plugins.catalog(entries, tmp_path)
    assert a == plugins.Plugin("a", "a", tmp_path / "Plugins" / "A", "A", ())
    assert (b.name, b.folder, b.engines) == ("B", "Renamed", ("5.7", "5.6"))


@pytest.mark.parametrize("entries", [
    ["unreal_mcp"],
    [{"source": "Plugins/A"}],
    [{"id": "a"}],
    [{"id": "a", "source": "Plugins/A"}, {"id": "a", "source": "Plugins/B"}],
    [{"id": "a", "source": "Plugins/A", "folder": "Sub/A"}],
    [{"id": "a", "source": "Plugins/A", "folder": ".."}],
    [{"id": "a", "source": "Plugins/A", "folder": "C:A"}],
    [{"id": "a", "source": "Plugins/A", "engines": "5.7"}],
])
def test_settings_that_are_wrong_are_an_error(tmp_path, entries):
    with pytest.raises(ValueError):
        plugins.catalog(entries, tmp_path)


# --- the choices --------------------------------------------------------------------------------------------
def test_on_5_7_both_are_checked_without_a_warning(tmp_path):
    listed = plugins.choices(plugins.catalog(ENTRIES, mnys(tmp_path)), "5.7")
    assert listed == [
        {"id": "unreal_mcp", "name": "Unreal MCP", "checked": True, "warning": "", "available": True},
        {"id": "ys_tag_usage", "name": "YSTagUsage", "checked": True, "warning": "", "available": True}]


def test_on_5_6_ys_tag_usage_is_unchecked_with_a_warning(tmp_path):
    mcp, tag = plugins.choices(plugins.catalog(ENTRIES, mnys(tmp_path)), "5.6")
    assert mcp == {"id": "unreal_mcp", "name": "Unreal MCP", "checked": True, "warning": "", "available": True}
    assert tag["id"] == "ys_tag_usage" and tag["checked"] is False and tag["available"] is True
    assert "5.7" in tag["warning"] and "5.6" in tag["warning"]


def test_a_source_that_is_not_there_is_unchecked_with_a_warning(tmp_path):
    root = mnys(tmp_path, tag=None)
    mcp, tag = plugins.choices(plugins.catalog(ENTRIES, root), "5.7")
    assert mcp["checked"] is True and mcp["warning"] == ""
    assert tag["checked"] is False and tag["available"] is False
    assert str(root / "Plugins" / "YSTagUsage") in tag["warning"]

    mcp, tag = plugins.choices(plugins.catalog(ENTRIES, ""), "5.6")  # no root: nowhere to look, and both said
    assert (mcp["checked"], mcp["available"], tag["checked"], tag["available"]) == (False, False, False, False)
    assert "studio_plugin_root" in mcp["warning"] and " / " not in mcp["warning"]
    assert "studio_plugin_root" in tag["warning"] and "5.6" in tag["warning"] and " / " in tag["warning"]


# --- what a request names -----------------------------------------------------------------------------------
def test_select_answers_in_the_order_of_the_list_each_once(tmp_path):
    listed = plugins.catalog(ENTRIES, mnys(tmp_path))
    assert plugins.select(listed, []) == []
    assert plugins.select(listed, ["ys_tag_usage", "unreal_mcp", "ys_tag_usage"]) == listed
    assert plugins.select(listed, ("ys_tag_usage",)) == [listed[1]]  # 5.7 only is a warning, not a refusal


def test_select_refuses_an_unknown_id_and_a_missing_source(tmp_path):
    listed = plugins.catalog(ENTRIES, mnys(tmp_path, tag=None))
    for ids in (["nope"], ["unreal_mcp", 3], [{"id": "unreal_mcp"}], "unreal_mcp", None):
        assert refusal(listed, ids).reason == "plugin_unknown"
    refused = refusal(listed, ["unreal_mcp", "ys_tag_usage"])
    assert refused.reason == "plugin_source_missing" and "YSTagUsage" in refused.message
    assert str(tmp_path / "MNYS" / "Plugins" / "YSTagUsage") in refused.message
    unset = refusal(plugins.catalog(ENTRIES, ""), ["unreal_mcp"])
    assert unset.reason == "plugin_source_missing" and "studio_plugin_root" in unset.message


# --- the step -----------------------------------------------------------------------------------------------
def test_install_copies_without_git_and_zip_and_registers(tmp_path):
    spec, root = project(tmp_path), mnys(tmp_path)
    before = snapshot(root)
    chosen = plugins.select(plugins.catalog(ENTRIES, root), ["ys_tag_usage", "unreal_mcp"])
    lines: list[str] = []

    result = plugins.install(spec, chosen, log=lines.append)

    home = spec.root / "Plugins"
    assert result.ok and result.step == "plugins" and result.error == ""
    assert result.created == [str(home), str(home / "Unreal-MCP-main"), str(home / "YSTagUsage")]
    assert tree(home / "Unreal-MCP-main") == MCP_COPIED and tree(home / "YSTagUsage") == TAG_COPIED
    assert not [path for path in home.rglob("*") if path.name == ".git" or path.name.lower().endswith(".zip")]
    assert (home / "Unreal-MCP-main" / "Python" / "server.py").read_bytes() == b"print(1)"
    made = json.loads(spec.uproject.read_text(encoding="utf-8"))
    assert made["Plugins"] == [MCP_ENTRY, TAG_ENTRY]
    assert made["EngineAssociation"] == "5.7" and made["Modules"][0]["Name"] == "Sample"
    assert snapshot(root) == before  # the source is only read
    assert result.tail == lines and len(lines) == 3
    assert "Unreal MCP" in lines[0] and "Plugins/Unreal-MCP-main" in lines[0] and "파일 5개" in lines[0]
    assert "UnrealMCP" in lines[2] and "YSTagUsage" in lines[2]


def test_install_with_nothing_chosen_makes_nothing(tmp_path):
    spec = project(tmp_path)
    before = spec.uproject.read_bytes()
    lines: list[str] = []
    result = plugins.install(spec, [], log=lines.append)
    assert result.ok and result.created == [] and result.tail == lines and len(lines) == 1
    assert not (spec.root / "Plugins").exists() and spec.uproject.read_bytes() == before


def test_install_fails_when_a_source_folder_is_not_there(tmp_path):
    spec, root = project(tmp_path), mnys(tmp_path)
    chosen = plugins.select(plugins.catalog(ENTRIES, root), ["unreal_mcp", "ys_tag_usage"])
    (root / "Plugins" / "YSTagUsage").rename(root / "Plugins" / "Moved")  # gone between the check and the step
    before = spec.uproject.read_bytes()
    lines: list[str] = []

    result = plugins.install(spec, chosen, log=lines.append)

    home = spec.root / "Plugins"
    assert not result.ok and "YSTagUsage" in result.error and str(root / "Plugins" / "YSTagUsage") in result.error
    assert result.created == [str(home), str(home / "Unreal-MCP-main")]  # what a cleanup has to take away
    assert tree(home / "Unreal-MCP-main") == MCP_COPIED and not (home / "YSTagUsage").exists()
    assert spec.uproject.read_bytes() == before  # written only after every copy went well
    assert result.tail == lines and lines[-1] == result.error

    unset = plugins.install(project_named(tmp_path, "Other"), plugins.catalog(ENTRIES, ""))
    assert not unset.ok and "studio_plugin_root" in unset.error
    assert unset.created == [str(tmp_path / "Projects" / "Other" / "Plugins")]


def project_named(tmp_path, name: str) -> creator.Spec:
    spec = creator.Spec(name, "5.7", tmp_path / "UE_5.7", tmp_path / "Projects")
    assert creator.folder(spec).ok
    return spec


def test_install_does_not_touch_a_folder_that_is_there(tmp_path):
    spec, root = project(tmp_path), mnys(tmp_path)
    kept = write(spec.root / "Plugins" / "Unreal-MCP-main", {"Mine.txt": "kept"})
    before = spec.uproject.read_bytes()
    result = plugins.install(spec, plugins.catalog(ENTRIES, root))
    assert not result.ok and str(kept) in result.error and result.created == []
    assert tree(spec.root / "Plugins") == ["Unreal-MCP-main/Mine.txt"] and spec.uproject.read_bytes() == before


def test_install_fails_on_a_copy_without_a_uplugin(tmp_path):
    spec = project(tmp_path)
    root = mnys(tmp_path, mcp={"README.md": "nothing to load", "Old.zip": "PK"})
    before = spec.uproject.read_bytes()
    result = plugins.install(spec, plugins.catalog(ENTRIES, root)[:1])
    home = spec.root / "Plugins"
    assert not result.ok and ".uplugin" in result.error
    assert result.created == [str(home), str(home / "Unreal-MCP-main")] and spec.uproject.read_bytes() == before


def test_install_fails_before_copying_when_the_uproject_cannot_be_read(tmp_path):
    spec, root = project(tmp_path), mnys(tmp_path)
    spec.uproject.write_bytes(b'{"Plugins": {}}')
    result = plugins.install(spec, plugins.catalog(ENTRIES, root))
    assert not result.ok and "Plugins" in result.error and result.created == []
    assert not (spec.root / "Plugins").exists()

    spec.uproject.unlink()
    result = plugins.install(spec, plugins.catalog(ENTRIES, root))
    assert not result.ok and result.created == [] and not (spec.root / "Plugins").exists()


def test_install_refuses_a_source_the_project_is_inside(tmp_path):
    spec = project(tmp_path)
    around = plugins.Plugin("around", "Around", tmp_path / "Projects", "Around")
    result = plugins.install(spec, [around])
    assert not result.ok and result.created == [str(spec.root / "Plugins")]
    assert not (spec.root / "Plugins" / "Around").exists()


def test_the_name_registered_is_the_uplugin_files_not_the_folders(tmp_path):
    spec = project(tmp_path)
    nested = {f"UnrealMCP/{relative}": text for relative, text in MCP_FILES.items()}
    nested |= {"README.md": "top", "UnrealMCP/Extra/Inner.uplugin": "{}"}  # below a plugin: not looked into
    root = mnys(tmp_path, mcp=nested)
    entries = [dict(ENTRIES[0], folder="McpCopy")]

    result = plugins.install(spec, plugins.catalog(entries, root))

    target = spec.root / "Plugins" / "McpCopy"
    assert result.ok and result.created == [str(spec.root / "Plugins"), str(target)]
    assert (target / "UnrealMCP" / "UnrealMCP.uplugin").is_file() and not (target / "UnrealMCP" / ".git").exists()
    assert json.loads(spec.uproject.read_text(encoding="utf-8"))["Plugins"] == [MCP_ENTRY]


def test_install_leaves_the_copy_writable(tmp_path):
    """`copy2` brings the read-only mark of a source along. The build has to write over a copy and what clears
    a failed job to delete one, so the copy is unlocked — the half made one of a failure as well."""
    spec, root = project(tmp_path), mnys(tmp_path)
    source = root / "Plugins" / "Unreal-MCP-main"
    bare = write(tmp_path / "Bare", {"README.md": "nothing to load"})
    locked = [source / "Resources" / "Icon128.png", source / "Source" / "UnrealMCP", bare / "README.md"]
    for path in locked:
        os.chmod(path, stat.S_IREAD | (stat.S_IEXEC if path.is_dir() else 0))
    try:
        lines: list[str] = []
        result = plugins.install(spec, plugins.catalog(ENTRIES, root)[:1], log=lines.append)
        target = spec.root / "Plugins" / "Unreal-MCP-main"
        assert result.ok and tree(target) == MCP_COPIED and "읽기 전용" in lines[0]
        assert all(path.stat().st_mode & stat.S_IWRITE for path in [target, *target.rglob("*")])
        assert not any(path.stat().st_mode & stat.S_IWRITE for path in locked)  # the source is only read
        shutil.rmtree(target)
        assert not target.exists()

        failed = plugins.install(project_named(tmp_path, "Other"), [plugins.Plugin("bare", "Bare", bare, "Bare")])
        half = Path(failed.created[-1])
        assert not failed.ok and half == tmp_path / "Projects" / "Other" / "Plugins" / "Bare"
        assert (half / "README.md").stat().st_mode & stat.S_IWRITE
        shutil.rmtree(half)
        assert not half.exists()
    finally:
        for path in locked:
            os.chmod(path, stat.S_IMODE(path.stat().st_mode) | stat.S_IWRITE)


# --- the .uproject ------------------------------------------------------------------------------------------
def test_register_changes_nothing_but_the_plugins_of_the_template(tmp_path):
    spec = project(tmp_path)
    original = spec.uproject.read_bytes()
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    listed = b'"Plugins": [\n\t\t{\n\t\t\t"Name": "UnrealMCP",\n\t\t\t"Enabled": true\n\t\t}\n\t]'.replace(b"\n", newline)
    assert original.count(b'"Plugins": []') == 1

    assert plugins.register(spec.uproject, ["UnrealMCP"]) == ["UnrealMCP"]

    assert spec.uproject.read_bytes() == original.replace(b'"Plugins": []', listed)


def test_register_keeps_the_line_ends_the_bom_and_what_is_listed(tmp_path):
    file = tmp_path / "Old.uproject"
    text = ('{\r\n\t"FileVersion": 3,\r\n\t"Description": "한글 설명",\r\n\t"Plugins": [\r\n\t\t{\r\n'
            '\t\t\t"Name": "unrealmcp",\r\n\t\t\t"Enabled": false,\r\n\t\t\t"TargetAllowList": [\r\n\t\t\t\t"Editor"\r\n'
            '\t\t\t]\r\n\t\t}\r\n\t]\r\n}')
    file.write_bytes(codecs.BOM_UTF8 + text.encode("utf-8"))

    assert plugins.register(file, ["UnrealMCP", "YSTagUsage"]) == ["YSTagUsage"]

    raw = file.read_bytes()
    written = raw.decode("utf-8-sig")
    assert raw.startswith(codecs.BOM_UTF8) and "한글 설명" in written
    assert "\n" not in written.replace("\r\n", "") and not written.endswith("\n")  # no last line end: none added
    assert json.loads(written)["Plugins"] == [
        {"Name": "unrealmcp", "Enabled": True, "TargetAllowList": ["Editor"]}, TAG_ENTRY]

    assert plugins.register(file, ["UNREALMCP", "YSTagUsage"]) == [] and file.read_bytes() == raw


def test_register_does_not_write_when_nothing_changes(tmp_path):
    file = tmp_path / "Loose.uproject"
    loose = b'{"FileVersion": 3,   "Plugins": [ {"Name": "UnrealMCP", "Enabled": true} ]}\n'  # not our layout
    file.write_bytes(loose)
    assert plugins.register(file, ["UnrealMCP"]) == [] and file.read_bytes() == loose
    assert plugins.register(file, []) == [] and file.read_bytes() == loose

    file.write_bytes(b'{"FileVersion": 3}\n')  # no Plugins: made when there is one to put in
    assert plugins.register(file, ["UnrealMCP"]) == ["UnrealMCP"]
    assert json.loads(file.read_text(encoding="utf-8")) == {"FileVersion": 3, "Plugins": [MCP_ENTRY]}
    assert file.read_bytes().endswith(b"}\n")


@pytest.mark.parametrize("content", [b'{"Plugins": "UnrealMCP"}', b'["Plugins"]', b"not json", b"\xff\xfe{"])
def test_register_refuses_what_is_not_a_uproject(tmp_path, content):
    file = tmp_path / "Broken.uproject"
    file.write_bytes(content)
    with pytest.raises(ValueError):
        plugins.register(file, ["UnrealMCP"])
    assert file.read_bytes() == content
    with pytest.raises(OSError):
        plugins.register(tmp_path / "Missing.uproject", ["UnrealMCP"])


# --- the documents ------------------------------------------------------------------------------------------
def test_the_documents_say_what_the_code_does():
    architecture = (DESIGN / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for reason in plugins.REASONS:
        assert f"`{reason}`" in architecture, reason
    for word in ("`relay_agent/studio/plugins.py`", "`Config.studio_plugins`", "`Config.studio_plugin_root`",
                 "`unreal_mcp`", "`ys_tag_usage`"):
        assert word in architecture, word
    assert "M-4 플러그인 폴더명" in (DESIGN / "VERIFICATION.md").read_text(encoding="utf-8")


def test_the_documents_say_how_the_port_is_given():
    architecture = (DESIGN / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for word in ("`allocate(taken, is_open=)`", "`step(spec, chosen, taken=, is_open=, run=, log=)`", "`PluginsResult`",
                 "`write_ini_port`", "`write_mcp_json`", "`write_guards`", "`relay_agent/studio/defaults/`",
                 str(plugins.MNYS_PORT), str(plugins.FIRST_PORT), " ".join(MCP_SERVER.args)):
        assert word in architecture, word
    verification = (DESIGN / "VERIFICATION.md").read_text(encoding="utf-8")
    for line in ("- [ ] M-4 배정 포트로 `health_check` 응답", "- [ ] M-4 태그 사용처 탭", "- [ ] M-4 MNYS 와 동시 실행"):
        assert line in verification, line


# --- the MCP port (M-4, the second half) ---------------------------------------------------------------------
MCP_SERVER = plugins.catalog(ENTRIES, None)[0].mcp
INI_REST = "[/Script/EngineSettings.GameMapsSettings]\nGameDefaultMap=/Game/Maps/Main\n+Paths=A\n+Paths=B\n"


def never(port) -> bool:
    return False


def unasked(port) -> bool:
    raise AssertionError(f"no port is to be knocked on: {port}")


def ini_of(root: Path, content: bytes | None) -> Path:
    file = root / "Config" / "DefaultEngine.ini"
    if content is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    return file


def test_allocate_starts_after_mnys_and_passes_what_is_taken_or_open():
    assert (plugins.MNYS_PORT, plugins.FIRST_PORT) == (55557, 55558)
    assert plugins.allocate((), is_open=never) == 55558
    assert plugins.allocate([55557], is_open=never) == 55558
    asked = []

    def is_open(port):
        asked.append(port)
        return port in (55559, 55561)

    assert plugins.allocate([55558, None, 55560, "55562", True], is_open=is_open) == 55562
    assert asked == [55559, 55561, 55562]  # a taken port is not knocked on; what is not a number is no port
    assert plugins.allocate(range(55558, 55600), is_open=never) == 55600
    assert plugins.allocate((), is_open=lambda port: port < 55570) == 55570  # nothing registered, all open


def test_allocate_never_gives_the_port_of_mnys_and_fails_when_none_is_left():
    assert plugins.allocate((), is_open=never, first=55556) == 55556
    assert plugins.allocate([55556], is_open=never, first=55556) == 55558
    assert plugins.allocate((), is_open=never, first=65535) == 65535
    with pytest.raises(ValueError):
        plugins.allocate([65535], is_open=never, first=65535)
    with pytest.raises(ValueError):
        plugins.allocate((), is_open=lambda port: True, first=65530)
    with pytest.raises(ValueError):
        plugins.allocate((), is_open=never, first=55557, last=55557)


@pytest.mark.parametrize("mcp", ["uv", [], {}, {"command": ""}, {"args": ["run"]}, {"command": "uv", "args": "run"},
                                 {"command": "uv", "args": [1]}, {"command": "uv", "server": ""},
                                 {"command": "uv", "server": 3}])
def test_an_mcp_entry_that_is_wrong_is_an_error(tmp_path, mcp):
    with pytest.raises(ValueError):
        plugins.catalog([{"id": "a", "source": "Plugins/A", "mcp": mcp}], tmp_path)


def test_an_mcp_entry_may_leave_out_the_server_and_the_args(tmp_path):
    (bare,) = plugins.catalog([{"id": "a", "source": "Plugins/A", "mcp": {"command": "node"}}], tmp_path)
    assert bare.mcp == plugins.McpServer("unreal", "node", ())
    (none,) = plugins.catalog([{"id": "a", "source": "Plugins/A", "mcp": None}], tmp_path)
    assert none.mcp is None


# --- the ini ------------------------------------------------------------------------------------------------
def test_write_ini_port_adds_the_section_and_keeps_the_rest(tmp_path):
    """CRLF and a BOM, as the editor saves it: every other section, line and byte stays."""
    rest = (INI_REST + "\n[Other]\nPort=1\n").replace("\n", "\r\n")
    file = ini_of(tmp_path, codecs.BOM_UTF8 + rest.encode("utf-8"))

    assert plugins.write_ini_port(tmp_path, 55558) == []  # nothing was made: the file was there
    expected = codecs.BOM_UTF8 + (rest + "\r\n[UnrealMCP]\r\nPort=55558\r\n").encode("utf-8")
    assert file.read_bytes() == expected and importer.ini_port(tmp_path) == 55558

    os.chmod(file, stat.S_IREAD)  # the same port again: the file is not written
    try:
        assert plugins.write_ini_port(tmp_path, 55558) == [] and file.read_bytes() == expected
    finally:
        os.chmod(file, stat.S_IREAD | stat.S_IWRITE)

    assert plugins.write_ini_port(tmp_path, 55560) == []  # another port: that line only
    assert file.read_bytes() == expected.replace(b"Port=55558", b"Port=55560")
    assert importer.ini_port(tmp_path) == 55560


@pytest.mark.parametrize("before, after", [
    ("[UnrealMCP]\nPort=55557\nHost=127.0.0.1\n\n[B]\nPort=9\n", "[UnrealMCP]\nPort=55558\nHost=127.0.0.1\n\n[B]\nPort=9\n"),
    ("[A]\nX=1\n[unrealmcp]\nHost=h\n", "[A]\nX=1\n[unrealmcp]\nPort=55558\nHost=h\n"),  # a section without one
    ("[UnrealMCP]\n;Port=1\nPort=2\n  port = 3\n[A]\nY=2", "[UnrealMCP]\n;Port=1\nPort=2\n  port = 55558\n[A]\nY=2"),
    ("[UnrealMCP]\nPort=2\n[A]\n[UnrealMCP]\nHost=h\n", "[UnrealMCP]\nPort=55558\n[A]\n[UnrealMCP]\nHost=h\n"),
    ("[A]\n[UnrealMCP]\n[B]\nPort=9\n[UnrealMCP]\n", "[A]\n[UnrealMCP]\n[B]\nPort=9\n[UnrealMCP]\nPort=55558\n"),
    ("[A]\nX=1\n[UnrealMCP]", "[A]\nX=1\n[UnrealMCP]\nPort=55558\n"),  # the heading is the last line, unended
    ("[UnrealMCP]\nPort=abc\n", "[UnrealMCP]\nPort=55558\n"),  # what is there is no port
    ("[A]\nX=1", "[A]\nX=1\n\n[UnrealMCP]\nPort=55558\n"),  # no line end at the end
    ("[A]\nX=1\n\n", "[A]\nX=1\n\n[UnrealMCP]\nPort=55558\n"),  # a blank line is there already
    ("[A]\r\nX=1\r\n", "[A]\r\nX=1\r\n\r\n[UnrealMCP]\r\nPort=55558\r\n"),
    ("", "[UnrealMCP]\r\nPort=55558\r\n"),
    # a comment at the end of the line is gone: importer.ini_port reads no port from a value with one in it
    ("[UnrealMCP]\nPort=55557 ; MNYS\nHost=h\n", "[UnrealMCP]\nPort=55558\nHost=h\n"),
    ("[UnrealMCP]\r\nPort = 55557\t; old\r\n", "[UnrealMCP]\r\nPort = 55558\r\n"),
])
def test_write_ini_port_changes_one_line_or_adds_one(tmp_path, before, after):
    file = ini_of(tmp_path, before.encode("utf-8"))
    assert plugins.write_ini_port(tmp_path, 55558) == []
    assert file.read_bytes() == after.encode("utf-8")
    assert importer.ini_port(tmp_path) == 55558
    assert plugins.write_ini_port(tmp_path, 55558) == [] and file.read_bytes() == after.encode("utf-8")


@pytest.mark.parametrize("mark, encoding", [(codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")])
def test_write_ini_port_keeps_utf_16(tmp_path, mark, encoding):
    rest = INI_REST.replace("\n", "\r\n") + "[한글]\r\nName=이름\r\n"
    file = ini_of(tmp_path, mark + rest.encode(encoding))
    assert plugins.write_ini_port(tmp_path, 55558) == []
    raw = file.read_bytes()
    assert raw.startswith(mark) and raw[2:].decode(encoding) == rest + "\r\n[UnrealMCP]\r\nPort=55558\r\n"
    assert importer.ini_port(tmp_path) == 55558


def test_write_ini_port_makes_a_file_that_is_not_there(tmp_path):
    file = ini_of(tmp_path, None)
    assert plugins.write_ini_port(tmp_path, 55558) == [file.parent, file]
    assert file.read_bytes() == b"[UnrealMCP]\r\nPort=55558\r\n" and importer.ini_port(tmp_path) == 55558
    other = tmp_path / "Other"
    (other / "Config").mkdir(parents=True)
    assert plugins.write_ini_port(other, 55559) == [ini_of(other, None)] and importer.ini_port(other) == 55559


@pytest.mark.parametrize("content", [b"[A]\n\xe9\xe9=1\n", b"\xff\xfe[\x00A"])
def test_write_ini_port_refuses_what_it_cannot_read(tmp_path, content):
    file = ini_of(tmp_path, content)
    with pytest.raises(ValueError):
        plugins.write_ini_port(tmp_path, 55558)
    assert file.read_bytes() == content


@pytest.mark.parametrize("port", [0, 65536, -1, "55558", 55558.0, True, None])
def test_what_is_not_a_port_is_not_written(tmp_path, port):
    file = ini_of(tmp_path, b"[A]\nX=1\n")
    with pytest.raises(ValueError):
        plugins.write_ini_port(tmp_path, port)
    with pytest.raises(ValueError):
        plugins.write_mcp_json(tmp_path, MCP_SERVER, port)
    assert file.read_bytes() == b"[A]\nX=1\n" and not (tmp_path / ".mcp.json").exists()


# --- .mcp.json ----------------------------------------------------------------------------------------------
def test_write_mcp_json_makes_the_file_with_the_port_fixed(tmp_path):
    server = plugins.McpServer("unreal", "{project}/run.cmd", ("--directory", "{plugin}/Python", "{other}"))
    values = {"plugin": "C:/P/Plugins/M", "project": "C:/P"}
    file = tmp_path / ".mcp.json"
    assert plugins.write_mcp_json(tmp_path, server, 55558, values) == [file]
    data = {"mcpServers": {"unreal": {"command": "C:/P/run.cmd", "args": ["--directory", "C:/P/Plugins/M/Python", "{other}"],
                                      "env": {"UNREAL_MCP_PORT": "55558"}}}}
    assert file.read_bytes() == (json.dumps(data, indent=2) + "\n").encode("utf-8")
    assert importer.mcp_json_port(tmp_path) == 55558
    assert plugins.write_mcp_json(tmp_path, server, 55558, values) == []  # there already: not made, not changed
    assert file.read_bytes() == (json.dumps(data, indent=2) + "\n").encode("utf-8")


def test_write_mcp_json_keeps_the_other_servers_the_other_keys_and_the_env(tmp_path):
    server = plugins.McpServer("unreal", "uv", ("run", "{plugin}/server.py"))
    katae = {"command": "python", "args": ["katae.py"], "env": {"KATAE_URL": "http://pc"}}
    before = {"mcpServers": {"katae": katae, "unreal": {"command": "old", "type": "stdio",
                                                        "env": {"UNREAL_MCP_PORT": "55557", "KEEP": "1"}}}, "other": [1]}
    file = tmp_path / ".mcp.json"
    file.write_bytes(codecs.BOM_UTF8 + json.dumps(before, indent=4).replace("\n", "\r\n").encode("utf-8"))

    assert plugins.write_mcp_json(tmp_path, server, 55558, {"plugin": "C:/P/Plugins/M"}) == []

    raw = file.read_bytes()
    assert raw.startswith(codecs.BOM_UTF8) and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert not raw.endswith(b"\n")  # it had no last line end
    data = json.loads(raw.decode("utf-8-sig"))
    assert data == {"mcpServers": {"katae": katae, "unreal": {
        "command": "uv", "args": ["run", "C:/P/Plugins/M/server.py"], "type": "stdio",
        "env": {"UNREAL_MCP_PORT": "55558", "KEEP": "1"}}}, "other": [1]}
    assert list(data["mcpServers"]) == ["katae", "unreal"]
    assert raw.decode("utf-8-sig") == json.dumps(data, indent=4).replace("\n", "\r\n")  # 4 spaces, as it was
    assert importer.mcp_json_port(tmp_path) == 55558

    os.chmod(file, stat.S_IREAD)  # the same again: the file is not written
    try:
        assert plugins.write_mcp_json(tmp_path, server, 55558, {"plugin": "C:/P/Plugins/M"}) == []
    finally:
        os.chmod(file, stat.S_IREAD | stat.S_IWRITE)
    assert file.read_bytes() == raw


@pytest.mark.parametrize("was, kept", [("\t", "\t"), (4, 4), (3, 3), (2, 2), (None, 2)])
def test_write_mcp_json_keeps_the_indentation_of_a_file_that_is_there(tmp_path, was, kept):
    """The unit is that of the line under the opening `{`; a file on one line has none and gets 2 spaces."""
    before = {"mcpServers": {"katae": {"command": "python", "args": ["katae.py"]}}, "note": "한글"}
    file = tmp_path / ".mcp.json"
    file.write_bytes((json.dumps(before, indent=was, ensure_ascii=False) + "\n").encode("utf-8"))

    assert plugins.write_mcp_json(tmp_path, MCP_SERVER, 55558) == []

    raw = file.read_bytes()
    text = raw.decode("utf-8")
    data = json.loads(text)
    assert text == json.dumps(data, indent=kept, ensure_ascii=False) + "\n"
    assert data["note"] == "한글" and list(data["mcpServers"]) == ["katae", "unreal"]
    assert importer.mcp_json_port(tmp_path) == 55558
    assert plugins.write_mcp_json(tmp_path, MCP_SERVER, 55558) == [] and file.read_bytes() == raw


def test_write_mcp_json_adds_the_server_to_a_file_without_it(tmp_path):
    file = tmp_path / ".mcp.json"
    file.write_bytes(b'{"note": "mine"}\n')
    assert plugins.write_mcp_json(tmp_path, MCP_SERVER, 55558) == []
    data = json.loads(file.read_text(encoding="utf-8"))
    assert data["note"] == "mine" and data["mcpServers"]["unreal"]["env"] == {"UNREAL_MCP_PORT": "55558"}
    assert file.read_bytes().endswith(b"}\n") and b"\r" not in file.read_bytes()


@pytest.mark.parametrize("content", [b"[]", b"not json", b'{"mcpServers": []}', b'{"mcpServers": null}', b"\xff\xfe{"])
def test_write_mcp_json_refuses_what_is_not_an_mcp_json(tmp_path, content):
    file = tmp_path / ".mcp.json"
    file.write_bytes(content)
    with pytest.raises(ValueError):
        plugins.write_mcp_json(tmp_path, MCP_SERVER, 55558)
    assert file.read_bytes() == content


# --- the guards ---------------------------------------------------------------------------------------------
def test_write_guards_puts_the_default_and_leaves_one_that_is_there(tmp_path):
    file = tmp_path / ".claude" / "mcp_guards.json"
    assert plugins.write_guards(tmp_path) == [file.parent, file]
    assert file.read_bytes() == plugins.GUARDS_DEFAULT.read_bytes()
    file.write_bytes(b'{"mine": true}')
    assert plugins.write_guards(tmp_path) == [] and file.read_bytes() == b'{"mine": true}'
    other = tmp_path / "Other"
    (other / ".claude").mkdir(parents=True)
    assert plugins.write_guards(other) == [other / ".claude" / "mcp_guards.json"]


def test_the_default_guards_are_json_and_package_data():
    """The default is a file of the repository beside the module, not of the project skeleton: a project without
    the MCP plugin does not get it."""
    assert isinstance(json.loads(plugins.GUARDS_DEFAULT.read_text(encoding="utf-8")), dict)
    assert creator.TEMPLATE_DIR not in plugins.GUARDS_DEFAULT.parents
    root = Path(plugins.__file__).resolve().parents[2]
    tool = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    package = plugins.GUARDS_DEFAULT.parent.parent
    listed = {p for pattern in tool["package-data"]["relay_agent.studio"] for p in package.glob(pattern)}
    assert plugins.GUARDS_DEFAULT in listed


# --- the step -----------------------------------------------------------------------------------------------
def test_step_copies_registers_and_writes_the_port_where_the_importer_reads_it(tmp_path):
    spec, lines = project(tmp_path), []
    listed = plugins.catalog(ENTRIES, mnys(tmp_path))
    ini = spec.root / "Config" / "DefaultEngine.ini"
    before = ini.read_bytes()
    assert "mcp" in importer.detect(spec.root, [])["missing"]  # the skeleton names no port

    result = plugins.step(spec, listed, taken=[55557, 55558, None], is_open=lambda port: port == 55559,
                          log=lines.append)

    assert isinstance(result, creator.StepResult) and result.ok and result.step == "plugins"
    assert result.mcp_port == 55560  # 55558 is another project's, on 55559 something listens
    after = ini.read_bytes()
    assert after.startswith(before) and after[len(before):].split() == [b"[UnrealMCP]", b"Port=55560"]
    assert (b"\r\n" in after[len(before):]) == (b"\r\n" in before)  # the line ends of the skeleton
    assert importer.ini_port(spec.root) == importer.mcp_json_port(spec.root) == 55560
    detected = importer.detect(spec.root, [])
    assert detected["mcp"] == {"port": 55560, "ini": 55560, "mcp_json": 55560, "in_use_by": None}
    assert "mcp" not in detected["missing"]

    home = (spec.root / "Plugins" / "Unreal-MCP-main").absolute().as_posix()
    written = (spec.root / ".mcp.json").read_text(encoding="utf-8")
    assert "{" + "plugin" not in written and "\\" not in written
    assert json.loads(written) == {"mcpServers": {"unreal": {
        "command": MCP_SERVER.command, "args": [arg.replace("{plugin}", home) for arg in MCP_SERVER.args],
        "env": {"UNREAL_MCP_PORT": "55560"}}}}
    assert f"{home}/Python" in json.loads(written)["mcpServers"]["unreal"]["args"]
    guards = spec.root / ".claude" / "mcp_guards.json"
    assert guards.read_bytes() == plugins.GUARDS_DEFAULT.read_bytes()

    home = spec.root / "Plugins"
    assert result.created == [str(home), str(home / "Unreal-MCP-main"), str(home / "YSTagUsage"),
                              str(spec.root / ".mcp.json"), str(guards.parent), str(guards)]
    assert tree(home / "Unreal-MCP-main") == MCP_COPIED  # the first part is `install`
    assert json.loads(spec.uproject.read_text(encoding="utf-8"))["Plugins"] == [MCP_ENTRY, TAG_ENTRY]
    assert result.tail == lines and "55560" in lines[-1]


def test_two_projects_are_given_ports_that_differ(tmp_path):
    one, listed = project(tmp_path), plugins.catalog(ENTRIES, mnys(tmp_path))
    two, three = project_named(tmp_path, "Other"), project_named(tmp_path, "Third")
    registered = [plugins.MNYS_PORT]  # what the caller knows: the ports of the projects there are

    first = plugins.step(one, listed, taken=registered, is_open=never)
    registered.append(first.mcp_port)
    second = plugins.step(two, listed, taken=registered, is_open=never)
    # not registered yet, but its editor is up: an open port is passed by as well
    third = plugins.step(three, listed, taken=[plugins.MNYS_PORT], is_open=lambda port: port in (55558, 55559))

    assert (first.mcp_port, second.mcp_port, third.mcp_port) == (55558, 55559, 55560)
    for spec, result in ((one, first), (two, second), (three, third)):
        assert result.ok and importer.detect(spec.root, [])["mcp"]["port"] == result.mcp_port


def test_step_without_an_mcp_plugin_gives_no_port_and_writes_none_of_the_files(tmp_path):
    spec, listed = project(tmp_path), plugins.catalog(ENTRIES, mnys(tmp_path))
    ini = (spec.root / "Config" / "DefaultEngine.ini").read_bytes()

    result = plugins.step(spec, [listed[1]], taken=[55557], is_open=unasked)

    assert result.ok and result.mcp_port is None
    assert result.created == [str(spec.root / "Plugins"), str(spec.root / "Plugins" / "YSTagUsage")]
    assert not (spec.root / ".mcp.json").exists() and not (spec.root / ".claude").exists()
    assert (spec.root / "Config" / "DefaultEngine.ini").read_bytes() == ini
    assert importer.detect(spec.root, [])["mcp"]["port"] is None

    other = project_named(tmp_path, "Other")
    before = snapshot(other.root)
    nothing = plugins.step(other, [], is_open=unasked)
    assert nothing.ok and nothing.mcp_port is None and nothing.created == [] and snapshot(other.root) == before


def test_step_stops_at_a_copy_that_failed(tmp_path):
    spec, listed = project(tmp_path), plugins.catalog(ENTRIES, mnys(tmp_path))
    write(spec.root / "Plugins" / "Unreal-MCP-main", {"Mine.txt": "kept"})
    before = snapshot(spec.root)

    result = plugins.step(spec, listed, is_open=unasked)

    assert not result.ok and result.mcp_port is None and result.error.startswith("플러그인을 넣지 못했다")
    assert result.created == [] and snapshot(spec.root) == before
    assert result.tail == [result.error]


def test_step_fails_when_the_settings_cannot_be_written(tmp_path):
    spec, listed = project(tmp_path), plugins.catalog(ENTRIES, mnys(tmp_path))
    (spec.root / ".mcp.json").write_bytes(b"[]")

    result = plugins.step(spec, listed, is_open=never)

    assert not result.ok and result.mcp_port is None and result.error.startswith("MCP 설정을 쓰지 못했다")
    assert (spec.root / ".mcp.json").read_bytes() == b"[]"
    home = spec.root / "Plugins"  # what was copied is still told, and nothing is deleted
    assert result.created == [str(home), str(home / "Unreal-MCP-main"), str(home / "YSTagUsage")]
    assert (home / "Unreal-MCP-main").is_dir() and result.tail[-1] == result.error


def test_step_fails_when_the_port_does_not_read_back(tmp_path):
    """Another server of `.mcp.json` names a port first: the importer would read that one, not ours."""
    spec, listed = project(tmp_path), plugins.catalog(ENTRIES, mnys(tmp_path))
    stale = {"mcpServers": {"old": {"command": "x", "env": {"UNREAL_MCP_PORT": "55557"}}}}
    (spec.root / ".mcp.json").write_text(json.dumps(stale), encoding="utf-8")

    result = plugins.step(spec, listed, is_open=never)

    assert not result.ok and result.mcp_port is None
    assert "55558" in result.error and "55557" in result.error
    assert json.loads((spec.root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["old"] == stale["mcpServers"]["old"]


def test_step_fails_when_no_port_is_left(tmp_path):
    spec, listed = project(tmp_path), plugins.catalog(ENTRIES, mnys(tmp_path))
    result = plugins.step(spec, listed, is_open=lambda port: True)
    assert not result.ok and result.mcp_port is None and "포트" in result.error
    assert not (spec.root / ".mcp.json").exists() and importer.ini_port(spec.root) is None
