"""FastAPI server for Agent 카태. Runs advance on background threads; the dashboard polls.

    uv run uvicorn relay_agent.server:app --port 8020
"""

from __future__ import annotations

import hmac
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import cc_import
from .config import Config, build_engine
from .pipeline import RelaySpec, RunState
from .runners import RunnerError, probe_claude_limits

cfg = Config.load()
engine = build_engine(cfg)
app = FastAPI(title="Agent 카태")
DASHBOARD = Path(__file__).with_name("dashboard.html")


@app.on_event("startup")
def recover() -> None:
    engine.recover_interrupted()
    engine.cleanup_workspaces(cfg.workspace_retention_days)

    def hourly():  # expired workspaces are removed while the server keeps running, not only at start
        import time

        while True:
            time.sleep(3600)
            try:
                engine.cleanup_workspaces(cfg.workspace_retention_days)
            except Exception:  # noqa: BLE001 - cleanup must never take the server down
                pass

    threading.Thread(target=hourly, daemon=True, name="katae-cleanup").start()


LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


@app.middleware("http")
async def guard(request: Request, call_next):
    """Localhost is trusted. Any other client needs `Authorization: Bearer <auth_token>`;
    with no token configured, remote access is refused outright."""
    host = request.client.host if request.client else ""
    origin = request.headers.get("origin")
    if request.method != "GET" and origin and urlparse(origin).netloc != request.headers.get("host"):
        # A web page on another site can't drive this server through the user's browser (CSRF).
        return JSONResponse({"detail": "다른 출처의 요청은 거부됩니다"}, 403)
    if request.url.path != "/" and host not in LOOPBACK:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not cfg.auth_token:
            return JSONResponse({"detail": "원격 접근은 auth_token(또는 KATAE_TOKEN) 설정이 필요합니다"}, 403)
        if not hmac.compare_digest(supplied.encode(), cfg.auth_token.encode()):
            return JSONResponse({"detail": "인증 토큰이 필요합니다"}, 401)
    return await call_next(request)


class CreateSession(BaseModel):
    title: str
    workdir: str | None = None
    repo: str | None = None


class CreateRun(BaseModel):
    goal: str
    relay: str = "default"
    session_id: str | None = None
    workdir: str | None = None  # defaults to the session's workdir
    workspace: str | None = None  # none | copy | inplace; defaults to the repo's, then the relay's setting
    auto_apply: bool | None = None  # copy mode: apply automatically when done
    repo: str | None = None  # registered repository name (path, verify commands, notes come from it)
    start: bool = True


class ImportClaudeCode(BaseModel):
    project: str | None = None
    session_id: str | None = None
    cwd: str | None = None  # import the latest conversation of this folder instead
    title: str | None = None
    target_session: str | None = None


def _advance_bg(run_id: str, resume: bool = False) -> None:
    def work():
        try:
            engine.advance(run_id, resume=resume)
        except ValueError:
            pass  # already advancing

    threading.Thread(target=work, daemon=True).start()


def _is_remote(request: Request) -> bool:
    return (request.client.host if request.client else "") not in LOOPBACK


def _check_path(request: Request, path: Path) -> None:
    """Remote clients may only target registered repositories, never an arbitrary folder."""
    if _is_remote(request) and engine.repos.match(path) is None:
        raise HTTPException(403, "원격 요청은 등록된 저장소 안의 경로만 사용할 수 있습니다 (relay repo add)")


def _load(run_id: str) -> RunState:
    try:
        return engine.load(run_id)
    except FileNotFoundError:
        raise HTTPException(404, f"run {run_id} not found")


