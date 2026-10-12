"""This computer, as the app's diagnosis screen reads it (M-1; design/studio/ARCHITECTURE.md §6.4): what
`GET /studio/machine` answers, and the four values of it the stream's `hello` carries (§7.1).

Nothing here imports api, cli or server. What is slow to ask or belongs to the server's engine — the checks of
`relay doctor`, the answer of `GET /providers`, the hash of the running code — is handed over by server.py
(`api.use_machine`). The rest is read here, each through a function a test replaces: the registry (`winreg`,
read calls only — nothing is ever written to it), the list of processes, the port of an editor's MCP, the disk.
A source that fails empties its own field and nothing else: the screen that shows what is wrong must not be the
thing that fails.

The talk with an editor's MCP port is here too (M-7): `mcp_send` — one command, one answer — and `health_check`,
what `healthy` means in the report and in studio/editor.py, which starts, stops and photographs an editor.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .schema import MIGRATIONS
from .store import now

log = logging.getLogger(__name__)

ENGINE_KEY = r"SOFTWARE\EpicGames\Unreal Engine"  # under HKEY_LOCAL_MACHINE: one sub key per installed version
ENGINE_VALUE = "InstalledDirectory"               # the value of a version's key: the folder of that engine
# One PowerShell question for the running editors. CIM gives the command line, which names the .uproject
# (`tasklist` does not, and `wmic` is gone from a new Windows). No double quote in it: it travels as one argument.
EDITOR_QUERY = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                "Get-CimInstance Win32_Process -Filter 'Name LIKE ''UnrealEditor%'' OR Name LIKE ''UE4Editor%''' | "
                "Select-Object ProcessId, Name, CommandLine | ConvertTo-Json -Compress")
QUERY_TIMEOUT = 8.0  # seconds PowerShell has to answer
PORT_TIMEOUT = 0.3   # seconds a connection to an editor's MCP port may take
HEALTH_COMMAND = "health_check"    # the MCP command an editor is asked (provisional: the plugin's source was not read)
HEALTH_TIMEOUT = 3.0               # seconds an editor has to answer it
MCP_MAX_BYTES = 8 * 1024 * 1024    # an answer longer than this is not read on


def is_windows() -> bool:
    return os.name == "nt"


# --- installed engines ------------------------------------------------------------------------------------
def _sub_keys(reg: Any, key: Any) -> list[str]:
    names: list[str] = []
    while True:
        try:
            names.append(reg.EnumKey(key, len(names)))
        except OSError:  # no more of them
            return names


def read_engine_registry(reg: Any = None, *, windows: bool | None = None) -> dict[str, str]:
    """`{version: folder}` of the engines the Epic launcher installed on this computer: every sub key of
    ENGINE_KEY (under HKEY_LOCAL_MACHINE) that has an `InstalledDirectory`. The 64-bit view is read first, then
    the 32-bit one, and the folder found first for a version stays. `{}` when this is not Windows (`reg` is not
    touched then) or the key is not there. `reg` stands in for `winreg` in a test; only OpenKey, EnumKey and
    QueryValueEx are called, with read access — the registry is never written. Engines built from source
    (the current user's `Builds` key) are not listed."""
    if not (is_windows() if windows is None else windows):
        return {}
    if reg is None:
        import winreg as reg  # Windows only, so not at the top (envpath.py does the same)
    found: dict[str, str] = {}
    for view in (reg.KEY_WOW64_64KEY, reg.KEY_WOW64_32KEY):
        access = reg.KEY_READ | view
        try:
            root = reg.OpenKey(reg.HKEY_LOCAL_MACHINE, ENGINE_KEY, 0, access)
        except OSError:
            continue  # no engine was ever installed in this view
        with root:
            for version in _sub_keys(reg, root):
                try:
                    with reg.OpenKey(root, version, 0, access) as key:
                        value, _ = reg.QueryValueEx(key, ENGINE_VALUE)
                except OSError:
                    continue  # a key of something else, or one an uninstall left empty
                folder = str(value or "").strip()
                if folder:
                    found.setdefault(str(version), folder)
    return found


def _version_order(version: str) -> tuple:
    numbers = tuple(int(n) for n in re.findall(r"\d+", version))
    return (not numbers, numbers, version)  # 5.10 after 5.7; a name without a number last


def engines(read: Callable[[], dict[str, str]] = read_engine_registry) -> list[dict]:
    """`[{version, path, exists}]` in the order of the versions as numbers (4.27, 5.1, 5.6, 5.7, 5.10).
    `exists`: the folder the registry names is there — a drive that was taken out leaves its key behind."""
    found = read()
    return [{"version": version, "path": found[version], "exists": os.path.isdir(found[version])}
            for version in sorted(found, key=_version_order)]


# --- running editors --------------------------------------------------------------------------------------
def parse_processes(text: str) -> list[dict]:
    """What EDITOR_QUERY printed, as `[{pid, name, cmdline}]`: nothing (no editor runs), one object (one
    editor) or an array. `UnrealEditor-Cmd.exe` — a commandlet: a cook, a build — is not an editor somebody
    works in and is left out."""
    text = (text or "").strip()
    if not text:
        return []
    data = json.loads(text)
    found: list[dict] = []
    for row in data if isinstance(data, list) else [data]:
        if not isinstance(row, dict) or not isinstance(row.get("ProcessId"), int):
            continue
        name = str(row.get("Name") or "")
        if name and not name.lower().endswith("-cmd.exe"):
            found.append({"pid": row["ProcessId"], "name": name, "cmdline": str(row.get("CommandLine") or "")})
    return found


def editor_processes(run: Callable[..., Any] = subprocess.run) -> list[dict]:
    """`[{pid, name, cmdline}]` of the Unreal editors running on this computer, asked of PowerShell. `[]` when
    this is not Windows, PowerShell is missing, the question takes longer than QUERY_TIMEOUT or the answer
    cannot be read. `run` stands in for `subprocess.run` in a test."""
    if not is_windows():
        return []
    try:
        done = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", EDITOR_QUERY], capture_output=True,
                   stdin=subprocess.DEVNULL, timeout=QUERY_TIMEOUT,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return parse_processes(bytes(done.stdout or b"").decode("utf-8-sig", errors="replace"))
    except Exception:  # noqa: BLE001 — a diagnosis that cannot ask says "none", it does not fail
        log.exception("studio: 실행 중인 에디터를 찾지 못했다")
        return []


_UPROJECT = re.compile(r'"([^"]+\.uproject)"|(\S+\.uproject)', re.IGNORECASE)


def uproject_of(cmdline: str) -> str | None:
    """The .uproject an editor was started with, quoted or not; None when its command line names none."""
    match = _UPROJECT.search(cmdline or "")
    return (match.group(1) or match.group(2)) if match else None


def port_open(port: Any, host: str = "127.0.0.1", timeout: float = PORT_TIMEOUT) -> bool:
    """Does something on this computer listen on `port`? What the port allocation asks (studio/plugins.py);
    whether an editor answers is `health_check`."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, TypeError, ValueError):
        return False


def mcp_send(port: Any, command: str, params: dict | None = None, *, timeout: float,
             host: str = "127.0.0.1") -> dict:
    """One command to the editor plugin that listens on `port`, and its answer (M-7). Provisional — the form of
    the public unreal-mcp, not read from the plugin's source: `{"type": command, "params": {…}}` goes out once as
    UTF-8 JSON (no newline, the sending side is not shut down), and the answer is read until the bytes are one
    JSON object or the editor closes. `timeout` is for the whole exchange. OSError: nobody listens, or no answer
    in time. ValueError: what came is not a JSON object."""
    request = json.dumps({"type": command, "params": dict(params or {})}, ensure_ascii=False).encode("utf-8")
    deadline = time.monotonic() + timeout
    got = b""
    with socket.create_connection((host, int(port)), timeout=timeout) as conn:
        conn.sendall(request)
        while len(got) <= MCP_MAX_BYTES:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"MCP 포트 {port} 가 {timeout:g}초 안에 답하지 않았다")
            conn.settimeout(left)
            chunk = conn.recv(65536)
            if not chunk:
                break  # the editor closed: what came is the whole answer
            got += chunk
            try:
                answer = json.loads(got.decode("utf-8"))
            except ValueError:  # not whole yet (a cut multi-byte character is one too)
                continue
            if isinstance(answer, dict):
                return answer
    answer = json.loads(got.decode("utf-8"))  # ValueError when it is empty, cut or too long
    if not isinstance(answer, dict):
        raise ValueError(f"MCP 포트 {port} 의 응답이 JSON 객체가 아니다")
    return answer


def health_check(port: Any, *, timeout: float = HEALTH_TIMEOUT, send: Callable[..., dict] = mcp_send) -> bool:
    """Does the editor on `port` answer `health_check` in time? What `healthy` means (§6.4, M-7). Any JSON object
    counts, one that says `status: error` too: an editor that answers is not stuck. An editor behind a modal
    window or one that crashed sends nothing, and that — like every other failure — is False."""
    try:
        return isinstance(send(port, HEALTH_COMMAND, timeout=timeout), dict)
    except Exception:  # noqa: BLE001 — no port, nobody listening, no answer, not JSON
        return False


def _same(path: Any) -> str:
    """A path as two of them are compared: forward slashes, lower case, no slash at the end (Windows paths)."""
    return str(path or "").replace("\\", "/").rstrip("/").lower()


def editors(processes: list[dict], projects: list[dict],
            healthy: Callable[[Any], bool] = health_check) -> list[dict]:
    """`[{project_id, pid, mcp_port, healthy, uproject}]`, by pid. An editor belongs to the project whose
    folder (`path`) holds its .uproject — the deepest one when folders are nested. An editor of no registered
    project has no `project_id` and no `mcp_port`, and is not healthy: there is no port to ask. `healthy` is
    the MCP `health_check` of that port (M-7)."""
    homes = sorted(((_same(p.get("path")), p) for p in projects if _same(p.get("path"))),
                   key=lambda home: len(home[0]), reverse=True)
    found = []
    for process in processes:
        uproject = uproject_of(process.get("cmdline") or "")
        where = _same(uproject)
        project = next((p for home, p in homes if where.startswith(home + "/")), None) if where else None
        port = project.get("mcp_port") if project else None
        found.append({"project_id": project["id"] if project else None, "pid": process["pid"], "mcp_port": port,
                      "healthy": bool(port) and bool(healthy(port)), "uproject": uproject})
    return sorted(found, key=lambda editor: editor["pid"])


# --- disk · time ------------------------------------------------------------------------------------------
def disk(folder: Any, usage: Callable[[Any], Any] = shutil.disk_usage) -> dict:
    """`{path, free_bytes, total_bytes}` of the drive the runs are written to: asked of the nearest folder at
    or above `folder` that exists (the runs folder is made at the first run). The two numbers are None when the
    drive cannot be asked."""
    at = Path(folder).absolute()
    try:
        while not at.exists() and at.parent != at:
            at = at.parent
        seen = usage(at)
        free, total = int(seen.free), int(seen.total)
    except Exception:  # noqa: BLE001
        log.exception("studio: %s 의 디스크 여유를 읽지 못했다", at)
        free = total = None
    return {"path": at.as_posix(), "free_bytes": free, "total_bytes": total}


def utc_offset_minutes() -> int:
    """Minutes this computer's clock is ahead of UTC now (540 in Korea). The studio's times carry no zone
    (§3.1); with this an app that looks at several computers can place them (§6.1)."""
    offset = datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds() // 60) if offset is not None else 0


# --- the report -------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Sources:
    """Where the report reads from. The first four are this module's own; the last three only the server has
    (None = nobody gave one: an empty list, an empty version)."""
    engines: Callable[[], dict[str, str]] = read_engine_registry   # {version: folder}
    processes: Callable[[], list[dict]] = editor_processes         # [{pid, name, cmdline}]
    healthy: Callable[[Any], bool] = health_check                  # mcp_port -> does the editor answer (M-7)
    usage: Callable[[Any], Any] = shutil.disk_usage                # folder -> (.total, .free)
    doctor: Callable[[], list[dict]] | None = None                 # cli.py `doctor_checks`
    providers: Callable[[], list[dict]] | None = None              # server.py `GET /providers`
    version: Callable[[], str] | None = None                       # server.py `RUNNING_CODE`


def _listed(what: str, read: Callable[[], Any] | None) -> list:
    """One list of the report. A source nobody gave, or one that raises, is an empty list (and a log line)."""
    if read is None:
        return []
    try:
        return list(read())
    except Exception:  # noqa: BLE001 — the diagnosis answers whatever one source does
        log.exception("studio: 컴퓨터 진단에서 %s 를 읽지 못했다", what)
        return []


def _server_version(src: Sources) -> str:
    if src.version is None:
        return ""
    try:
        return str(src.version() or "")
    except Exception:  # noqa: BLE001
        log.exception("studio: 서버 코드 버전을 읽지 못했다")
        return ""


def brief(src: Sources) -> dict:
    """`{name, server_version, schema_version, utc_offset_minutes}`: the values that cost nothing to read. The
    stream's `hello` sends them at every connection (§7.1); `report` has the same ones. `schema_version` is the
    last of MIGRATIONS — the store brings a DB up to it when it opens, and refuses a newer one (§3.2)."""
    return {"name": platform.node(), "server_version": _server_version(src),
            "schema_version": MIGRATIONS[-1][0] if MIGRATIONS else 0, "utc_offset_minutes": utc_offset_minutes()}


def report(cfg: Any, projects: list[dict], src: Sources) -> dict:
    """The answer of `GET /studio/machine`, its keys in the order of §6.4. `projects` are the registered ones:
    what a running editor is matched with."""
    head = brief(src)
    return {
        "name": head["name"],
        "server_version": head["server_version"],
        "schema_version": head["schema_version"],
        "now": now(),
        "utc_offset_minutes": head["utc_offset_minutes"],
        "doctor": _listed("relay doctor 의 점검", src.doctor),
        "engines": _listed("설치된 엔진", lambda: engines(src.engines)),
        "disk": disk(cfg.runs_dir, src.usage),
        "providers": _listed("AI 사용량", src.providers),
        "editors": _listed("실행 중인 에디터", lambda: editors(src.processes(), projects, src.healthy)),
        "limits": {"max_concurrent_runs": cfg.studio_max_runs, "max_exchanges": cfg.studio_max_exchanges,
                   "group_max_members": cfg.studio_group_max_members,
                   "group_max_exchanges": cfg.studio_group_max_exchanges},
    }
