"""Relay engine: runs stages in order, passing only the baton between them.

Features:
- per-stage provider / model / effort / tools / MCP servers, with alternates
- automatic provider switch when one is over its usage limit, unavailable, or out of budget
- human gate: pause after a stage until approved; run budget: pause when spend passes the limit
- review loop-back: verdict "retry" jumps back to `on_retry` (optionally stronger model/effort)
- workspace: snapshot the target folder, work in a copy (or in place), return result.patch
- cancel a running relay; runs interrupted by a server restart become resumable
- sessions and activity events in HistoryStore; checkpoints in runs/<id>/
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import yaml
from pydantic import BaseModel, Field

from .baton import Baton, StopNote
from .history import HistoryStore
from .providers import LEGACY_RUNNER, ProviderRegistry
from .runners import Runner, RunnerError, StageCall, make_runner
from .usage import UsageStore
from .repos import RepoRegistry, RepoSpec
from .workspace import DEFAULT_EXCLUDES, Workspace, WorkspaceError, dir_size, gc_shadow

WRITE_TOOLS = {"Edit", "Write", "Bash", "NotebookEdit"}
VERIFY_NOISE_PREFIX = re.compile(r'^cd\s+("[^"]*"|\S+)\s*&&\s*')
LOOKAROUND = {"ls", "dir", "pwd", "cat", "head", "tail", "echo", "find", "tree", "cd"}


class Alternate(BaseModel):
    provider: str
    model: str | None = None
    effort: str | None = None


class StageSpec(BaseModel):
    name: str
    provider: str | None = Field(None, description="claude | anthropic_api | codex | opencode | gemini | openai | ...")
    runner: str = "claude_cli"  # legacy alias for provider
    model: str | None = "sonnet"
    effort: str | None = None
    alternates: list[Alternate] = Field(default_factory=list, description="tried in order when the primary can't run")
    optional: bool = Field(False, description="skip instead of failing when no provider is usable")
    prompt: str = Field(description="role prompt file, relative to the relay file")
    tools: list[str] | None = None
    mcp: list[str] = []
    allowed_tools: list[str] = Field(default_factory=list, description='pre-approved, e.g. "Bash(uv run pytest:*)"')
    permission_mode: str = "default"
    reads_outputs: list[str] = Field(default_factory=list, description="earlier stage outputs to include")
    gate: Literal["none", "human"] = "none"
    on_retry: str | None = None
    max_retries: int = 1
    timeout_s: int = 1800
    # token controls
    retry_effort: str | None = Field(None, description="effort used when this stage runs again after a send-back")
    retry_model: str | None = Field(None, description="stronger model used when this stage runs again")
    system_mode: Literal["replace", "append"] = "replace"
    isolate: bool = False
    max_budget_usd: float | None = None
    fallback_model: str | None = None

    @property
    def primary(self) -> str:
        return self.provider or LEGACY_RUNNER.get(self.runner, self.runner)

    @property
    def writes(self) -> bool:
        return bool(WRITE_TOOLS & set(self.tools or []))


class RelaySpec(BaseModel):
    name: str
    description: str = ""
    workspace: Literal["none", "copy", "inplace"] = "none"
    max_run_cost_usd: float | None = Field(None, description="pause for approval once a run spends this much")
    stages: list[StageSpec]

    @classmethod
    def load(cls, path: Path) -> tuple["RelaySpec", Path]:
        spec = cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        names = [s.name for s in spec.stages]
        for s in spec.stages:
            if s.on_retry and s.on_retry not in names[: names.index(s.name)]:
                raise ValueError(f"{s.name}.on_retry must name an earlier stage")
        return spec, path.parent


class StageRecord(BaseModel):
    stage: str
    at: str
    verdict: str
    runner: str
    model: str
    total_input: int
    output_tokens: int
    cost_usd: float | None


class RunState(BaseModel):
    id: str
    session_id: str
    relay: str
    workdir: str
    status: Literal["pending", "running", "awaiting_approval", "done", "failed", "cancelled"] = "pending"
    index: int = 0
    retries: dict[str, int] = {}
    baton: Baton
    history: list[StageRecord] = []
    error: str | None = None
    budget_extra_usd: float = 0.0  # raised each time the user approves past the run budget
    repo: str | None = None  # registered repository name, if the run targets one
    workspace_mode: Literal["none", "copy", "inplace"] = "none"
    changes: dict | None = None  # {files, insertions, deletions, stat}
    changes_status: Literal["none", "ready", "applied", "discarded", "rolled_back"] = "none"
    workspace_cleaned: bool = False  # copy/snapshot deleted after retention; result.patch kept

    @property
    def cost_usd(self) -> float:
        return sum(h.cost_usd or 0 for h in self.history)


RESUMABLE = ("pending", "failed", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


STAGE_FOOTER = """
---
## 이번 단계: {stage}
- 작업 디렉터리: `{workdir}`
- 위 HANDOFF 가 이전 단계에서 넘어온 전부다. 필요한 파일은 포인터를 따라 필요한 부분만 읽어라.
- 결과는 지정된 JSON 스키마로만 반환한다. output 에는 다음 단계가 읽어야 할 내용만 간결하게 쓴다.
"""


class RelayEngine:
    def __init__(
        self,
        runs_dir: Path,
        usage: UsageStore,
        history: HistoryStore,
        runner_factory: Callable[[str], Runner] | None = None,
        providers: ProviderRegistry | None = None,
        workspace_excludes: list[str] | None = None,
        extra_allowed_tools: list[str] | None = None,
        repos: RepoRegistry | None = None,
        mcp_registry: dict[str, dict] | None = None,
        max_snapshot_mb: float = 500,
        max_file_mb: float = 20,
    ):
        self.max_snapshot_mb = max_snapshot_mb
        self.max_file_mb = max_file_mb
        self.extra_allowed_tools = extra_allowed_tools or []
        self.repos = repos or RepoRegistry()
        self.mcp_registry = mcp_registry or {}
        self.runs_dir = runs_dir
        self.usage = usage
        self.history = history
        self.providers = providers
        self.workspace_excludes = workspace_excludes
        self._runner_factory = runner_factory or (lambda name: make_runner(name, providers))
        self._runners: dict[str, Runner] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._cancel: dict[str, threading.Event] = {}

    # --- persistence -------------------------------------------------------
    def _dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def save(self, run: RunState) -> None:
        d = self._dir(run.id)
        d.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a poller never reads a half-written file, a crash never leaves a truncated one.
        tmp = d / f"run.json.{threading.get_ident()}.tmp"
        tmp.write_text(run.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, d / "run.json")
        (d / "HANDOFF.md").write_text(run.baton.to_markdown(max_output_chars=None), encoding="utf-8")
        # Full stage outputs live on disk; prompts carry a capped copy plus this path.
        (d / "outputs").mkdir(exist_ok=True)
        for stage, text in run.baton.outputs.items():
            (d / "outputs" / f"{stage}.md").write_text(text, encoding="utf-8")
        self.history.update_turn(run.id, run.status, self._result_text(run))

    @staticmethod
    def _result_text(run: RunState) -> str:
        if run.error:
            return run.error
        last = run.history[-1].stage if run.history else None
        output = run.baton.outputs.get(last, "") if last else ""
        return f"{run.baton.state}\n\n{output}".strip()

    def load(self, run_id: str) -> RunState:
        return RunState.model_validate_json((self._dir(run_id) / "run.json").read_text(encoding="utf-8"))

    def list_runs(self) -> list[RunState]:
        runs = []
        for p in sorted(self.runs_dir.glob("*/run.json")):
            try:
                runs.append(self.load(p.parent.name))
            except (OSError, ValueError):
                continue  # one damaged run must not take the server down
        return runs

    def _event(self, run: RunState, stage: str, kind: str, **detail) -> None:
        self.history.add_event(run.id, stage, kind, detail)

    def workspace(self, run: RunState) -> Workspace | None:
        if run.workspace_mode == "none":
            return None
        excludes = list(self.workspace_excludes if self.workspace_excludes is not None else DEFAULT_EXCLUDES)
        repo = self._repo(run)
        if repo:
            excludes += repo.excludes
        return Workspace(
            self._dir(run.id), Path(run.workdir), run.workspace_mode, excludes,
            shadow_root=self.runs_dir / "shadow",
            include_ignored=bool(repo and repo.include_ignored),
            max_snapshot_mb=(repo.max_snapshot_mb if repo and repo.max_snapshot_mb else self.max_snapshot_mb),
            max_file_mb=self.max_file_mb,
        )

    def _repo(self, run: RunState) -> RepoSpec | None:
        return self.repos.repos.get(run.repo) if run.repo else None

    # --- lifecycle ---------------------------------------------------------
    def create(self, relay_path: Path, goal: str, workdir: Path | None = None, session_id: str | None = None,
               workspace: str | None = None, repo: str | None = None) -> RunState:
        """Start a run as a new turn. Without session_id a new session is opened.
        With a registered repo, its path, default workspace mode, verify commands and notes apply."""
        spec, _ = RelaySpec.load(relay_path)
        session = None
        if session_id is not None:
            session = self.history.get_session(session_id)
            if session is None:
                raise ValueError(f"session {session_id} not found")
            repo = repo or session.get("repo")
        repo_spec = None
        if repo:
            try:
                repo_spec = self.repos.get(repo)
            except KeyError as e:
                raise ValueError(str(e)) from e
            if workdir is None:
                if not repo_spec.exists:
                    raise ValueError(f"저장소 {repo} 의 경로가 이 머신에 없습니다: {repo_spec.path or '(미지정)'}")
                workdir = repo_spec.resolved
        if workdir is None:
            workdir = Path(session["workdir"]) if session else None
        if workdir is None:
            raise ValueError("workdir 또는 repo 가 필요합니다")
        if session_id is None:
            session_id = self.history.create_session(goal[:60], str(workdir.resolve()), repo)["id"]
        if workspace is None and not any(s.writes for s in spec.stages):
            workspace = "none"  # read-only relay: a snapshot would cost time and disk for nothing
        mode = workspace or (repo_spec.workspace if repo_spec else None) or spec.workspace
        if mode not in ("none", "copy", "inplace"):
            raise ValueError(f"workspace must be none, copy or inplace (got {mode})")
        run = RunState(
            id=datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
            session_id=session_id,
            relay=str(relay_path.resolve()),
            workdir=str(workdir.resolve()),
            baton=Baton(goal=goal, session_context=self.history.session_context(session_id)),
            workspace_mode=mode,
            repo=repo,
        )
        self.history.add_turn(session_id, goal, spec.name, run.id)
        self._event(run, "-", "run_created", relay=spec.name, workdir=run.workdir, workspace=mode, repo=repo,
                    stages=[s.name for s in spec.stages])
        self.save(run)
        return run

    def approve(self, run_id: str) -> RunState:
        run = self.load(run_id)
        if run.status != "awaiting_approval":
            raise ValueError(f"run {run_id} is {run.status}, not awaiting_approval")
        spec, _ = RelaySpec.load(Path(run.relay))
        if spec.max_run_cost_usd and run.cost_usd >= spec.max_run_cost_usd + run.budget_extra_usd:
            # approved past the budget: allow one more full budget on top of what is already spent
            run.budget_extra_usd = run.cost_usd
        run.status = "pending"
        self._event(run, "-", "approved")
        self.save(run)
        return run

    def cancel(self, run_id: str) -> RunState:
        """Stop a running relay (kills the current AI process) or a paused one. Resumable later."""
        event = self._cancel.get(run_id)
        if event is not None:
            # A thread owns this run (running, or just about to start): let it record the cancellation,
            # otherwise its next save would overwrite ours.
            event.set()
            return self.load(run_id)
        run = self.load(run_id)
        if run.status in ("done", "cancelled"):
            raise ValueError(f"run {run_id} is already {run.status}")
        run.status, run.error = "cancelled", "사용자가 취소함"
        self._event(run, "-", "run_cancelled")
        self._write_stop(run, "cancelled", "-", "사용자가 취소함")
        self._settle_workspace(run)
        self.save(run)
        return run

    # --- one writer per folder ------------------------------------------------
    def _folder_lock(self, folder: str) -> Path:
        key = hashlib.sha1(os.path.normcase(os.path.abspath(folder)).encode()).hexdigest()[:16]
        return self.runs_dir / "locks" / f"{key}.lock"

    def folder_holder(self, folder: str, exclude: str | None = None) -> str | None:
        """Run id currently editing this folder in place (file lock shared by server and CLI)."""
        path = self._folder_lock(folder)
        if not path.exists():
            return None
        holder = path.read_text(encoding="utf-8").strip()
        if holder == exclude:
            return None
        try:
            alive = self.load(holder).status == "running"
        except (OSError, ValueError):
            alive = False
        if not alive:
            path.unlink(missing_ok=True)  # stale lock from a finished or crashed run
            return None
        return holder

    def _acquire_folder(self, run: RunState) -> str | None:
        """Take the in-place lock for run.workdir. Returns the other run id if the folder is busy."""
        path = self._folder_lock(run.workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self.folder_holder(run.workdir, exclude=run.id)
                if holder:
                    return holder
                if path.exists() and path.read_text(encoding="utf-8").strip() == run.id:
                    return None
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(run.id)
            return None
        return "?"

    def _release_folder(self, run_id: str, folder: str) -> None:
        path = self._folder_lock(folder)
        try:
            if path.read_text(encoding="utf-8").strip() == run_id:
                path.unlink()
        except OSError:
            pass

    def cleanup_workspaces(self, retention_days: float = 7) -> dict:
        """Free disk: workspaces with nothing left to decide go now; undecided ones (patch not applied,
        failed/cancelled runs that could be resumed) go after retention_days. result.patch is always kept."""
        cutoff = time.time() - retention_days * 86400
        cleaned, freed = [], 0
        for run in self.list_runs():
            if run.workspace_mode == "none" or run.workspace_cleaned or run.status not in ("done", "failed", "cancelled"):
                continue
            decided = run.changes_status in ("applied", "discarded", "rolled_back") or (
                run.status == "done" and run.changes_status == "none")
            try:
                old = (self._dir(run.id) / "run.json").stat().st_mtime < cutoff
            except OSError:
                continue
            if not (decided or old):
                continue
            ws = self.workspace(run)
            try:
                freed += ws.cleanup()
            except OSError:
                continue
            run.workspace_cleaned = True
            self._event(run, "-", "workspace_cleaned", decided=decided)
            self.save(run)
            cleaned.append(run.id)
        freed += gc_shadow(self.runs_dir / "shadow")
        return {"cleaned": cleaned, "freed_mb": round(freed / 1_048_576, 1)}

    def disk_usage(self) -> dict:
        """Where the bytes are: shared snapshot stores (per repo), working copies, patches, databases."""
        stores = []
        shadow = self.runs_dir / "shadow"
        names = {}
        for repo in self.repos.repos.values():
            if repo.resolved:
                from .workspace import shadow_dir_for
                names[shadow_dir_for(shadow, repo.resolved).name] = repo.name
        for store in sorted(shadow.glob("*.git")) if shadow.exists() else []:
            stores.append({"store": store.name, "repo": names.get(store.name), "mb": round(dir_size(store) / 1_048_576, 1)})
        copies, patches, runs = 0, 0, []
        for d in self.runs_dir.iterdir() if self.runs_dir.exists() else []:
            if not d.is_dir() or d.name in ("shadow", "locks"):
                continue
            size = dir_size(d)
            copies += dir_size(d / "workspace") if (d / "workspace").exists() else 0
            patches += (d / "result.patch").stat().st_size if (d / "result.patch").exists() else 0
            runs.append((d.name, size))
        dbs = sum(f.stat().st_size for f in self.runs_dir.glob("*.sqlite*")) if self.runs_dir.exists() else 0
        total = dir_size(self.runs_dir) if self.runs_dir.exists() else 0
        mb = lambda b: round(b / 1_048_576, 1)  # noqa: E731
        return {
            "total_mb": mb(total), "snapshot_stores": stores, "working_copies_mb": mb(copies),
            "patches_mb": mb(patches), "databases_mb": mb(dbs), "runs": len(runs),
            "largest_runs": [{"run": r, "mb": mb(s)} for r, s in sorted(runs, key=lambda x: -x[1])[:5]],
        }

    def recover_interrupted(self) -> list[str]:
        """Runs left 'running' by a server stop are marked failed so they can be resumed."""
        recovered = []
        for run in self.list_runs():
            if run.status == "running" and run.id not in self._cancel:
                try:
                    run.status, run.error = "failed", "서버 재시작으로 중단됨 — 재개하면 중단된 단계부터 다시 실행"
                    self._event(run, "-", "run_failed", error=run.error)
                    self.save(run)
                    recovered.append(run.id)
                except (OSError, ValueError):
                    continue
        return recovered

    # --- workspace results -------------------------------------------------
    def _settle_workspace(self, run: RunState) -> None:
        ws = self.workspace(run)
        if ws is None or not ws.prepared or run.changes_status not in ("none", "ready"):
            return
        try:
            run.changes = ws.collect()
        except (WorkspaceError, OSError) as e:
            self._event(run, "-", "workspace_error", error=str(e))
            return
        run.changes_status = "ready" if run.changes["files"] else "none"
        if run.changes["files"]:
            self._event(run, "-", "changes_ready", mode=run.workspace_mode, files=run.changes["files"],
                        insertions=run.changes["insertions"], deletions=run.changes["deletions"])

    def _changes_action(self, run_id: str, action: str) -> RunState:
        run = self.load(run_id)
        if run.status not in ("done", "cancelled"):
            # Applying/discarding mid-relay would remove the workspace later stages still need.
            raise ValueError(f"실행이 끝난 뒤에만 가능합니다 (현재: {run.status}). 중간에 멈추려면 먼저 취소하세요")
        ws = self.workspace(run)
        if ws is None:
            raise ValueError("this run has no workspace")
        if run.workspace_cleaned and action != "apply":
            raise ValueError("보관 기간이 지나 작업 공간이 정리됐습니다 (result.patch 만 남음)")
        if not ws.prepared and not run.workspace_cleaned:
            raise ValueError("this run has no workspace")
        holder = self.folder_holder(run.workdir, exclude=run.id)
        if holder:
            raise ValueError(f"다른 실행({holder})이 이 폴더를 원본에서 수정 중입니다. 끝난 뒤 다시 시도하세요")
        if run.changes_status in ("applied", "discarded", "rolled_back"):
            raise ValueError(f"changes already {run.changes_status}")
        try:
            if action == "apply":
                ws.apply()
                run.changes_status = "applied"
            elif action == "discard":
                ws.discard()
                run.changes_status = "discarded"
            else:
                run.changes = ws.rollback()
                run.changes_status = "rolled_back"
        except WorkspaceError as e:
            raise ValueError(str(e)) from e
        run.workspace_cleaned = True
        self._event(run, "-", f"changes_{run.changes_status}", files=(run.changes or {}).get("files", 0))
        self.save(run)
        gc_shadow(self.runs_dir / "shadow")
        return run

    def apply_changes(self, run_id: str) -> RunState:
        return self._changes_action(run_id, "apply")

    def discard_changes(self, run_id: str) -> RunState:
        return self._changes_action(run_id, "discard")

    def rollback_changes(self, run_id: str) -> RunState:
        return self._changes_action(run_id, "rollback")

    def summary(self, run_id: str) -> dict:
        """At-a-glance result: one line, key actions, changed files, what to check, how it was verified,
        MCP/tool usage and cost. Built from stored state only (no AI call)."""
        run = self.load(run_id)
        events = self.history.events(run_id)
        tools: dict[str, int] = {}
        mcp: dict[str, dict] = {}
        commands: list[str] = []
        large: list[dict] = []
        for e in events:
            d, kind = e["detail"], e["kind"]
            if kind == "tool_use":
                tools[d.get("tool") or "?"] = tools.get(d.get("tool") or "?", 0) + 1
                cmd = VERIFY_NOISE_PREFIX.sub("", d.get("target") or "").strip() if d.get("tool") == "Bash" else ""
                if cmd and re.split(r"[\s;&|]+", cmd)[0] not in LOOKAROUND and cmd not in commands:
                    commands.append(cmd)
            elif kind in ("mcp_call", "mcp_result"):
                entry = mcp.setdefault(f"{d.get('server')}.{d.get('tool')}", {"calls": 0, "result_chars": 0, "errors": 0})
                if kind == "mcp_call":
                    entry["calls"] += 1
                else:
                    entry["result_chars"] += d.get("chars", 0)
                    entry["errors"] += 1 if d.get("is_error") else 0
            elif kind == "tool_result_large":
                large.append({"tool": d.get("tool"), "target": d.get("target"), "chars": d.get("chars")})

        files = []
        for line in ((run.changes or {}).get("stat") or "").splitlines():
            if "|" in line:
                name, _, delta = line.partition("|")
                files.append({"path": name.strip(), "delta": delta.strip()})

        if run.status in ("failed", "cancelled"):
            headline = run.error or ""
        elif run.status == "awaiting_approval" and run.baton.stop:
            headline = run.baton.stop.reason
        else:
            headline = run.baton.log[-1].summary if run.baton.log else run.baton.state
        highlights = [f"[{stage}] {h}" for stage, items in run.baton.highlights.items() for h in items][:6]
        diagram_stage = next(reversed(run.baton.diagrams), None) if run.baton.diagrams else None
        return {
            "id": run.id, "status": run.status, "goal": run.baton.goal, "repo": run.repo,
            "headline": headline, "state": run.baton.state, "highlights": highlights,
            "user_checks": run.baton.user_checks, "open_issues": run.baton.open_issues[:5],
            "changes": {"files": files, "insertions": (run.changes or {}).get("insertions", 0),
                        "deletions": (run.changes or {}).get("deletions", 0), "status": run.changes_status,
                        "mode": run.workspace_mode},
            "verification": commands[-3:],
            "diagram": run.baton.diagrams.get(diagram_stage) if diagram_stage else None,
            "diagram_stage": diagram_stage,
            "tools": tools, "mcp": mcp, "large_results": large[:5],
            "stop": run.baton.stop.model_dump() if run.baton.stop else None,
            "cost_usd": round(run.cost_usd, 4),
            "tokens": sum(h.total_input + h.output_tokens for h in run.history),
            "stages": [{"stage": h.stage, "model": h.model, "provider": h.runner, "verdict": h.verdict,
                        "tokens": h.total_input + h.output_tokens, "cost_usd": h.cost_usd} for h in run.history],
        }

    def patch_text(self, run_id: str) -> str:
        path = self._dir(run_id) / "result.patch"
        return path.read_bytes().decode("utf-8", "replace") if path.exists() else ""

    # --- execution -----------------------------------------------------------
    def _runner(self, name: str) -> Runner:
        if name not in self._runners:
            self._runners[name] = self._runner_factory(name)
        return self._runners[name]

    PARTIAL_KINDS = ("tool_use", "mcp_call")

    def _write_stop(self, run: RunState, kind: str, stage: str, reason: str) -> None:
        """Handoff up to the stopping point, built from the run state and activity log (no AI call)."""
        try:
            spec, _ = RelaySpec.load(Path(run.relay))
            names = [s.name for s in spec.stages]
        except (OSError, ValueError):
            names = []
        done = list(dict.fromkeys(h.stage for h in run.history))
        partial: list[str] = []
        if kind in ("cancelled", "failed") and stage != "-":
            events = self.history.events(run.id)
            starts = [i for i, e in enumerate(events) if e["stage"] == stage and e["kind"] == "stage_started"]
            for e in events[starts[-1] + 1:] if starts else []:
                if e["stage"] == stage and e["kind"] in self.PARTIAL_KINDS:
                    d = e["detail"]
                    label = f"{d.get('server')}.{d.get('tool')}" if e["kind"] == "mcp_call" else d.get("tool")
                    partial.append(f"{label} {d.get('target', '')}".strip()[:160])
            if len(partial) > 10:
                partial = partial[:10] + [f"… 외 {len(partial) - 10}건"]
        remaining = names[run.index:] if names else []
        hint = {
            "cancelled": f"재개하면 `{stage}` 단계부터 다시 실행합니다 (relay resume {run.id})",
            "failed": f"원인을 해결한 뒤 재개하면 `{stage}` 단계부터 다시 실행합니다 (relay resume {run.id})",
            "awaiting_approval": f"승인하면 `{remaining[0] if remaining else stage}` 단계부터 진행합니다 (relay approve {run.id})",
            "budget": f"승인하면 예산 1회분을 더 허용하고 `{stage}` 단계부터 진행합니다 (relay approve {run.id})",
        }.get(kind, "")
        run.baton.stop = StopNote(kind=kind, stage=stage, reason=reason[:300], at=_now(), done_stages=done,
                                  remaining_stages=remaining, partial_actions=partial, resume_hint=hint)

    def _fail(self, run: RunState, stage: str, error: str, status: str = "failed") -> RunState:
        run.status, run.error = status, f"[{stage}] {error}"
        self._event(run, stage, "run_cancelled" if status == "cancelled" else "run_failed", error=error)
        self._write_stop(run, status, stage, error)
        self._settle_workspace(run)
        self.save(run)
        return run

    def _on_stage_event(self, run_id: str, stage: str):
        def handle(kind: str, detail: dict) -> None:
            if kind == "rate_limit":  # subscription usage snapshot, stored, not logged as activity
                self.history.record_limits(detail["provider"], detail.get("status"), detail.get("windows", []))
                return
            self.history.add_event(run_id, stage, kind, detail)
        return handle

    def _model_for(self, provider: str, model: str | None) -> str | None:
        if self.providers is None or provider not in self.providers.specs:
            return model
        return self.providers.get(provider).resolve_model(model)

    def _run_stage(self, run: RunState, stage: StageSpec, call: StageCall):
        """Try the primary provider, then alternates. Returns (result, usage) or raises RunnerError."""
        options = [{"provider": stage.primary}] + [a.model_dump() for a in stage.alternates]
        if self.providers is not None:
            options, skipped = self.providers.candidates(stage.primary, options[1:], stage.writes)
            if skipped:
                self._event(run, stage.name, "providers_skipped", skipped=skipped)
        if not options:
            raise RunnerError("사용 가능한 AI 가 없습니다 (모두 미설치·한도 초과·예산 소진)", "unavailable")

        last_error: RunnerError | None = None
        for i, option in enumerate(options):
            name = option["provider"]
            if i > 0 or name != stage.primary:
                self._event(run, stage.name, "provider_switch", to=name,
                            reason=str(last_error)[:200] if last_error else "기본 AI 사용 불가")
            is_claude = name in ("claude", "anthropic_api", "mock") or (
                self.providers is not None and self.providers.get(name).kind in ("claude_cli", "api"))
            attempt = replace(
                call,
                model=self._model_for(name, option.get("model") or call.model),
                effort=option.get("effort") or call.effort,
                fallback_model=call.fallback_model if is_claude else None,
            )
            try:
                return self._runner(name).run(attempt)
            except RunnerError as e:
                if e.kind == "cancelled":
                    raise
                last_error = e
                if e.kind == "quota" and self.providers is not None:
                    until = self.providers.mark_exhausted(name, str(e)[:200])
                    self._event(run, stage.name, "provider_exhausted", provider=name, until=until)
                if e.kind not in ("quota", "unavailable") or i == len(options) - 1:
                    raise
        raise last_error or RunnerError("no provider ran")

    def advance(self, run_id: str, resume: bool = False) -> RunState:
        """Run stages until done, failed, cancelled, or a pause. A failed or cancelled run only continues
        when resume=True, so a start that races with a cancel can't revive the run."""
        lock = self._locks.setdefault(run_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ValueError(f"run {run_id} is already advancing")
        cancel = self._cancel[run_id] = threading.Event()
        try:
            return self._advance(run_id, cancel, resume)
        except Exception as e:  # never leave a run stuck in "running" on an unexpected error
            run = self.load(run_id)
            if run.status == "running":
                return self._fail(run, "-", f"예상치 못한 오류: {type(e).__name__}: {e}")
            raise
        finally:
            self._cancel.pop(run_id, None)
            try:
                self._release_folder(run_id, self.load(run_id).workdir)
            except (OSError, ValueError):
                pass
            lock.release()

    def _advance(self, run_id: str, cancel: threading.Event, resume: bool) -> RunState:
        run = self.load(run_id)
        if run.status not in (RESUMABLE if resume else ("pending",)):
            return run
        spec, base = RelaySpec.load(Path(run.relay))
        names = [s.name for s in spec.stages]
        if run.workspace_mode == "inplace":
            holder = self._acquire_folder(run)
            if holder:
                run.status, run.error = "failed", f"[-] 다른 실행({holder})이 같은 폴더를 원본에서 수정 중 — 그 실행이 끝나면 재개하세요"
                self._event(run, "-", "run_failed", error=run.error)
                self._write_stop(run, "failed", "-", run.error)
                self.save(run)
                return run
        run.status, run.error = "running", None
        self.save(run)

        ws = self.workspace(run)
        if ws is not None and not ws.prepared:
            try:
                info = ws.prepare()
            except (WorkspaceError, OSError) as e:
                return self._fail(run, "-", f"작업 공간 준비 실패: {e}")
            self._event(run, "-", "workspace_ready", **info)
        cwd = ws.path if ws is not None else Path(run.workdir)
        repo = self._repo(run)
        repo_lines = "\n".join(repo.prompt_lines(repo.git_info().get("branch"))) + "\n" if repo else ""
        repo_tools = (repo.verify_tools() + repo.allowed_tools) if repo else []
        mcp_overrides = {}
        if repo and repo.docs_root and "docs_read" in self.mcp_registry:
            entry = dict(self.mcp_registry["docs_read"])
            entry["args"] = list(entry.get("args", []))[:-1] + [str(repo.docs_root)]
            mcp_overrides["docs_read"] = entry  # this repo's docs, not the global docs_root

        while run.index < len(spec.stages):
            stage = spec.stages[run.index]
            if cancel.is_set():  # a cancel that arrived between stages
                return self._fail(run, stage.name, "사용자가 취소함", status="cancelled")
            limit = spec.max_run_cost_usd
            if limit and run.cost_usd >= limit + run.budget_extra_usd:
                run.status = "awaiting_approval"
                self._event(run, stage.name, "budget_exceeded", spent_usd=round(run.cost_usd, 4),
                            limit_usd=limit + run.budget_extra_usd, next=stage.name)
                self._write_stop(run, "budget", stage.name,
                                 f"실행 비용 ${run.cost_usd:.2f} 가 한도 ${limit + run.budget_extra_usd:.2f} 에 도달")
                self._settle_workspace(run)
                self.save(run)
                return run

            # Cheap first pass; spend more only when this stage is being redone.
            redo = any(h.stage == stage.name for h in run.history)
            effort = stage.retry_effort if redo and stage.retry_effort else stage.effort
            model = stage.retry_model if redo and stage.retry_model else stage.model
            self._event(run, stage.name, "stage_started", runner=stage.primary, model=model,
                        effort=effort, reads=stage.reads_outputs, system_mode=stage.system_mode,
                        escalated=redo and bool(stage.retry_model or stage.retry_effort))
            call = StageCall(
                stage=stage.name,
                model=model,
                effort=effort,
                system=(base / stage.prompt).read_text(encoding="utf-8"),
                prompt=run.baton.to_markdown(
                    include_outputs=stage.reads_outputs,
                    output_ref=str(self._dir(run.id) / "outputs" / "{stage}.md"),
                )
                + STAGE_FOOTER.format(stage=stage.name, workdir=cwd) + repo_lines,
                cwd=cwd,
                tools=stage.tools,
                mcp_servers=stage.mcp,
                allowed_tools=stage.allowed_tools + (
                    self.extra_allowed_tools + repo_tools if "Bash" in (stage.tools or []) else []),
                mcp_overrides=mcp_overrides,
                permission_mode=stage.permission_mode,
                timeout_s=stage.timeout_s,
                system_mode=stage.system_mode,
                isolate=stage.isolate,
                max_budget_usd=stage.max_budget_usd,
                fallback_model=stage.fallback_model,
                on_event=self._on_stage_event(run.id, stage.name),
                cancel_event=cancel,
            )
            try:
                result, usage = self._run_stage(run, stage, call)
            except RunnerError as e:
                if e.kind == "cancelled" or cancel.is_set():
                    return self._fail(run, stage.name, "사용자가 취소함", status="cancelled")
                if e.kind == "unavailable" and stage.optional:
                    self._event(run, stage.name, "stage_skipped", reason=str(e)[:200])
                    run.index += 1
                    self.save(run)
                    continue
                return self._fail(run, stage.name, str(e))
            except (OSError, ValueError) as e:
                return self._fail(run, stage.name, str(e))

            self.usage.record(run.id, stage.name, usage)
            run.baton = result.apply(run.baton, stage.name)
            run.history.append(StageRecord(
                stage=stage.name, at=_now(), verdict=result.verdict, runner=usage.runner,
                model=usage.model, total_input=usage.total_input,
                output_tokens=usage.output_tokens, cost_usd=usage.cost_usd,
            ))
            self._event(run, stage.name, "stage_finished", summary=result.summary, verdict=result.verdict,
                        provider=usage.runner, model=usage.model, estimated=usage.estimated,
                        input_tokens=usage.total_input, output_tokens=usage.output_tokens,
                        cost_usd=usage.cost_usd, duration_ms=usage.duration_ms)

            if result.verdict == "fail":
                return self._fail(run, stage.name, f"verdict=fail: {result.summary}")
            if result.verdict == "retry" and stage.on_retry:
                count = run.retries.get(stage.name, 0) + 1
                run.retries[stage.name] = count
                if count > stage.max_retries:
                    return self._fail(run, stage.name, f"retries exhausted ({stage.max_retries})")
                run.index = names.index(stage.on_retry)
                self._event(run, stage.name, "sent_back", to=stage.on_retry, attempt=count,
                            issues=result.open_issues)
                self.save(run)
                continue

            run.index += 1
            if stage.gate == "human" and run.index < len(spec.stages):
                run.status = "awaiting_approval"
                self._event(run, stage.name, "awaiting_approval", next=spec.stages[run.index].name)
                self._write_stop(run, "awaiting_approval", stage.name, f"`{stage.name}` 결과 확인 후 승인 필요")
                self._settle_workspace(run)
                self.save(run)
                return run
            self.save(run)

        run.status = "done"
        run.baton.stop = None
        self._event(run, "-", "run_done")
        self._settle_workspace(run)
        if ws is not None and run.changes_status == "none" and not run.workspace_cleaned:
            try:
                ws.cleanup()
                run.workspace_cleaned = True
            except OSError:
                pass
        self.save(run)
        return run
