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
import json
import os
import re
import shutil
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
from .notify import NOTIFY_KINDS
from .projctx import ProjectContext, discover
from .runners import LiveChannel, pid_alive, Runner, RunnerError, StageCall, Usage, make_runner
from .usage import UsageStore
from .repos import RepoRegistry, RepoSpec
from .journal import JournalWorkspace
from .workspace import DEFAULT_EXCLUDES, Workspace, WorkspaceError, dir_size, gc_shadow

WRITE_TOOLS = {"Edit", "Write", "Bash", "NotebookEdit"}
READ_ONLY_MCP = {"docs_read", "handoff", "digest"}  # every other MCP server may change the real project
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
    stall_s: int = 480  # no output from the AI for this long -> process killed and the stage started once more
    # token controls
    retry_effort: str | None = Field(None, description="effort used when this stage runs again after a send-back")
    retry_model: str | None = Field(None, description="stronger model used when this stage runs again")
    system_mode: Literal["replace", "append"] = "replace"
    isolate: bool = False
    # text: the result is a JSON object at the end of the answer (measured: one turn less than the
    # structured-output tool, and that turn re-reads the whole context). schema: Claude Code --json-schema.
    result_mode: Literal["text", "schema"] = "text"
    # auto: keep Bash only if the repo has verify commands or allowed Bash tools (or no repo is registered).
    # The Bash tool definition alone is ~3.5K tokens, re-read on every turn. always: keep it.
    bash: Literal["auto", "always"] = "auto"
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
    auto_apply: bool = False  # copy mode: apply the patch to the original as soon as the run finishes
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
    stage_budget_boost: dict[str, float] = {}  # stage -> multiplier on max_budget_usd, doubled per approval
    pending_budget_stage: str | None = None  # stage paused because it hit its own budget
    repo: str | None = None  # registered repository name, if the run targets one
    auto_apply: bool = False
    stage_models: dict[str, dict] = {}  # stage -> {"provider": ..., "model": ...} chosen for this run
    notes_seen: int = 0  # lines of notes.jsonl already merged into the baton
    # design gates: ai = continue unless the stage itself asks for a decision (needs_approval),
    # always = pause every time, never = run through. Budget limits pause in every mode.
    approval: Literal["ai", "always", "never"] = "ai"
    owner_pid: int | None = None
    handoff_closed: bool = False  # the user marked the session's work complete: no HANDOFF.md, nothing carried on
    # stage -> Claude Code conversation id of an unfinished attempt (continued on resume, dropped when it finishes)
    stage_sessions: dict[str, str] = {}  # process advancing the run (server or a CLI); recovery leaves live owners alone
    workspace_mode: Literal["none", "copy", "inplace"] = "none"
    changes: dict | None = None  # {files, insertions, deletions, stat}
    changes_status: Literal["none", "ready", "applied", "discarded", "rolled_back"] = "none"
    workspace_cleaned: bool = False  # copy/snapshot deleted after retention; result.patch kept

    @property
    def cost_usd(self) -> float:
        return sum(h.cost_usd or 0 for h in self.history)


RESUMABLE = ("pending", "failed", "cancelled")


def _replace(tmp: Path, dst: Path) -> None:
    """os.replace with retries: on Windows the rename fails with WinError 5 while another process (the
    dashboard reading run.json, antivirus, a drive sync) holds the target open. As a last resort write in place."""
    for attempt in range(8):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            time.sleep(0.05 * (2 ** attempt))  # ~13 s in total
    try:
        dst.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
        tmp.unlink(missing_ok=True)
    except OSError as e:
        raise OSError(f"{dst.name} 저장 실패 (다른 프로그램이 파일을 잡고 있음: {e})") from e


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def prompt_breakdown(system: str, prompt: str) -> dict:
    """Size of each part of what a stage is sent (chars; ~3 chars per token for mixed Korean/code)."""
    sections: dict[str, int] = {}
    current = "(머리말)"
    for line in prompt.splitlines(keepends=True):
        if line.startswith("## ") or line.startswith("# "):
            current = line.strip("# \n")[:40]
        sections[current] = sections.get(current, 0) + len(line)
    return {"system_chars": len(system), "prompt_chars": len(prompt),
            "est_tokens": (len(system) + len(prompt)) // 3,
            "sections": dict(sorted(sections.items(), key=lambda kv: -kv[1])[:10])}


