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
    notify: bool = True  # Windows toast when a run finishes, fails or needs approval
    handoff_model: str = "haiku"  # writes the hand-over when a run stops ("" = no AI, engine-built only)

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

    @cached_property
    def mcp_registry(self) -> dict[str, dict]:
        """Built-in MCP servers plus the configured ones (configured entries win)."""
        py = sys.executable
        builtin = {
            "docs_read": {"command": py, "args": [str(ROOT / "mcp_servers" / "docs_read.py"), str(self.docs_root)]},
            "handoff": {"command": py, "args": [str(ROOT / "mcp_servers" / "handoff.py"), str(self.runs_dir)]},
            "digest": {"command": py, "args": [str(ROOT / "mcp_servers" / "digest.py"), str(self.runs_dir / "digest-cache")]},
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

    from . import router

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
    )
