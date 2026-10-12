"""The plugins of a new UE project (M-4): which ones may be chosen, their folders copied under the project's
`Plugins/`, their names put in the `.uproject`, and — when one of them is an MCP plugin — the port its editor is
given, written where the plugin (`[UnrealMCP]` of `Config/DefaultEngine.ini`) and Claude Code (`.mcp.json`) read
it, with the default `.claude/mcp_guards.json`.

Contract: design/studio/ARCHITECTURE.md §6.4 (`POST /studio/projects/create`, `plugins[]`) and §7.4 (`job.step`:
… → `plugins` → …). The route, the job and its events are M-8's, and so is keeping two
jobs that run at once from being given the same port: `step` knows only the `taken` it is handed.

Nothing here imports api, cli or server. The list is `Config.studio_plugins` with `Config.studio_plugin_root`
(`catalog`). `choices` is what the making screen shows, `select` what a request is checked with — it raises
`creator.Refused` with the field `plugins` — and `step` is the step: called like the steps of creator.py, it
answers with a `PluginsResult` (a `StepResult` with `mcp_port`) instead of raising, and what it made is in
`created` when it failed as well. Its first part is `install` (the copy and the `.uproject`), its second
`allocate` · `write_ini_port` · `write_mcp_json` · `write_guards`, and it reads the port back with importer.py:
what was written is what a later import of the folder finds. The port and the three files are only for a chosen
plugin whose entry has `mcp`. A plugin's source folder is only read. Nothing is ever deleted here. A copy is left writable — the read-only
mark of a source does not come along.

Whether the MCP plugin's folder must be named `UnrealMCP` could not be confirmed: the source in MNYS could not be
read from a relay stage (§6.4, design/studio/VERIFICATION.md). So a plugin keeps the name of its source folder
unless its entry gives a `folder`, and the name put in the `.uproject` is that of the `.uplugin` file found in
the copy — the engine knows a plugin by that file's name, not by its folder's.

Two more things are tentative for the same reason (VERIFICATION.md): the server entry put in `.mcp.json` — the
`mcp` of the plugin's entry in the settings, written from general knowledge and to be corrected there, not here
— and the default guards, `defaults/mcp_guards.json`, which hold no rule: MNYS's file could not be read, and no
rule was made up.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import shutil
import stat
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import importer, machine
from .creator import TAIL_LINES, Log, Refused, Run, Spec, StepResult, _quiet, run_process

SKIP_NAMES = (".git",)  # a folder, or the file a submodule or a worktree has in its place
SKIP_SUFFIXES = (".zip",)
REASONS = ("plugin_unknown", "plugin_source_missing")
ROOT_SETTING = "studio_plugin_root"
MNYS_PORT = 55557  # MNYS's editor listens here; it is never given to another project
FIRST_PORT, LAST_PORT = 55558, 65535
DEFAULT_SERVER = "unreal"  # the name of the server in `.mcp.json` when the settings give none
INI_HEADER = "[UnrealMCP]"  # as it is written; importer.INI_SECTION is the same in lower case
INI_MARKS = ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"), (codecs.BOM_UTF8, "utf-8"))
GUARDS = (".claude", "mcp_guards.json")
GUARDS_DEFAULT = Path(__file__).with_name("defaults") / "mcp_guards.json"


@dataclass(frozen=True)
class McpServer:
    """The `mcp` of an entry: the server put in the project's `.mcp.json`. `{plugin}` and `{project}` in
    `command` and `args` stand for the copied plugin's folder and the project's, as absolute paths."""
    name: str
    command: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plugin:
    """One entry of `Config.studio_plugins`. `source` is None when the entry's path is not absolute and no root
    is set. `engines`: the versions it is confirmed on; none = all. `mcp`: None unless it is an MCP plugin —
    the project that is given one gets a port (`step`)."""
    id: str
    name: str
    source: Path | None
    folder: str
    engines: tuple[str, ...] = ()
    mcp: McpServer | None = None


@dataclass
class PluginsResult(StepResult):
    """What `step` did: a `StepResult` with the port the project was given, None when no MCP plugin was chosen
    or the step failed."""
    mcp_port: int | None = None


# --- the list -----------------------------------------------------------------------------------------------
def catalog(entries: Iterable[Any], root: Path | str | None) -> list[Plugin]:
    """`Config.studio_plugins` as plugins, in the order given. A `source` that is not absolute is taken from
    `root` (`Config.studio_plugin_root`). `ValueError` — the settings are wrong — for an entry without `id` or
    `source`, an `id` given twice, `engines` that is not a list, a `folder` that is not one name, and an `mcp`
    that is not `{server, command, args[]}` (`server` may be left out)."""
    base = Path(root) if root else None
    listed: list[Plugin] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"studio_plugins 의 항목은 사전이어야 한다: {entry!r}")
        ident, source = entry.get("id"), entry.get("source")
        if not isinstance(ident, str) or not ident:
            raise ValueError(f"studio_plugins 의 항목에 id 가 없다: {entry!r}")
        if not isinstance(source, str) or not source:
            raise ValueError(f"studio_plugins 의 {ident} 에 source 가 없다")
        if any(known.id == ident for known in listed):
            raise ValueError(f"studio_plugins 에 id 가 두 번 나온다: {ident}")
        folder = entry.get("folder") or Path(source).name
        if not isinstance(folder, str) or not folder or folder in (".", "..") or any(c in folder for c in "/\\:"):
            raise ValueError(f"studio_plugins 의 {ident} 의 folder 는 폴더 이름 하나여야 한다: {folder!r}")
        engines = entry.get("engines") or []
        if not isinstance(engines, (list, tuple)):
            raise ValueError(f"studio_plugins 의 {ident} 의 engines 는 목록이어야 한다: {engines!r}")
        path = Path(source)
        if not path.is_absolute():
            path = base / path if base else None
        if path is not None and not path.is_absolute():
            path = None
        listed.append(Plugin(ident, str(entry.get("name") or ident), path, folder, tuple(str(e) for e in engines),
                             _server(ident, entry.get("mcp"))))
    return listed


def _server(ident: str, value: Any) -> McpServer | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"studio_plugins 의 {ident} 의 mcp 는 사전이어야 한다: {value!r}")
    name, command, args = value.get("server", DEFAULT_SERVER), value.get("command"), value.get("args", [])
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"studio_plugins 의 {ident} 의 mcp.server 는 서버 이름이어야 한다: {name!r}")
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"studio_plugins 의 {ident} 의 mcp 에 command 가 없다")
    if not isinstance(args, (list, tuple)) or not all(isinstance(arg, str) for arg in args):
        raise ValueError(f"studio_plugins 의 {ident} 의 mcp.args 는 문자열의 목록이어야 한다: {args!r}")
    return McpServer(name, command, tuple(args))


def _there(plugin: Plugin) -> bool:
    return plugin.source is not None and plugin.source.is_dir()


def _confirmed(plugin: Plugin, engine_version: str) -> bool:
    return not plugin.engines or engine_version in plugin.engines


def choices(plugins: Sequence[Plugin], engine_version: str) -> list[dict]:
    """What the making screen lists for an engine: `{id, name, checked, warning, available}` for each plugin.
    `available`: its source folder is there. `checked` — chosen unless the person says otherwise — when it is
    available and confirmed on that engine. `warning` is '' when there is nothing to say; a plugin that is not
    confirmed on the engine may still be chosen, one that is not available may not (`select`)."""
    listed = []
    for plugin in plugins:
        available, confirmed, warnings = _there(plugin), _confirmed(plugin, engine_version), []
        if not confirmed:
            warnings.append(f"UE {' · '.join(plugin.engines)} 에서만 확인됐다 (고른 엔진: {engine_version}). "
                            f"기본으로는 넣지 않는다")
        if plugin.source is None:
            warnings.append(f"원본 위치를 모른다 — relay.config.local.yaml 에 {ROOT_SETTING} 를 적는다")
        elif not available:
            warnings.append(f"원본 폴더가 없다: {plugin.source}")
        listed.append({"id": plugin.id, "name": plugin.name, "checked": available and confirmed,
                       "warning": " / ".join(warnings), "available": available})
    return listed


def select(plugins: Sequence[Plugin], ids: Any) -> list[Plugin]:
    """The plugins a request names (`plugins[]`), in the list's order, each once. `Refused` with the field
    `plugins`: `plugin_unknown` for an id the list has not, `plugin_source_missing` for one whose source folder
    is not there. An engine it is not confirmed on is no reason to refuse — the screen has warned."""
    if not isinstance(ids, (list, tuple)):
        raise Refused("precondition_failed", "plugins", "plugin_unknown", "plugins 는 플러그인 id 의 목록이어야 한다")
    known = {plugin.id for plugin in plugins}
    for wanted in ids:
        if not isinstance(wanted, str) or wanted not in known:
            raise Refused("precondition_failed", "plugins", "plugin_unknown",
                          f"모르는 플러그인이다: {wanted!r} (설정의 플러그인: {', '.join(sorted(known)) or '없음'})")
    chosen = [plugin for plugin in plugins if plugin.id in ids]
    for plugin in chosen:
        if not _there(plugin):
            raise Refused("precondition_failed", "plugins", "plugin_source_missing", _missing(plugin))
    return chosen


def _missing(plugin: Plugin) -> str:
    if plugin.source is None:
        return f"플러그인 {plugin.name} 의 원본 위치를 모른다: relay.config.local.yaml 에 {ROOT_SETTING} 를 적는다"
    return f"플러그인 {plugin.name} 의 원본 폴더가 없다: {plugin.source}"


# --- the .uproject ------------------------------------------------------------------------------------------
def _uplugins(folder: Path) -> list[Path]:
    """The `.uplugin` files of a copied plugin, found as the engine finds them: a folder that has one is a
    plugin and is not looked into further; one that has none has its folders looked into, by name."""
    try:
        entries = sorted(folder.iterdir(), key=lambda entry: entry.name.lower())
    except OSError:
        return []
    found = [entry for entry in entries if entry.suffix.lower() == ".uplugin" and entry.is_file()]
    if found:
        return found
    return [file for entry in entries if entry.is_dir() for file in _uplugins(entry)]


def _load(uproject: Path) -> tuple[bytes, str, dict, list]:
    raw = Path(uproject).read_bytes()  # bytes: the line ends stay as they are
    text = raw.decode("utf-8-sig")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f".uproject 가 JSON 객체가 아니다: {uproject}")
    listed = data.get("Plugins", [])
    if not isinstance(listed, list):
        raise ValueError(f".uproject 의 Plugins 가 목록이 아니다: {uproject}")
    return raw, text, data, listed


def register(uproject: Path | str, names: Iterable[str]) -> list[str]:
    """Put the plugins in the `Plugins` of a `.uproject`, enabled; answers the names that were added. One
    that is listed already (any case) is only switched on. The file is written — tabs, its own line ends, its
    BOM and last line end kept — only when something changed. `OSError` · `ValueError` when it cannot be read
    or is not what a `.uproject` is."""
    uproject = Path(uproject)
    raw, text, data, listed = _load(uproject)
    added, changed = [], False
    for name in names:
        entry = next((e for e in listed if isinstance(e, dict) and str(e.get("Name", "")).lower() == name.lower()),
                     None)
        if entry is None:
            listed.append({"Name": name, "Enabled": True})
            added.append(name)
            changed = True
        elif entry.get("Enabled") is not True:
            entry["Enabled"] = True
            changed = True
    if not changed:
        return added
    data["Plugins"] = listed
    newline = "\r\n" if "\r\n" in text else "\n"
    written = json.dumps(data, indent="\t", ensure_ascii=False).replace("\n", newline)
    if text.endswith("\n"):
        written += newline
    bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
    uproject.write_bytes(bom + written.encode("utf-8"))
    return added


# --- the step -----------------------------------------------------------------------------------------------
def _skip(folder: str, names: list[str]) -> list[str]:
    return [name for name in names if name.lower() in SKIP_NAMES or name.lower().endswith(SKIP_SUFFIXES)]


def _within(inner: Path, outer: Path) -> bool:
    try:
        inner.resolve().relative_to(outer.resolve())
    except ValueError:
        return False
    return True


def _why(error: Exception) -> str:
    """An error as one line. `shutil.Error` of a copy carries a list of (from, to, why)."""
    failed = error.args[0] if isinstance(error, shutil.Error) and error.args else None
    if isinstance(failed, list) and failed:
        shown = "; ".join(f"{entry[0]}: {entry[-1]}" for entry in failed[:3])
        return shown + (f" 외 {len(failed) - 3}건" if len(failed) > 3 else "")
    return str(error)


def _unlock(folder: Path) -> int:
    """Take the read-only mark off `folder` and all below it; the answer is how many had one. `copy2` brings
    the mark of the source along, and the build has to write over a copy, and what clears a failed job to
    delete one. A link is passed by. It never raises: what cannot be changed is left as it is."""
    freed = 0
    try:
        paths = [folder, *folder.rglob("*")]
    except OSError:
        paths = [folder]
    for path in paths:
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or mode & stat.S_IWRITE:
                continue
            os.chmod(path, stat.S_IMODE(mode) | stat.S_IWRITE)
            freed += 1
        except OSError:
            pass
    return freed


def install(spec: Spec, chosen: Sequence[Plugin], *, run: Run = run_process, log: Log = _quiet) -> StepResult:
    """The first part of step `plugins` (`step`): copy each chosen plugin's folder to `<project>/Plugins/<folder>` — without `.git` and
    zip files — and then, once, put their names in the `.uproject`. `created` is `Plugins/` when it was made
    here, then each folder copied to, also one a copy that failed left half made. It stops at the first plugin
    that fails: a source that is not there, a folder that is there already (it is not touched), a copy without
    a `.uplugin`. The `.uproject` is written only after every copy went well. Nothing chosen is no failure.
    A copy is left writable, a half made one as well: the read-only mark of a source does not come along.
    `run` is not used; it is taken so that every step is called alike."""
    result = StepResult("plugins")
    tail: deque[str] = deque(maxlen=TAIL_LINES)

    def heard(line: str) -> None:
        tail.append(line)
        log(line)

    try:
        if not chosen:
            heard("플러그인: 고른 것이 없다")
        else:
            _load(spec.uproject)  # one that cannot be read fails here, before anything is copied
            home, names = spec.root / "Plugins", []
            if not home.is_dir():
                home.mkdir()
                result.created.append(str(home))
            for plugin in chosen:
                if not _there(plugin):
                    raise ValueError(_missing(plugin))
                target = home / plugin.folder
                if target.exists() or target.is_symlink():
                    raise ValueError(f"이미 있는 폴더다: {target}")
                if _within(target, plugin.source):
                    raise ValueError(f"플러그인 {plugin.name} 의 원본 폴더 안에 프로젝트가 있다: {plugin.source}")
                unlocked = 0
                try:
                    shutil.copytree(plugin.source, target, ignore=_skip)
                finally:
                    if target.exists():
                        result.created.append(str(target))
                        unlocked = _unlock(target)  # the copy only: not `Plugins/`, not the source
                found = _uplugins(target)
                if not found:
                    raise ValueError(f"플러그인 {plugin.name} 에 .uplugin 파일이 없다: {plugin.source}")
                names += [file.stem for file in found]
                count = sum(1 for file in target.rglob("*") if file.is_file())
                freed = f", 읽기 전용 해제 {unlocked}개" if unlocked else ""
                heard(f"플러그인: {plugin.name} → Plugins/{plugin.folder} (파일 {count}개{freed})")
            added = register(spec.uproject, names)
            heard(f".uproject 등록: {', '.join(names)}" + ("" if len(added) == len(names) else
                                                          f" (새로 넣은 것: {', '.join(added) or '없음'})"))
    except (OSError, ValueError) as e:
        result.error = f"플러그인을 넣지 못했다: {_why(e)}"
    if result.error:
        heard(result.error)
    result.tail = list(tail)
    return result


# --- the MCP port and its files -----------------------------------------------------------------------------
def allocate(taken: Iterable[Any] = (), *, is_open: Callable[[int], bool] = machine.port_open,
             first: int = FIRST_PORT, last: int = LAST_PORT) -> int:
    """The port a new project's editor is given: the lowest from `first` (55558) that is not MNYS's (55557), is
    not in `taken` — the `mcp_port` of every registered project, the archived ones as well — and is not open on
    this computer (`is_open`: an editor, or anything else, listens there). What is not a whole number in
    `taken` (the None of a project without a port) is passed by, and a taken port is not knocked on.
    `ValueError` when none is left."""
    used = {port for port in taken if isinstance(port, int) and not isinstance(port, bool)}
    for port in range(first, last + 1):
        if port != MNYS_PORT and port not in used and not is_open(port):
            return port
    raise ValueError(f"배정할 MCP 포트가 없다 ({first}–{last} 가 모두 쓰이고 있다)")


def _port(port: Any) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"포트가 아니다: {port!r}")
    return port


def _ending(line: str) -> str:
    """The line end a line of `splitlines(keepends=True)` has; '' for a last line without one."""
    return line[len(line.splitlines()[0]):] if line else ""


def write_ini_port(root: Path | str, port: int) -> list[Path]:
    """Put `Port=<port>` in the `[UnrealMCP]` section of `<root>/Config/DefaultEngine.ini`, so that
    `importer.ini_port` reads it. The last `Port=` line of the section, when there is one, is the only line
    changed; a section without one gets the line right under its (last) heading; a file without the section
    gets a blank line, the heading and the line at its end. Everything else stays: the other sections, the line
    ends, the encoding (UTF-8, or UTF-16 when it starts with that mark) and the BOM. Nothing is written when
    the port is there already. A file that is not there is made, `Config/` as well; the answer is what was made
    here. `ValueError` for a file that is not text of its encoding (it is left as it is) and for what is not
    a port. Of the `Port=` line that is changed, all that is after the `=` but its leading blanks becomes the
    port, so a comment at the end of that line is gone: `importer.ini_port` takes all that is after the `=`
    as the value, and reads no port from one with a comment in it."""
    port = _port(port)
    file = Path(root).joinpath(*importer.INI)
    fresh = not file.exists()
    raw = b"" if fresh else file.read_bytes()
    bom, encoding = next(((mark, name) for mark, name in INI_MARKS if raw.startswith(mark)), (b"", "utf-8"))
    try:
        text = raw[len(bom):].decode(encoding)
    except UnicodeDecodeError as e:
        raise ValueError(f"{'/'.join(importer.INI)} 가 {encoding} 글이 아니다: {e}") from e
    newline = "\n" if "\n" in text and "\r\n" not in text else "\r\n"  # the engine's own, when the file has none
    lines = text.splitlines(keepends=True)  # split as importer.ini_port splits
    heading = found = None
    inside = False
    for at, line in enumerate(lines):
        bare = line.strip()
        if bare.startswith("["):
            inside = bare.lower() == importer.INI_SECTION
            heading = at if inside else heading
        elif inside and not bare.startswith((";", "#")):
            key, sep, _ = bare.partition("=")
            if sep and key.strip().lower() == importer.INI_KEY:
                found = at
    if found is not None:
        key, _, value = lines[found].partition("=")
        lead = value[:len(value) - len(value.lstrip(" \t"))]
        lines[found] = f"{key}={lead}{port}{_ending(lines[found])}"
    elif heading is not None:
        if not _ending(lines[heading]):
            lines[heading] += newline
        lines.insert(heading + 1, f"Port={port}{newline}")
    else:
        if lines and not _ending(lines[-1]):
            lines[-1] += newline
        if lines and lines[-1].strip():
            lines.append(newline)
        lines += [f"{INI_HEADER}{newline}", f"Port={port}{newline}"]
    written = "".join(lines)
    made: list[Path] = []
    if fresh or written != text:
        if not file.parent.is_dir():
            file.parent.mkdir(parents=True)
            made.append(file.parent)
        file.write_bytes(bom + written.encode(encoding))
        if fresh:
            made.append(file)
    return made


def _filled(text: str, values: Mapping[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def _indent(text: str) -> str | int:
    """What one level of a JSON text is indented with: the blanks (tabs or spaces) the line under its opening
    `{` starts with — that line is always one level deep. 2 when there is no such line (one line, `{}`)."""
    found = re.match(r"\s*\{[ \t]*\r?\n([ \t]+)\S", text)
    return found.group(1) if found else 2


def write_mcp_json(root: Path | str, server: McpServer, port: int,
                   values: Mapping[str, str] | None = None) -> list[Path]:
    """Put the server in `mcpServers` of `<root>/.mcp.json` with `UNREAL_MCP_PORT` fixed in its `env` — as
    digits in a string, which is what an environment takes — so that `importer.mcp_json_port` reads it. The
    port is fixed there because a session started in a folder below the project, or in a copy of it, does not
    find the editor's `Saved/UnrealMCP/port.txt`. `values` fills `{plugin}` · `{project}` of the command and
    its arguments. A file that is there keeps its other servers, the other keys of this one and the rest of its
    `env`, its indentation (that of the line under its opening `{`; 2 spaces when it has none), its line ends
    and its BOM, and is not written when nothing changes; one that is not there is made (2 spaces, LF) and is
    the answer. `ValueError` for a file that is not a JSON object with `mcpServers` an
    object (it is left as it is) and for what is not a port."""
    port, values = _port(port), values or {}
    file = Path(root) / importer.MCP_JSON
    entry = {"command": _filled(server.command, values), "args": [_filled(arg, values) for arg in server.args]}
    env = {importer.PORT_ENV: str(port)}
    if not file.exists():
        data = {"mcpServers": {server.name: {**entry, "env": env}}}
        file.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        return [file]
    raw = file.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
        data = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise ValueError(f"{importer.MCP_JSON} 이 JSON 이 아니다: {e}") from e
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise ValueError(f"{importer.MCP_JSON} 이 mcpServers 를 객체로 가진 JSON 객체가 아니다")
    before = json.dumps(data, sort_keys=True)
    old = servers.get(server.name) if isinstance(servers.get(server.name), dict) else {}
    kept = old.get("env") if isinstance(old.get("env"), dict) else {}
    servers[server.name] = {**old, **entry, "env": {**kept, **env}}
    data["mcpServers"] = servers
    if json.dumps(data, sort_keys=True) == before:
        return []
    newline = "\r\n" if "\r\n" in text else "\n"
    written = json.dumps(data, indent=_indent(text), ensure_ascii=False).replace("\n", newline)
    if text.endswith("\n"):
        written += newline
    bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
    file.write_bytes(bom + written.encode("utf-8"))
    return []


def write_guards(root: Path | str) -> list[Path]:
    """Put the default guards at `<root>/.claude/mcp_guards.json` — the bytes of `GUARDS_DEFAULT` — unless a
    file is there already, which is not touched. The answer is what was made here: `.claude/` when it was not
    there, and the file."""
    file = Path(root).joinpath(*GUARDS)
    if file.exists() or file.is_symlink():
        return []
    default = GUARDS_DEFAULT.read_bytes()  # read first: without it nothing is made
    made: list[Path] = []
    if not file.parent.is_dir():
        file.parent.mkdir(parents=True)
        made.append(file.parent)
    file.write_bytes(default)
    made.append(file)
    return made


def step(spec: Spec, chosen: Sequence[Plugin], *, taken: Iterable[Any] = (),
         is_open: Callable[[int], bool] = machine.port_open, run: Run = run_process,
         log: Log = _quiet) -> PluginsResult:
    """Step `plugins`: `install`, and then — when a chosen plugin has an `mcp` (the first that has) — a port
    for the project (`allocate` with `taken` and `is_open`), written to `Config/DefaultEngine.ini` and
    `.mcp.json`, and the default `.claude/mcp_guards.json`. The port is read back as an import of the folder
    would read it, and one that does not come back the same fails the step. `mcp_port` is the port, None when
    no MCP plugin was chosen — then no port is looked for and none of the three files is touched — or when the
    step failed. A copy that failed ends the step with `install`'s error. `created` is `install`'s, then what
    was made here: `.mcp.json`, `.claude/`, the guards (the ini only if it was not there — the one of the
    `folder` step is changed, not made). Nothing is deleted when it fails."""
    tail: deque[str] = deque(maxlen=TAIL_LINES)

    def heard(line: str) -> None:
        tail.append(line)
        log(line)

    copied = install(spec, chosen, run=run, log=heard)  # its own error is heard there
    result = PluginsResult("plugins", created=list(copied.created), error=copied.error)
    if result.ok:
        plugin = next((one for one in chosen if one.mcp is not None), None)
        try:
            if plugin is None:
                heard("MCP: 고른 것에 MCP 플러그인이 없다 — 포트를 배정하지 않는다")
            else:
                port = allocate(taken, is_open=is_open)
                root = spec.root.absolute()
                values = {"plugin": (root / "Plugins" / plugin.folder).as_posix(), "project": root.as_posix()}
                result.created += [str(path) for path in write_ini_port(spec.root, port)]
                result.created += [str(path) for path in write_mcp_json(spec.root, plugin.mcp, port, values)]
                guards = write_guards(spec.root)
                result.created += [str(path) for path in guards]
                read = importer.ini_port(spec.root), importer.mcp_json_port(spec.root)
                if read != (port, port):
                    raise ValueError(f"쓴 포트 {port} 가 그대로 읽히지 않는다 ({'/'.join(importer.INI)}: {read[0]}, "
                                     f"{importer.MCP_JSON}: {read[1]})")
                result.mcp_port = port
                heard(f"MCP 포트 {port}: {'/'.join(importer.INI)} {INI_HEADER} · {importer.MCP_JSON} "
                      f"({plugin.mcp.name}) · {'/'.join(GUARDS)}" + ("" if guards else " (있던 것 그대로)"))
        except (OSError, ValueError) as e:
            result.error = f"MCP 설정을 쓰지 못했다: {_why(e)}"
            heard(result.error)
    result.tail = list(tail)
    return result