RESUME_LINE = ("- 이 실행은 중단됐다가 재개됐다. 위 `중단 지점` HANDOFF 를 반드시 먼저 읽고, "
               "'작업된 내역'은 반복하지 말고 '남은 일'부터 한다.\n")

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
        notifier: Callable[[str, str, str, dict], None] | None = None,
        handoff_writer: Callable[[str], tuple[str, Usage]] | None = None,
        max_snapshot_mb: float = 500,
        max_file_mb: float = 20,
    ):
        self.max_snapshot_mb = max_snapshot_mb
        self.max_file_mb = max_file_mb
        self.extra_allowed_tools = extra_allowed_tools or []
        self.repos = repos or RepoRegistry()
        self.mcp_registry = mcp_registry or {}
        self.notifier = notifier
        self.handoff_writer = handoff_writer  # lightweight model that writes the stop hand-over
        self.runs_dir = runs_dir
        self.usage = usage
        self.history = history
        self.providers = providers
        self.workspace_excludes = workspace_excludes
        self._runner_factory = runner_factory or (lambda name: make_runner(name, providers))
        self._runners: dict[str, Runner] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._live: dict[str, LiveChannel] = {}
        self._pulse: dict[str, dict] = {}  # run -> last sign of life from the running AI
        self._pulse_written: dict[str, float] = {}  # run -> stdin of the AI session running right now

    # --- persistence -------------------------------------------------------
    def _dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def save(self, run: RunState) -> None:
        d = self._dir(run.id)
        d.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a poller never reads a half-written file, a crash never leaves a truncated one.
        tmp = d / f"run.json.{threading.get_ident()}.tmp"
        tmp.write_text(run.model_dump_json(indent=2), encoding="utf-8")
        _replace(tmp, d / "run.json")
        # HANDOFF.md stays until the user marks the session's work complete; the record stays in run.json
        if run.handoff_closed:
            (d / "HANDOFF.md").unlink(missing_ok=True)
        else:
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
        path = self._dir(run_id) / "run.json"
        for attempt in range(5):  # a replace by the run thread may be in flight
            try:
                return RunState.model_validate_json(path.read_text(encoding="utf-8"))
            except (PermissionError, FileNotFoundError):
                if attempt == 4 or not self._dir(run_id).is_dir():
                    raise
                time.sleep(0.05 * (attempt + 1))
        raise FileNotFoundError(path)

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
        if self.notifier is not None and kind in NOTIFY_KINDS:
            try:
                self.notifier(run.id, kind, run.baton.goal.splitlines()[0][:80], detail)
            except Exception:  # an alert must never break the run
                pass

    def workspace(self, run: RunState) -> Workspace | None:
        if run.workspace_mode == "none":
            return None
        excludes = list(self.workspace_excludes if self.workspace_excludes is not None else DEFAULT_EXCLUDES)
        repo = self._repo(run)
        if repo:
            excludes += repo.excludes
        if run.workspace_mode == "inplace" and not (self._dir(run.id) / "snapshot.ready").exists():
            # any size, any VCS: journal of (size, mtime) + backups of edited/uncommitted files — never a snapshot
            return JournalWorkspace(self._dir(run.id), Path(run.workdir), extra_skip=repo.excludes if repo else None,
                                    hook_only=repo.hook_only if repo else None)
        return Workspace(
            self._dir(run.id), Path(run.workdir), run.workspace_mode, excludes,
            shadow_root=self.runs_dir / "shadow",
            include_ignored=bool(repo and repo.include_ignored),
            max_snapshot_mb=(repo.max_snapshot_mb if repo and repo.max_snapshot_mb else self.max_snapshot_mb),
            max_file_mb=self.max_file_mb,
        )

    def _repo(self, run: RunState) -> RepoSpec | None:
        return self.repos.repos.get(run.repo) if run.repo else None

    def _previous_handoff(self, session_id: str) -> str:
        """The hand-over of the session's last run, so the next request (a re-run or the next step) continues
        from it — until the user marks the work complete."""
        turns = self.history.turns(session_id)
        if not turns:
            return ""
        try:
            prev = self.load(turns[-1]["run_id"])
        except (OSError, ValueError):
            return ""
        if prev.handoff_closed:
            return ""
        body = prev.baton.stop.to_markdown() if prev.baton.stop else prev.baton.handoff
        if not body:
            return ""
        state = "끝남" if prev.status == "done" else prev.status
        return f"- 이전 실행: `{prev.id}` ({state}) · 목표: {prev.baton.goal.splitlines()[0][:200]}\n\n" + body

    def complete_session(self, session_id: str) -> dict:
        """The user says the work is done: delete the session's HANDOFF.md files and stop carrying them on."""
        if self.history.get_session(session_id) is None:
            raise FileNotFoundError(session_id)
        runs = [t["run_id"] for t in self.history.turns(session_id)]
        active = [r for r in runs if r in self._cancel]
        if active:
            raise ValueError(f"진행 중인 실행이 있습니다: {', '.join(active)} — 끝나거나 취소한 뒤 완료하세요")
        closed = 0
        for run_id in runs:
            try:
                run = self.load(run_id)
            except (OSError, ValueError):
                continue
            closed += (self._dir(run_id) / "HANDOFF.md").exists()
            run.handoff_closed = True
            self.save(run)
        self.history.set_completed(session_id)
        return {"session_id": session_id, "runs": len(runs), "handoffs_removed": closed}

    def project_context(self, run: RunState) -> ProjectContext | None:
        repo = self._repo(run)
        if repo is not None and not repo.project_context:
            return None
        try:
            return discover(Path(run.workdir))
        except OSError:
            return None

    # --- lifecycle ---------------------------------------------------------
    DEFAULT_APPROVAL = "ai"

    def create(self, relay_path: Path, goal: str, workdir: Path | None = None, session_id: str | None = None,
               workspace: str | None = None, repo: str | None = None, auto_apply: bool | None = None,
               stage_models: dict[str, dict] | None = None, attachments: list[Path] | None = None,
               approval: str | None = None) -> RunState:
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
        chosen: dict[str, dict] = {}
        by_name = {s.name: s for s in spec.stages}
        for stage_name, choice in (stage_models or {}).items():
            if not choice or not (choice.get("provider") or choice.get("model") or choice.get("effort")):
                continue  # "default" in the picker
            if stage_name not in by_name:
                raise ValueError(f"이 릴레이에 없는 단계입니다: {stage_name}")
            provider = choice.get("provider") or by_name[stage_name].primary
            if self.providers is not None:
                problem = self.providers.check_choice(provider, by_name[stage_name].writes)
                if problem:
                    raise ValueError(f"[{stage_name}] {problem}")
            effort = choice.get("effort") or None
            if effort and self.providers is not None:
                model = choice.get("model") or by_name[stage_name].model
                allowed = self.providers.efforts_for(provider, model)
                if effort not in allowed:
                    raise ValueError(f"[{stage_name}] {model or provider} 는 effort '{effort}' 를 지원하지 않습니다"
                                     + (f" (가능: {', '.join(allowed)})" if allowed else " (effort 조절 없음)"))
            chosen[stage_name] = {"provider": provider, "model": choice.get("model") or None}
            if effort:
                chosen[stage_name]["effort"] = effort
        live_mcp = sorted({m for s in spec.stages for m in s.mcp if m not in READ_ONLY_MCP})
        forced = None
        if mode == "copy" and live_mcp:
            # MCP tools (e.g. the Unreal editor) change the real project, not a copy: results would split
            mode, forced = "inplace", f"MCP({', '.join(live_mcp)}) 가 실제 프로젝트를 바꾸므로 복사본 대신 원본에서 작업"
        if auto_apply is None:
            auto_apply = bool(repo_spec.auto_apply) if repo_spec and repo_spec.auto_apply is not None else spec.auto_apply
        run = RunState(
            id=datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
            session_id=session_id,
            relay=str(relay_path.resolve()),
            workdir=str(workdir.resolve()),
            baton=Baton(goal=goal, session_context=self.history.session_context(session_id),
                        previous_handoff=self._previous_handoff(session_id)),
            workspace_mode=mode,
            repo=repo,
            auto_apply=bool(auto_apply),
            stage_models=chosen,
            approval=approval if approval in ("ai", "always", "never") else self.DEFAULT_APPROVAL,
        )
        if attachments:
            # copied into the run folder: the AI may read them (--add-dir), the originals are never touched
            folder = self._dir(run.id) / "attachments"
            folder.mkdir(parents=True, exist_ok=True)
            for src in attachments:
                src = Path(src)
                if not src.is_file():
                    raise ValueError(f"첨부 파일이 없습니다: {src}")
                dst = folder / src.name
                n = 1
                while dst.exists():
                    dst, n = folder / f"{src.stem}-{n}{src.suffix}", n + 1
                shutil.copy2(src, dst)
                run.baton.attachments.append(dst.as_posix())
        self.history.add_turn(session_id, goal, spec.name, run.id)
        self._event(run, "-", "run_created", relay=spec.name, workdir=run.workdir, workspace=mode, repo=repo,
                    auto_apply=run.auto_apply, stages=[s.name for s in spec.stages], stage_models=chosen)
        if forced:
            self._event(run, "-", "workspace_forced", reason=forced)
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
        if run.pending_budget_stage:
            stage = run.pending_budget_stage
            run.stage_budget_boost[stage] = run.stage_budget_boost.get(stage, 1.0) * 2
            run.pending_budget_stage = None
        run.status = "pending"
        self._event(run, "-", "approved")
        self.save(run)
        return run

    def interject(self, run_id: str, text: str) -> dict:
        """Add an instruction while a relay runs. If the current AI session accepts input (Claude Code),
        the note goes straight into that conversation — no restart, the context and cache stay. Either way
        it is merged into the baton when the next stage starts, so every later stage sees it too.
        Notes go to their own file: the run thread owns run.json and would overwrite a direct edit."""
        text = text.strip()
        if not text:
            raise ValueError("빈 메시지입니다")
        run = self.load(run_id)
        if run.status == "done":
            raise ValueError("이미 끝난 실행입니다 — 이어서 새 요청으로 보내세요")
        live = self._live.get(run_id)
        delivered = live is not None and live.send(
            f"[사용자 추가 지시 — 실행 중 개입] {text}\n지금 하던 작업에 바로 반영하고, 결과 요약에도 반영 여부를 적어라.")
        with (self._dir(run_id) / "notes.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": _now(), "text": text, "live": delivered}, ensure_ascii=False) + "\n")
        running = run_id in self._cancel and run.status == "running"
        when = "live" if delivered else ("next_stage" if running else "on_resume")
        self.history.add_event(run_id, "-", "user_interject", {"text": text[:2000], "applied": when})
        return {"status": run.status, "applied": when}

    def _merge_notes(self, run: RunState) -> list[str]:
        path = self._dir(run.id) / "notes.jsonl"
        if not path.exists():
            return []
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        new = []
        for line in lines[run.notes_seen:]:
            try:
                new.append(json.loads(line)["text"])
            except (ValueError, KeyError):
                continue
        run.notes_seen = len(lines)
        run.baton.user_notes += new
        return new

    def delete_check(self, session_id: str) -> dict:
        """What deleting a session would lose, so the UI can ask once with the facts."""
        if self.history.get_session(session_id) is None:
            raise FileNotFoundError(session_id)
        runs = []
        for turn in self.history.turns(session_id):
            try:
                runs.append(self.load(turn["run_id"]))
            except (FileNotFoundError, ValueError):
                continue
        ready = [r for r in runs if r.changes_status == "ready"]
        return {
            "runs": runs,
            "active": [r.id for r in runs if r.status in ("running", "pending") or r.id in self._cancel],
            # changes that exist only in a copy: deleting throws them away
            "unapplied": [r.id for r in ready if r.workspace_mode == "copy"],
            # changes already in the original (in place): only the rollback backup goes
            "rollback_only": [r.id for r in ready if r.workspace_mode != "copy"],
        }

    def delete_session(self, session_id: str, force: bool = False) -> dict:
        """Delete a session and its runs' folders (logs, HANDOFF, backups). Usage totals are kept.
        Refuses while a run is active. Changes that live only in a copy need force; in-place changes are
        already in the original, so only their rollback backup goes and that doesn't block."""
        check = self.delete_check(session_id)
        if check["active"]:
            raise ValueError(f"진행 중인 실행이 있어 삭제할 수 없습니다: {', '.join(check['active'])} — 먼저 취소하세요")
        if check["unapplied"] and not force:
            raise ValueError(f"복사본에만 있고 원본에 적용하지 않은 변경이 있습니다: {', '.join(check['unapplied'])}")
        removed = self.history.delete_session(session_id)
        for run_id in removed:
            shutil.rmtree(self._dir(run_id), ignore_errors=True)
        return {"deleted": session_id, "runs": len(removed)}

    def liveness(self, run_id: str) -> dict:
        """Is the AI really working? Process alive + seconds since its last output + what it was doing."""
        run = self.load(run_id)
        events = self.history.events(run_id)
        started = next((e for e in reversed(events) if e["kind"] == "stage_started"), None)
        last = events[-1] if events else None
        here = run_id in self._cancel
        alive = here or bool(run.owner_pid and run.owner_pid != os.getpid() and pid_alive(run.owner_pid))
        pulse = self._pulse.get(run_id) if here else None
        if pulse is None and alive and run.status == "running":
            try:  # the run belongs to another process (relay run in a terminal): its last pulse is on disk
                pulse = json.loads((self._dir(run_id) / "pulse.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pulse = None
        now = time.time()

        def age(iso: str | None) -> float | None:
            try:
                return max(0.0, now - datetime.fromisoformat(iso).timestamp()) if iso else None
            except ValueError:
                return None

        quiet = (now - pulse["at"]) if pulse else age(last["at"] if last else None)
        return {
            "status": run.status, "alive": alive if run.status == "running" else False,
            "stage": started["stage"] if started else None,
            "stage_seconds": age(started["at"]) if started else None,
            "quiet_seconds": quiet, "doing": pulse.get("doing") if pulse else None,
            "turn": pulse.get("turn") if pulse else None,
            "snippet": (pulse.get("snippet") or "")[-160:] if pulse else "",
            "tool": pulse.get("tool") if pulse else None, "target": pulse.get("target") if pulse else None,
            "tools": pulse.get("tools") if pulse else None,
            "tool_seconds": (now - pulse["tool_since"]) if pulse and pulse.get("tool_since") else None,
            "last": {"kind": last["kind"], "stage": last["stage"], "detail": last["detail"]} if last else None,
            "precise": pulse is not None,  # False: no pulse yet — judged by the activity log only
        }

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
        uploads = self.runs_dir / "uploads"  # staged attachments; a run keeps its own copy
        if uploads.is_dir():
            for folder in uploads.iterdir():
                try:
                    if folder.stat().st_mtime < time.time() - 86400:
                        shutil.rmtree(folder, ignore_errors=True)
                except OSError:
                    continue
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
            if run.status == "running" and run.id not in self._cancel and not (
                    run.owner_pid and run.owner_pid != os.getpid() and pid_alive(run.owner_pid)):
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

    EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

    def _stage_work(self, run: RunState, stage: str) -> tuple[list[str], list[str]]:
        """What the interrupted stage said and which files it changed (work, not the tool log)."""
        events = self.history.events(run.id)
        starts = [i for i, e in enumerate(events) if e["stage"] == stage and e["kind"] == "stage_started"]
        said: list[str] = []
        files: list[str] = []
        for e in events[starts[-1] + 1:] if starts else []:
            if e["stage"] != stage:
                continue
            d = e["detail"]
            if e["kind"] == "note" and d.get("text"):
                said.append(" ".join(str(d["text"]).split())[:600])
            elif e["kind"] == "stage_answer" and d.get("summary"):
                said.append(" ".join(str(d["summary"]).split())[:600])
            elif e["kind"] == "tool_use" and d.get("tool") in self.EDIT_TOOLS and d.get("target"):
                if d["target"] not in files:
                    files.append(d["target"])
        return said[-6:], files[:30]

    def _handoff_facts(self, run: RunState, stop: StopNote, said: list[str], files: list[str]) -> str:
        b = run.baton
        parts = [f"요청(목표): {b.goal}"]
        if b.user_notes:
            parts.append("실행 중 사용자 추가 지시:\n" + "\n".join(f"- {n}" for n in b.user_notes))
        parts.append(f"중단: {stop.kind} · 멈춘 단계 {stop.stage} · 사유 {stop.reason}")
        if b.log:
            parts.append("끝난 단계의 결과:\n" + "\n".join(f"- [{e.stage}] {e.summary}" for e in b.log[-8:]))
        if b.state and b.state != "시작 전":
            parts.append(f"현재 상태: {b.state}")
        if b.decisions:
            parts.append("결정:\n" + "\n".join(f"- {d.decision} — {d.reason}" for d in b.decisions[-8:]))
        if b.open_issues:
            parts.append("미해결 이슈:\n" + "\n".join(f"- {i}" for i in b.open_issues))
        if b.next_steps:
            parts.append("예정된 다음 작업:\n" + "\n".join(f"- {i}" for i in b.next_steps))
        if stop.remaining_stages:
            parts.append("남은 단계: " + " → ".join(stop.remaining_stages))
        if said:
            parts.append(f"멈춘 단계({stop.stage})에서 AI 가 남긴 말:\n" + "\n".join(f"- {s}" for s in said))
        if files:
            parts.append(f"멈춘 단계({stop.stage})에서 바꾼 파일: " + ", ".join(files))
        return "\n\n".join(parts)[:12000]

    @staticmethod
    def _plain_handoff(run: RunState, stop: StopNote, files: list[str]) -> str:
        """Same three sections without an AI call (no writer configured, or the call failed)."""
        b = run.baton
        asked = [b.goal.splitlines()[0][:300]] + b.user_notes
        done = [f"[{e.stage}] {e.summary}" for e in b.log[-8:]]
        if files:
            done.append(f"[{stop.stage}] 바꾼 파일: " + ", ".join(files[:15]))
        left = b.next_steps or [f"`{s}` 단계" for s in stop.remaining_stages]

        def section(title: str, items: list[str]) -> list[str]:
            return [f"### {title}"] + ([f"- {i}" for i in items] or ["- 없음"])

        return "\n".join(section("요청", asked) + section("작업된 내역", done) + section("남은 일", left))

    def _done_facts(self, run: RunState) -> str:
        b = run.baton
        parts = [f"요청(목표): {b.goal}"]
        if b.user_notes:
            parts.append("실행 중 사용자 추가 지시:\n" + "\n".join(f"- {n}" for n in b.user_notes))
        parts.append("상태: 모든 단계를 마침")
        if b.log:
            parts.append("단계별 결과:\n" + "\n".join(f"- [{e.stage}] {e.summary}" for e in b.log[-10:]))
        if b.state and b.state != "시작 전":
            parts.append(f"현재 상태: {b.state}")
        if b.decisions:
            parts.append("결정:\n" + "\n".join(f"- {d.decision} — {d.reason}" for d in b.decisions[-8:]))
        if run.changes and run.changes.get("stat"):
            parts.append("바뀐 파일 (diff stat):\n" + str(run.changes["stat"])[:2000])
        if b.open_issues:
            parts.append("미해결 이슈:\n" + "\n".join(f"- {i}" for i in b.open_issues))
        if b.next_steps:
            parts.append("제안된 다음 작업:\n" + "\n".join(f"- {i}" for i in b.next_steps))
        if b.user_checks:
            parts.append("사람이 확인할 것:\n" + "\n".join(f"- {i}" for i in b.user_checks))
        return "\n\n".join(parts)[:12000]

    def _write_done_handoff(self, run: RunState) -> None:
        """A finished run also leaves a hand-over (the next request in the session reads it until the user
        marks the work complete). Same three sections, same lightweight model."""
        b = run.baton

        def section(title: str, items: list[str]) -> list[str]:
            return [f"### {title}"] + ([f"- {i}" for i in items] or ["- 없음"])

        b.handoff = "\n".join(section("요청", [b.goal.splitlines()[0][:300]] + b.user_notes)
                               + section("작업된 내역", [f"[{e.stage}] {e.summary}" for e in b.log[-8:]])
                               + section("남은 일", b.open_issues + b.next_steps))
        b.handoff_by = ""
        if self.handoff_writer is None:
            return
        self.save(run)
        try:
            text, usage = self.handoff_writer(self._done_facts(run))
        except Exception as e:
            self._event(run, "-", "handoff_failed", reason=str(e)[:200])
            return
        b.handoff, b.handoff_by = text, usage.model
        self.usage.record(run.id, "-:handoff", usage)
        self._event(run, "-", "handoff_written", model=usage.model, input_tokens=usage.total_input,
                    output_tokens=usage.output_tokens, cost_usd=usage.cost_usd)

    def _write_stop(self, run: RunState, kind: str, stage: str, reason: str) -> None:
        """Hand-over up to the stopping point: request / work done / what is left, written by a lightweight
        model (the tool log is not part of it). Saved first without AI so the stop shows up at once."""
        try:
            spec, _ = RelaySpec.load(Path(run.relay))
            names = [s.name for s in spec.stages]
        except (OSError, ValueError):
            names = []
        done = list(dict.fromkeys(h.stage for h in run.history))
        said, files = self._stage_work(run, stage) if stage != "-" else ([], [])
        remaining = names[run.index:] if names else []
        hint = {
            "cancelled": f"재개하면 `{stage}` 단계부터 다시 실행합니다 (relay resume {run.id})",
            "failed": f"원인을 해결한 뒤 재개하면 `{stage}` 단계부터 다시 실행합니다 (relay resume {run.id})",
            "awaiting_approval": f"승인하면 `{remaining[0] if remaining else stage}` 단계부터 진행합니다 (relay approve {run.id})",
            "budget": f"승인하면 예산 1회분을 더 허용하고 `{stage}` 단계부터 진행합니다 (relay approve {run.id})",
            "stage_budget": f"승인하면 `{stage}` 단계의 비용 한도를 2배로 올려 다시 실행합니다 (relay approve {run.id})",
        }.get(kind, "")
        stop = run.baton.stop = StopNote(kind=kind, stage=stage, reason=reason[:300], at=_now(), done_stages=done,
                                         remaining_stages=remaining, resume_hint=hint)
        stop.summary = self._plain_handoff(run, stop, files)
        if self.handoff_writer is None or not (run.history or said or files):
            return
        self.save(run)
        try:
            text, usage = self.handoff_writer(self._handoff_facts(run, stop, said, files))
        except Exception as e:  # the plain hand-over stays
            self._event(run, stage, "handoff_failed", reason=str(e)[:200])
            return
        stop.summary, stop.writer = text, usage.model
        self.usage.record(run.id, f"{stage}:handoff", usage)
        self._event(run, stage, "handoff_written", model=usage.model, input_tokens=usage.total_input,
                    output_tokens=usage.output_tokens, cost_usd=usage.cost_usd)

    def _fail(self, run: RunState, stage: str, error: str, status: str = "failed") -> RunState:
        run.status, run.error = status, f"[{stage}] {error}"
        self._event(run, stage, "run_cancelled" if status == "cancelled" else "run_failed", error=error)
        self._write_stop(run, status, stage, error)
        self._settle_workspace(run)
        self.save(run)
        return run

    def _on_stage_event(self, run_id: str, stage: str):
        def handle(kind: str, detail: dict) -> None:
            if kind == "pulse":  # liveness only: in memory (+ a small file so a terminal run shows in the dashboard)
                pulse = self._pulse[run_id] = {"at": time.time(), "stage": stage, **detail}
                if pulse["at"] - self._pulse_written.get(run_id, 0) > 1.0:
                    self._pulse_written[run_id] = pulse["at"]
                    try:
                        (self._dir(run_id) / "pulse.json").write_text(json.dumps(pulse, ensure_ascii=False), encoding="utf-8")
                    except OSError:
                        pass
                return
            if kind == "rate_limit":  # subscription usage snapshot, stored, not logged as activity
                self.history.record_limits(detail["provider"], detail.get("status"), detail.get("windows", []))
                return
            self.history.add_event(run_id, stage, kind, detail)
        return handle

    @staticmethod
    def _stage_tools(stage: StageSpec, repo, ctx: ProjectContext | None = None) -> list[str] | None:
        tools = stage.tools
        if tools and ctx is not None:
            # the project's instructions and skills name commands to run: keep them working in every stage
            extra = (["Bash"] if ctx.allowed_bash() else []) + (["Skill"] if ctx.skills else [])
            tools = tools + [t for t in extra if t not in tools]
            if ctx.allowed_bash():
                return tools
        if tools and "Bash" in tools and stage.bash == "auto" and repo is not None:
            needs = repo.verify or any(t.startswith("Bash") for t in repo.allowed_tools)
            if not needs:
                # without Bash the stage greps with the dedicated tools instead
                return [t for t in tools if t != "Bash"] + [t for t in ("Grep", "Glob") if t not in tools]
        return tools

    def _stage_mcp(self, overrides: dict, run: RunState, stage: str, cwd: Path) -> dict:
        """The digest server needs to know where it works and whom to bill (its summaries are separate calls)."""
        if "digest" not in self.mcp_registry:
            return overrides
        entry = dict(overrides.get("digest") or self.mcp_registry["digest"])
        entry["env"] = {**entry.get("env", {}), "KATAE_WORKDIR": str(cwd), "KATAE_RUN_ID": run.id, "KATAE_STAGE": stage,
                        "KATAE_EXTRA_DIRS": str(self._dir(run.id) / "attachments"),
                        "KATAE_USAGE_DB": str(getattr(self.usage, "path", "")), "PATH": os.environ.get("PATH", "")}
        return {**overrides, "digest": entry}

    def _model_for(self, provider: str, model: str | None) -> str | None:
        if self.providers is None or provider not in self.providers.specs:
            return model
        return self.providers.get(provider).resolve_model(model)

    def _run_stage(self, run: RunState, stage: StageSpec, call: StageCall, primary: str | None = None):
        """Try the primary provider (the run's pick or the relay's), then alternates."""
        primary = primary or stage.primary
        alternates = [a.model_dump() for a in stage.alternates if a.provider != primary]
        if primary != stage.primary:
            alternates.insert(0, {"provider": stage.primary})  # the relay's own AI becomes the first fallback
        options = [{"provider": primary}] + alternates
        if self.providers is not None:
            options, skipped = self.providers.candidates(primary, options[1:], stage.writes)
            if skipped:
                self._event(run, stage.name, "providers_skipped", skipped=skipped)
        if not options:
            raise RunnerError("사용 가능한 AI 가 없습니다 (모두 미설치·한도 초과·예산 소진)", "unavailable")

        last_error: RunnerError | None = None
        for i, option in enumerate(options):
            name = option["provider"]
            if i > 0 or name != primary:
                self._event(run, stage.name, "provider_switch", to=name,
                            reason=str(last_error)[:200] if last_error else "기본 AI 사용 불가")
            is_claude = name in ("claude", "anthropic_api", "mock") or (
                self.providers is not None and self.providers.get(name).kind in ("claude_cli", "api"))
            requested = self._model_for(name, option.get("model") or call.model)
            while True:
                model = requested
                if is_claude and self.providers is not None:
                    model = self.providers.usable_model(name, requested, call.fallback_model)
                    if model is None:
                        last_error = RunnerError(f"{name}: 사용할 수 있는 모델이 모두 사용량 소진", "quota")
                        break
                    if model != requested:
                        self._event(run, stage.name, "model_substituted", provider=name, requested=requested, used=model,
                                    until=self.providers.model_exhausted_until(name, requested))
                attempt = replace(
                    call, model=model, effort=option.get("effort") or call.effort,
                    fallback_model=(call.fallback_model if call.fallback_model != model else None) if is_claude else None,
                )
                try:
                    return self._runner(name).run(attempt)
                except RunnerError as err:
                    if err.kind == "cancelled":
                        raise
                    last_error = err
                    if err.kind == "quota" and self.providers is not None and is_claude:
                        hit_model = getattr(err, "model", None) or model
                        overall = self.providers.overall_limited(name) or (
                            getattr(err, "limit_type", None) in self.providers.get(name).gate_windows)
                        lower = self.providers.LADDER
                        can_step_down = self.providers.tier(hit_model) in lower[:-1]
                        if not overall and can_step_down:
                            # only this model is out (e.g. Fable 100% while overall usage is 8%): stay on this AI
                            until = self.providers.mark_model_exhausted(name, hit_model, getattr(err, "resets_at", None),
                                                                        str(err)[:200])
                            self._event(run, stage.name, "model_exhausted", provider=name, model=hit_model, until=until)
                            continue
                    if err.kind == "quota" and self.providers is not None:
                        until = self.providers.mark_exhausted(name, str(err)[:200])
                        self._event(run, stage.name, "provider_exhausted", provider=name, until=until)
                    break
            if last_error is None or last_error.kind not in ("quota", "unavailable") or i == len(options) - 1:
                raise last_error or RunnerError("no provider ran")
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
            self._live.pop(run_id, None)
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
        run.status, run.error, run.owner_pid = "running", None, os.getpid()
        self.save(run)

        ws = self.workspace(run)
        if ws is not None and not ws.prepared:
            try:
                info = ws.prepare()
            except (WorkspaceError, OSError) as e:
                return self._fail(run, "-", f"작업 공간 준비 실패: {e}")
            self._event(run, "-", "workspace_ready", **info)
        cwd = ws.path if ws is not None else Path(run.workdir)
        hook_settings = ws.settings_path if isinstance(ws, JournalWorkspace) and ws.settings_path.exists() else None
        repo = self._repo(run)
        repo_lines = "\n".join(repo.prompt_lines(repo.git_info().get("branch"))) + "\n" if repo else ""
        repo_tools = (repo.verify_tools() + repo.allowed_tools) if repo else []
        ctx = self.project_context(run)
        if ctx is not None:
            self._event(run, "-", "project_context", root=ctx.root.as_posix(),
                        instructions=[p.relative_to(ctx.root).as_posix() for p in ctx.instructions],
                        skills=ctx.skills, mcp=list(ctx.mcp), commands=ctx.commands)
            repo_lines += "\n".join(ctx.prompt_lines(cwd)) + "\n"
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

            new_notes = self._merge_notes(run)
            if new_notes:
                self._event(run, stage.name, "user_notes_applied", notes=new_notes)
            live = self._live[run_id] = LiveChannel()
            resume_sid = run.stage_sessions.get(stage.name)
            session_id = resume_sid or str(uuid.uuid4())
            if not resume_sid:
                run.stage_sessions[stage.name] = session_id
                self.save(run)  # known before the AI starts, so a cancel/crash can continue this conversation

            # Cheap first pass; spend more only when this stage is being redone.
            redo = any(h.stage == stage.name for h in run.history)
            effort = stage.retry_effort if redo and stage.retry_effort else stage.effort
            choice = run.stage_models.get(stage.name) or {}
            if choice.get("effort"):
                effort = choice["effort"]  # the user's pick, also on retries
            primary = choice.get("provider") or stage.primary
            if choice.get("model") or (choice.get("provider") and choice["provider"] != stage.primary):
                # the user's pick wins, also on retries (no silent escalation to another model)
                model = choice.get("model") if choice.get("model") is not None else (
                    stage.model if primary == stage.primary else None)
            else:
                model = stage.retry_model if redo and stage.retry_model else stage.model
            self._event(run, stage.name, "stage_started", runner=primary, model=model, chosen=bool(choice),
                        effort=effort, reads=stage.reads_outputs, system_mode=stage.system_mode,
                        escalated=redo and bool(stage.retry_model or stage.retry_effort))
            fresh_prompt = None
            tools = self._stage_tools(stage, repo, ctx)
            if resume_sid:
                self._event(run, stage.name, "stage_resumed", session=resume_sid)
            call = StageCall(
                stage=stage.name,
                model=model,
                effort=effort,
                system=(base / stage.prompt).read_text(encoding="utf-8"),
                prompt=run.baton.to_markdown(
                    include_outputs=stage.reads_outputs,
                    output_ref=str(self._dir(run.id) / "outputs" / "{stage}.md"),
                )
                + STAGE_FOOTER.format(stage=stage.name, workdir=cwd)
                + (RESUME_LINE if run.baton.stop and run.baton.stop.kind != "awaiting_approval" else "") + repo_lines,
                cwd=cwd,
                tools=tools,
                mcp_servers=stage.mcp + ([m for m in ctx.mcp if m not in stage.mcp] if ctx and tools else []),
                result_mode=stage.result_mode,
                add_dirs=([str(self._dir(run.id) / "attachments")] if run.baton.attachments else [])
                + ([ctx.root.as_posix()] if ctx and Path(cwd).resolve() != ctx.root else []),
                allowed_tools=stage.allowed_tools + (
                    self.extra_allowed_tools + repo_tools + (ctx.allowed_bash() if ctx else [])
                    if "Bash" in (tools or []) else []) + (ctx.allowed_other() if ctx and tools else []),
                mcp_overrides={**(ctx.mcp if ctx else {}), **self._stage_mcp(mcp_overrides, run, stage.name, cwd)},
                project=ctx is not None,
                # a copy outside the project doesn't see the project's CLAUDE.md by walking up: load it via --add-dir
                env={"CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1"} if ctx and not ctx.contains(cwd) else {},
                settings_path=hook_settings,
                permission_mode=stage.permission_mode,
                timeout_s=stage.timeout_s,
                stall_s=stage.stall_s,
                system_mode=stage.system_mode,
                isolate=stage.isolate,
                max_budget_usd=(stage.max_budget_usd * run.stage_budget_boost.get(stage.name, 1.0)
                                if stage.max_budget_usd is not None else None),
                fallback_model=stage.fallback_model,
                on_event=self._on_stage_event(run.id, stage.name),
                cancel_event=cancel,
                live=live,
                session_id=None if resume_sid else session_id,
                resume_session=resume_sid,
            )
            if resume_sid:
                # continue the same conversation: only what changed, not the whole baton again
                fresh_prompt = call.prompt
                notes = "\n".join(f"- {n}" for n in new_notes)
                handoff = run.baton.stop.to_markdown() if run.baton.stop else ""
                call = replace(call, fresh_prompt=fresh_prompt, prompt=(
                    "[재개] 이 단계는 중단됐다가 다시 이어서 진행한다. 먼저 아래 HANDOFF(중단 시점 인계서)를 읽고 "
                    "그 기준으로 한다. 위 대화와 HANDOFF 의 '작업된 내역'은 반복하지 말고, 현재 파일 상태를 확인해 "
                    "'남은 일'만 마친 뒤 결과 JSON 을 낸다."
                    + (f"\n\n# HANDOFF\n{handoff}" if handoff else "")
                    + (f"\n그사이 사용자 추가 지시:\n{notes}" if notes else "")))
            self._event(run, stage.name, "prompt_breakdown", **prompt_breakdown(call.system, call.prompt))
            try:
                result, usage = self._run_stage(run, stage, call, primary)
            except RunnerError as e:
                if e.kind == "cancelled" or cancel.is_set():
                    return self._fail(run, stage.name, "사용자가 취소함", status="cancelled")
                if e.kind == "budget":
                    # a stage that ran out of its own budget waits for approval instead of failing the run
                    spent = getattr(e, "cost_usd", None)
                    if spent:
                        self.usage.record(run.id, stage.name, Usage(stage.primary, call.model or "?", cost_usd=spent))
                    run.status, run.pending_budget_stage = "awaiting_approval", stage.name
                    self._event(run, stage.name, "stage_budget_exceeded", limit_usd=call.max_budget_usd,
                                spent_usd=spent, reason=str(e))
                    self._write_stop(run, "stage_budget", stage.name, str(e))
                    self._settle_workspace(run)
                    self.save(run)
                    return run
                if e.kind == "unavailable" and stage.optional:
                    self._event(run, stage.name, "stage_skipped", reason=str(e)[:200])
                    run.index += 1
                    self.save(run)
                    continue
                return self._fail(run, stage.name, str(e))
            except (OSError, ValueError) as e:
                return self._fail(run, stage.name, str(e))

            run.stage_sessions.pop(stage.name, None)  # finished: a later redo starts fresh
            run.baton.stop = None  # the hand-over was for the stage that picked the work back up
            run.baton.previous_handoff = ""
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
            pause = stage.gate == "human" and run.index < len(spec.stages) and (
                run.approval == "always" or (run.approval == "ai" and result.needs_approval))
            if stage.gate == "human" and run.index < len(spec.stages) and not pause:
                self._event(run, stage.name, "gate_skipped", next=spec.stages[run.index].name, mode=run.approval,
                            reason="AI 판단: 사람이 정할 것 없음" if run.approval == "ai" else "승인 생략 설정")
            elif pause:
                run.status = "awaiting_approval"
                why = result.approval_reason.strip() if result.needs_approval else ""
                self._event(run, stage.name, "awaiting_approval", next=spec.stages[run.index].name, reason=why)
                self._write_stop(run, "awaiting_approval", stage.name,
                                 f"`{stage.name}` 결과 확인 후 승인 필요" + (f" — {why}" if why else ""))
                self._settle_workspace(run)
                self.save(run)
                return run
            self.save(run)

        run.status = "done"
        run.baton.stop = None
        self._event(run, "-", "run_done")
        self._settle_workspace(run)
        self._write_done_handoff(run)
        if ws is not None and run.workspace_mode == "copy" and run.auto_apply and run.changes_status == "ready":
            try:
                ws.apply()
                run.changes_status, run.workspace_cleaned = "applied", True
                self._event(run, "-", "changes_auto_applied", files=(run.changes or {}).get("files", 0))
            except (WorkspaceError, OSError) as e:
                # the original moved on meanwhile: keep the patch for a manual decision instead of forcing it
                self._event(run, "-", "auto_apply_failed", error=str(e)[:300])
        if ws is not None and run.changes_status == "none" and not run.workspace_cleaned:
            try:
                ws.cleanup()
                run.workspace_cleaned = True
            except OSError:
                pass
        self.save(run)
        return run
