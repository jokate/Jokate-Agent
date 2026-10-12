"""Server config: paths, AI providers, and the MCP registry stages may opt into by name."""

from __future__ import annotations

import shutil
import os
import sys
from functools import cached_property
from pathlib import Path

import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent


class Config(BaseModel):
    runs_dir: Path = ROOT / "runs"
    usage_db: Path = ROOT / "runs" / "usage.sqlite"
    history_db: Path = ROOT / "runs" / "history.sqlite"
    relays_dir: Path = ROOT / "relays"
    docs_root: Path = ROOT / "docs"
    server_url: str = "http://127.0.0.1:8020"
    # name -> Claude Code mcpServers entry. Stages list names in `mcp:`; nothing else is loaded.
    mcp_servers: dict[str, dict] = {}
    # provider overrides / additions (see providers.BUILTIN), e.g. {"codex": {"model_map": {...}, "daily_usd": 5}}
    providers: dict[str, dict] = {}
    # tried after a stage's own alternates when its provider can't run (usage limit, not installed, budget)
    fallback_chain: list[str] = []
    # folders/globs never snapshotted into a workspace; None = workspace.DEFAULT_EXCLUDES
    workspace_excludes: list[str] | None = None
    # Workspaces nobody decided on (patch not applied, resumable failures) are deleted after this many days.
    workspace_retention_days: float = 3
    # A snapshot larger than this is refused before anything is written (names the biggest folders).
    max_snapshot_mb: float = 500
    # Single files larger than this are left out of snapshots (reported in workspace_ready).
    max_file_mb: float = 20
    # Registered repositories (see repos.py). Shared rules here, each machine's `path` in the local file.
    repos: dict[str, dict] = {}
    # Where `relay repo clone` puts repositories that have a url but no path on this machine.
    repos_root: str = ""  # empty = next to this agent folder
    # Added to every stage that has Bash. Headless runs can't ask for approval, and a denied verification
    # command was measured to burn 2.6x tokens in retries — list this machine's test/build commands here.
    extra_allowed_tools: list[str] = []
    # Required for any request that is not from this machine (env KATAE_TOKEN wins). Empty = localhost only.
    auth_token: str = ""
    # Address `relay serve` (and so start.bat and the dashboard restart) binds to (env KATAE_HOST wins).
    # 0.0.0.0 = reachable from other devices; set with `relay remote on|off`, which also creates the token.
    serve_host: str = "127.0.0.1"
    # When set, a non-local client must also come from one of these networks (CIDR), e.g. the Tailscale
    # tailnet: `relay remote on --tailscale`. Empty = any address that has the token.
    remote_networks: list[str] = []
    notify: bool = True  # Windows toast when a run finishes, fails or needs approval
    handoff_model: str = "haiku"  # writes the hand-over when a run stops ("" = no AI, engine-built only)
    # Before a run's first stage, check that the MCP servers it attaches answer (editor closed -> stop, no cost).
    mcp_check: bool = True
    # Katae Studio: the ceilings of an employee whose file and part name none (ARCHITECTURE.md §9.5 의 6).
    studio_model_cap: str | None = None   # haiku | sonnet | opus | fable
    studio_effort_cap: str | None = None  # low | medium | high | xhigh | max
    studio_max_runs: int = 3  # Katae Studio: task runs at work at once, over all projects (ARCHITECTURE.md §4.6)
    # Katae Studio: seconds a task run may go on after the answer of a T4 (`ask_owner`, the guard) before the
    # server cuts it — 기다림 = 런 종료 (ARCHITECTURE.md §8.3 '서버의 뒤처리'). 0 = at once.
    studio_cut_after: float = 60.0
    # Katae Studio: seconds the editor check of a task run waits for the project's editor to answer `health_check`
    # before the system asks the owner (`editor_unresponsive`) — ARCHITECTURE.md §4.3 T3. Read at every check.
    studio_editor_wait: float = 300.0
    # Katae Studio: the turns employees may put in for each other in one task (`room_post` mentions) before the
    # system asks the owner — 왕복 한도 (ARCHITECTURE.md §4.6). Read at every mention; T5 of that item counts anew.
    studio_max_exchanges: int = 20
    # Katae Studio: hosts an employee may upload to without the owner being asked, besides the git remotes of
    # the registered projects (ARCHITECTURE.md §10.4 의 5). An approved upload does not add to it.
    studio_known_hosts: list[str] = []
    # Katae Studio: what each part looks after, by department id — globs of paths from the project folder, any
    # case; one that ends with `/` is everything under that folder. A file a task run changed that fits none of
    # its assignee's part is listed as 담당 밖 변경 in the review package (ARCHITECTURE.md §5.4 `out_of_scope`).
    # A part without a list is not judged.
    studio_scopes: dict[str, list[str]] = {}
    # Katae Studio: the Firebase service account key file the pushes are sent with (FCM HTTP v1, N-1 —
    # ARCHITECTURE.md §5.9). "" = no push. A path that is not absolute is taken from this repository's folder.
    # Read at the first push, not at the start: a file that is missing or cannot be used leaves the push off and
    # the server runs as before. Keep the file outside every repository, in `.katae` of the user folder — the
    # guard keeps the employees out of there. No place is looked in that is not named here.
    studio_fcm_key: str = ""
    # Katae Studio: seconds an inbox item may wait before the owner's devices are told of it again, counted from
    # when it was made or last told of (N-2 — ARCHITECTURE.md §5.9 재알림). 0 or less = no reminder. Read at every
    # look at the waiting items (the server's start, then once a minute).
    studio_remind_after: float = 7200.0
    # Katae Studio: the folder a `source` of `studio_plugins` that is not an absolute path is taken from — the
    # MNYS project's folder, as an absolute path (M-4 — ARCHITECTURE.md §6.4). Each machine has its own, so it
    # goes in relay.config.local.yaml. "" = not set: such a plugin has no source and cannot be chosen.
    studio_plugin_root: str = ""
    # Katae Studio: the folder of the game-harness repository, as an absolute path — where `bootstrap.bat` and
    # `index_build.bat` are (M-5 — ARCHITECTURE.md §6.4, studio/harness.py). Each machine has its own, so it goes
    # in relay.config.local.yaml. "" = not set: the `harness` step of a new project fails unless it is turned off.
    studio_harness_root: str = ""
    # Katae Studio: the plugins a new UE project may be given, in the order the making screen lists them (M-4 —
    # ARCHITECTURE.md §6.4, studio/plugins.py). `id`: what `plugins[]` of the request names. `name`: what the
    # screen shows. `source`: the plugin's folder, absolute or from `studio_plugin_root`; it is only read.
    # `folder` (may be left out): the folder's name under the project's `Plugins/` — the last part of `source`
    # when not given. `engines`: the engine versions it is confirmed on, as quoted strings ("5.7"); [] = all. On
    # another engine it is listed unchecked with a warning, and may still be chosen. `mcp` (the MCP plugin only):
    # the server put in the project's `.mcp.json` — `server` (its name; "unreal" when left out), `command` and
    # `args`, in which `{plugin}` and `{project}` become the absolute paths of the copied plugin's folder and of
    # the project. A project given a plugin with `mcp` gets an MCP port of its own (55558 and up). The `mcp` of
    # unreal_mcp is tentative — MNYS's own entry could not be read (design/studio/VERIFICATION.md): correct it
    # here or in relay.config.yaml, not in the code.
    studio_plugins: list[dict] = [
        {"id": "unreal_mcp", "name": "Unreal MCP", "source": "Plugins/Unreal-MCP-main", "engines": [],
         "mcp": {"server": "unreal", "command": "uv",
                 "args": ["--directory", "{plugin}/Python", "run", "unreal_mcp_server.py"]}},
        {"id": "ys_tag_usage", "name": "YSTagUsage", "source": "Plugins/YSTagUsage", "engines": ["5.7"]},
    ]

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """relay.config.yaml (shared, in git) overlaid with relay.config.local.yaml (this machine only)."""
        path = path or ROOT / "relay.config.yaml"
        data: dict = {}
        for p in (path, path.with_name(path.stem + ".local.yaml")):
            if p.exists():
                data = _merge(data, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
        cfg = cls.model_validate(data)
        cfg.auth_token = os.environ.get("KATAE_TOKEN", cfg.auth_token)
        cfg.serve_host = os.environ.get("KATAE_HOST", cfg.serve_host)
        for name in ("runs_dir", "usage_db", "history_db", "relays_dir", "docs_root"):
            p = getattr(cfg, name)
            if not p.is_absolute():
                setattr(cfg, name, (ROOT / p).resolve())
        return cfg

    @property
    def studio_dir(self) -> Path:
        """Katae Studio data (design/studio/ARCHITECTURE.md §2.2). Derived, so it follows runs_dir."""
        return self.runs_dir / "studio"

    @property
    def studio_db(self) -> Path:
        return self.studio_dir / "studio.sqlite"

    @cached_property
    def mcp_registry(self) -> dict[str, dict]:
        """Built-in MCP servers plus the configured ones (configured entries win)."""
        py = sys.executable
        builtin = {
            "docs_read": {"command": py, "args": [str(ROOT / "mcp_servers" / "docs_read.py"), str(self.docs_root)]},
            "handoff": {"command": py, "args": [str(ROOT / "mcp_servers" / "handoff.py"), str(self.runs_dir)]},
            "digest": {"command": py, "args": [str(ROOT / "mcp_servers" / "digest.py"), str(self.runs_dir / "digest-cache")]},
            # its env (server address, actor token) is filled per stage by RelayEngine.mcp_env, for studio runs only
            "studio": {"command": py, "args": [str(ROOT / "mcp_servers" / "studio.py")]},
        }
        return builtin | self.mcp_servers


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def build_engine(cfg: Config):
    """Engine wired with the configured stores, provider registry and real runners."""
    from .history import HistoryStore
    from .notify import Notifier
    from .handoff import HandoffWriter
    from .pipeline import RelayEngine
    from .providers import ProviderRegistry
    from .repos import RepoRegistry
    from .runners import make_runner
    from .usage import UsageStore

    from . import mcpcheck, router

    usage, history = UsageStore(cfg.usage_db), HistoryStore(cfg.history_db)
    providers = ProviderRegistry(cfg.providers, cfg.fallback_chain, usage=usage, history=history)
    has_claude = shutil.which("claude") is not None
    return RelayEngine(
        cfg.runs_dir, usage, history,
        runner_factory=lambda name: make_runner(name, providers, cfg.mcp_registry),
        providers=providers,
        workspace_excludes=cfg.workspace_excludes,
        max_snapshot_mb=cfg.max_snapshot_mb,
        max_file_mb=cfg.max_file_mb,
        extra_allowed_tools=cfg.extra_allowed_tools,
        repos=RepoRegistry(cfg.repos),
        mcp_registry=cfg.mcp_registry,
        notifier=Notifier(cfg.server_url, cfg.notify),
        handoff_writer=HandoffWriter(cfg.handoff_model) if cfg.handoff_model and has_claude else None,
        # same one-shot call as the hand-over, with the router's own instructions (model from relays/auto.yaml)
        router=(lambda text, model: HandoffWriter(model, timeout_s=90, system=router.SYSTEM)(text)) if has_claude else None,
        mcp_probe=mcpcheck.probe if cfg.mcp_check else None,
    )
