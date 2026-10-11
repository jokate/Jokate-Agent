"""The guard on the server's side (S-10): what `POST /studio/guard/check` answers (§6.4 '가드'), and what a studio
run is given so that the hook hangs on it (§10.1 `studio-full`, §10.2 guard_ctx.json, §11.4).

The hook (relay_agent/studio_guard.py) judges every tool call by itself and writes the audit log (§10.7); it comes
here only with a command of the ask list (§10.4). `check` is the table of §6.4, read top to bottom; the one-time
grant it uses is `Inbox.use_grant` (§10.5). `profile` · `write_ctx` · `write_settings` are what `api.guard_stage` —
the engine's callback — hands to a stage. Nothing here imports api.py.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from .. import config, studio_guard
from .inbox import Inbox, InboxError, grant_key
from .tasks import SYSTEM, TaskMachine, TransitionError
from .tools import END_TURN, Caller

REASONS = {  # rule_id (§10.4, the five that are asked) -> why the owner is asked: `reason` of the payload (§5.3)
    "git_risky": "git 의 push · reset --hard · clean -f 는 원격 저장소나 커밋하지 않은 작업을 되돌릴 수 없게 바꾼다.",
    "outside_delete": "등록된 프로젝트 폴더 · 이 런의 작업 폴더 · 임시 폴더 밖의 파일을 지우거나 옮긴다.",
    "install_env": "프로그램을 설치 · 제거하거나 전역 환경(전역 도구 · 사용자 환경 변수)을 바꾼다.",
    "power_state": "이 PC 를 끄거나 다시 시작하거나 절전으로 보낸다.",
    "net_transfer": "1GB 를 넘게 내려받거나, 알려진 호스트가 아닌 곳으로 파일을 올린다.",
}
CTX_NAME, SETTINGS_NAME = "guard_ctx.json", "studio_settings.json"  # in the run's folder (§11.4 의 2 · 3)
JOURNAL_NAME = "hook-settings.json"  # JournalWorkspace.settings_path (journal.py): the backup hook's own settings file
HOOK = Path(studio_guard.__file__).resolve()
MATCH_ALL = "*"  # the hook is asked about every tool; the spelling is checked with a real Claude Code (VERIFICATION.md)
WORK_TOOLS = ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "mcp__studio"]  # §10.1: a task run
TURN_TOOLS = ["mcp__studio"]                                                         # a chat · read turn


# --- POST /studio/guard/check (§6.4) --------------------------------------------------------------------------
def _answer(decision: str, message: str, *, inbox_id: str | None = None, end_turn: bool = False) -> dict:
    return {"decision": decision, "message": f"{message} {END_TURN}" if end_turn else message,
            "inbox_id": inbox_id, "end_turn": end_turn}


def _title(tool: str, command: str) -> str:
    return f"{tool}: {' '.join(command.split())}"[:80]


def check(caller: Caller, tool: str, command: str, rule_id: str, *, machine: TaskMachine, inbox: Inbox,
          stage: str = "") -> dict:
    """The four rows of §6.4 for one question of the hook: `{decision, message, inbox_id, end_turn}`.

    A task run is a run whose id is `tasks.run_id` of the token's task — the DB of now, not the token's `mode`.
    1 the command has a grant left: it is used, `allow`. 2 the task works: the system's T4 files a
    `risky_command` (the caller hands `inbox_id` to `api.answered`). 3 the task is blocked or in review: a `guard`
    row in task_events and no item. 4 anything else — and a `rule_id` that is not one of the five: `deny`."""
    reason = REASONS.get(rule_id)
    if reason is None:
        return _answer("deny", f"서버에 물을 수 있는 규칙이 아니라({rule_id or '없음'}) 이 명령은 실행하지 않는다.")
    store = machine.store
    task = store.get_task(caller.task_id) if caller.task_id else None
    if task is None or not task["run_id"] or task["run_id"] != caller.run_id:
        return _answer("deny", f"owner 의 결재가 필요한 명령인데 이 턴은 작업 런이 아니라 결재를 올릴 수 없다. {reason} "
                               "이 명령은 실행하지 않는다 — 필요하면 방에 글로 그대로 물어라.")
    if inbox.use_grant(task["id"], grant_key(tool, command), caller.run_id) is not None:
        return _answer("allow", "owner 가 승인한 명령이다. 이번 한 번만 실행된다.")
    if task["status"] == "working":
        payload = {"command": command, "tool": tool, "rule_id": rule_id, "reason": reason}
        if stage:
            payload["stage"] = stage
        try:
            item = inbox.create("risky_command", SYSTEM, task_id=task["id"], title=_title(tool, command),
                                payload=payload)
        except (TransitionError, InboxError):  # the task moved in between, or the payload cannot be filed
            task = store.get_task(task["id"]) or task
        else:
            return _answer("deny", f"owner 의 결재가 필요한 명령이라 실행하지 않고 결재를 올렸다 ({item['id']}). {reason} "
                                   "결정은 이 작업방에 온다. 승인되면 같은 명령을 글자 그대로 다시 실행하라.",
                           inbox_id=item["id"], end_turn=True)
    if task["status"] in ("blocked", "review"):
        machine.record(task["id"], SYSTEM, "guard", f"가드가 명령을 거부했다 ({rule_id}). {reason}",
                       detail={"run_id": caller.run_id, "tool": tool, "command": command, "rule_id": rule_id})
        if task["status"] == "blocked":
            return _answer("deny", f"이 작업은 owner 의 결정을 기다리는 중이라 이 명령은 실행하지 않는다. {reason}",
                           end_turn=True)
        return _answer("deny", f"검수를 기다리는 작업이라 owner 의 결재가 필요한 이 명령은 실행하지 않는다. {reason} "
                               "이 명령 없이 남은 일을 마쳐라.")
    return _answer("deny", f"owner 의 결재가 필요한 명령인데 지금은 결재를 올릴 수 없어 실행하지 않는다. {reason}")


# --- what a stage of a studio run is given (§10.1, §10.2, §11.4) -----------------------------------------------
def profile(mode: str) -> list[str]:
    """`studio-full` (§10.1): what is approved ahead for a run of that `mode`. It adds no tool — a stage whose
    relay gives it no Bash gets none. The attached MCP servers are approved by the runner."""
    return list(WORK_TOOLS if mode == "work" else TURN_TOOLS)


def _real(path: Any) -> str:
    """A path in the spelling the hook reduces a command's paths to (links and short names followed, §10.2)."""
    return studio_guard._resolve(str(path))


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _git_hosts(folder: Any) -> list[str]:
    """The hosts of that folder's git remotes, read from `.git/config` — best effort: a host that is missed is
    only asked about once more (§10.4 의 5)."""
    try:
        text = (Path(folder) / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hosts = []
    for url in re.findall(r"(?m)^\s*(?:push)?url\s*=\s*(\S+)", text):
        found = re.match(r"(?i)^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?(\[[0-9a-f:]+\]|[^/:]+)", url) \
            or re.match(r"^(?:[^@/\\]+@)?([^/:\\]{2,}):", url)  # scp form: git@host:path (not a drive letter)
        if found:
            hosts.append(found.group(1).lower())
    return hosts


def _fcm_key(cfg: Any) -> str:
    """The key file of the push (`Config.studio_fcm_key`, §5.9) in the hook's spelling, "" for none. It is a secret
    wherever the owner keeps it — `studio_guard.default_secrets` knows `~/.katae` only, and nothing of the server's
    settings."""
    from . import push  # late: only its reading of that path is wanted, and only when a stage starts

    file = push.key_file(getattr(cfg, "studio_fcm_key", ""))
    return _real(file) if file is not None else ""


def write_ctx(run_dir: Path, cfg: Any, run_id: str, cwd: Any, projects: list[dict],
              journal: Path | None = None) -> Path:
    """`<run_dir>/guard_ctx.json` (§10.2): what the hook judges with, and no secret. `projects` are the registered
    projects that are not archived. Written again at every stage, so the file is the server's whatever became
    of it in between — and a key file named in the settings meanwhile is in `secrets` from the next stage on.

    `journal` is the settings file of the backup hook that `write_settings` reads from. It is protected like
    the two files written here — where a JournalWorkspace keeps it in the run's folder (JOURNAL_NAME) whether
    this run has one or not, and where `journal` says when that is another place: what is written into it would
    be carried into the hook's settings at the next stage."""
    root, run_dir, runs_dir = config.ROOT, Path(run_dir), Path(cfg.runs_dir)
    folders = [p["path"] for p in projects if p.get("path")]
    listed = [str(h).strip().lower() for h in getattr(cfg, "studio_known_hosts", None) or []]
    guarded = [root / "relay_agent", root / "relay.config.yaml", root / "relay.config.local.yaml",
               runs_dir / "studio", run_dir / CTX_NAME, run_dir / SETTINGS_NAME, run_dir / JOURNAL_NAME]
    if journal is not None:
        guarded.append(Path(journal))
    secrets, key = studio_guard.default_secrets(dict(os.environ), _real(root)), _fcm_key(cfg)
    if key:
        secrets = _unique(secrets + [key])
    data = {
        "v": 1, "run_id": run_id, "server_root": _real(root), "runs_dir": _real(runs_dir),
        "server_url": cfg.server_url, "server_pid": os.getpid(),
        # §10.8: the guard, the server's code and settings, studio's data, the hooks' settings of this run
        "protected": _unique([_real(p) for p in guarded]),
        "secrets": secrets,  # §10.3 의 3, and the push's key file (§5.9)
        "delete_roots": _unique([_real(Path(f)) for f in folders] + [_real(cwd), _real(tempfile.gettempdir())]),
        "known_hosts": _unique([h for f in folders for h in _git_hosts(f)] + listed),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / CTX_NAME
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def write_settings(run_dir: Path, ctx_path: Path, journal: Path | None = None) -> Path:
    """`<run_dir>/studio_settings.json` (§11.4 의 2): the Claude Code settings of a studio run. The guard is the
    first PreToolUse entry, on every tool; the command line carries paths only — the token goes by the
    environment. `journal` is the settings file of a JournalWorkspace: its `hooks.PreToolUse` entries (the backup
    hook) follow the guard, and nothing else of that file is carried over — no other top-level key
    (`disableAllHooks`, `permissions`, …) and no other hook event, so that file cannot switch the guard off or
    widen what the run may do. A `journal` that cannot be read or holds no such list raises ValueError · OSError:
    the stage does not run without its backups."""
    command = f'"{sys.executable}" "{HOOK}" "{ctx_path}"'
    backups: list = []
    if journal is not None:
        data = json.loads(Path(journal).read_text(encoding="utf-8"))
        hooks = data.get("hooks") if isinstance(data, dict) else None
        found = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
        if not isinstance(found, list):
            raise ValueError(f"{journal}: no hooks.PreToolUse list")
        backups = found
    entries = [{"matcher": MATCH_ALL, "hooks": [{"type": "command", "command": command}]}, *backups]
    path = Path(run_dir) / SETTINGS_NAME
    path.write_text(json.dumps({"hooks": {"PreToolUse": entries}}, ensure_ascii=False), encoding="utf-8")
    return path
