"""Campaigns: one request that keeps relaying until a whole roadmap or feature is done.

A planner relay reads the request and the target (roadmap, code) and splits it into ordered tasks, each small
enough for one relay run and with its own finish line. Then one run per task, in the same session, so each run
starts from the previous run's hand-over — until every task is done:
- a task that ends with open issues gets a follow-up task right after it (max_followups per task);
- a usage limit waits for the reset and continues the same run; a server restart resumes it;
- a task that still fails after max_attempts runs, the cost ceiling, or a cancelled run pauses the campaign for
  the user (resume / skip / cancel). An approval wait just waits: approving the run lets the campaign go on.
State lives in runs/campaigns/<id>.json. Runs, sessions and hand-overs are the engine's, unchanged.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

from .pipeline import CampaignSpec, RelayEngine, RelaySpec, RunState
from .providers import looks_like_quota
from .runners import pid_alive

TERMINAL = ("done", "failed", "cancelled")
NO_FORMAT = "이 단계는 정해진 결과 형식 없이"  # StageResult.from_text boilerplate, not a real open issue
POLL_S = 20
BUSY_WAIT_S = 60
DEFAULT_WAIT_S = 30 * 60
MAX_WAIT_S = 12 * 3600


class Task(BaseModel):
    id: str
    title: str
    goal: str
    status: Literal["pending", "running", "done", "failed", "skipped"] = "pending"
    run_ids: list[str] = []
    attempts: int = 0
    result: str = ""
    followup_of: str | None = None


class Campaign(BaseModel):
    id: str
    session_id: str
    goal: str
    workdir: str
    repo: str | None = None
    spec: CampaignSpec = CampaignSpec()
    approval: str | None = None
    mcp: list[str] | None = None
    model_cap: str | None = None  # ceilings for every run of the campaign
    effort_cap: str | None = None
    attachments: list[str] = []
    status: Literal["planning", "running", "waiting", "paused", "done", "failed", "cancelled"] = "planning"
    reason: str = ""
    wait_until: str | None = None
    plan_run_id: str = ""
    plan_attempts: int = 0
    tasks: list[Task] = []
    stop_requested: Literal["", "pause", "cancel"] = ""
    owner_pid: int | None = None
    cost_usd: float = 0.0
    budget_step: float = 0.0  # the original max_cost_usd: each resume past the ceiling adds this much
    created_at: str = ""
    updated_at: str = ""

    @property
    def current(self) -> Task | None:
        return next((t for t in self.tasks if t.status in ("running", "pending", "failed")), None)

    def progress(self) -> str:
        finished = sum(t.status in ("done", "skipped") for t in self.tasks)
        return f"{finished}/{len(self.tasks)}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_tasks(items: list[str], limit: int) -> list[Task]:
    """Planner next_steps -> tasks. Each item is "제목 :: 할 일과 완료 기준" (a bare line becomes both)."""
    tasks = []
    for raw in items:
        text = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", raw or "").strip()
        if not text or text.startswith(NO_FORMAT):
            continue
        title, sep, body = text.partition("::")
        title, body = title.strip(), body.strip()
        tasks.append(Task(id=f"T{len(tasks) + 1}", title=title[:120] if sep else text[:80],
                          goal=body if sep and body else text))
        if len(tasks) >= limit:
            break
    return tasks


class CampaignRunner:
    def __init__(self, engine: RelayEngine, relays_dir: Path, folder: Path | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.engine = engine
        self.relays_dir = Path(relays_dir)
        self.folder = Path(folder or engine.runs_dir / "campaigns")
        self.sleep = sleep
        self._lock = threading.Lock()
        self._active: set[str] = set()  # campaigns a thread of this process is driving

    # --- storage ---------------------------------------------------------------------
    def _path(self, cid: str) -> Path:
        return self.folder / f"{cid}.json"

    def load(self, cid: str) -> Campaign:
        return Campaign.model_validate_json(self._path(cid).read_text(encoding="utf-8"))

    def save(self, c: Campaign, clear_stop: bool = False) -> None:
        with self._lock:
            # stop requests arrive from other threads (the API): never overwrite one with an older copy
            if self._path(c.id).exists() and not c.stop_requested and not clear_stop:
                c.stop_requested = self.load(c.id).stop_requested
            c.updated_at = _now()
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = self._path(c.id).with_suffix(".tmp")
            tmp.write_text(c.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, self._path(c.id))

    def list(self, session_id: str | None = None) -> list[Campaign]:
        out = []
        for p in sorted(self.folder.glob("*.json"), reverse=True):
            try:
                c = Campaign.model_validate_json(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if session_id is None or c.session_id == session_id:
                out.append(c)
        return out

    def _relay(self, name: str) -> Path:
        return self.relays_dir / f"{name}.yaml"

    # --- start / control ---------------------------------------------------------------
    def start(self, goal: str, workdir: Path | None, session_id: str | None = None, repo: str | None = None,
              spec: CampaignSpec | None = None, approval: str | None = None, mcp: list[str] | None = None,
              attachments: list[Path] | None = None, model_cap: str | None = None,
              effort_cap: str | None = None) -> tuple[Campaign, RunState]:
        """Create the campaign and its planning run (not started: call spawn() or drive())."""
        spec = spec or CampaignSpec()
        model_cap, effort_cap = model_cap or spec.model_cap, effort_cap or spec.effort_cap
        plan_goal = (f"{goal}\n\n[캠페인 계획] 위 요청을 끝까지 완료하기 위한 작업 목록을 만든다. "
                     f"작업은 최대 {spec.max_tasks}개, 순서대로 하나씩 릴레이로 실행된다.")
        run = self.engine.create(self._relay(spec.planner), plan_goal, workdir, session_id, repo=repo,
                                 approval="never", mcp=mcp, attachments=attachments,
                                 model_cap=model_cap, effort_cap=effort_cap)
        c = Campaign(id="c" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
                     session_id=run.session_id, goal=goal, workdir=run.workdir, repo=run.repo, spec=spec,
                     approval=approval, mcp=mcp, model_cap=model_cap, effort_cap=effort_cap,
                     attachments=list(run.baton.attachments), plan_run_id=run.id,
                     budget_step=spec.max_cost_usd, created_at=_now())
        self.save(c)
        self.engine._event(run, "-", "campaign_started", campaign=c.id, max_tasks=spec.max_tasks,
                           max_cost_usd=spec.max_cost_usd)
        return c, run

    def spawn(self, cid: str) -> None:
        threading.Thread(target=self.drive, args=(cid,), daemon=True, name=f"campaign-{cid}").start()

    def recover(self) -> list[str]:
        """Server start: continue campaigns that were running when it stopped."""
        resumed = []
        for c in self.list():
            if c.status in ("planning", "running", "waiting") and not self._foreign_owner(c):
                self.spawn(c.id)
                resumed.append(c.id)
        return resumed

    def request(self, cid: str, action: str) -> Campaign:
        """pause (after the current task) | cancel (now) | resume | skip (the stuck task, then resume)."""
        with self._lock:
            c = self.load(cid)
            if action in ("pause", "cancel"):
                if c.status in ("done", "cancelled"):
                    raise ValueError(f"캠페인이 이미 {c.status} 상태입니다")
                if c.status in ("paused", "failed") or not self._driving(c):
                    c.status, c.reason = ("cancelled", "사용자가 취소함") if action == "cancel" else (c.status, c.reason)
                else:
                    c.stop_requested = action
            elif action in ("resume", "skip"):
                if c.status not in ("paused", "failed", "cancelled") and self._driving(c):
                    raise ValueError("캠페인이 이미 진행 중입니다")
                task = c.current
                if action == "skip" and task is not None:
                    task.status = "skipped"
                elif task is not None and task.status == "failed":
                    task.status, task.attempts = "running", 0  # resume continues its last run
                if c.cost_usd >= c.spec.max_cost_usd:  # resuming past the ceiling allows one more budget
                    c.spec.max_cost_usd = round(c.cost_usd + (c.budget_step or c.spec.max_cost_usd), 2)
                c.status = "running" if c.tasks else "planning"
                c.stop_requested, c.reason, c.plan_attempts = "", "", 0
            else:
                raise ValueError(f"unknown action: {action}")
            c.updated_at = _now()
            self._path(c.id).write_text(c.model_dump_json(indent=2), encoding="utf-8")
        if action == "cancel":
            running = c.current.run_ids[-1] if c.current and c.current.run_ids else c.plan_run_id
            try:
                self.engine.cancel(running)
            except (ValueError, FileNotFoundError):
                pass
        if action in ("resume", "skip"):
            self.spawn(cid)
        return c

    @staticmethod
    def _foreign_owner(c: Campaign) -> bool:
        """Another live process (e.g. a terminal `relay campaign`) is driving it."""
        return (bool(c.owner_pid) and c.owner_pid != os.getpid() and pid_alive(c.owner_pid)
                and c.status in ("planning", "running", "waiting"))

    def _driving(self, c: Campaign) -> bool:
        return c.id in self._active or self._foreign_owner(c)

    # --- the loop ------------------------------------------------------------------------
    def drive(self, cid: str) -> Campaign:
        """Plan, then run every task. Blocks until done, paused, failed or cancelled."""
        with self._lock:
            if cid in self._active:
                return Campaign.model_validate_json(self._path(cid).read_text(encoding="utf-8"))
            self._active.add(cid)
        try:
            c = self.load(cid)
            if self._foreign_owner(c):
                return c
            c.owner_pid = os.getpid()
            self.save(c)
            return self._drive(cid)
        finally:
            with self._lock:
                self._active.discard(cid)

    def _drive(self, cid: str) -> Campaign:
        c = self.load(cid)
        try:
            if c.status == "planning" and not self._plan(c):
                return self.load(cid)
            while True:
                c = self.load(cid)
                if self._stopped(c):
                    return c
                task = c.current
                if task is None:
                    self._finish(c, "done", f"작업 {len(c.tasks)}개 모두 끝남")
                    return c
                c.cost_usd = self._cost(c)
                if c.cost_usd >= c.spec.max_cost_usd:
                    self._finish(c, "paused", f"캠페인 비용 한도 ${c.spec.max_cost_usd:.2f} 도달 "
                                              f"(사용 ${c.cost_usd:.2f}) — 재개하면 계속합니다")
                    return c
                if not self._run_task(c, task):
                    return self.load(cid)
        except Exception as e:  # noqa: BLE001 - a campaign never stays "running" after its driver died
            c = self.load(cid)
            self._finish(c, "paused", f"캠페인 진행 중 오류: {str(e)[:200]} — 재개하면 이어서 합니다")
            return c
        finally:
            c = self.load(cid)
            if c.owner_pid == os.getpid():
                c.owner_pid = None
                self.save(c)

    def _stopped(self, c: Campaign) -> bool:
        if c.stop_requested == "cancel":
            self._finish(c, "cancelled", "사용자가 취소함")
            return True
        if c.stop_requested == "pause":
            self._finish(c, "paused", "사용자가 일시 정지 — 재개하면 다음 작업부터")
            return True
        return False

    def _finish(self, c: Campaign, status: str, reason: str) -> None:
        c.status, c.reason, c.stop_requested, c.wait_until = status, reason, "", None
        c.cost_usd = self._cost(c)
        self.save(c, clear_stop=True)
        last = next((t.run_ids[-1] for t in reversed(c.tasks) if t.run_ids), c.plan_run_id)
        try:
            run = self.engine.load(last)
        except (OSError, ValueError):
            return
        kind = {"done": "campaign_done", "paused": "campaign_paused", "failed": "campaign_paused"}.get(status,
                                                                                                  "campaign_stopped")
        self.engine._event(run, "-", kind, campaign=c.id, reason=reason, progress=c.progress(),
                           cost_usd=round(c.cost_usd, 4))

    def _cost(self, c: Campaign) -> float:
        total = 0.0
        for rid in [c.plan_run_id] + [r for t in c.tasks for r in t.run_ids]:
            try:
                total += self.engine.load(rid).cost_usd
            except (OSError, ValueError):
                continue
        return total

    # --- planning --------------------------------------------------------------------------
    def _plan(self, c: Campaign) -> bool:
        run = self._drive_run(c, c.plan_run_id, None)
        c = self.load(c.id)
        if run is None or self._stopped(c):
            return False
        if run.status != "done":
            self._finish(c, "paused", f"계획 단계가 끝나지 않았습니다: {run.error or run.status} — 재개하면 다시 시도")
            return False
        tasks = parse_tasks(run.baton.next_steps, c.spec.max_tasks)
        if not tasks:
            self._finish(c, "failed", "계획 단계가 작업 목록(next_steps)을 내지 않았습니다")
            return False
        c.tasks, c.status, c.reason = tasks, "running", ""
        self.save(c)
        self.engine._event(run, "-", "campaign_planned", campaign=c.id, tasks=[f"{t.id} {t.title}" for t in tasks])
        return True

    # --- one task ----------------------------------------------------------------------------
    def _task_goal(self, c: Campaign, task: Task) -> str:
        lines = []
        for t in c.tasks:
            mark = {"done": "✅", "skipped": "⏭", "running": "▶"}.get(t.status, "▶" if t is task else "·")
            lines.append(f"{mark} {t.id} {t.title}")
        return "\n".join([
            f"[캠페인 {c.progress()}] {task.id} {task.title}",
            "",
            task.goal,
            "",
            f"전체 요청: {c.goal.splitlines()[0][:200]}",
            "전체 작업 순서 (이번에는 ▶ 표시된 작업만 한다. 앞 작업의 결과는 인계서에 있다):",
            *lines,
            "",
            "이 작업의 완료 기준을 채우면 끝낸다. 끝내지 못한 것·막힌 것은 open_issues 에 구체적으로 남긴다 "
            "(다음 릴레이가 이어받는다). 다른 작업은 하지 않는다.",
        ])

    def _read_only(self, rid: str) -> bool:
        """The run's relay has no stage that changes files (e.g. a docs Q&A relay)."""
        try:
            spec, _ = RelaySpec.load(Path(self.engine.load(rid).relay))
        except (OSError, ValueError):
            return False
        return not any(s.writes for s in spec.stages)

    def _run_task(self, c: Campaign, task: Task) -> bool:
        if task.status == "running" and task.run_ids and not self._read_only(task.run_ids[-1]):
            rid = task.run_ids[-1]  # continue (server restart, resume after a pause)
        else:
            # a task is work on the target: never a read-only relay, and a run that landed on one is not
            # continued (it cannot change anything however often it is retried) — start over and route again
            run = self.engine.create(self._relay(c.spec.task_relay), self._task_goal(c, task), Path(c.workdir),
                                     c.session_id, repo=c.repo, approval=c.approval, mcp=c.mcp,
                                     attachments=[Path(a) for a in c.attachments if Path(a).is_file()],
                                     model_cap=c.model_cap, effort_cap=c.effort_cap, needs_writes=True)
            rid = run.id
            task.run_ids.append(rid)
        task.status = "running"
        self.save(c)
        run = self._drive_run(c, rid, task)
        c = self.load(c.id)
        task = next(t for t in c.tasks if t.id == task.id)
        if run is None:
            return False  # paused/cancelled while waiting
        if c.stop_requested == "cancel":
            self._stopped(c)  # the user cancelled the campaign (and so this run)
            return False
        if run.status == "done":
            task.status = "done"
            task.result = (run.baton.log[-1].summary if run.baton.log else run.baton.state)[:300]
            self._followup(c, task, run)
            self.save(c)
            return True
        task.status = "failed"
        task.result = (run.error or run.status)[:300]
        self.save(c)
        why = "실행이 취소됨" if run.status == "cancelled" else f"{task.attempts}번 실행해도 실패"
        self._finish(c, "paused", f"{task.id} {task.title}: {why} — {task.result[:160]} "
                                  "(재개: 이어서 다시 / 건너뛰기: 다음 작업으로)")
        return False

    def _followup(self, c: Campaign, task: Task, run: RunState) -> None:
        issues = [i for i in run.baton.open_issues if i.strip() and not i.startswith(NO_FORMAT)]
        root = task.followup_of or task.id
        done_followups = sum(t.followup_of == root for t in c.tasks)
        if not issues or done_followups >= c.spec.max_followups:
            return
        followup = Task(id=f"{root}.{done_followups + 1}", title=f"{c_title(c, root)} 마무리", followup_of=root,
                        goal="앞 작업이 남긴 미해결 이슈를 해결하고 원래 완료 기준을 채운다:\n"
                             + "\n".join(f"- {i}" for i in issues[:10]))
        c.tasks.insert(c.tasks.index(task) + 1, followup)
        self.engine._event(run, "-", "campaign_followup", campaign=c.id, task=followup.id, issues=issues[:5])

    # --- one run, through limits and restarts -----------------------------------------------
    def _drive_run(self, c: Campaign, rid: str, task: Task | None) -> RunState | None:
        """Run to a final state. Usage limits wait, restarts and a busy folder retry, other failures get
        max_attempts. None = the campaign was paused/cancelled meanwhile."""
        resume = self.engine.load(rid).status in ("failed", "cancelled")  # continuing after a pause
        while True:
            run = self._settle(c, rid, resume)
            if run is None:
                return None
            if run.status != "failed":
                return run
            error = run.error or ""
            if error.startswith("서버 재시작"):
                resume = True
                continue
            if "다른 실행(" in error:
                if not self._wait(c, BUSY_WAIT_S, "같은 폴더를 다른 실행이 쓰는 중 — 끝나면 이어서"):
                    return None
                resume = True
                continue
            if self._usage_limited(error):
                if not self._wait_for_usage(c):
                    return None
                resume = True
                continue
            c = self.load(c.id)
            holder = next((t for t in c.tasks if task and t.id == task.id), None)
            attempts = holder.attempts + 1 if holder else c.plan_attempts + 1
            if holder:
                holder.attempts = attempts
            else:
                c.plan_attempts = attempts
            self.save(c)
            if attempts >= c.spec.max_attempts:
                return run
            resume = True

    def _settle(self, c: Campaign, rid: str, resume: bool) -> RunState | None:
        try:
            self.engine.advance(rid, resume=resume)
        except ValueError:
            pass  # already advancing (e.g. approved from the dashboard)
        while True:
            run = self.engine.load(rid)
            if run.status in TERMINAL:
                return run
            fresh = self.load(c.id)
            if fresh.stop_requested == "cancel" or (fresh.stop_requested and run.status == "awaiting_approval"):
                self._stopped(fresh)
                return None
            waiting = run.status == "awaiting_approval"
            want = "waiting" if waiting else ("running" if fresh.tasks else "planning")
            if fresh.status != want:
                fresh.status = want
                fresh.reason = f"실행 {rid} 승인 대기 — 대시보드에서 승인하면 이어서 진행" if waiting else ""
                self.save(fresh)
            if run.status == "pending" and rid not in self.engine._cancel:
                try:  # approved, and nobody else (the server) picked it up
                    self.engine.advance(rid)
                    continue
                except ValueError:
                    pass
            self.sleep(POLL_S)

    @staticmethod
    def _usage_limited(error: str) -> bool:
        return looks_like_quota(error) or "사용량" in error or "사용 가능한 AI 가 없습니다" in error

    def _reset_at(self) -> datetime:
        """When the usage should be back: the provider's bench or window reset, else a default wait."""
        now = datetime.now(timezone.utc)
        best = None
        providers = self.engine.providers
        if providers is not None:
            try:
                st = providers.status("claude")
                if st.get("exhausted_until"):
                    best = datetime.fromisoformat(st["exhausted_until"].replace("Z", "+00:00"))
                for row in st.get("limits") or []:
                    if row.get("gating") and row.get("resets_at") and (row.get("utilization") or 0) >= 0.95:
                        at = datetime.fromtimestamp(row["resets_at"], timezone.utc)
                        best = max(best, at) if best else at
            except (KeyError, ValueError, TypeError):
                best = None
        if best is None or best.tzinfo is None or best <= now:
            best = now + timedelta(seconds=DEFAULT_WAIT_S)
        return min(best + timedelta(minutes=1), now + timedelta(seconds=MAX_WAIT_S))

    def _wait_for_usage(self, c: Campaign) -> bool:
        until = self._reset_at()
        seconds = max(60.0, (until - datetime.now(timezone.utc)).total_seconds())
        local = until.astimezone().strftime("%m-%d %H:%M")
        return self._wait(c, seconds, f"사용량 한도 — {local} 쯤 이어서 진행", until.isoformat())

    def _wait(self, c: Campaign, seconds: float, reason: str, until: str | None = None) -> bool:
        c = self.load(c.id)
        c.status, c.reason, c.wait_until = "waiting", reason, until
        self.save(c)
        waited = 0.0
        while waited < seconds:
            step = min(POLL_S * 3, seconds - waited)
            self.sleep(step)
            waited += step
            if self._stopped(self.load(c.id)):
                return False
        c = self.load(c.id)
        c.status, c.reason, c.wait_until = ("running" if c.tasks else "planning"), "", None
        self.save(c)
        return True


def c_title(c: Campaign, task_id: str) -> str:
    return next((t.title for t in c.tasks if t.id == task_id), task_id)


def to_dict(c: Campaign) -> dict:
    data = json.loads(c.model_dump_json())
    data["progress"] = c.progress()
    data["current"] = c.current.id if c.current else None
    return data