def _conflict(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


STAGE_KO = {"scout": "정찰", "plan": "설계", "build": "구현", "review": "검토", "answer": "답변", "cross_review": "교차검토"}


@app.get("/relays")
def list_relays() -> list[dict]:
    """Relays described by role and model tier, not by vendor: any provider may run a stage."""
    from .repos import TIER

    out = []
    for p in sorted(cfg.relays_dir.glob("*.yaml")):
        spec, _ = RelaySpec.load(p)
        steps = []
        for s in spec.stages:
            tier = TIER.get((s.model or "").lower(), s.model or "기본")
            extra = ["승인"] if s.gate == "human" else []
            if s.retry_model:
                extra.append(f"재시도 {TIER.get(s.retry_model, s.retry_model)}")
            if s.primary == "mock":
                tier = "모의"
            steps.append(f"{STAGE_KO.get(s.name, s.name)}({tier}{'·' + '·'.join(extra) if extra else ''})")
        out.append({
            "name": p.stem,
            "description": spec.description,
            "workspace": spec.workspace,
            "stages": steps,
            "switchable": any(s.alternates for s in spec.stages),
        })
    return out


# --- providers & usage limits ---------------------------------------------------
@app.get("/providers")
def list_providers() -> list[dict]:
    """Every AI with availability, reported usage windows (e.g. Claude 5h/7d %) and 24h spend here."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    spend = engine.usage.by_runner_since(since)
    out = []
    for name in engine.providers.specs:
        if name == "mock":
            continue
        st = engine.providers.status(name)
        st["usage_24h"] = spend.get(name, {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0})
        st["models"] = sorted(set(engine.providers.get(name).model_map.values())) or None
        out.append(st)
    order = {"claude": 0, "codex": 1, "opencode": 2, "gemini": 3}
    return sorted(out, key=lambda s: (not s["available"] and not s.get("limits"), order.get(s["name"], 9)))


@app.post("/providers/{name}/probe")
def probe_provider(name: str) -> dict:
    """Refresh usage windows. Claude: one tiny Haiku call (~hundreds of tokens)."""
    if name not in engine.providers.specs:
        raise HTTPException(404, f"unknown provider {name}")
    spec = engine.providers.get(name)
    if spec.kind == "claude_cli":
        try:
            found = probe_claude_limits()
        except (RunnerError, OSError) as e:
            raise HTTPException(502, str(e))
        if found.get("windows"):
            engine.history.record_limits(name, found.get("status"), found["windows"])
    return engine.providers.status(name)


@app.post("/providers/{name}/reset")
def reset_provider(name: str) -> dict:
    engine.history.set_provider_exhausted(name, None, "")
    return engine.providers.status(name)


# --- sessions & history ------------------------------------------------------
@app.get("/sessions")
def list_sessions() -> list[dict]:
    return engine.history.list_sessions()


@app.post("/sessions")
def create_session(body: CreateSession, request: Request) -> dict:
    workdir = body.workdir
    if body.repo:
        try:
            repo = engine.repos.get(body.repo)
        except KeyError as e:
            raise HTTPException(404, str(e))
        if not repo.exists:
            raise HTTPException(400, f"저장소 {body.repo} 경로가 이 머신에 없습니다")
        workdir = workdir or str(repo.resolved)
    if not workdir or not Path(workdir).is_dir():
        raise HTTPException(400, f"workdir {workdir} is not a directory")
    _check_path(request, Path(workdir))
    repo_name = body.repo or (m.name if (m := engine.repos.match(workdir)) else None)
    return engine.history.create_session(body.title, str(Path(workdir).resolve()), repo_name)


@app.get("/repos")
def list_repos() -> list[dict]:
    """Registered repositories with path, git branch/dirty state and their defaults."""
    return engine.repos.status()


class RepoIn(BaseModel):
    name: str
    path: str
    workspace: str | None = None
    verify: list[str] = []
    docs: str | None = None
    notes: str | None = None


def _reload_repos() -> None:
    from .repos import RepoRegistry

    fresh = Config.load()
    cfg.repos = fresh.repos
    engine.repos = RepoRegistry(fresh.repos)


@app.post("/repos")
def register_repo(body: RepoIn, request: Request) -> list[dict]:
    """Register (or update) a repository on this machine from the dashboard."""
    import re

    from .config import ROOT
    from .repos import save_local_repo

    if _is_remote(request):
        raise HTTPException(403, "저장소 등록은 이 PC 에서만 할 수 있습니다")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", body.name):
        raise HTTPException(400, "이름은 영문·숫자·-_. 만 (40자 이내)")
    folder = Path(body.path)
    if not folder.is_dir():
        raise HTTPException(400, f"폴더가 없습니다: {body.path}")
    if body.workspace not in (None, "", "copy", "inplace", "none"):
        raise HTTPException(400, "workspace 는 copy / inplace / none")
    save_local_repo(ROOT / "relay.config.local.yaml", body.name, {
        "path": folder.resolve().as_posix(), "workspace": body.workspace or None,
        "verify": [v for v in body.verify if v.strip()], "docs": body.docs, "notes": body.notes,
    })
    _reload_repos()
    return engine.repos.status()


@app.delete("/repos/{name}")
def unregister_repo(name: str, request: Request) -> list[dict]:
    from .config import ROOT
    from .repos import save_local_repo

    if _is_remote(request):
        raise HTTPException(403, "저장소 삭제는 이 PC 에서만 할 수 있습니다")
    save_local_repo(ROOT / "relay.config.local.yaml", name, None, remove=True)
    _reload_repos()
    return engine.repos.status()


@app.get("/fs/list")
def fs_list(request: Request, path: str | None = None) -> dict:
    """Folder picker for the dashboard (this PC only): drives/home at the top, sub-folders below."""
    import os
    import string

    if _is_remote(request):
        raise HTTPException(403, "폴더 탐색은 이 PC 에서만 할 수 있습니다")
    home = Path.home()
    if not path:
        roots = [{"name": f"{d}:\\", "path": f"{d}:\\"} for d in string.ascii_uppercase
                 if os.name == "nt" and os.path.exists(f"{d}:\\")] or [{"name": "/", "path": "/"}]
        shortcuts = [{"name": n, "path": str(p)} for n, p in (("홈", home), ("Projects", home / "Projects"),
                                                                 ("바탕 화면", home / "Desktop")) if p.is_dir()]
        return {"path": None, "parent": None, "dirs": shortcuts + roots, "is_repo": False}
    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise HTTPException(404, f"폴더가 없습니다: {path}")
    try:
        entries = sorted((e for e in os.scandir(folder) if e.is_dir(follow_symlinks=False)
                          and not e.name.startswith(("$", "."))), key=lambda e: e.name.lower())
        dirs = [{"name": e.name, "path": e.path} for e in entries[:500]]
    except PermissionError:
        dirs = []
    parent = str(folder.parent) if folder.parent != folder else ""
    return {"path": str(folder.resolve()), "parent": parent, "dirs": dirs,
            "is_repo": (folder / ".git").exists(), "registered": (m.name if (m := engine.repos.match(folder)) else None)}


@app.get("/repos/match")
def match_repo(path: str) -> dict:
    repo = engine.repos.match(path)
    return {"repo": repo.name if repo else None}


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    session = engine.history.get_session(session_id)
    if session is None:
        raise HTTPException(404, f"session {session_id} not found")
    return {**session, "turns": engine.history.turns(session_id)}


@app.get("/history/search")
def search_history(q: str, limit: int = 30) -> list[dict]:
    return engine.history.search_turns(q, limit)


# --- Claude Code import ----------------------------------------------------------
@app.get("/claude-code/projects")
def cc_projects() -> list[dict]:
    return cc_import.list_projects()


@app.get("/claude-code/sessions")
def cc_sessions(project: str, limit: int = 30) -> list[dict]:
    try:
        return cc_import.list_sessions(project, limit)
    except FileNotFoundError:
        raise HTTPException(404, "unknown project")


class ImportTurns(BaseModel):
    source_id: str
    turns: list[dict]
    meta: dict = {}
    title: str | None = None
    workdir: str | None = None
    target_session: str | None = None


@app.post("/claude-code/import-turns")
def cc_import_turns(body: ImportTurns) -> dict:
    """Import turns extracted on the client machine (katae MCP does this)."""
    turns = [{"at": str(t.get("at") or "")[:19], "question": str(t.get("question", ""))[:1500],
              "answer": str(t.get("answer", ""))[: cc_import.ANSWER_CHARS + 1]} for t in body.turns if t.get("question")]
    return cc_import.import_turns(engine.history, body.source_id, turns, body.meta, body.title,
                                  body.workdir, body.target_session)


@app.post("/claude-code/import")
def cc_import_session(body: ImportClaudeCode) -> dict:
    project, session_id = body.project, body.session_id
    if body.cwd and not session_id:
        found = cc_import.latest_session_for_cwd(body.cwd)
        if not found:
            raise HTTPException(404, f"no Claude Code conversation for {body.cwd}")
        project, session_id = found
    if not project or not session_id:
        raise HTTPException(400, "project and session_id (or cwd) are required")
    try:
        return cc_import.import_session(engine.history, project, session_id, body.title,
                                        body.cwd, body.target_session)
    except FileNotFoundError:
        raise HTTPException(404, "conversation not found")


# --- runs ----------------------------------------------------------------------
@app.post("/runs")
def create_run(body: CreateRun, request: Request) -> RunState:
    relay_path = cfg.relays_dir / f"{body.relay}.yaml"
    if not relay_path.exists():
        raise HTTPException(404, f"relay {body.relay} not found")
    session = engine.history.get_session(body.session_id) if body.session_id else None
    if body.session_id and session is None:
        raise HTTPException(404, f"session {body.session_id} not found")
    repo = body.repo or (session or {}).get("repo")
    if not body.workdir and not session and not repo:
        raise HTTPException(400, "repo 또는 workdir 가 필요합니다")
    workdir = Path(body.workdir) if body.workdir else None
    if workdir is not None and not workdir.is_dir():
        raise HTTPException(400, f"workdir {workdir} is not a directory")
    if workdir is None and not repo:
        workdir = Path(session["workdir"])
    if workdir is not None:
        _check_path(request, workdir)
        repo = repo or (m.name if (m := engine.repos.match(workdir)) else None)
    run = _conflict(engine.create, relay_path, body.goal, workdir, body.session_id, body.workspace or None, repo,
                    body.auto_apply)
    if body.start:
        _advance_bg(run.id)
    return run


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> RunState:
    return _load(run_id)


@app.get("/runs/{run_id}/track")
def get_track(run_id: str) -> list[dict]:
    """Stage-by-stage state for the relay track visual: done / active / waiting / failed / pending."""
    run = _load(run_id)
    spec, _ = RelaySpec.load(Path(run.relay))
    track = []
    for i, s in enumerate(spec.stages):
        records = [h for h in run.history if h.stage == s.name]
        if run.status in ("failed", "cancelled") and i == run.index:
            status = "failed"
        elif run.status == "running" and i == run.index:
            status = "active"
        elif run.status == "awaiting_approval" and i == run.index - 1:
            status = "waiting"
        elif i < run.index or run.status == "done":
            status = "done"
        else:
            status = "pending"
        last = records[-1] if records else None
        track.append({
            "name": s.name, "model": last.model if last else s.model, "provider": last.runner if last else s.primary,
            "gate": s.gate == "human", "status": status, "laps": len(records),
            "cost_usd": round(sum(r.cost_usd or 0 for r in records), 4),
            "tokens": sum(r.total_input + r.output_tokens for r in records),
        })
    return track


@app.get("/stats")
def get_stats(session_id: str | None = None) -> dict:
    """Headline numbers for the dashboard (optionally one session)."""
    run_ids = None
    if session_id:
        run_ids = {t["run_id"] for t in engine.history.turns(session_id)}
    rows = [r for rid in (run_ids if run_ids is not None else [None]) for r in engine.usage.summary(rid)]
    total_in = sum(r["input_tokens"] + r["cache_creation_input_tokens"] + r["cache_read_input_tokens"] for r in rows)
    cache_read = sum(r["cache_read_input_tokens"] for r in rows)
    sessions = engine.history.list_sessions()
    return {
        "sessions": len(sessions) if not session_id else 1,
        "turns": len(run_ids) if run_ids is not None else sum(s["turns"] for s in sessions),
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
        "input_tokens": total_in,
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "cache_hit_ratio": round(cache_read / total_in, 3) if total_in else 0.0,
    }


@app.get("/runs/{run_id}/summary")
def get_summary(run_id: str) -> dict:
    """At-a-glance result (headline, key actions, files, checks, verification, MCP usage, cost)."""
    _load(run_id)
    return engine.summary(run_id)


@app.get("/runs/{run_id}/events")
def get_events(run_id: str, after: int = 0) -> list[dict]:
    """Activity log. Poll with ?after=<last id> to follow a running relay."""
    _load(run_id)
    return engine.history.events(run_id, after)


@app.get("/runs/{run_id}/handoff", response_class=PlainTextResponse)
def get_handoff(run_id: str) -> str:
    return _load(run_id).baton.to_markdown(max_output_chars=None)


@app.get("/runs/{run_id}/patch", response_class=PlainTextResponse)
def get_patch(run_id: str) -> str:
    _load(run_id)
    return engine.patch_text(run_id)


@app.post("/runs/{run_id}/approve")
def approve(run_id: str) -> RunState:
    _load(run_id)
    run = _conflict(engine.approve, run_id)
    _advance_bg(run_id)
    return run


@app.post("/runs/{run_id}/resume")
def resume(run_id: str) -> RunState:
    run = _load(run_id)
    if run.status not in ("pending", "failed", "cancelled"):
        raise HTTPException(409, f"run is {run.status}")
    _advance_bg(run_id, resume=True)
    return run


@app.post("/runs/{run_id}/cancel")
def cancel(run_id: str) -> RunState:
    _load(run_id)
    return _conflict(engine.cancel, run_id)


@app.post("/runs/{run_id}/changes/apply")
def apply_changes(run_id: str) -> RunState:
    _load(run_id)
    return _conflict(engine.apply_changes, run_id)


@app.post("/runs/{run_id}/changes/discard")
def discard_changes(run_id: str) -> RunState:
    _load(run_id)
    return _conflict(engine.discard_changes, run_id)


@app.post("/runs/{run_id}/changes/rollback")
def rollback_changes(run_id: str) -> RunState:
    _load(run_id)
    return _conflict(engine.rollback_changes, run_id)


@app.get("/disk")
def get_disk() -> dict:
    return engine.disk_usage()


@app.post("/disk/cleanup")
def post_cleanup(days: float | None = None) -> dict:
    return engine.cleanup_workspaces(cfg.workspace_retention_days if days is None else days)


@app.get("/usage")
def get_usage(run_id: str | None = None) -> list[dict]:
    return engine.usage.summary(run_id)
