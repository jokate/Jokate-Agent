"""Studio REST (S-3): the /studio router, its authentication, the basic resources, campaigns and tasks.

Contract: design/studio/ARCHITECTURE.md — §6.1 common rules, §6.2 authentication, §6.3 error body, §6.4 the
rows marked S-3 (projects, departments, employees, rooms, messages, attachments, campaigns, tasks), §6.6
posting and attachments. Task changes go through studio/tasks.py; the one transition the owner calls by path
is cancel (T10 · T11). `openapi_schema()` is the contract as OpenAPI, saved in design/studio/openapi.json.

server.py connects three points: `install(app, cfg)` next to the app, `open_store()` at startup, and the
guard handing every `/studio/` path to `authenticate` before its loopback pass.

The live stream (S-4, studio/ws.py) hangs on three things here: `on_commit` — told the stream_events ids of
every commit, which is also when the retention of §7.2 is looked at —, the `liveness` handed to `install`,
and `live_runs` · `live_data`: the content of `run.live` and of `GET /studio/runs/{run_id}/live`.

Employees (S-6): an actor token (`sat1.`, studio/sat.py) is checked by `actor_subject` — §8.2 의 검사 1–3 —
and establishes an employee on the actor paths only. `POST /studio/mcp/<tool>` hands the caller and the
arguments to studio/tools.py and answers 200 with the result of §8.3; `actor_token(run_id)` makes the token
a run is given, and `mcp_env` is the engine's env callback for the `studio` MCP server — None for a run
that is not in studio's ledger, so no studio server is attached to it.

Bringing in a project that is already there (M-6): `POST /studio/projects` has studio/importer.py read the
folder — nothing is written in it — and registers it; the `repos` handed to `install` says which registered
repository the folder lies in (§3.3).

Campaigns (S-14): made, changed and driven by path — `start` · `pause` · `resume` · `cancel` (§4.10). The task
machine does the change; what follows it (the look at the campaign) is studio/campaigns.py's, through
`Effects.after`.

Making a new project (M-8): `POST /studio/projects/create` has studio/jobs.py check the request and start a job —
a file under runs/studio/jobs, run on a thread of its own — `GET /studio/jobs/{id}` reads it, and
`GET /studio/projects/create/options` is what the making screen offers. `use_jobs` is the server's connection (the
settings file and the reload `register` needs); `on_live` is how ws.py hears the `job.log` nobody stores.

A project's editor (M-7): `GET /studio/projects/{id}/editor` and `POST …/editor/start` · `/stop` · `/screenshot`
hand the project to studio/editor.py with the processes and the health check of `_machine` — what
`GET /studio/machine` reads from — and the `_editor` tools. A screenshot is saved under the attachments folder
(`shot_<hex12>/`) and, with a `room_id`, posted in that room as a message of `system`; a `shot_` folder that was
not posted is removed a day later, when the next picture is taken. `stop` may carry `{force}` (left out: true).

An employee's files (H-3, part 1): `PUT /studio/employees/{id}/files/{name}`, `GET …/history` and `POST …/revert`
write, list and put back the four files of the folder, each write a commit of the folder's own git
(studio/employee_git.py; §9.6). `commit_folders()` is the start's part: a folder that has no `.git` yet gets one.

Not here yet, on purpose: an owner's message is stored and announced; the turns it starts belong to
S-7 · S-9. Tasks are not started or stopped here (S-8).
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Callable, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection

from . import app_release, creator, devices, employee_git, employees, files, gate, guard, importer, jobs as create_jobs, memory, policy as project_policy
from . import editor as editor_control
from . import usage
from . import limits as machine_limits, machine as machine_info, review, sat, staffing, stats, tools, work
from .inbox import Hooks, Inbox, InboxError
from .store import StudioStore, StudioStoreError, Tx, now
from .tasks import (OWNER, STATES, TERMINAL, TEXT_FIELDS, CampaignNotFound, Effects, TaskMachine, TaskNotFound,
                    TransitionError)

if TYPE_CHECKING:
    from ..config import Config

router = APIRouter(prefix="/studio", tags=["studio"])
log = logging.getLogger(__name__)

# --- errors (§6.3) ----------------------------------------------------------------------------------------
STATUS = {  # code -> HTTP status: the table of §6.3
    "unauthorized": 401, "actor_invalid": 401, "actor_not_allowed": 403, "not_found": 404,
    "illegal_transition": 409, "precondition_failed": 409, "stale_state": 409, "already_decided": 409,
    "conflict": 409, "engine_error": 409, "pairing_invalid": 400, "payload_too_large": 413,
    "missing_fields": 422, "invalid_decision": 422, "unavailable": 503,
}


class StudioError(Exception):
    """A refusal with the §6.3 body: `{detail, code}` plus the fields its code adds (`field`, `failed`, …)."""

    def __init__(self, code: str, detail: str, **extra: Any):
        if code not in STATUS:
            raise ValueError(f"unknown studio error code: {code!r}")
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = dict(extra)
        if code == "precondition_failed":
            self.extra.setdefault("failed", [])  # always there; [] = no code is defined for this condition

    def body(self) -> dict:
        return {"detail": self.detail, "code": self.code, **self.extra}

    def response(self) -> JSONResponse:
        return JSONResponse(self.body(), STATUS[self.code])


def error_response(code: str, detail: str, **extra: Any) -> JSONResponse:
    return StudioError(code, detail, **extra).response()


async def _on_studio_error(request: Request, exc: StudioError) -> JSONResponse:
    return exc.response()


async def _on_transition_error(request: Request, exc: TransitionError) -> JSONResponse:
    return JSONResponse(exc.as_dict(), STATUS[exc.code])  # the five codes of §4.5 keep their names


async def _on_task_not_found(request: Request, exc: TaskNotFound) -> JSONResponse:
    return error_response("not_found", f"작업을 찾을 수 없습니다: {exc}")


async def _on_campaign_not_found(request: Request, exc: CampaignNotFound) -> JSONResponse:
    return error_response("not_found", f"캠페인을 찾을 수 없습니다: {exc}")


async def _on_inbox_error(request: Request, exc: InboxError) -> JSONResponse:
    return JSONResponse(exc.as_dict(), STATUS[exc.code])  # §5.1: `inbox` · `allowed` · `fields` ride along


def add_error_handlers(app: FastAPI) -> None:
    """Request validation errors are left alone: FastAPI's 422 with a list in `detail`, no `code` (§6.3)."""
    app.add_exception_handler(StudioError, _on_studio_error)
    app.add_exception_handler(TransitionError, _on_transition_error)
    app.add_exception_handler(TaskNotFound, _on_task_not_found)
    app.add_exception_handler(CampaignNotFound, _on_campaign_not_found)
    app.add_exception_handler(InboxError, _on_inbox_error)


# --- the server's config and the one store ----------------------------------------------------------------
_cfg: Config | None = None
_lock = threading.Lock()
_store: StudioStore | None = None
_failure: str | None = None  # why the store could not start; kept so the start is not retried per request
_effects: Effects | None = None  # what a task change does outside the DB (runs, gates, apply); None = nothing
_queue: Any = None  # queue.TaskQueue: the line and its slots (§4.6); None = nothing starts or stops a run
_cutoff: Any = None  # cutoff.Cutoff: what follows the answer of a T4 (§8.3 '서버의 뒤처리'); None = no run is cut
_hooks: Hooks | None = None  # what a decision asks of the engine and the turns (interject, commit, chat turn)
_models: Callable[[], list[dict]] | None = None  # the engine's ProviderRegistry.catalog; None = the config's own
_run_view: Callable[[str], dict | None] | None = None  # run id -> {id, status, changes_status}; use_engine sets it
_run_cost: Callable[[str], float] | None = None  # run id -> USD so far (`GET /studio/stats`); use_engine sets it
_engine: Callable[[], Any] | None = None  # -> the server's RelayEngine as it is now (use_engine); None = no engine
_tokens: Callable[[str], dict] | None = None  # run id -> the server's `GET /runs/{id}/tokens` (use_engine)
_liveness: Callable[[str], dict] | None = None  # run id -> RelayEngine.liveness(run id); None = no engine connected
_workdir: Callable[[str], Path | None] | None = None  # run id -> the folder that run's stages work in (§6.6)
_repos: Callable[[], Any] | None = None  # -> the server's RepoRegistry as it is now (§3.3); None = no registry
_commit_listeners: list[Callable[[tuple[int, ...]], None]] = []  # see on_commit
_revoke_listeners: list[Callable[[str], Any]] = []  # see on_revoke
_live_listeners: list[Callable[..., Any]] = []  # see on_live
_job_parts: dict[str, Any] | None = None  # what use_jobs was given; None = no project can be made here
_jobs: Any = None  # jobs.Jobs, made on first use from _job_parts (see _job_runner)
_pruned_at = 0.0  # time.monotonic() of the last look at the retention of stream_events
PRUNE_EVERY = 24 * 3600.0  # §7.2: at the start and once a day


def install(app: FastAPI, cfg: Config, effects: Effects | None = None, *,
            liveness: Callable[[str], dict] | None = None, hooks: Hooks | None = None,
            workdir: Callable[[str], Path | None] | None = None,
            repos: Callable[[], Any] | None = None) -> None:
    """Connect the studio layer to the server: remember its config (and the effects of task changes, once
    S-8 has them), add the router and the error bodies. `liveness` is the engine's — what `run.live` and
    `GET /studio/runs/{run_id}/live` report (§7.4). `hooks` is what an inbox decision asks of the engine and
    the turns (studio/inbox.py); without it a decision is stored and its text waits to be delivered.
    `workdir` gives the folder a run's stages work in (studio/runs.py `workdir`): what a relative path of an
    employee's attachment is taken from (§6.6); without it only absolute paths in the project folder pass.
    `repos` gives the server's RepoRegistry — a function, because the server replaces the object when its
    repositories are reloaded; without it no project can be brought in (`POST /studio/projects`, §3.3)."""
    global _cfg, _effects, _liveness, _hooks, _workdir, _repos
    _cfg, _effects, _liveness, _hooks, _workdir, _repos = cfg, effects, liveness, hooks, workdir, repos
    app.include_router(router)
    add_error_handlers(app)


def _config() -> Config:
    if _cfg is None:
        raise StudioError("unavailable", "studio 레이어가 서버에 연결되지 않았습니다 (install 이 불리지 않았습니다)")
    return _cfg


def on_commit(listener: Callable[[tuple[int, ...]], None]) -> None:
    """Have `listener(ids)` called after every commit that wrote stream events — "커밋 뒤 WS 이벤트를 낸다"
    of §4.5. On the committing thread, outside the store lock. It stays through `close_store()`."""
    if listener not in _commit_listeners:
        _commit_listeners.append(listener)


def on_revoke(listener: Callable[[str], Any]) -> None:
    """Have `listener(device_id)` called after a device was revoked and that is committed (§6.5 의 5): ws.py
    closes the device's connections with it (§7.1). On the revoking thread. It stays through `close_store()`."""
    if listener not in _revoke_listeners:
        _revoke_listeners.append(listener)


def on_live(listener: Callable[..., Any]) -> None:
    """Have `listener(type, data, project_id=)` called for every event that is not stored and is not `run.live`
    (§7.2: `job.log`): ws.py sends it to the connections. On the thread that has it. It stays through
    `close_store()`."""
    if listener not in _live_listeners:
        _live_listeners.append(listener)


def _say(type: str, data: dict, *, project_id: str | None = None) -> None:
    for listener in tuple(_live_listeners):
        try:
            listener(type, data, project_id=project_id)
        except Exception:  # noqa: BLE001 - one listener must not silence the others, nor stop who is telling
            log.exception("studio: a live listener failed (%s)", type)


def _prune(store: StudioStore) -> None:
    """The retention of §7.2. A failure is logged and nothing more: rows kept too long only cost disk."""
    global _pruned_at
    _pruned_at = time.monotonic()
    try:
        store.prune_stream_events()
    except (StudioStoreError, sqlite3.Error) as e:
        log.warning("studio: stream_events 를 정리하지 못했습니다 — %s: %s", type(e).__name__, e)


def _after_commit(store: StudioStore, ids: tuple[int, ...]) -> None:
    """The store's commit listener: pass the ids on, then the daily pruning. No timer thread — rows are only
    added by emit(), and every emit() comes through here."""
    for listener in tuple(_commit_listeners):
        try:
            listener(ids)
        except Exception:  # noqa: BLE001 - one listener must not silence the others
            log.exception("studio: a stream listener failed (stream events %s)", ids)
    if time.monotonic() - _pruned_at >= PRUNE_EVERY:
        _prune(store)


def get_store() -> StudioStore:
    """The one StudioStore of this server, opened on first use. Also the FastAPI dependency.

    Not opened at import: importing the server must not create runs/studio. A start that failed is
    remembered and answered with 503 `unavailable` from then on — a failed migration is not run again for
    every request (§3.2)."""
    global _store, _failure
    cfg = _config()
    with _lock:
        if _store is None and _failure is None:
            try:
                store = StudioStore(cfg.studio_db)
            except (StudioStoreError, sqlite3.Error, OSError) as e:
                _failure = f"{type(e).__name__}: {e}"
            else:
                store.commit_listeners.append(lambda ids: _after_commit(store, ids))
                _prune(store)  # §7.2: at the start; from here on _after_commit looks once a day
                _store = store
        if _store is None:
            raise StudioError("unavailable", f"studio 레이어가 시작하지 못했습니다 — {_failure}")
        return _store


def open_store() -> bool:
    """Open the store ahead of the first request (server startup). False when it cannot start."""
    try:
        get_store()
    except StudioError:
        return False
    return True


def seed_employee() -> dict | None:
    """Make the seed employee when the studio DB has no employee at all (§3.15; studio/employees.py `seed`).
    Called once at the server's start, after `open_store()` — not from `get_store()`, which every request
    passes. Returns the row, or None: nothing was made, the store is not open, or it failed (logged — the
    server starts without it)."""
    with _lock:
        store = _store
    if store is None or _cfg is None:
        return None
    try:
        return employees.seed(store, _cfg.studio_dir)
    except Exception:  # noqa: BLE001 - a start must not depend on the seed
        log.exception("studio: 시드 직원을 만들지 못했다")
        return None


def ensure_hr() -> dict | None:
    """Make the HR employee when the studio DB has no row of the HR part (§3.15; studio/employees.py
    `ensure_hr`), and fill the HR part's empty defaults (`fill_hr_part`). Called once at the server's start,
    after `seed_employee()`. The two are tried apart — one that fails is logged and leaves the other, and the
    server starts either way. Returns the employee's row, or None: nothing was made, or the store is not open.
    An HR employee that is there keeps its files, so a GUIDE.md older than the section of the values of a hire
    is only named in the log (`employees.hr_guides_behind`): the owner brings it up to date by hand."""
    with _lock:
        store = _store
    if store is None or _cfg is None:
        return None
    try:
        employees.fill_hr_part(store)
    except Exception:  # noqa: BLE001 - a start must not depend on the part's defaults
        log.exception("studio: 인사 파트의 기본값을 채우지 못했다")
    made = None
    try:
        made = employees.ensure_hr(store, _cfg.studio_dir)
    except Exception:  # noqa: BLE001 - a start must not depend on the HR employee
        log.exception("studio: 인사 직원을 만들지 못했다")
    try:
        _add_policy_talk(store)
    except Exception:  # noqa: BLE001 - a start must not depend on a section of a guide
        log.exception("studio: 인사 GUIDE.md 에 정책 면담 절을 덧붙이지 못했다")
    try:
        for employee_id in employees.hr_guides_behind(store, _cfg.studio_dir):
            log.warning("studio: 인사 %s 의 GUIDE.md 가 옛 문안이다 — '%s' 절을 가리키는 말이 없다 (%s). 서버는 "
                        "덮어쓰지 않는다 — relay_agent/studio/hr/GUIDE.md 를 보고 손으로 맞춘다", employee_id,
                        employees.HIRING_TITLE, employees.folder(_cfg.studio_dir, employee_id) / "GUIDE.md")
    except Exception:  # noqa: BLE001 - a start must not depend on this look at a file
        log.exception("studio: 인사의 GUIDE.md 를 살피지 못했다")
    return made


def _add_policy_talk(store: StudioStore) -> list[str]:
    """H-6: the '## 프로젝트 정책' section of hr/GUIDE.md added to the guide of an HR employee that lacks it
    (`employees.add_policy_talk`; the rest of the guide is not touched), and the folder committed."""
    def write(folder: Path, text: str) -> None:
        (folder / "GUIDE.md").write_text(text, encoding="utf-8", newline="")
        _folder_commit(folder, "guide: 정책 면담 절")

    with employee_git.lock:
        return employees.add_policy_talk(store, _cfg.studio_dir, write=write)


def fill_parts() -> list[dict]:
    """Fill the empty defaults of the three other parts from relay_agent/studio/parts/ (H-1; §3.4 · §3.15;
    studio/employees.py `fill_parts`): a cell that has a value is never written over,
    except that a guide gets the original's '## 인계' section added when it has none (H-5; `with_handover`). Called once at the server's
    start, after `ensure_hr()` and apart from it. Returns the rows that were written; [] when nothing was empty,
    the store is not open, or it failed (logged — the server starts without it)."""
    with _lock:
        store = _store
    if store is None or _cfg is None:
        return []
    try:
        return employees.fill_parts(store)
    except Exception:  # noqa: BLE001 - a start must not depend on the parts' defaults
        log.exception("studio: 파트의 기본값을 채우지 못했다")
        return []


def commit_folders() -> list[str]:
    """The first commit of every employee's folder that is no git repository yet (H-3; §2.2 · §9.6): the folders
    made before H-3, and the ones the start itself makes (the seed, HR). The files are committed as they are —
    nothing in them is written. A folder that has a `.git` is left alone, whatever is uncommitted there: the next
    write takes it along. Called once at the server's start, after the folders of the start are made. Returns the
    ids that got their first commit; a folder that fails is logged and the server starts without it."""
    with _lock:
        store = _store
    if store is None or _cfg is None:
        return []
    try:
        rows = store.list_employees(include_retired=True)
    except Exception:  # noqa: BLE001 - a start must not depend on the folders' history
        log.exception("studio: 직원 폴더의 첫 커밋을 위해 직원 목록을 읽지 못했다")
        return []
    done = []
    for row in rows:
        folder = employees.folder(_cfg.studio_dir, row["id"])
        try:
            if folder.is_dir() and not employee_git.is_repo(folder):
                if employee_git.commit(folder, f"adopt: {row['id']}"):
                    done.append(row["id"])
        except Exception:  # noqa: BLE001 - one folder must not keep the others, or the start, from going on
            log.exception("studio: 직원 폴더의 첫 커밋을 만들지 못했다 (%s)", folder)
    return done


def close_store() -> None:
    """Close and forget the store and a remembered failure (tests, and a retry after the cause is fixed)."""
    global _store, _failure
    with _lock:
        if _store is not None:
            try:
                _store.close()
            except (StudioStoreError, sqlite3.Error):
                pass
        _store, _failure = None, None


# --- OpenAPI (§6.1) ---------------------------------------------------------------------------------------
OPENAPI_PATH = Path(__file__).resolve().parents[2] / "design" / "studio" / "openapi.json"


def openapi_schema() -> dict:
    """The contract of `/studio/*` as OpenAPI: this router on an app of its own, so making it needs neither
    the server's config nor the engine, and a change of the server's other paths does not touch it. Request
    bodies, queries and paths are typed; responses are plain objects (the handlers return rows)."""
    app = FastAPI(title="Agent 카태 studio API", version="1",
                  description="design/studio/ARCHITECTURE.md §6. 다시 내보내기: uv run relay studio openapi")
    app.include_router(router)
    return app.openapi()


def openapi_text() -> str:
    """The content of the saved file: keys sorted, so the text does not follow the order of declaration."""
    return json.dumps(openapi_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def export_openapi(path: Path | None = None) -> Path:
    """Write the contract (UTF-8, LF) and return where it went. `relay studio openapi` calls this."""
    path = Path(path) if path is not None else OPENAPI_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(openapi_text().encode("utf-8"))  # bytes: Windows must not turn LF into CRLF
    return path


def openapi_is_current(path: Path | None = None) -> bool:
    """Is the saved file the contract this code serves? Compared as JSON: line endings do not matter."""
    try:
        saved = json.loads((Path(path) if path is not None else OPENAPI_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return saved == json.loads(openapi_text())


# --- authentication (§6.2) --------------------------------------------------------------------------------
ACTOR_PATHS = ("/studio/mcp/", "/studio/guard/")   # employees' paths; every other /studio path is the owner's
TOKENLESS = {("POST", "/studio/devices/pair")}      # a phone has no token before the exchange (§6.2 의 2)
DEVICE_PREFIX = devices.TOKEN_PREFIX                # "sdt_": what the exchange of §6.5 hands out
SEEN_EVERY = timedelta(seconds=60)                  # devices.last_seen_at is refreshed once a minute at most
NO_SHARED_TOKEN = (" 이 서버에는 공용 토큰이 없습니다 — relay.config.local.yaml 의 auth_token 또는 환경 변수 "
                   "KATAE_TOKEN 을 설정하고 서버를 다시 시작하세요.")


def _bearer(request: HTTPConnection) -> str | None:
    """The token of `Authorization: Bearer <token>`. None without the header, "" for another scheme.
    Reads the headers only, so the WebSocket handshake (studio/ws.py) is asked the same way."""
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, token = header.strip().partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _older_than(stamp: str | None, age: timedelta) -> bool:
    if not stamp:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(stamp) > age
    except ValueError:
        return True


def owner_device(token: str) -> dict | None:
    """The unrevoked device a `sdt_` token belongs to (its SHA-256 is devices.token_hash), marked as seen.

    Raises StudioError `unavailable` when the DB cannot answer: a lookup that failed is not a wrong token
    (401), and a `last_seen_at` write that failed is not swallowed, so a broken DB shows."""
    store = get_store()
    try:
        device = store.get_device_by_token_hash(hashlib.sha256(token.encode()).hexdigest())
        if device is None or device["revoked_at"] is not None:
            return None
        if _older_than(device["last_seen_at"], SEEN_EVERY):
            device = store.update_device(device["id"], last_seen_at=now()) or device
    except (StudioStoreError, sqlite3.Error) as e:
        raise StudioError("unavailable", f"기기 토큰을 확인하지 못했습니다 — {type(e).__name__}: {e}") from e
    return device


def run_is_running(run_id: str) -> bool:
    """§8.2 의 검사 2: is that run `running` now? Asked of the engine's liveness when that is connected, else
    of `_run_view` — on the server both are there and both give `RunState.status` of the same engine
    (`RelayEngine.liveness`, studio/review.py `run_view`). A run the engine does not know is not running.
    StudioError `unavailable` when neither is connected — "cannot say" is not "ended"."""
    if _liveness is not None:
        try:
            return _liveness(run_id).get("status") == "running"
        except FileNotFoundError:  # RelayEngine.load: no run.json under that id
            return False
    if _run_view is None:
        raise StudioError("unavailable", "런 상태를 읽을 엔진이 연결되지 않았습니다 (install 의 liveness)")
    view = _run_view(run_id)
    return view is not None and view.get("status") == "running"


def actor_subject(token: str) -> dict | None:
    """§8.2 의 검사 1–3 for an actor token: `{"kind": "employee", employee_id, run_id, room_id, task_id,
    mode}`, or None when it establishes nobody — the signature is wrong (1), the run is not in studio's
    ledger or no longer running (2), the employee is gone or retired (3).

    The ledger is `run_head`: tasks.run_id for a task run, the tracked runs for chat · read turns. Raises
    StudioError `unavailable` when the DB or the engine cannot answer: that is not a wrong token (401)."""
    claims = sat.verify(token)
    if claims is None:
        return None
    store = get_store()
    try:
        head = run_head(store, claims["run"])
        employee = store.get_employee(claims["emp"])
    except (StudioStoreError, sqlite3.Error) as e:
        raise StudioError("unavailable", f"행위자 토큰을 확인하지 못했습니다 — {type(e).__name__}: {e}") from e
    if head is None or employee is None or employee["retired_at"] is not None:
        return None
    if not run_is_running(claims["run"]):
        return None
    return {"kind": "employee", "employee_id": employee["id"], "run_id": claims["run"],
            "room_id": claims["room"], "task_id": claims["task"], "mode": claims["mode"]}


def actor_token(run_id: str) -> str | None:
    """A new actor token for a run of studio's ledger (§8.2 '주입': one per stage), from whose run it is —
    the employee, the room, the task and the mode of `run_head`. None for a run studio did not start (a
    dashboard or CLI run: no studio MCP server is attached to it) and while the store is not open.
    Called from outside a request: the engine's env callback (S-6 2/2)."""
    with _lock:
        store = _store
    if store is None:
        return None
    head = run_head(store, run_id)
    if head is None or not head.get("employee_id") or not head.get("room_id"):
        return None
    return sat.issue(run=run_id, emp=head["employee_id"], room=head["room_id"], task=head.get("task_id"),
                     mode=head["mode"])


def mcp_env(run_id: str, stage: str = "") -> dict[str, str] | None:
    """The engine's env callback for the `studio` MCP server (§8.2 '주입'; server.py registers it as
    `engine.mcp_env["studio"]`): the server's address and a new actor token for this stage of a run of
    studio's ledger. None for any other run — the engine then attaches no studio server to it (§11.4)."""
    token = actor_token(run_id)
    if token is None or _cfg is None:
        return None
    return {"STUDIO_URL": _cfg.server_url, "STUDIO_ACTOR_TOKEN": token}


def guard_stage(run_id: str, stage: str = "", *, run_dir: Path, cwd: Path, settings: Path | None = None) -> dict | None:
    """The engine's guard callback (§11.4 의 1; server.py registers it as `engine.guard_stage`): what a stage of
    a studio run is given on top of its own — `allowed_tools` (`studio-full`, §10.1), `settings_path` (the hook,
    with the backup hook of `settings` when the workspace has one), `env` (where the hook asks and as whom) and
    `claude_only` (§10.8 의 9). guard_ctx.json and the settings file are written again at every stage.

    When this server was given the shared token by its environment (`KATAE_TOKEN`), `env` also blanks it for the
    run (§10.8 의 4): the employee's shell would otherwise read the owner's token there. The key is not added
    when the server has no such variable.

    None for a run that is not in studio's ledger (a dashboard · CLI · campaign run: its call stays as it was)
    and while the store is not open. It raises when it cannot tell or cannot write — the engine then fails the
    stage instead of running a studio run without its guard."""
    with _lock:
        store = _store
    if store is None or _cfg is None:
        return None
    head = run_head(store, run_id)
    if head is None or not head.get("employee_id") or not head.get("room_id"):
        return None
    token = actor_token(run_id)
    if token is None:
        raise RuntimeError(f"studio 런 {run_id} 의 행위자 토큰을 만들지 못했다")
    ctx_path = guard.write_ctx(Path(run_dir), _cfg, run_id, cwd, store.list_projects(), settings)
    env = {"STUDIO_URL": _cfg.server_url, "STUDIO_ACTOR_TOKEN": token}
    if "KATAE_TOKEN" in os.environ:
        env["KATAE_TOKEN"] = ""
    return {"allowed_tools": guard.profile(head["mode"]),
            "settings_path": guard.write_settings(Path(run_dir), ctx_path, settings),
            "env": env, "claude_only": True}


def run_workdir(run_id: str) -> Path | None:
    """The folder that run's stages work in (§6.6 '직원의 첨부'), from the callback `install` was given. None
    when no callback is connected or the engine has no such run."""
    found = _workdir(run_id) if _workdir is not None else None
    return Path(found) if found else None


def identify(token: str, cfg: Config) -> dict | None:
    """Who a token is: `{"kind": "owner", "device_id": id | None}`, `{"kind": "employee", …}` for an actor
    token that passes §8.2 의 검사 1–3 (`actor_subject`), or None when it establishes nobody.

    The shared token is checked first and needs no DB. A device token is accepted with or without a shared
    token configured. Raises StudioError `unavailable` when a device or actor token cannot be looked up."""
    if not token:
        return None
    if cfg.auth_token and hmac.compare_digest(token.encode(), cfg.auth_token.encode()):
        return {"kind": "owner", "device_id": None}
    if token.startswith(DEVICE_PREFIX):
        device = owner_device(token)
        if device is not None:
            return {"kind": "owner", "device_id": device["id"]}
    if token.startswith(sat.PREFIX):
        return actor_subject(token)
    return None


async def authenticate(request: Request, cfg: Config) -> JSONResponse | None:
    """§6.2 for one HTTP request under /studio/: None lets it through, a response refuses it.

    The server's guard calls this after its origin and network checks and before its loopback pass, because
    /studio/* asks this PC for a token too. The 401 code follows the path, not the token: `actor_invalid` on
    the actor paths, `unauthorized` elsewhere. 403 `actor_not_allowed` is for a subject that was established
    on a path that is not its own."""
    path = request.url.path
    if (request.method, path) in TOKENLESS:
        return None
    actor_path = path.startswith(ACTOR_PATHS)
    code = "actor_invalid" if actor_path else "unauthorized"
    hint = "" if cfg.auth_token else NO_SHARED_TOKEN
    token = _bearer(request)
    if token is None:
        return error_response(code, "인증 토큰이 필요합니다 (/studio 는 이 PC 에서 불러도 토큰을 요구합니다)." + hint)
    try:
        who = await run_in_threadpool(identify, token, cfg)  # a device token reads the DB: off the event loop
    except StudioError as e:
        return e.response()
    if who is None:
        if actor_path:
            return error_response(code, "행위자 토큰이 무효입니다 (서명이 맞지 않거나, 런이 끝났거나, 직원이 없습니다).")
        return error_response(code, "토큰이 맞지 않습니다 (대소문자 구분)." + hint)
    if (who["kind"] == "employee") != actor_path:  # a subject, but not this path's (§6.2)
        return error_response("actor_not_allowed", "이 경로는 직원(행위자) 토큰만 받습니다" if actor_path else
                              "이 경로는 owner 토큰만 받습니다 (행위자 토큰은 /studio/mcp/ · /studio/guard/ 에서만 씁니다)")
    request.state.studio_subject = who  # `devices/me` (A-11) reads the device id, `get_caller` the employee
    return None


# --- shared helpers ---------------------------------------------------------------------------------------
MAX_PAGE = 200  # the largest page of a list with a cursor (messages, task events)


def _need(row: dict | None, what: str, id: Any) -> dict:
    if row is None:
        raise StudioError("not_found", f"{what}을(를) 찾을 수 없습니다: {id}")
    return row


def _with_dm_room(store: StudioStore, rows: list[dict]) -> list[dict]:
    """Employee rows with `dm_room_id` (None while the 1:1 room does not exist) and `company_wide` (true for
    an employee of the HR part: of the company, not of a project — §3.5)."""
    rooms = {r["employee_id"]: r["id"] for r in store.list_rooms(kind="dm", include_archived=True)}
    return [{**e, "dm_room_id": rooms.get(e["id"]),
             "company_wide": e["department_id"] == employees.HR_DEPARTMENT} for e in rows]


def _project_staff(store: StudioStore, project_id: str, *, department_id: str | None = None,
                   include_retired: bool = False) -> list[dict]:
    """The employees a project shows (§6.4): the ones placed in it, then the HR employee in service — who is
    placed in no project and works for all of them. A part filter other than HR leaves HR out; a retired HR
    is of no project, so `include_retired` does not bring one in. Other employees that are not placed stay out."""
    rows = store.list_employees(project_id=project_id, department_id=department_id, include_retired=include_retired)
    if department_id in (None, employees.HR_DEPARTMENT):
        seen = {row["id"] for row in rows}
        rows = rows + [row for row in store.list_employees(department_id=employees.HR_DEPARTMENT)
                       if row["id"] not in seen]
    return rows


EMPLOYEE_FILES = employees.FILE_KEYS


def _employee_files(employee_id: str) -> dict:
    """The four files of runs/studio/employees/<id>/ (§2.2) as text; None for a file that is not there."""
    texts = employees.read_files(_config().studio_dir, employee_id)
    return {key: texts[name] for key, name in EMPLOYEE_FILES.items()}


_room_project = files.room_project  # the project a room's events are filed under; tools.room_post uses it too


# --- projects ---------------------------------------------------------------------------------------------
class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    engine_version: str | None = None  # null is stored as '' (§3.3: the column is NOT NULL DEFAULT '')
    mcp_port: int | None = Field(default=None, ge=1, le=65535)  # null clears the port


@router.get("/projects")
def list_projects(archived: int = Query(0, ge=0, le=1), store: StudioStore = Depends(get_store)) -> dict:
    """`archived=1` adds the archived projects to the list."""
    return {"items": store.list_projects(include_archived=bool(archived))}


class ProjectImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str                # the folder the .uproject is in, absolute
    name: str | None = None  # default: the .uproject's name
    repo: str | None = None  # default: the registered repository the folder lies in


def _import_repo(folder: Path, repo: str | None) -> Any:
    """The RepoSpec a project in `folder` is registered under (§3.3): the registered repository the folder lies
    in (`RepoRegistry.match`) — which `repo`, when it is given, has to name (`RepoRegistry.get`)."""
    registry = _repos() if _repos is not None else None
    if repo is not None:
        try:
            if registry is None:
                raise KeyError(f"등록되지 않은 저장소: {repo} (등록: relay repo add {repo} <경로>)")
            registry.get(repo)
        except KeyError as e:
            raise StudioError("precondition_failed", str(e.args[0] if e.args else e)) from e
    home = registry.match(folder) if registry is not None else None
    if home is None:
        raise StudioError("precondition_failed",
                          f"등록된 저장소 안의 폴더가 아닙니다: {folder} (등록: relay repo add <이름> <경로>)")
    if repo is not None and home.name != repo:
        raise StudioError("precondition_failed",
                          f"이 폴더는 저장소 '{home.name}' 안에 있습니다. repo 를 빼거나 '{home.name}' 으로 보내세요.")
    return home


# §6.4 (M-6): bring in a UE project that is already on this computer. The folder is only read (studio/importer.py):
# what is missing — the MCP port, the harness — is told in `detected`, and filling it in is M-4 · M-5's. The
# repository is asked before the folder is looked at, so a path outside every registered repository gets one answer
# whether it is there or not. No docstring: the saved contract would carry it as the description.
@router.post("/projects", status_code=201)
def import_project(body: ProjectImport, store: StudioStore = Depends(get_store)) -> dict:
    path = body.path.strip()
    name = body.name.strip() if body.name is not None else None
    repo = body.repo.strip() if body.repo is not None else None
    blank = [key for key, value in (("path", path), ("name", name), ("repo", repo)) if value == ""]
    if blank:
        raise StudioError("missing_fields", f"{' · '.join(blank)} 은(는) 비울 수 없습니다", fields=blank)
    try:
        folder = Path(path).resolve() if Path(path).is_absolute() else None
    except (OSError, ValueError):  # a text no path can be made of
        folder = None
    if folder is None:
        raise StudioError("precondition_failed", f"path 는 프로젝트 폴더의 절대 경로여야 합니다: {path}")
    spec = _import_repo(folder, repo)
    if not folder.is_dir():
        if folder.is_file():  # the .uproject itself, most often: the same refusal, told apart
            raise StudioError("precondition_failed",
                              f"path 가 파일입니다 — .uproject 가 있는 폴더의 경로를 주세요: {folder}")
        raise StudioError("precondition_failed", f"폴더가 없습니다: {folder}")
    if importer.find_uproject(folder) is None:
        raise StudioError("precondition_failed", f"이 폴더에 .uproject 파일이 없습니다: {folder}")
    try:
        engines = machine_info.engines(_machine.engines)
    except Exception:  # noqa: BLE001 — a registry that cannot be read does not stop an import
        log.exception("studio: 설치된 엔진 목록을 읽지 못했다")
        engines = []
    found = importer.detect(folder, engines, top=spec.resolved)
    name = name or Path(found["uproject"] or folder.name).stem
    with store.transaction() as tx:
        others = store.list_projects(include_archived=True, tx=tx)  # an archived one keeps its name and repo
        clash = importer.conflict(others, name=name, folder=folder, repo=spec.name)
        if clash is not None:  # (field, detail): path, name, repo — the first of them, in this order
            raise StudioError("conflict", clash[1], field=clash[0])
        port = found["mcp"]["port"]
        holder = next((p["id"] for p in others
                       if port is not None and p["mcp_port"] == port and p["archived_at"] is None), None)
        if holder is not None:  # not refused: the row goes in without a port, and the answer says who has it
            found["mcp"]["in_use_by"], port = holder, None
        try:
            row = store.create_project(name, str(folder), spec.name, tx=tx, mcp_port=port,
                                       engine_version=importer.engine_version_of(found["engine"]["association"]))
        except sqlite3.IntegrityError as e:
            raise StudioError("conflict", "이름 · 저장소 · 포트가 다른 프로젝트와 겹칩니다",
                              field=_unique_field(str(e))) from e
        store.emit(tx, "project.updated", {"project": row}, project_id=row["id"])
    _begin_policy_talk(store, row, folder)
    return {**row, "detected": found}


def _begin_policy_talk(store: StudioStore, project: dict, rules_from: Path | None = None) -> None:
    """H-6: HR's 1:1 room gets the system message that opens the policy talk, and HR's chat turn is asked for.
    The project is committed by now: whatever fails here is logged and the answer of the call does not change."""
    try:
        started = project_policy.begin_interview(store, _config().studio_dir, project, rules_from=rules_from)
    except Exception:  # noqa: BLE001
        log.exception("studio: 프로젝트 %s 의 정책 면담을 시작하지 못했다", project.get("id"))
        return
    if started is not None:
        _ask_turn(*started)


# §6.4 (M-8): make a new UE project. The request is checked here (studio/jobs.py `admit`) and nothing else is done:
# the five steps run on the job's thread, told of by `job.step` · `job.log` · `job.finished` (§7.4). `plugins` and
# `harness` may be left out — then they are what the options below have checked. No docstrings: the saved contract
# would carry them as descriptions.
class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    engine_version: str
    parent_dir: str                    # the folder the project's folder is made in, absolute
    plugins: list[str] | None = None   # ids of the options; left out: the checked ones, []: none
    harness: bool | None = None        # left out: on when the harness folder of the settings can be used


def _engines() -> list[dict]:
    try:
        return machine_info.engines(_machine.engines)
    except Exception:  # noqa: BLE001 — a registry that cannot be read is a computer without engines
        log.exception("studio: 설치된 엔진 목록을 읽지 못했다")
        return []


def _refused(e: creator.Refused) -> StudioError:
    return StudioError(e.code, e.message, field=e.field, reason=e.reason)


@router.get("/projects/create/options")
def project_create_options(engine_version: str | None = Query(None, max_length=40)) -> dict:
    cfg = _config()
    try:
        return create_jobs.options(engine_version, engines=_engines(), entries=cfg.studio_plugins,
                                   plugin_root=cfg.studio_plugin_root, harness_root=cfg.studio_harness_root)
    except creator.Refused as e:
        raise _refused(e) from e


@router.post("/projects/create", status_code=202)
def create_new_project(body: ProjectCreate, store: StudioStore = Depends(get_store)) -> dict:
    cfg, runner = _config(), _job_runner()
    try:
        admitted = create_jobs.admit(
            body.name, body.engine_version, body.parent_dir, body.plugins, body.harness, engines=_engines(),
            entries=cfg.studio_plugins, plugin_root=cfg.studio_plugin_root, harness_root=cfg.studio_harness_root,
            registry=_repos() if _repos is not None else None,
            projects=store.list_projects(include_archived=True))
        return {"job": runner.create(store, admitted)}
    except creator.Refused as e:
        raise _refused(e) from e


@router.get("/jobs/{id}")
def read_job(id: str) -> dict:
    return _need(create_jobs.load(_config().studio_dir, id), "생성 작업", id)


@router.post("/jobs/{id}/retry", status_code=202)
def retry_job(id: str, store: StudioStore = Depends(get_store)) -> dict:
    cfg, runner = _config(), _job_runner()
    try:
        job = runner.retry(store, id, entries=cfg.studio_plugins, plugin_root=cfg.studio_plugin_root)
    except creator.Refused as e:
        raise _refused(e) from e
    return {"job": _need(job, "생성 작업", id)}


@router.post("/jobs/{id}/cleanup", status_code=202)
def cleanup_job(id: str, store: StudioStore = Depends(get_store)) -> dict:
    runner = _job_runner()
    try:
        job = runner.cleanup(store, id)
    except creator.Refused as e:
        raise _refused(e) from e
    return {"job": _need(job, "생성 작업", id)}


@router.get("/projects/{id}")
def get_project(id: str, store: StudioStore = Depends(get_store)) -> dict:
    row = _need(store.get_project(id), "프로젝트", id)
    staff = _with_dm_room(store, _project_staff(store, id))  # as `GET /employees?project_id`: HR comes last
    # `editor` stays null (M-7, §6.4): the status is `GET …/editor`'s — here it would read the processes each time
    return {**row, "employees": staff, "task_counts": store.count_tasks_by_status(id), "editor": None}


# §6.4 · §6.6 (M-7): the project's Unreal editor — how it is, start it, stop it, a picture of its viewport. The
# work is studio/editor.py's; the processes and the health check are those `GET /studio/machine` reads from
# (`_machine`), so the two answers agree. `start` answers at once (an editor takes minutes to come up); `stop`
# asks the editor to close and ends it by force after a wait — not with `{force: false}`, and its body may be
# left out; `screenshot` starts an editor that is off and waits for it — the request stays open that long. No
# docstrings: the saved contract would carry them.
class EditorShot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_id: str | None = None  # the room the picture is posted in; left out: the attachment alone


class EditorStop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = True  # false: the editor is only asked to close, and may still run when the answer comes


_editor = editor_control.Tools()  # how an editor is started, ended and asked; a test replaces parts of it


def _editor_refused(e: editor_control.Refused) -> StudioError:
    return StudioError(e.code, e.message, failed=list(e.failed))


def _editor_sources() -> dict:
    return {"processes": _machine.processes, "healthy": _machine.healthy}


@router.get("/projects/{id}/editor")
def read_editor(id: str, store: StudioStore = Depends(get_store)) -> dict:
    project = _need(store.get_project(id), "프로젝트", id)
    return editor_control.status(project, store.list_projects(), **_editor_sources())


@router.post("/projects/{id}/editor/start")
def start_editor(id: str, store: StudioStore = Depends(get_store)) -> dict:
    project = _need(store.get_project(id), "프로젝트", id)
    try:
        return editor_control.start(project, store.list_projects(), _engines(), tools=_editor, **_editor_sources())
    except editor_control.Refused as e:
        raise _editor_refused(e) from e


@router.post("/projects/{id}/editor/stop")
def stop_editor(id: str, body: EditorStop | None = None, store: StudioStore = Depends(get_store)) -> dict:
    project = _need(store.get_project(id), "프로젝트", id)
    return editor_control.stop(project, store.list_projects(), tools=_editor, **_editor_sources(),
                               force=True if body is None else body.force)


@router.post("/projects/{id}/editor/screenshot", status_code=201)
def shoot_editor(id: str, body: EditorShot, store: StudioStore = Depends(get_store)) -> dict:
    project = _need(store.get_project(id), "프로젝트", id)
    if body.room_id is not None:  # before anything is started: an editor is not launched for a room that is not there
        _need(store.get_room(body.room_id), "방", body.room_id)
    try:
        shot = editor_control.screenshot(project, store.list_projects(), _engines(), _attachment_root(),
                                         tools=_editor, **_editor_sources())
    except editor_control.Refused as e:
        raise _editor_refused(e) from e
    if body.room_id is None:  # the picture stays in its `shot_` folder: that is the address it is fetched from
        return {"attachment": {"name": shot.name, "path": f"{shot.parent.name}/{shot.name}",
                               "mime": _mime(shot.name), "size": shot.stat().st_size}, "message": None}
    copied: Path | None = None
    try:
        with store.transaction() as tx:  # the row, its file and `message.created` together, like post_message
            room = _need(store.get_room(body.room_id, tx=tx), "방", body.room_id)
            message = store.create_message(room["id"], "system", tx=tx, body=f"{project['name']} 에디터 뷰포트",
                                           meta={"event": "editor.screenshot", "project_id": id})
            copied = _attachment_root() / str(message["id"])  # named after the message id, known only now
            refs = files.copy([(shot, shot.name)], copied)
            message = store.set_message_attachments(tx, message["id"], refs)
            store.emit(tx, "message.created", {"message": message}, room_id=room["id"], task_id=room["task_id"],
                       project_id=_room_project(store, room, tx))
    except BaseException:
        if copied is not None:  # rolled back: the id may be handed out again, so its folder must not stay
            shutil.rmtree(copied, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(shot.parent, ignore_errors=True)  # the message's folder has the picture now
    return {"attachment": refs[0], "message": message}


def _unique_field(message: str) -> str:
    """'UNIQUE constraint failed: projects.name' -> 'name'."""
    found = re.search(r"projects\.(\w+)", message)
    return found.group(1) if found else ""


@router.patch("/projects/{id}")
def patch_project(id: str, body: ProjectPatch, store: StudioStore = Depends(get_store)) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()
        if not fields["name"]:
            raise StudioError("missing_fields", "name 은 비울 수 없습니다", fields=["name"])
    if "engine_version" in fields:
        fields["engine_version"] = (fields["engine_version"] or "").strip()
    with store.transaction() as tx:
        row = _need(store.get_project(id, tx=tx), "프로젝트", id)
        fields = {k: v for k, v in fields.items() if row[k] != v}
        if not fields:
            return row  # nothing changes: no write, no event
        others = [p for p in store.list_projects(include_archived=True, tx=tx) if p["id"] != id]
        if "name" in fields and any(p["name"] == fields["name"] for p in others):
            raise StudioError("conflict", f"이름 '{fields['name']}' 은(는) 다른 프로젝트가 쓰고 있습니다", field="name")
        port = fields.get("mcp_port")
        if port is not None and row["archived_at"] is None and any(
                p["mcp_port"] == port and p["archived_at"] is None for p in others):
            raise StudioError("conflict", f"MCP 포트 {port} 은(는) 다른 프로젝트가 쓰고 있습니다", field="mcp_port")
        try:
            row = store.update_project(id, tx=tx, **fields)
        except sqlite3.IntegrityError as e:
            raise StudioError("conflict", "이름 · 포트가 다른 프로젝트와 겹칩니다", field=_unique_field(str(e))) from e
        store.emit(tx, "project.updated", {"project": row}, project_id=id)
    return row


@router.post("/projects/{id}/archive")
def archive_project(id: str, store: StudioStore = Depends(get_store)) -> dict:
    with store.transaction() as tx:
        row = _need(store.get_project(id, tx=tx), "프로젝트", id)
        if row["archived_at"] is not None:
            return row  # already archived: the same row, no second event
        counts = store.count_tasks_by_status(id, tx=tx)
        unfinished = sum(n for status, n in counts.items() if status not in TERMINAL)
        if unfinished:
            raise StudioError("precondition_failed",
                              f"끝나지 않은 작업이 {unfinished}개 있습니다. 완료 · 취소한 뒤에 보관하세요.")
        row = store.update_project(id, tx=tx, archived_at=now())
        store.emit(tx, "project.updated", {"project": row}, project_id=id)
    return row


# --- departments · employees ------------------------------------------------------------------------------
@router.get("/departments")
def list_departments(store: StudioStore = Depends(get_store)) -> dict:
    return {"items": store.list_departments()}


class DepartmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    default_relay: str | None = None            # null · '' = no relay of its own (§3.4)
    default_mcp: list[str] | None = None
    default_models: dict[str, Any] | None = None  # {chat, work, cap} of §9.2
    guide_template: str | None = None


class DepartmentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    default_relay: str | None = None            # null · '' clears the relay
    default_mcp: list[str] | None = None        # null is stored as []
    default_models: dict[str, Any] | None = None  # null is stored as {}
    guide_template: str | None = None           # null is stored as ''


def _department_fields(fields: dict, department_id: str) -> dict:
    """The keys a part's POST · PATCH was given, as the columns take them and checked (§3.4): the name is not
    empty, `default_mcp` is names without `studio`, and `default_relay` · `default_models` pass the checks of
    §9.4 as the keys of an employee.yaml (`employees.defaults_problems`) — the codes go out in `failed[]`."""
    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()
        if not fields["name"]:
            raise StudioError("missing_fields", "name 은 비울 수 없습니다", fields=["name"])
    if "default_relay" in fields:
        fields["default_relay"] = (fields["default_relay"] or "").strip() or None
    if "default_mcp" in fields:
        names = [name.strip() for name in fields["default_mcp"] or []]
        if not all(names) or employees.STUDIO_MCP in names:
            raise StudioError("precondition_failed", "default_mcp 는 MCP 서버 이름의 목록입니다. studio 는 서버가 "
                              "항상 붙이므로 적지 않습니다.", failed=["mcp_unknown"])
        fields["default_mcp"] = list(dict.fromkeys(names))
    if "default_models" in fields:
        fields["default_models"] = fields["default_models"] or {}
    if "guide_template" in fields:
        fields["guide_template"] = fields["guide_template"] or ""
    if fields.get("default_relay") is not None or fields.get("default_models"):
        problems = employees.defaults_problems(
            department_id, relay=fields.get("default_relay"), mcp=None, models=fields.get("default_models") or {},
            checks=employees.context(_config()))
        if problems:
            raise StudioError("precondition_failed", "파트 기본값이 employee.yaml 의 검사에 걸립니다: "
                              + " / ".join(p["message"] for p in problems), failed=[p["code"] for p in problems])
    return fields


def _name_taken(name: str) -> StudioError:
    return StudioError("conflict", f"이름 '{name}' 은(는) 다른 파트가 쓰고 있습니다 (보관된 파트 포함)", field="name")


# H-1 (§3.4 · §3.6): a part of the owner's comes with its room, in the one transaction; the room's `room.updated`
# is the only event (§7.4 has none of a part). Comments, not docstrings, on the three below: the saved contract
# (openapi.json) would carry a docstring as the path's description.
@router.post("/departments", status_code=201)
def create_department(body: DepartmentCreate, store: StudioStore = Depends(get_store)) -> dict:
    fields = _department_fields(body.model_dump(exclude_unset=True), "new")
    name = fields.pop("name")
    fields = {key: value for key, value in fields.items() if value is not None}
    with store.transaction() as tx:
        parts = store.list_departments(include_archived=True, tx=tx)
        if any(part["name"] == name for part in parts):
            raise _name_taken(name)
        order = max((part["sort_order"] for part in parts), default=-1) + 1
        try:
            row = store.create_department(name, tx=tx, sort_order=order, **fields)
            room = store.create_room("department", title=name, department_id=row["id"], tx=tx)
        except sqlite3.IntegrityError as e:
            raise _name_taken(name) from e
        store.emit(tx, "room.updated", {"room": room, "members": []}, room_id=room["id"])
    return row


# A builtin part is patched as any other: only its id and `builtin` cannot change. A new name goes to the part's
# room as well. Nothing changes: no write, no event.
@router.patch("/departments/{id}")
def patch_department(id: str, body: DepartmentPatch, store: StudioStore = Depends(get_store)) -> dict:
    with store.transaction() as tx:
        row = _need(store.get_department(id, tx=tx), "파트", id)
        fields = _department_fields(body.model_dump(exclude_unset=True), id)
        fields = {key: value for key, value in fields.items() if row[key] != value}
        if not fields:
            return row
        name = fields.get("name")
        if name is not None and any(part["name"] == name and part["id"] != id
                                    for part in store.list_departments(include_archived=True, tx=tx)):
            raise _name_taken(name)
        try:
            row = store.update_department(id, tx=tx, **fields)
        except sqlite3.IntegrityError as e:
            raise _name_taken(name or row["name"]) from e
        if name is not None:
            for room in store.list_rooms(kind="department", department_id=id, include_archived=True, tx=tx)[:1]:
                room = store.update_room(room["id"], tx=tx, title=name)
                members = store.list_room_members(room_id=room["id"], tx=tx)
                store.emit(tx, "room.updated", {"room": room, "members": members}, room_id=room["id"])
    return row


# Refused for a builtin part and for one that has an employee in service (not retired). The part's room is
# archived with it. Already archived: the same row, no second event. Its name stays taken.
@router.post("/departments/{id}/archive")
def archive_department(id: str, store: StudioStore = Depends(get_store)) -> dict:
    with store.transaction() as tx:
        row = _need(store.get_department(id, tx=tx), "파트", id)
        if row["archived_at"] is not None:
            return row
        if row["builtin"]:
            raise StudioError("precondition_failed", "기본 파트는 보관할 수 없습니다.")
        staff = store.list_employees(department_id=id, tx=tx)
        if staff:
            raise StudioError("precondition_failed",
                              f"재직 중인 직원이 {len(staff)}명 있습니다. 퇴사시키거나 다른 파트로 옮긴 뒤에 보관하세요.")
        stamp = now()
        row = store.update_department(id, tx=tx, archived_at=stamp)
        for room in store.list_rooms(kind="department", department_id=id, tx=tx)[:1]:
            room = store.update_room(room["id"], tx=tx, archived_at=stamp)
            members = store.list_room_members(room_id=room["id"], tx=tx)
            store.emit(tx, "room.updated", {"room": room, "members": members}, room_id=room["id"])
    return row


# §6.4: the alias of `GET /models`, narrowed to the providers a guard hook can be put on — Claude Code. The
# values an `employee.yaml` slot and a `start` override may name (§9.4 의 8, §9.5 의 5). No docstring: the
# saved contract (openapi.json) would carry it as the path's description.
@router.get("/models")
def list_models() -> list[dict]:
    checks = employees.context(_config())
    return work.models((_models or checks.providers.catalog)(), checks)


@router.get("/employees")
def list_employees(project_id: str | None = None, department_id: str | None = None,
                   retired: int = Query(0, ge=0, le=1), store: StudioStore = Depends(get_store)) -> dict:
    """`retired=1` adds the retired employees to the list."""
    # With `project_id` the HR employee in service is listed too, after the ones placed there (`_project_staff`,
    # §6.4) — said here and not in the docstring, which the saved contract (openapi.json) carries. A project
    # that does not exist has nobody, HR neither: an empty list and no error, as `list_campaigns` filters.
    if project_id is None:
        rows = store.list_employees(department_id=department_id, include_retired=bool(retired))
    elif store.get_project(project_id) is None:
        rows = []
    else:
        rows = _project_staff(store, project_id, department_id=department_id, include_retired=bool(retired))
    return {"items": _with_dm_room(store, rows)}


@router.get("/employees/{id}")
def get_employee(id: str, store: StudioStore = Depends(get_store)) -> dict:
    row = _with_dm_room(store, [_need(store.get_employee(id), "직원", id)])[0]
    tasks = [t for t in store.list_tasks(assignee_id=id) if t["status"] not in TERMINAL]
    # `runs` (the employee's studio runs going on now) is filled by S-7 · S-8
    return {**row, "files": _employee_files(id), "tasks": tasks, "runs": []}


# --- an employee's files and their history (H-3, part 1; §6.4 · §9.6) ---------------------------------------
# The owner's direct write and the revert: check -> write -> commit of the folder's own git -> `employee.updated`
# (`change = file`). `employee_git.lock` is held from the read to the commit, so of two writes made from one
# base only the first passes. A commit that fails leaves the file written and answers `commit: null` (logged):
# the next commit takes the change along, as the approvals' `Inbox._safely` does. Comments, not docstrings,
# where the saved contract (openapi.json) would carry more than a line.
EmployeeFileName = Literal["PERSONA.md", "GUIDE.md", "MEMORY.md", "employee.yaml"]  # employees.FILES


class EmployeeFileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    base_sha256: str  # of the file the text was made from; "" = there was no file


class EmployeeRevert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit: str  # a sha of the folder's history, or the start of one (7–40 hex digits)


def _file_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError as e:
        raise StudioError("precondition_failed", f"파일을 읽지 못했습니다: {e}") from e


def _file_restore(path: Path, old: bytes | None) -> None:
    try:
        if old is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(old)
    except OSError:
        log.exception("studio: %s 를 되돌리지 못했다", path)


def _yaml_checked(store: StudioStore, employee: dict, text: object) -> None:
    """§9.6: the checks of §9.4, the same on every way an `employee.yaml` is written."""
    problems = employees.check(text, department_id=employee["department_id"],
                               department=store.get_department(employee["department_id"]),
                               checks=employees.context(_config()))
    if problems:
        raise StudioError("precondition_failed", "employee.yaml 이 검사에 걸립니다: "
                          + " / ".join(p["message"] for p in problems), failed=[p["code"] for p in problems])


def _folder_head(folder: Path) -> str | None:
    try:
        return employee_git.head(folder)
    except Exception:  # noqa: BLE001 - the answer of a write must not depend on reading the history
        log.exception("studio: 직원 폴더의 마지막 커밋을 읽지 못했다 (%s)", folder)
        return None


def _folder_commit(folder: Path, message: str) -> str | None:
    """Commit the folder and give the sha it is at now. None when git failed: logged, the files stay written."""
    try:
        return employee_git.commit(folder, message) or employee_git.head(folder)
    except Exception:  # noqa: BLE001 - the file is written: what follows must not hide or undo that
        log.exception("studio: 직원 폴더를 커밋하지 못했다 (%s, '%s') — 파일은 그대로다", folder, message)
        return None


def _file_changed(store: StudioStore, employee: dict) -> None:
    with store.transaction() as tx:
        store.emit(tx, "employee.updated", {"employee": employee, "change": "file"},
                   project_id=employee["project_id"])


@router.put("/employees/{id}/files/{name}")
def put_employee_file(id: str, name: EmployeeFileName, body: EmployeeFileBody,
                      store: StudioStore = Depends(get_store)) -> dict:
    """Write the file if it is still the one `base_sha256` names ("" = no file yet), and commit the folder."""
    with employee_git.lock:
        employee = _need(store.get_employee(id), "직원", id)
        if name == "employee.yaml":
            _yaml_checked(store, employee, body.text)
        elif name == "MEMORY.md":
            problems = employees.memory_problems(body.text)
            if problems:
                raise StudioError("precondition_failed", " / ".join(p["message"] for p in problems),
                                  failed=[p["code"] for p in problems])
        folder = employees.folder(_config().studio_dir, id)
        path = folder / name
        old = _file_bytes(path)
        if (hashlib.sha256(old).hexdigest() if old is not None else "") != body.base_sha256.strip().lower():
            raise StudioError("precondition_failed", "읽은 뒤에 파일이 바뀌었습니다. 지금 파일을 다시 읽고 고쳐 보냅니다.",
                              failed=["base_mismatch"])
        new = body.text.encode("utf-8")
        digest = hashlib.sha256(new).hexdigest()
        if new == old:  # nothing to write: no commit and no event
            return {"name": name, "sha256": digest, "commit": _folder_head(folder)}
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_bytes(new)
        except OSError as e:
            _file_restore(path, old)
            raise StudioError("precondition_failed", f"파일을 쓰지 못했습니다: {e}") from e
        commit = _folder_commit(folder, f"edit: {name}")
        _file_changed(store, employee)
        return {"name": name, "sha256": digest, "commit": commit}


# The policy of a project (H-6; `studio/policy.py`): studio/projects/<id>/POLICY.md. `base_sha256` is the file's
# as it was read ("" = no file yet); another one is `precondition_failed`. Written under `employee_git.lock`, as an
# approval of `policy_draft` writes it (§9.6). No event: the answer carries the new state.
class ProjectPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    base_sha256: str


@router.get("/projects/{id}/policy")
def get_project_policy(id: str, store: StudioStore = Depends(get_store)) -> dict:
    _need(store.get_project(id), "프로젝트", id)
    return project_policy.read(_config().studio_dir, id)


@router.put("/projects/{id}/policy")
def put_project_policy(id: str, body: ProjectPolicyBody, store: StudioStore = Depends(get_store)) -> dict:
    _need(store.get_project(id), "프로젝트", id)
    path = project_policy.path(_config().studio_dir, id)
    with employee_git.lock:
        old = _file_bytes(path)
        if (hashlib.sha256(old).hexdigest() if old is not None else "") != body.base_sha256.strip().lower():
            raise StudioError("precondition_failed", "읽은 뒤에 정책이 바뀌었습니다. 지금 정책을 다시 읽고 고쳐 보냅니다.",
                              failed=["base_mismatch"])
        new = body.text.encode("utf-8")
        if new != old:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(new)
            except OSError as e:
                _file_restore(path, old)
                raise StudioError("precondition_failed", f"정책을 쓰지 못했습니다: {e}") from e
        return project_policy.read(_config().studio_dir, id)


# The memory of an employee (H-4; `studio/memory.py`): MEMORY.md as the lines it is made of. An item's id is the
# hash of its line, so a line the server moved is found and one that was changed meanwhile is `item_unknown`
# (no `base_sha256` needed). The whole file is written by `PUT …/files/MEMORY.md`. `commit` is the folder's head.
class MemoryItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


def _memory_refused(e: memory.MemoryRefused) -> StudioError:
    return StudioError("precondition_failed", str(e), failed=[e.failed])


@router.get("/employees/{id}/memory")
def get_employee_memory(id: str, store: StudioStore = Depends(get_store)) -> dict:
    employee = _need(store.get_employee(id), "직원", id)
    studio_dir = _config().studio_dir
    return {**memory.view(studio_dir, employee["id"]), "commit": _folder_head(employees.folder(studio_dir, id))}


@router.patch("/employees/{id}/memory/{item_id}")
def patch_employee_memory(id: str, item_id: str, body: MemoryItemPatch, store: StudioStore = Depends(get_store)) -> dict:
    if not memory.one_line(body.text):
        raise StudioError("missing_fields", "text 는 비울 수 없습니다", fields=["text"])
    employee = _need(store.get_employee(id), "직원", id)
    try:
        return memory.edit(store, _config().studio_dir, employee, item_id, body.text)
    except memory.MemoryRefused as e:
        raise _memory_refused(e) from e


@router.delete("/employees/{id}/memory/{item_id}")
def delete_employee_memory(id: str, item_id: str, store: StudioStore = Depends(get_store)) -> dict:
    employee = _need(store.get_employee(id), "직원", id)
    try:
        return memory.remove(store, _config().studio_dir, employee, item_id)
    except memory.MemoryRefused as e:
        raise _memory_refused(e) from e


# The newest first, 200 at most; `files` are all the files that commit changed. [] while the folder has no
# history. A git that cannot be read is `engine_error`, as the refusals of git are elsewhere (§4.3 T7).
@router.get("/employees/{id}/history")
def employee_history(id: str, file: EmployeeFileName | None = None,
                     store: StudioStore = Depends(get_store)) -> list[dict]:
    """`file` keeps the commits that changed that file."""
    _need(store.get_employee(id), "직원", id)
    try:
        return employee_git.history(employees.folder(_config().studio_dir, id), file)
    except employee_git.GitError as e:
        raise StudioError("engine_error", f"직원 폴더의 이력을 읽지 못했습니다: {e}") from e


# The four files go back to what that commit has, in a new commit: nothing of the history is taken away, and a
# file that commit does not have is left as it is. The `employee.yaml` it would bring back is checked first —
# nothing is written when it fails (§9.6). What is uncommitted in the folder is committed before (`keep:`), so
# a change made by hand is not lost to the revert.
@router.post("/employees/{id}/revert")
def revert_employee(id: str, body: EmployeeRevert, store: StudioStore = Depends(get_store)) -> dict:
    """Put the four files back to what that commit has, as a new commit."""
    with employee_git.lock:
        employee = _need(store.get_employee(id), "직원", id)
        folder = employees.folder(_config().studio_dir, id)
        try:
            sha = employee_git.resolve(folder, body.commit)
            then = {name: employee_git.show(folder, sha, name) for name in employees.FILES} if sha else {}
        except employee_git.GitError as e:
            raise StudioError("engine_error", f"직원 폴더의 이력을 읽지 못했습니다: {e}") from e
        if sha is None:
            raise StudioError("precondition_failed", "이 직원 폴더의 이력에 없는 커밋입니다. sha(7–40자)로 적습니다.",
                              failed=["commit_unknown"])
        then = {name: data for name, data in then.items() if data is not None}
        if "employee.yaml" in then:
            try:
                text: object = then["employee.yaml"].decode("utf-8")
            except UnicodeDecodeError:
                text = None  # not a text: check 1 says so
            _yaml_checked(store, employee, text)
        changed = {}
        for name, data in then.items():
            old = _file_bytes(folder / name)
            if old != data:
                changed[name] = (old, data)
        if not changed:  # already as that commit has it: no commit and no event
            return {"commit": _folder_head(folder)}
        _folder_commit(folder, "keep: before revert")  # nothing to commit, unless the folder was changed by hand
        written = []
        try:
            for name, (old, data) in changed.items():
                written.append(name)
                (folder / name).write_bytes(data)
        except OSError as e:
            for name in written:
                _file_restore(folder / name, changed[name][0])
            raise StudioError("precondition_failed", f"파일을 쓰지 못했습니다: {e}") from e
        commit = _folder_commit(folder, f"revert: {sha[:7]}")
        _file_changed(store, employee)
        return {"commit": commit}


# --- an employee's life: rename · placement · leave · retirement (H-3 ②, §6.4 · §4.9 · §4.10) ----------------
# Comments, not docstrings, on the four routes: the saved contract (openapi.json) would carry a docstring as
# the path's description. One transaction holds the read, the checks, the write and the events of each; a value
# that is already so writes nothing and says nothing. After a change that is committed the running campaigns
# are looked at (`_look_at_campaigns`): a task that waited for this employee may start now.
class EmployeePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class EmployeeAssign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None  # required; null = out of every project


class EmployeeLeave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool


def _look_at_campaigns() -> None:
    """The look of §4.10 over every running campaign, after a change of an employee (assigned · leave · retired · renamed:
    a rename has nothing to look at, but the same helper is called for it).
    What it fails at is logged: the change is committed and the answer does not depend on it."""
    look = getattr(_effects, "campaign", None)
    if look is None:
        return
    try:
        look(None)
    except Exception:  # noqa: BLE001
        log.exception("studio: 직원 변경 뒤 캠페인을 훑지 못했다")


def _employee_changed(store: StudioStore, tx: Tx, row: dict, change: str) -> None:
    store.emit(tx, "employee.updated", {"employee": row, "change": change}, project_id=row["project_id"])


def _engaged(store: StudioStore, row: dict, tx: Tx, what: str) -> None:
    """Refuse when the employee holds a task that is going on or is in the line (§4.9): `working` · `blocked` ·
    `review`, or a `backlog` task that has been queued (`queued_at`)."""
    busy = [t for t in store.list_tasks(assignee_id=row["id"], tx=tx)
            if t["status"] in ("working", "blocked", "review") or (t["status"] == "backlog" and t["queued_at"])]
    if busy:
        titles = " · ".join(f"'{t['title']}'" for t in busy[:3])
        raise StudioError("precondition_failed", f"{row['name']} 은(는) 진행 중인 작업({titles})이 있어 {what} 수 없습니다. "
                          "먼저 작업을 멈추거나 담당을 바꿉니다", failed=["active_tasks"])


def _not_retired(row: dict) -> None:
    if row["retired_at"] is not None:
        raise StudioError("precondition_failed", f"{row['name']} 은(는) 퇴사한 직원입니다", failed=["retired"])


def _employee_answer(store: StudioStore, row: dict, changed: bool) -> dict:
    if changed:
        _look_at_campaigns()
    return _with_dm_room(store, [row])[0]


# The name is the one of the others too — a retired one's among them (`name` is unique for good). The 1:1 room
# that carries the old name carries the new one. A retired employee can be renamed: that is how a name is freed.
@router.patch("/employees/{id}")
def patch_employee(id: str, body: EmployeePatch, store: StudioStore = Depends(get_store)) -> dict:
    name = body.name.strip()
    if not name:
        raise StudioError("missing_fields", "name 은 비울 수 없습니다", fields=["name"])
    with store.transaction() as tx:
        row = _need(store.get_employee(id, tx=tx), "직원", id)
        if row["name"] == name:
            changed = False
        else:
            taken = StudioError("conflict", f"이름 '{name}' 은(는) 다른 직원이 쓰고 있습니다 (퇴사한 직원 포함)",
                                field="name")
            if any(e["name"] == name and e["id"] != id for e in store.list_employees(include_retired=True, tx=tx)):
                raise taken
            try:
                new = store.update_employee(id, tx=tx, name=name)
            except sqlite3.IntegrityError as e:
                raise taken from e
            for room in store.list_rooms(kind="dm", employee_id=id, include_archived=True, tx=tx)[:1]:
                if room["title"] == row["name"]:
                    room = store.update_room(room["id"], tx=tx, title=name)
                    members = store.list_room_members(room_id=room["id"], tx=tx)
                    store.emit(tx, "room.updated", {"room": room, "members": members}, room_id=room["id"])
            _employee_changed(store, tx, new, "renamed")
            row, changed = new, True
    return _employee_answer(store, row, changed)


# `project_id` null takes the employee out of every project. The HR one is of the company: no project but null.
# A project that is not there or is archived is refused, and so is an employee with a task going on.
@router.post("/employees/{id}/assign")
def assign_employee(id: str, body: EmployeeAssign, store: StudioStore = Depends(get_store)) -> dict:
    with store.transaction() as tx:
        row = _need(store.get_employee(id, tx=tx), "직원", id)
        _not_retired(row)
        target = body.project_id
        if target is not None and row["department_id"] == employees.HR_DEPARTMENT:
            raise StudioError("precondition_failed", f"{row['name']} 은(는) 회사 전체의 직원이라 프로젝트에 배치하지 않습니다",
                              failed=["company_wide"])
        if target == row["project_id"]:
            changed = False
        else:
            if target is not None:
                project = store.get_project(target, tx=tx)
                if project is None:
                    raise StudioError("precondition_failed", f"프로젝트를 찾을 수 없습니다: {target}",
                                      failed=["project_unknown"])
                if project["archived_at"] is not None:
                    raise StudioError("precondition_failed", f"보관된 프로젝트입니다: {project['name']}",
                                      failed=["project_archived"])
                problem = gate.placement_problem(store.get_department(row["department_id"], tx=tx), project)
                if problem:
                    raise StudioError("precondition_failed", problem, failed=[gate.NO_HARNESS])
            _engaged(store, row, tx, "배치를 바꿀")
            row = store.update_employee(id, tx=tx, project_id=target)
            _employee_changed(store, tx, row, "assigned")
            changed = True
    return _employee_answer(store, row, changed)


# `on` true: on_leave (refused with a task going on); false: the status of §4.9 worked out again from the tasks
# held. A retired employee takes no leave. The status change is told before the update.
@router.post("/employees/{id}/leave")
def leave_employee(id: str, body: EmployeeLeave, store: StudioStore = Depends(get_store)) -> dict:
    with store.transaction() as tx:
        row = _need(store.get_employee(id, tx=tx), "직원", id)
        _not_retired(row)
        if (row["status"] == "on_leave") == body.on:
            changed = False
        else:
            if body.on:
                _engaged(store, row, tx, "휴직할")
                status = "on_leave"
            else:
                held = [t["status"] for t in store.list_tasks(assignee_id=id, tx=tx)]
                status = employees.derive_status("idle", held)
            prev = row["status"]
            row = store.update_employee(id, tx=tx, status=status)
            if status != prev:
                store.emit(tx, "employee.status", {"employee_id": id, "status": status, "prev": prev},
                           project_id=row["project_id"])
            _employee_changed(store, tx, row, "leave")
            changed = True
    return _employee_answer(store, row, changed)


# Retired: `retired_at` only. The row, the folder, the 1:1 room and what was said in it stay (it can be read);
# the employee leaves the default list. Already retired: the same row, no event. Refused with a task going on.
@router.post("/employees/{id}/retire")
def retire_employee(id: str, store: StudioStore = Depends(get_store)) -> dict:
    with store.transaction() as tx:
        row = _need(store.get_employee(id, tx=tx), "직원", id)
        if row["retired_at"] is not None:
            changed = False
        else:
            _engaged(store, row, tx, "퇴사할")
            row = store.update_employee(id, tx=tx, retired_at=now())
            _employee_changed(store, tx, row, "retired")
            changed = True
    return _employee_answer(store, row, changed)


# --- campaigns · tasks (§6.4) -----------------------------------------------------------------------------
TaskStatus = Literal["proposed", "backlog", "working", "blocked", "review", "done", "rejected", "cancelled"]
TaskType = Literal["feature", "fix", "chore", "doc", "data"]
CampaignStatus = Literal["draft", "running", "paused", "done", "cancelled"]
CampaignMode = Literal["nonstop", "milestone", "each"]
CampaignAction = Literal["start", "pause", "resume", "cancel"]
NEW_TASK_REQUIRED = ("project_id", "title", "reason", "spec", "done_criteria", "type")
TASK_NOT_NULL = TEXT_FIELDS + ("type", "is_milestone", "sort_order")  # PATCH: null clears relay · campaign_id only
NEW_CAMPAIGN_REQUIRED = ("project_id", "title", "goal")
# action -> (the status it is taken in, the status it leads to). `transition_campaign` takes a draft and a paused
# campaign to `running` alike, so the path tells `start` from `resume`; None = wherever the table of §4.10 has it
CAMPAIGN_ACTIONS = {"start": ("draft", "running"), "pause": ("running", "paused"), "resume": ("paused", "running"),
                    "cancel": (None, "cancelled")}


class NewCampaign(BaseModel):
    """A draft. The first three are required: an empty or missing one is 422 missing_fields naming all of them."""
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    title: str | None = None
    goal: str | None = None
    mode: CampaignMode = "milestone"


class CampaignPatch(BaseModel):
    """Only the keys that are sent change; none of them takes null. A new mode holds from the next start (§4.10)."""
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    goal: str | None = None
    mode: CampaignMode | None = None


class NewTask(BaseModel):
    """C2. The first six are required: an empty or missing one is 422 `missing_fields` naming all of them,
    so they carry defaults here instead of failing the request validation one by one."""
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    title: str | None = None
    reason: str | None = None
    spec: str | None = None
    done_criteria: str | None = None
    type: TaskType | None = None
    campaign_id: str | None = None
    parent_id: str | None = None
    assignee_id: str | None = None
    is_milestone: bool = False
    relay: str | None = None
    depends_on: list[str] = []


class TaskPatch(BaseModel):
    """Only the keys that are sent change. null clears `relay` and `campaign_id`; the others take no null."""
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    reason: str | None = None
    spec: str | None = None
    done_criteria: str | None = None
    type: TaskType | None = None
    relay: str | None = None
    campaign_id: str | None = None
    is_milestone: bool | None = None
    sort_order: int | None = Field(default=None, ge=-2**31, le=2**31 - 1)


class Assign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee_id: str | None  # the key is required; null clears the assignee


class Deps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    depends_on: list[str]


class Cancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # the override of this start (§9.5 의 3): {stage: {provider, model, effort}}. {} clears the one the task
    # keeps; null or no key leaves it as it is
    stage_models: dict[str, Any] | None = None


def get_machine(store: StudioStore = Depends(get_store)) -> TaskMachine:
    """The task machine of a request: the one store, and the effects install() was given (none before S-8)."""
    return TaskMachine(store, _effects)


def _empty(value: Any) -> bool:
    return not (isinstance(value, str) and value.strip())


def _clean(value: str | None) -> str | None:
    """An optional text: blank is NULL."""
    return (value or "").strip() or None


@router.get("/campaigns")
def list_campaigns(project_id: str | None = None, status: CampaignStatus | None = None,
                   store: StudioStore = Depends(get_store)) -> dict:
    counts = store.count_tasks_by_campaign()
    rows = store.list_campaigns(project_id=project_id, status=status)
    return {"items": [{**row, "progress": stats.progress(counts.get(row["id"], {}))} for row in rows]}


@router.post("/campaigns", status_code=201)
def create_campaign(body: NewCampaign, machine: TaskMachine = Depends(get_machine)) -> dict:
    """A campaign opens as a draft and starts nothing until its start (§4.10). 201 with the row."""
    empty = [name for name in NEW_CAMPAIGN_REQUIRED if _empty(getattr(body, name))]
    if empty:
        raise StudioError("missing_fields", f"비어 있습니다: {', '.join(empty)}", fields=empty)
    return machine.create_campaign(OWNER, body.project_id, title=body.title, goal=body.goal, mode=body.mode)


@router.patch("/campaigns/{id}")
def patch_campaign(id: str, body: CampaignPatch, machine: TaskMachine = Depends(get_machine)) -> dict:
    """Only the keys that are sent change, until the campaign has ended. A new mode holds from the next start."""
    fields = body.model_dump(exclude_unset=True)
    empty = [key for key, value in fields.items() if value is None or (key != "mode" and _empty(value))]
    if empty:
        raise StudioError("missing_fields", f"비울 수 없습니다: {', '.join(empty)}", fields=empty)
    row = machine.edit_campaign(id, OWNER, fields)  # None: nothing differs — the row as it is, no write, no event
    return row if row is not None else _need(machine.store.get_campaign(id), "캠페인", id)


@router.post("/campaigns/{id}/{action}")
def campaign_action(id: str, action: CampaignAction, machine: TaskMachine = Depends(get_machine)) -> dict:
    """The owner's start (a draft), pause (a running one), resume (a paused one) or cancel (before its end): §4.10."""
    campaign = _need(machine.store.get_campaign(id), "캠페인", id)
    frm, to = CAMPAIGN_ACTIONS[action]
    if frm is not None and campaign["status"] != frm:
        raise TransitionError("illegal_transition",
                              f"{action} 은 {frm} 캠페인에만 한다. 이 캠페인은 {campaign['status']} 다",
                              from_status=campaign["status"], to_status=to)
    return machine.transition_campaign(id, to, OWNER)


@router.get("/tasks")
def list_tasks(project_id: str | None = None, campaign_id: str | None = None, status: TaskStatus | None = None,
               assignee_id: str | None = None, parent_id: str | None = None,
               store: StudioStore = Depends(get_store)) -> dict:
    """By `sort_order`, then in the order the tasks were made. `status` is one value."""
    return {"items": store.list_tasks(project_id=project_id, campaign_id=campaign_id, status=status,
                                      assignee_id=assignee_id, parent_id=parent_id)}


@router.post("/tasks", status_code=201)
def create_task(body: NewTask, machine: TaskMachine = Depends(get_machine)) -> dict:
    """C2 (§4.3): the owner registers a task straight into backlog; its room opens with it. 201 with the row."""
    empty = [name for name in NEW_TASK_REQUIRED if _empty(getattr(body, name))]
    if empty:
        raise StudioError("missing_fields", f"비어 있습니다: {', '.join(empty)}", fields=empty)
    return machine.create_task(
        OWNER, "backlog", body.project_id, title=body.title, reason=body.reason, spec=body.spec,
        done_criteria=body.done_criteria, type=body.type, campaign_id=_clean(body.campaign_id),
        parent_id=_clean(body.parent_id), assignee_id=_clean(body.assignee_id), is_milestone=body.is_milestone,
        relay=_clean(body.relay), depends_on=body.depends_on).task


@router.get("/tasks/{id}")
def get_task(id: str, machine: TaskMachine = Depends(get_machine)) -> dict:
    """The row and what a task screen shows next to it. `queue_position` counts from 1 in the server's whole
    line (provisional); `run` is `{id, status, changes_status}` of the task's run, null when it has none."""
    store = machine.store
    row = _need(store.get_task(id), "작업", id)
    rooms = store.list_rooms(kind="task", task_id=id, include_archived=True)
    pending = store.list_inbox(task_id=id, status="pending")  # at most one (inbox_one_pending_per_task)
    line = [task["id"] for task in store.list_queued_tasks()]
    return {
        **row,
        "room_id": rooms[0]["id"] if rooms else None,
        "depends_on": [dep["depends_on_id"] for dep in store.list_task_deps(task_id=id)],
        "dependents": [dep["task_id"] for dep in store.list_task_deps(depends_on_id=id)],
        "deps_met": machine.deps_ready(row),
        "pending_inbox_id": pending[0]["id"] if pending else None,
        "queue_position": line.index(id) + 1 if id in line else None,
        "run": _run_view(row["run_id"]) if _run_view is not None and row["run_id"] else None,
    }


@router.patch("/tasks/{id}")
def patch_task(id: str, body: TaskPatch, machine: TaskMachine = Depends(get_machine)) -> dict:
    """What may change in which state is the machine's rule (`TaskMachine.edit`). Nothing changes: the row as
    it is, no write and no event."""
    fields = body.model_dump(exclude_unset=True)
    empty = [key for key, value in fields.items()
             if key in TASK_NOT_NULL and (value is None or (key in TEXT_FIELDS and _empty(value)))]
    if empty:
        raise StudioError("missing_fields", f"비울 수 없습니다: {', '.join(empty)}", fields=empty)
    for key in ("relay", "campaign_id"):
        if key in fields:
            fields[key] = _clean(fields[key])
    outcome = machine.edit(id, OWNER, fields)
    return outcome.task if outcome is not None else _need(machine.store.get_task(id), "작업", id)


@router.post("/tasks/{id}/assign")
def assign_task(id: str, body: Assign, machine: TaskMachine = Depends(get_machine)) -> dict:
    """Set or clear (null) the assignee: only in backlog and out of the line (§3.10)."""
    return machine.assign(id, OWNER, _clean(body.assignee_id)).task


@router.put("/tasks/{id}/deps")
def put_task_deps(id: str, body: Deps, machine: TaskMachine = Depends(get_machine)) -> dict:
    """Replace the set of tasks this one waits for (§3.11). The same set: no write and no event."""
    outcome = machine.set_deps(id, OWNER, body.depends_on)
    row = outcome.task if outcome is not None else _need(machine.store.get_task(id), "작업", id)
    return {**row, "depends_on": [dep["depends_on_id"] for dep in machine.store.list_task_deps(task_id=id)]}


@router.post("/tasks/{id}/start")
def start_task(id: str, body: Start | None = None, machine: TaskMachine = Depends(get_machine)) -> dict:
    """The trigger of T3 (§4.3): the task joins the line and is still in backlog here — the queue does T3 when
    it has a slot. `stage_models` is kept as the task's override; one that cannot run is `precondition_failed`."""
    return machine.request_start(id, OWNER, stage_models=body.stage_models if body else None, vet=True).task


@router.post("/tasks/{id}/stop")
def stop_task(id: str, machine: TaskMachine = Depends(get_machine)) -> dict:
    """Take the task out of the line, or end its run: a working task goes to blocked with a `question` (T4) —
    at once when its run has stopped, else when it stops (§6.4). Nothing to stop is `precondition_failed`."""
    _need(machine.store.get_task(id), "작업", id)
    if _queue is None:  # nothing gives slots here: only the line can be left
        return machine.unqueue(id, OWNER).task
    return _queue.stop(id, get_inbox(machine))  # the request's inbox: get_inbox is defined further down


@router.post("/tasks/{id}/cancel")
def cancel_task(id: str, body: Cancel | None = None, machine: TaskMachine = Depends(get_machine)) -> dict:
    """The one transition the owner calls by path (§4.2): T10 from backlog, T11 from blocked. Any other state
    is 409 `illegal_transition` with `from` · `to` (§4.4)."""
    try:
        return machine.transition(id, "cancelled", OWNER, reason=(body.reason if body else None) or "").task
    except ValueError as e:
        # `engine_error` (§6.3) is a refusal: the engine said no before anything was written. A failure after
        # the commit (Effects.after) leaves the task cancelled, so it goes out as it is — a server error, not a
        # refusal the app would read as "still blocked".
        row = machine.store.get_task(id)
        if row is not None and row["status"] == "cancelled":
            raise
        raise StudioError("engine_error", str(e) or "엔진이 요청을 거부했습니다") from e


@router.get("/tasks/{id}/events")
def list_task_events(id: str, after_id: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=MAX_PAGE),
                     store: StudioStore = Depends(get_store)) -> dict:
    """Ascending by id: the first `limit` after `after_id`. `has_more`: there are more after the last one."""
    _need(store.get_task(id), "작업", id)
    rows = store.list_task_events(id, after_id=after_id, limit=limit + 1)
    return {"items": rows[:limit], "has_more": len(rows) > limit}


# --- inbox (§5, §6.4 '결재') --------------------------------------------------------------------------------
InboxStatus = Literal["pending", "decided", "cancelled"]
InboxKind = Literal["task_proposal", "hire", "design_approval", "structure_choice", "question", "pre_approval",
                    "budget", "risky_command", "final_review", "hire_recommendation"]


class Decide(BaseModel):
    """§5.1. `decision` and `via` are plain text here: a value that does not fit the item is 422
    `invalid_decision` with `allowed[]`, not the 422 of the request validation.
    `via` = notification takes only a plain approve — no comment, no data — of a kind whose push says
    `data.action` = approve (§5.2 · §5.9)."""
    model_config = ConfigDict(extra="forbid")

    decision: str
    comment: str | None = None
    data: dict[str, Any] | None = None
    via: str | None = None  # app (default) · notification · dashboard


def get_inbox(machine: TaskMachine = Depends(get_machine)) -> Inbox:
    """The inbox of a request: the request's machine, and the hooks the server gave (install() · use_hooks() —
    studio/runs.py for the runs that are no task's; the turns come with S-7 · S-8)."""
    cfg = _config()
    return Inbox(machine.store, machine, _hooks, studio_dir=cfg.studio_dir, checks=employees.context(cfg),
                 max_exchanges=cfg.studio_max_exchanges, hire_cooldown_days=cfg.studio_hire_cooldown_days)


# --- the engine's side of the inbox (§5.8) ----------------------------------------------------------------
# Called from outside a /studio request: server.py's old /runs endpoints and the thread that advanced a run
# (studio/runs.py). They use the store only when it is open — it is, from the server's start on — and the two
# the old endpoints call never raise: the dashboard and a run must not depend on the studio layer.
def use_hooks(hooks: Hooks | None) -> None:
    """Set what an inbox decision asks of the engine, after install(): server.py builds it from what it
    defines further down (`_advance_bg`)."""
    global _hooks
    _hooks = hooks


def staffing_for(store: StudioStore) -> staffing.Staffing:
    """The hiring triggers (studio/staffing.py, §5.3) on that store, asking HR through the hooks the server
    gave (`Hooks.staffing_ask`; nothing wired = nothing asked). The limits are read now, from the config."""
    cfg = _config()

    def ask(signal: dict) -> bool | None:
        return _hooks.staffing_ask(signal) if _hooks is not None else False

    return staffing.Staffing(store, ask=ask, backlog_limit=cfg.studio_hire_backlog_limit,
                             cooldown_days=cfg.studio_hire_cooldown_days)


def staffing_on_task(task: dict, item: dict | None = None) -> None:
    """(가)·(나) of §5.3 for a task just filed: `WorkEffects.staffing_look`. Never raises."""
    try:
        store = get_store()
        staffing_for(store).on_task(task, item)
    except Exception:  # noqa: BLE001 - the filing of the task is not this look's to undo
        log.exception("studio: 작업 %s 의 채용 계기를 보지 못했습니다", task.get("id"))


def staffing_sweep(store: StudioStore) -> None:
    """(라) of §5.3: the daily report's sweep of every open task (`stats.Reporter(sweep=)`)."""
    staffing_for(store).sweep()


def use_effects(effects: Effects | None, *, models: Callable[[], list[dict]] | None = None) -> None:
    """Set what a task change does outside the DB, after install(): server.py builds it on its engine
    (studio/work.py `WorkEffects`). `models` is the catalog `GET /studio/models` reads — the engine's registry,
    which knows the models that are benched; without it the config's own registry is asked."""
    global _effects, _models
    _effects, _models = effects, models


def use_queue(queue: Any) -> None:
    """Set the line `stop` asks (studio/queue.py `TaskQueue`), after install(): server.py builds it on its
    engine. `start` needs none — the effects reach the queue themselves (`WorkEffects.kick`)."""
    global _queue
    _queue = queue


def use_cutoff(cutoff: Any) -> None:
    """Set what follows the answer of a T4 (studio/cutoff.py `Cutoff`), after install(): server.py builds it on
    its engine. `answered` below is how the tools — and the guard — reach it."""
    global _cutoff
    _cutoff = cutoff


def use_engine(engine: Callable[[], Any] | None, *, tokens: Callable[[str], dict] | None = None) -> None:
    """Set the engine the final review reads (studio/review.py), after install(): the `run` of a task, the
    `package` of a final review and the alias of a run's patch. A function, not the object — the server's
    engine is a module variable (a test puts another one there), so it is asked for at every read. `tokens`
    is the server's own `GET /runs/{run_id}/tokens`, which the alias here answers with."""
    global _engine, _tokens, _run_view, _run_cost
    _engine, _tokens = engine, tokens
    _run_view = (lambda run_id: review.run_view(engine(), run_id)) if engine is not None else None
    _run_cost = (lambda run_id: review.run_cost(engine(), run_id)) if engine is not None else None


def use_jobs(*, reload: Callable[[], Any], local_file: Path | str, run: Callable[..., int] | None = None,
             is_open: Callable[[int], bool] | None = None) -> None:
    """Set what making a project needs of the server (studio/jobs.py `Jobs`), after install(): `local_file` —
    the settings file the `register` step writes the repository in — and `reload`, which makes the server read
    its settings again and gives its RepoRegistry. `run` and `is_open` stand in for the process runner and the
    port check (tests); left out, they are the real ones. The `Jobs` made from an earlier call is forgotten."""
    global _job_parts, _jobs
    parts: dict[str, Any] = {"reload": reload, "local_file": Path(local_file)}
    if run is not None:
        parts["run"] = run
    if is_open is not None:
        parts["is_open"] = is_open
    with _lock:
        _job_parts, _jobs = parts, None


def _job_runner() -> Any:
    """The `Jobs` of this server, made on first use — and again when the studio folder is another one (a test
    moves `runs_dir`). 503 `unavailable` while nobody called `use_jobs`."""
    global _jobs
    cfg = _config()
    with _lock:
        if _job_parts is None:
            raise StudioError("unavailable", "프로젝트 생성이 서버에 연결되지 않았습니다 (use_jobs 가 불리지 않았습니다)")
        if _jobs is None or _jobs.studio_dir != cfg.studio_dir:
            _jobs = create_jobs.Jobs(cfg.studio_dir, harness_root=lambda: _config().studio_harness_root,
                                     live=_say, on_registered=_begin_policy_talk, **_job_parts)
        return _jobs


def recover_jobs() -> list[dict]:
    """What server.py does once when it starts: the job files a server that stopped left `pending` or
    `running` are made `failed`, so they can be retried or cleaned (`Jobs.recover`); the answer is those jobs.
    The store is opened only when there is such a file. Never raises: a start that could not look is logged."""
    try:
        return _job_runner().recover(get_store)
    except StudioError:  # nobody called use_jobs: no job was ever made here
        return []
    except Exception:  # noqa: BLE001 - the server starts all the same
        log.exception("studio: 중단된 생성 작업을 확인하지 못했다")
        return []


def held_runs() -> set[str]:
    """What server.py gives `RelayEngine.held_runs`: the runs of the tasks under review, whose workspaces
    `cleanup_workspaces` keeps past the retention period (§4.3 T9). From the store when it is open, else
    read-only from the DB file — a store that could not start must not let those snapshots go. What this
    raises the engine reads as "cannot say": nothing expires in that round."""
    with _lock:
        store = _store
    if store is not None:
        return review.held_runs(store)
    return review.held_runs_ro(_config().studio_db)


def run_inbox() -> Inbox | None:
    """An inbox outside a request, with the effects and hooks the server gave. None while the store is not
    open (it could not start, or nothing has opened it yet): then nothing is filed or closed."""
    with _lock:
        store = _store
    if store is None or _cfg is None:
        return None
    return Inbox(store, TaskMachine(store, _effects), _hooks, studio_dir=_cfg.studio_dir,
                 checks=employees.context(_cfg), max_exchanges=_cfg.studio_max_exchanges,
                 hire_cooldown_days=_cfg.studio_hire_cooldown_days)


def _learn(item: dict) -> None:
    """The lessons of a decided final review into the assignee's MEMORY.md (H-4), on the thread that calls. What
    fails is logged: the decision was made before this."""
    with _lock:
        store = _store
    if store is None or _cfg is None:
        return
    try:
        ask = memory.writer(_cfg.studio_memory_model)
        if ask is not None:
            memory.learn(store, _cfg.studio_dir, item, ask)
    except Exception:  # noqa: BLE001
        log.exception("studio: 메모리 추출이 실패했다 (결재 %s)", item.get("id"))


def learn_later(item: dict) -> threading.Thread:
    """`_learn` on a thread of its own: the call to the model takes up to a minute and a decision must not wait
    for it. A turn that follows a rework at once may not carry the new lines; the next one does (§9.1)."""
    thread = threading.Thread(target=_learn, args=(item,), name="studio-memory", daemon=True)
    thread.start()
    return thread


def is_task_run(run_id: str) -> bool:
    """§5.8 다: is this the run of a studio task (`tasks.run_id`)? False when the studio layer cannot say."""
    try:
        inbox = run_inbox()
        return inbox is not None and bool(inbox.store.list_task_runs(run_id=run_id))
    except Exception:  # noqa: BLE001
        log.exception("studio: 런 %s 이 작업 런인지 확인하지 못했다", run_id)
        return False


def close_run_items(run_id: str) -> None:
    """§5.8 나: the run was approved or cancelled outside the inbox — its pending items without a task are
    closed as cancelled."""
    try:
        inbox = run_inbox()
        if inbox is not None:
            inbox.close_run(run_id)
    except Exception:  # noqa: BLE001
        log.exception("studio: 런 %s 의 결재를 닫지 못했다", run_id)


def _sweep_runs(store: StudioStore) -> None:
    """§5.8 나: before the inbox is listed, the items of runs that were released outside it are closed
    (`Inbox.sweep_runs`). Never fails the list."""
    try:
        Inbox(store, hooks=_hooks).sweep_runs()
    except Exception:  # noqa: BLE001
        log.exception("studio: 밖에서 풀린 런의 결재를 정리하지 못했다")


@router.get("/inbox")
def list_inbox(status: InboxStatus = "pending", kind: InboxKind | None = None, project_id: str | None = None,
               task_id: str | None = None, before: str | None = None,
               limit: int = Query(50, ge=1, le=MAX_PAGE), store: StudioStore = Depends(get_store)) -> dict:
    """`pending`: the longest waiting first (`created_at`); `before` is not used. Any other status: the newest
    closed first, and `before` is the `closed_at` cursor. `pending_count` counts every pending item, whatever
    the filters (provisional)."""
    _sweep_runs(store)  # §5.8 나: an item whose run no longer waits for approval is closed before it is listed
    closed = status != "pending"
    filters = {"status": status, "kind": kind, "project_id": project_id, "task_id": task_id}
    rows = store.list_inbox(**filters, closed_first=closed, before=before if closed else None, limit=limit + 1)
    items, has_more = rows[:limit], len(rows) > limit
    if closed and has_more and rows[limit]["closed_at"] == items[-1]["closed_at"]:
        # `closed_at` has whole seconds and the cursor is that value alone (§6.4): a page that ended inside a
        # second would lose the rest of it to `before`. So everything closed in the page's last second comes
        # with it — `items` is then longer than `limit` (provisional, S-5).
        edge = items[-1]["closed_at"]
        items = ([row for row in items if row["closed_at"] != edge]
                 + store.list_inbox(**filters, closed_first=True, closed_at=edge))
        has_more = bool(store.list_inbox(**filters, closed_first=True, before=edge, limit=1))
    return {"items": items, "has_more": has_more, "pending_count": store.count_inbox(status="pending")}


@router.get("/inbox/{id}")
def get_inbox_item(id: str, inbox: Inbox = Depends(get_inbox)) -> dict:
    """The row + `task` (null for `hire`) + `allowed_decisions` ([] once it is closed). A final review also
    has `package` (§5.4), computed now from the run as it is (studio/review.py)."""
    view = inbox.view(id)
    if "package" in view:  # an Inbox knows neither the engine nor the queue: the request fills the key
        view["package"] = review.package(
            inbox.store, view, view["task"], _engine() if _engine is not None else None,
            holds=getattr(_queue, "holds", None), scopes=getattr(_config(), "studio_scopes", None))
    return view


@router.post("/inbox/{id}/decide")
def decide_inbox(id: str, body: Decide, inbox: Inbox = Depends(get_inbox)) -> dict:
    """The one way a decision is made (§5.1): `{inbox, task}`. A second decision of the same item is 409
    `already_decided` with the item as it is now."""
    result = inbox.decide(id, body.decision, comment=body.comment, data=body.data, via=body.via)
    if usage.is_hold(result["inbox"]):  # V-4: the owner answered the hold of the line — look at it again
        kick = getattr(_queue, "kick", None)
        if kick is not None:
            kick()
    return result


# --- stats (S-13, studio/stats.py) ------------------------------------------------------------------------
@router.get("/stats")
def get_stats(project_id: str | None = None, campaign_id: str | None = None,
              frm: str | None = Query(None, alias="from"), to: str | None = None,
              store: StudioStore = Depends(get_store)) -> dict:
    """Counted now from tasks, task_events, inbox and the runs — nothing is stored. A bad `from` · `to`: 422."""
    try:
        since, until = stats.bounds(frm, to)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return stats.collect(store, project_id=project_id, campaign_id=campaign_id, since=since, until=until,
                         cost_of=_run_cost)


@router.get("/growth")
def get_growth(store: StudioStore = Depends(get_store)) -> dict:
    """Levels, experience, achievements and today's summary, counted now from task_events and inbox by the rule tables of §6.4 — nothing is stored."""
    return stats.growth(store)


# --- rooms ------------------------------------------------------------------------------------------------
RoomKind = Literal["dm", "task", "department", "system", "group"]


class ReadMark(BaseModel):
    message_id: int = Field(ge=0)


class RoomPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    muted: bool


class NewRoom(BaseModel):  # a group room (§3.6): who is invited, and its name (blank = the members' names)
    model_config = ConfigDict(extra="forbid")

    employee_ids: list[str]
    title: str = ""
    client_room_id: str | None = None  # the app's key of this making: a retry makes no second room (§6.4)


class RoomRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str


@router.get("/rooms")
def list_rooms(kind: RoomKind | None = None, archived: int = Query(0, ge=0, le=1),
               store: StudioStore = Depends(get_store)) -> dict:
    """`archived=1` adds the archived rooms to the list."""
    rooms = store.list_rooms(kind=kind, include_archived=bool(archived))
    unread, last = store.count_unread(), store.last_messages()
    return {"items": [{**r, "unread": unread.get(r["id"], 0), "last_message": last.get(r["id"])} for r in rooms]}


@router.get("/rooms/{id}")
def get_room(id: str, store: StudioStore = Depends(get_store)) -> dict:
    row = _need(store.get_room(id), "방", id)
    return {**row, "members": store.list_room_members(room_id=id), "unread": store.count_unread(id).get(id, 0)}


# N-2 (§5.9): what an employee writes in a muted room is not pushed — the room's notices, its task's progress and
# every inbox item still are (studio/push.py `classify`). `room.updated` only when the value changed. A comment,
# not a docstring: the saved contract (openapi.json) would carry it as the path's description.
@router.patch("/rooms/{id}")
def patch_room(id: str, body: RoomPatch, store: StudioStore = Depends(get_store)) -> dict:
    with store.transaction() as tx:
        room = _need(store.get_room(id, tx=tx), "방", id)
        if bool(room["muted"]) != body.muted:
            room = store.update_room(id, tx=tx, muted=body.muted)
            members = store.list_room_members(room_id=id, tx=tx)
            store.emit(tx, "room.updated", {"room": room, "members": members}, room_id=id,
                       task_id=room["task_id"], project_id=_room_project(store, room, tx))
    return room


@router.post("/rooms/{id}/read")
def mark_room_read(id: str, body: ReadMark, store: StudioStore = Depends(get_store)) -> dict:
    """`owner_read_id` only grows, and never past the room's last message (a later message stays unread)."""
    with store.transaction() as tx:
        room = _need(store.get_room(id, tx=tx), "방", id)
        last = store.last_messages(id, tx=tx).get(id)
        upto = min(body.message_id, last["id"] if last else 0)
        if upto > room["owner_read_id"]:
            room = store.update_room(id, tx=tx, owner_read_id=upto)
            members = store.list_room_members(room_id=id, tx=tx)
            store.emit(tx, "room.updated", {"room": room, "members": members}, room_id=id,
                       task_id=room["task_id"], project_id=_room_project(store, room, tx))
    return room


# --- group rooms (§3.6 · §6.4) ----------------------------------------------------------------------------
# A room the owner makes by inviting employees. Here is how it is kept, the REST of it and what its turns are
# asked with: the owner's message asks every member's turn, or the ones it mentions (`post_message`), and the
# turns themselves are studio/chat.py's (`ChatTurns.group`, one at a time for a room). Comments, not docstrings,
# on the four paths: the saved contract (openapi.json) would carry a docstring as the path's description.
GROUP_LIMIT, GROUP_CONTINUED = "group_exchange_limit", "group_continued"  # `meta.event` of the two notices (§7.6)
GROUP_KINDS = ("group", "department")  # the rooms that take their turns in the room's line (tools.py keeps the same pair)


def _group_limit() -> int:
    return _config().studio_group_max_members  # read at every making and at every invitation (§3.16)


def _group_max_exchanges() -> int | None:
    """§4.6: the turns the members of a group room may put in for each other after one message of the owner's.
    Read before every turn a mention asks for; None = the count is kept and never looked at."""
    return _config().studio_group_max_exchanges


def group_order(store: StudioStore, room_id: str, tx: Tx | None = None) -> list[str]:
    """The order the members of a group room take their turns in (§6.6): who is seated and has not retired, by
    `(joined_at, employee_id)` — what the rows of `members[]` give the app too."""
    rows = sorted(store.list_room_members(room_id=room_id, tx=tx), key=lambda m: (m["joined_at"], m["employee_id"]))
    staff = (store.get_employee(row["employee_id"], tx=tx) for row in rows)
    return [employee["id"] for employee in staff if employee is not None and employee["retired_at"] is None]


def group_halted(store: StudioStore, room_id: str, tx: Tx | None = None) -> bool:
    """§4.6: is the room held at its limit? Told by its messages, the newest first: the notice of the limit
    before any message of the owner's and any `group_continued` = held. No column: a restart changes nothing,
    and a room that is held is told so once. Not bound to one page: it goes back page by page until it is told,
    or to the start of the room."""
    before_id = None
    while True:
        page = store.list_messages(room_id, before_id=before_id, limit=MAX_PAGE, tx=tx)
        for message in reversed(page):
            event = (message["meta"] or {}).get("event") if message["author_kind"] == "system" else None
            if message["author_kind"] == "owner" or event == GROUP_CONTINUED:
                return False
            if event == GROUP_LIMIT:
                return True
        if len(page) < MAX_PAGE:
            return False
        before_id = page[0]["id"]


def group_owed(store: StudioStore, room_id: str) -> list[str]:
    """Whose turn a held room kept back (§4.6), in the room's order: a member an employee's message mentions
    after what that member last read (`fed_message_id`). Read from the DB: the line of a room is memory only.
    Not bound to one page: it reads on to the end of the room, page by page."""
    owed = []
    for employee_id in group_order(store, room_id):
        member = store.get_room_member(room_id, employee_id)
        after_id = member["fed_message_id"] or 0
        while True:
            page = store.list_messages(room_id, after_id=after_id, limit=MAX_PAGE)
            if any(m["author_kind"] == "employee" and m["author_id"] != employee_id
                   and employee_id in (m["mentions"] or []) for m in page):
                owed.append(employee_id)
                break
            if len(page) < MAX_PAGE:
                break
            after_id = page[-1]["id"]
    return owed


def _group_turns(room_id: str, employee_ids: list[str], mentioned: bool = False) -> list[str]:
    """§6.6: ask for those members' turns in that group room, in that order (`Hooks.group_turns`; `ChatTurns.group`
    behind it), and who is in the room's line for it. Like `_ask_turn` it never fails the call: what was
    written is stored by now, and a hook that raises is logged here."""
    if _hooks is None or not employee_ids:
        return []
    try:
        return list(_hooks.group_turns(room_id, list(employee_ids), mentioned))
    except Exception:  # noqa: BLE001
        log.exception("studio: 단체방 %s 의 턴을 넣지 못했다", room_id)
        return []


def _group_guest(store: StudioStore, employee_id: str, tx: Tx) -> dict:
    """One who can be invited: an employee that is there (404) and has not retired — the hr one and one placed
    in no project too. `on_leave` is not looked at: a leave stops a task from starting (§4.9), not a talk."""
    employee = _need(store.get_employee(employee_id, tx=tx), "직원", employee_id)
    if employee["retired_at"] is not None:
        raise StudioError("precondition_failed", f"{employee['name']} 은(는) 퇴사한 직원입니다. 단체방에는 재직 중인 "
                          "직원만 초대합니다")
    return employee


def _group_notice(store: StudioStore, tx: Tx, room: dict, event: str, body: str, **meta) -> dict:
    """What the owner did to a group room, said in it: a system message (`meta.event`), then `room.updated` and
    `message.created` — the room first, so that a room just made is known before its message. The room's row
    as it is after.

    It is the owner's own doing, so it is read: when nothing waited to be read `owner_read_id` goes up to the
    notice (`count_unread` counts system messages). When something did, the mark stays where it is."""
    waiting = store.count_unread(room["id"], tx=tx).get(room["id"], 0)
    message = store.create_message(room["id"], "system", tx=tx, body=body, meta={"event": event, **meta})
    if not waiting:
        room = store.update_room(room["id"], tx=tx, owner_read_id=message["id"])
    members = store.list_room_members(room_id=room["id"], tx=tx)
    store.emit(tx, "room.updated", {"room": room, "members": members}, room_id=room["id"])
    store.emit(tx, "message.created", {"message": message}, room_id=room["id"])
    return room


def _group_made_by(store: StudioStore, client_room_id: str, tx: Tx) -> dict | None:
    """The group room a `client_room_id` made already, one the owner left too. The key is kept in `meta` of the
    room's first message — the notice of its making —, as a message keeps its `client_msg_id`: one read per
    group room."""
    for room in store.list_rooms(kind="group", include_archived=True, tx=tx):
        first = store.list_messages(room["id"], after_id=0, limit=1, tx=tx)
        if first and first[0]["author_kind"] == "system" and \
                (first[0]["meta"] or {}).get("client_room_id") == client_room_id:
            return room
    return None


# What is looked at, in this order (§6.4): the key of a retry (200 with the room it made: nothing new, no
# event, the rest of the body is not looked at), how many (under two, then over the limit), that every one is
# there (404), that none retired (409). A blank key is no key.
@router.post("/rooms", status_code=201)
def create_room(body: NewRoom, store: StudioStore = Depends(get_store)) -> dict:
    ids, limit, key = list(dict.fromkeys(body.employee_ids)), _group_limit(), body.client_room_id or None
    with store.transaction() as tx:
        made = _group_made_by(store, key, tx) if key else None
        if made is not None:  # the same body as GET /rooms/{id}
            members = store.list_room_members(room_id=made["id"], tx=tx)
            unread = store.count_unread(made["id"], tx=tx).get(made["id"], 0)
            return JSONResponse({**made, "members": members, "unread": unread}, 200)
        if len(ids) < 2:
            raise StudioError("precondition_failed", "단체방은 직원 2명 이상을 초대해 만듭니다")
        if len(ids) > limit:
            raise StudioError("precondition_failed",
                              f"단체방의 직원은 {limit}명까지입니다 ({len(ids)}명을 골랐습니다)")
        for employee_id in ids:  # every one first: one who is not there is told before one who retired
            _need(store.get_employee(employee_id, tx=tx), "직원", employee_id)
        guests = [_group_guest(store, employee_id, tx) for employee_id in ids]
        names = ", ".join(guest["name"] for guest in guests)  # the default name, in the order they were named
        room = store.create_room("group", title=body.title.strip() or names, tx=tx)
        for employee_id in ids:
            store.create_room_member(room["id"], employee_id, tx=tx)
        kept = {"client_room_id": key} if key else {}
        room = _group_notice(store, tx, room, "group_invited", f"{names} 님을 초대했습니다", employee_ids=ids,
                             **kept)
        members = store.list_room_members(room_id=room["id"], tx=tx)
    return {**room, "members": members, "unread": 0}


@router.post("/rooms/{id}/rename")
def rename_room(id: str, body: RoomRename, store: StudioStore = Depends(get_store)) -> dict:
    title = body.title.strip()
    with store.transaction() as tx:
        room = _need(store.get_room(id, tx=tx), "방", id)
        if room["kind"] != "group":
            raise StudioError("precondition_failed", f"단체방이 아닙니다: {id} ({room['kind']}). 이름은 단체방에서만 "
                              "바꿉니다")
        if room["archived_at"] is not None:
            raise StudioError("precondition_failed", f"보관한 단체방입니다: {id}")
        if not title:
            raise StudioError("missing_fields", "title 은 비울 수 없습니다", fields=["title"])
        if title != room["title"]:  # the same name changes and says nothing
            old = room["title"]
            room = store.update_room(id, tx=tx, title=title)
            room = _group_notice(store, tx, room, "group_renamed", f"방 이름을 '{title}' (으)로 바꿨습니다",
                                 title=title, old_title=old)
    return room


@router.post("/rooms/{id}/archive")
def archive_room(id: str, store: StudioStore = Depends(get_store)) -> dict:
    with store.transaction() as tx:
        room = _need(store.get_room(id, tx=tx), "방", id)
        if room["kind"] != "group":
            raise StudioError("precondition_failed", f"단체방이 아닙니다: {id} ({room['kind']}). 보관하는 방은 "
                              "단체방뿐입니다")
        if room["archived_at"] is not None:
            return room  # already archived: the same row, no second event
        room = store.update_room(id, tx=tx, archived_at=now())  # the owner leaves: members and messages stay
        members = store.list_room_members(room_id=id, tx=tx)
        store.emit(tx, "room.updated", {"room": room, "members": members}, room_id=id)
    return room


# §4.6 · §6.4: the owner has a group room or a department room that is held at its limit go on. Looked at in this
# order: the room (404), that it is a group or department room, that the owner has not left it, that it is held — one that is not answers
# with its row and nothing happens (no message, no event, no turn), however often it is asked. A held room
# counts anew, says so (a system message) and the turns it kept back go into its line; the answer does not
# wait for them.
@router.post("/rooms/{id}/continue")
def continue_room(id: str, store: StudioStore = Depends(get_store)) -> dict:
    with store.transaction() as tx:
        room = _need(store.get_room(id, tx=tx), "방", id)
        if room["kind"] not in GROUP_KINDS:
            raise StudioError("precondition_failed", f"단체방도 파트방도 아닙니다: {id} ({room['kind']}). 이어 가는 "
                              "방은 단체방과 파트방뿐입니다")
        if room["archived_at"] is not None:
            raise StudioError("precondition_failed", f"보관한 방입니다: {id}")
        if not group_halted(store, id, tx):
            return room
        room = store.update_room(id, tx=tx, exchange_count=0)
        room = _group_notice(store, tx, room, GROUP_CONTINUED, "직원들의 대화를 이어 갑니다")
    _group_turns(id, group_owed(store, id), mentioned=True)
    return room


def _seat(store: StudioStore, id: str, employee_id: str, tx: Tx) -> tuple[dict, dict, dict | None]:
    """What both calls on the members of a room look at first, in this order: the room (404), that it is a task
    room or a group room (`precondition_failed`), the employee (404), and of a group room that the owner has not
    left it. (the room, the employee, the room's task — None in a group room)."""
    room = _need(store.get_room(id, tx=tx), "방", id)
    if room["kind"] not in ("task", "group"):
        raise StudioError("precondition_failed", f"작업방도 단체방도 아닙니다: {id} ({room['kind']}). 구성원은 "
                          "작업방과 단체방에서만 넣고 뺍니다")
    employee = _need(store.get_employee(employee_id, tx=tx), "직원", employee_id)
    if room["kind"] == "group":
        if room["archived_at"] is not None:
            raise StudioError("precondition_failed", f"보관한 단체방입니다: {id}")
        return room, employee, None
    task = _need(store.get_task(room["task_id"], tx=tx) if room["task_id"] else None, "작업", room["task_id"])
    return room, employee, task


def _seated(store: StudioStore, room: dict, tx: Tx, changed: bool) -> dict:
    """`{members}`: who is in the room now (one who left is not). `room.updated` only when that changed."""
    members = store.list_room_members(room_id=room["id"], tx=tx)
    if changed:
        store.emit(tx, "room.updated", {"room": room, "members": members}, room_id=room["id"],
                   task_id=room["task_id"], project_id=_room_project(store, room, tx))
    return {"members": members}


@router.post("/rooms/{id}/members/{employee_id}")
def add_room_member(id: str, employee_id: str, store: StudioStore = Depends(get_store)) -> dict:
    """Seats an employee of the task's project in the task room (S-9; §6.4), so that the room's mentions reach
    it. One who is seated already changes nothing; one who left comes back to the row it had — the conversation
    it kept there —, as `TaskMachine._join_room` does for the assignee.

    A group room (§3.6) takes any employee who has not retired, wherever it is placed, up to
    `studio_group_max_members`, and says so in the room (a system message)."""
    with store.transaction() as tx:
        room, employee, task = _seat(store, id, employee_id, tx)
        member = store.get_room_member(id, employee_id, tx=tx)
        seated = member is not None and member["left_at"] is None
        if task is None:  # a group room
            employee, limit = _group_guest(store, employee_id, tx), _group_limit()
            if not seated and len(store.list_room_members(room_id=id, tx=tx)) >= limit:
                raise StudioError("precondition_failed", f"단체방의 직원은 {limit}명까지입니다. 자리가 없습니다")
        elif employee["project_id"] is None or employee["project_id"] != task["project_id"]:
            raise StudioError("precondition_failed", f"{employee['name']} 은(는) 이 작업의 프로젝트에 배치된 직원이 "
                              "아닙니다. 작업방에는 그 프로젝트의 직원만 넣습니다")
        if member is None:
            store.create_room_member(id, employee_id, tx=tx)
        elif not seated:
            store.update_room_member(id, employee_id, left_at=None, tx=tx)
        if task is None and not seated:  # the notice tells the room and its members itself
            room = _group_notice(store, tx, room, "group_invited", f"{employee['name']} 님을 초대했습니다",
                                 employee_ids=[employee_id])
        out = _seated(store, room, tx, changed=task is not None and not seated)
    return out


@router.delete("/rooms/{id}/members/{employee_id}")
def remove_room_member(id: str, employee_id: str, store: StudioStore = Depends(get_store)) -> dict:
    """Takes a member out of the task room: `left_at` is set and the row stays (`_may_post` and the turns look at
    `left_at`; POST brings it back). One who is not in the room changes nothing. Where the employee is placed
    is not looked at: one who was moved away can still be taken out. The assignee stays — its run writes there;
    `/tasks/{id}/assign` is how the assignee changes.

    A group room (§3.6) has no assignee: anyone in it can be taken out, and the room says so (a system
    message)."""
    with store.transaction() as tx:
        room, employee, task = _seat(store, id, employee_id, tx)
        if task is not None and employee_id == task["assignee_id"]:
            raise StudioError("precondition_failed", "담당은 작업방에서 뺄 수 없습니다. 담당을 바꾸려면 "
                              f"/studio/tasks/{task['id']}/assign 을 씁니다")
        member = store.get_room_member(id, employee_id, tx=tx)
        seated = member is not None and member["left_at"] is None
        if seated:
            store.update_room_member(id, employee_id, left_at=now(), tx=tx)
        if task is None and seated:  # the notice tells the room and its members itself
            room = _group_notice(store, tx, room, "group_removed", f"{employee['name']} 님을 내보냈습니다",
                                 employee_ids=[employee_id])
        out = _seated(store, room, tx, changed=task is not None and seated)
    return out


# --- messages (§6.6) --------------------------------------------------------------------------------------
DEDUPE_WINDOW = 50  # "the room's recent messages" a client_msg_id is looked up in


class NewMessage(BaseModel):
    body: str = ""
    mentions: list[str] = []
    attachments: list[str] = []  # ids from POST /studio/attachments
    client_msg_id: str | None = None


@router.get("/rooms/{id}/messages")
def list_messages(id: str, before_id: int | None = Query(None, ge=0), after_id: int | None = Query(None, ge=0),
                  limit: int = Query(50, ge=1, le=MAX_PAGE), store: StudioStore = Depends(get_store)) -> dict:
    """Ascending by id. `after_id` alone reads forward (the first `limit` after it); every other form reads
    the newest `limit` (of the room, before `before_id`, or between the two). `has_more`: there are more in
    that direction."""
    _need(store.get_room(id), "방", id)
    rows = store.list_messages(id, after_id=after_id, before_id=before_id, limit=limit + 1)
    has_more = len(rows) > limit
    if has_more:
        forward = after_id is not None and before_id is None
        rows = rows[:limit] if forward else rows[1:]
    return {"items": rows, "has_more": has_more}


@router.post("/rooms/{id}/messages", status_code=201)
def post_message(id: str, body: NewMessage, store: StudioStore = Depends(get_store)):
    """The owner writes in a room: the row and `message.created`, in one transaction. 201 with the row.

    A `client_msg_id` already among the room's recent messages creates nothing: 200 with that message.

    A staged upload is claimed inside the transaction (`_claim`, under the store lock), so two messages
    written at once cannot both attach one id. A rollback gives the claim back; a commit deletes the upload."""
    mentions = list(dict.fromkeys(body.mentions))
    taken: list[Path] = []  # the claimed uploads: files under `_staged/<id>.taken/`
    copied: Path | None = None
    try:
        with store.transaction() as tx:
            room = _need(store.get_room(id, tx=tx), "방", id)
            if room["kind"] == "system":
                raise StudioError("precondition_failed", "시스템 공지 방에는 글을 쓸 수 없습니다")
            if body.client_msg_id:  # first: a retry's attachments were already moved by the first try
                for m in reversed(store.list_messages(id, limit=DEDUPE_WINDOW, tx=tx)):
                    if m["author_kind"] == "owner" and (m["meta"] or {}).get("client_msg_id") == body.client_msg_id:
                        return JSONResponse(m, 200)
            # a group room the owner left (§3.6) takes nothing more, like its name and its members. After the
            # retry above: a message saved before the leaving still answers its retry
            if room["kind"] == "group" and room["archived_at"] is not None:
                raise StudioError("precondition_failed", f"보관한 단체방입니다: {id}")
            staged = [_staged_file(a) for a in dict.fromkeys(body.attachments)]
            if not body.body.strip() and not staged:
                raise StudioError("missing_fields", "본문과 첨부가 모두 비었습니다", fields=["body"])
            members = {m["employee_id"] for m in store.list_room_members(room_id=id, tx=tx)}
            strangers = [e for e in mentions if e not in members]
            if strangers:
                raise StudioError("precondition_failed",
                                  f"이 방의 구성원이 아닌 직원을 멘션했습니다: {', '.join(strangers)}")
            meta = {"client_msg_id": body.client_msg_id} if body.client_msg_id else {}
            message = store.create_message(id, "owner", tx=tx, body=body.body, mentions=mentions, meta=meta)
            if staged:  # the folder is named after the message id, known only now
                for file in staged:  # one at a time: when a claim fails, the ones before it are released
                    taken.append(_claim(file))
                copied = _attachment_root() / str(message["id"])
                message = store.set_message_attachments(tx, message["id"], _copy_in(copied, taken))
            store.emit(tx, "message.created", {"message": message}, room_id=id, task_id=room["task_id"],
                       project_id=_room_project(store, room, tx))
            if room["kind"] in GROUP_KINDS and room["exchange_count"]:  # §4.6: the owner's message is counted from anew
                room = store.update_room(id, tx=tx, exchange_count=0)
                store.emit(tx, "room.updated", {"room": room, "members": store.list_room_members(room_id=id, tx=tx)},
                           room_id=id)
    except BaseException:
        if copied is not None:  # rolled back: the id may be handed out again, so its folder must not stay
            shutil.rmtree(copied, ignore_errors=True)
        _release(taken)  # and its uploads are staged again, for the next try
        raise
    for file in taken:  # committed: the originals are no longer needed (a folder left behind stays claimed)
        shutil.rmtree(file.parent, ignore_errors=True)
    if room["kind"] == "dm" and room["employee_id"]:  # §6.6: the employee of a 1:1 room answers (S-7)
        _ask_turn(room["employee_id"], id)
    elif room["kind"] == "task":  # §6.6: into the task's run, and the read turns of who is mentioned (S-9)
        _tell_task_room(store, room, message)
    elif room["kind"] in GROUP_KINDS:  # §6.6 (a department room too): who is mentioned, else every member, one after the other in the room's order
        _group_turns(id, mentions or group_order(store, id))
    return message


def _tell_task_room(store: StudioStore, room: dict, message: dict) -> None:
    """§6.6, the owner's message in a task room (S-9). While the task is working its run is told the message
    (`Hooks.tell_run`: `engine.interject`, the call of `/runs/{id}/interject` — into the conversation that is
    going on, else with the next stage). A task that waits for the owner takes none: its run is about to be cut
    (§8.3), and the decision is what goes on with it. Each member the message mentions who is not the assignee
    gets a read turn — the assignee's turn in that room is the run. Nothing is counted: the owner's messages
    are no exchange (§4.6). Like `_ask_turn` it never fails the POST: the message is stored by now."""
    task = store.get_task(room["task_id"]) if room["task_id"] else None
    if task is None:
        return
    names = ", ".join(str(a.get("name")) for a in message.get("attachments") or [] if isinstance(a, dict))
    text = ((message["body"] or "").strip() + (f"\n(작업방에 붙인 파일: {names})" if names else "")).strip()
    if _hooks is not None and text and task["status"] == "working" and task["run_id"]:
        try:
            _hooks.tell_run(task["run_id"], f"owner 가 작업방에 쓴 글이다: {text}")
        except Exception:  # noqa: BLE001
            log.exception("studio: owner 의 글을 작업 %s 의 런 %s 에 넣지 못했다", task["id"], task["run_id"])
    for employee_id in message["mentions"] or []:
        if employee_id != task["assignee_id"]:
            _ask_turn(employee_id, room["id"])


def _ask_turn(employee_id: str, room_id: str) -> None:
    """§6.6: ask for that employee's chat turn in that room (`Hooks.chat_turn`; studio/chat.py behind it). The
    message is stored by now and the answer of the POST does not depend on the turn: what the turn cannot do
    it says in the room itself (§9.5), and a hook that raises is logged here."""
    if _hooks is None:
        return
    try:
        _hooks.chat_turn(employee_id, room_id)
    except Exception:  # noqa: BLE001
        log.exception("studio: 직원 %s 의 채팅 턴(방 %s)을 넣지 못했다", employee_id, room_id)


# --- runs: liveness (§6.4 의 별칭, §7.4 `run.live`) ---------------------------------------------------------
LIVE_HEAD = ("run_id", "employee_id", "room_id", "task_id", "mode")  # what `run.live` puts before the engine's fields
_runs_lock = threading.Lock()
_tracked: dict[str, dict] = {}  # runs that are no task's run: chat · read turns, by run id (see track_run)


def track_run(run_id: str, *, mode: Literal["chat", "read"], employee_id: str, room_id: str,
              task_id: str | None = None, project_id: str | None = None) -> None:
    """Have a run that is no task's run — a chat turn of a 1:1, department or group room, a read-only turn of a
    task room — reported as `run.live` while it runs. S-7 · S-9 call this when they start one and
    `untrack_run` when it has ended. A task run needs neither: tasks.run_id of a working task is enough."""
    with _runs_lock:
        _tracked[run_id] = {"run_id": run_id, "employee_id": employee_id, "room_id": room_id,
                            "task_id": task_id, "project_id": project_id, "mode": mode}


def untrack_run(run_id: str) -> None:
    with _runs_lock:
        _tracked.pop(run_id, None)


def tracked_run(run_id: str) -> dict | None:
    """The chat · read turn that run is of, while it is tracked: None before `track_run`, after `untrack_run`
    and for every task run. Memory only — nothing of the DB is read, so the engine's notifier may ask on the
    run's own thread (studio/push.py leaves a tracked turn's failure to the notice written in its room)."""
    with _runs_lock:
        head = _tracked.get(run_id)
    return dict(head) if head is not None else None


def live_runs(store: StudioStore) -> dict[str, dict]:
    """The studio runs that may be running now, by run id: LIVE_HEAD plus `project_id` (for the envelope).
    The runs of working tasks (`mode` work) and the tracked chat · read turns."""
    runs = {r["run_id"]: {**r, "mode": "work"} for r in store.list_task_runs(status="working")}
    with _runs_lock:
        runs.update({run_id: dict(head) for run_id, head in _tracked.items()})
    return runs


def run_head(store: StudioStore, run_id: str) -> dict | None:
    """Whose run this is, in the shape of live_runs() — also for a task run that has stopped (blocked,
    review, done keep tasks.run_id). None for a run studio did not start."""
    head = tracked_run(run_id)
    if head is not None:
        return head
    rows = store.list_task_runs(run_id=run_id)
    return {**rows[0], "mode": "work"} if rows else None


def live_data(head: dict) -> dict | None:
    """The `data` of `run.live` (§7.4): LIVE_HEAD, then the engine's liveness fields as they are. None when
    the engine has no such run. StudioError `unavailable` when no engine is connected."""
    if _liveness is None:
        raise StudioError("unavailable", "런 상태를 읽을 엔진이 연결되지 않았습니다 (install 의 liveness)")
    try:
        fields = _liveness(head["run_id"])
    except FileNotFoundError:  # RelayEngine.load: no run.json under that id
        return None
    return {**{key: head.get(key) for key in LIVE_HEAD}, **fields}


@router.get("/runs/{run_id}/live")
def get_run_live(run_id: str, store: StudioStore = Depends(get_store)) -> dict:
    """What a `run.live` event carries in `data` (§7.4), read once after reconnecting: that event is not
    stored, so `after_id` does not bring it back. 404 for a run that is not a studio run."""
    head = _need(run_head(store, run_id), "런", run_id)
    return _need(live_data(head), "런", run_id)


@router.get("/runs/{run_id}/patch", response_class=PlainTextResponse)
def get_run_patch(run_id: str, store: StudioStore = Depends(get_store)) -> str:
    """The run's `result.patch` as text: what `package.changes.patch_url` of a final review points at (§5.4).
    Empty when the run changed no file. 404 for a run that is not a studio run."""
    _need(run_head(store, run_id), "런", run_id)
    if _engine is None:
        raise StudioError("unavailable", "런을 읽을 엔진이 연결되지 않았습니다 (use_engine)")
    engine = _engine()
    _need(review.run_view(engine, run_id), "런", run_id)  # a studio run the engine has no more
    return engine.patch_text(run_id)


@router.get("/runs/{run_id}/tokens")
def get_run_tokens(run_id: str, store: StudioStore = Depends(get_store)) -> dict:
    """The token watch of that run, as the server's `GET /runs/{run_id}/tokens` gives it (§6.4). 404 for a run
    that is not a studio run."""
    _need(run_head(store, run_id), "런", run_id)
    if _tokens is None:
        raise StudioError("unavailable", "런을 읽을 엔진이 연결되지 않았습니다 (use_engine)")
    try:
        return _tokens(run_id)
    except HTTPException as e:  # the server's own 404: a studio run the engine has no more
        if e.status_code != 404:
            raise
        raise StudioError("not_found", f"런을(를) 찾을 수 없습니다: {run_id}") from e


# --- studio MCP (§8): the employees' tools ------------------------------------------------------------------
# One path per tool (§8.1). The body is the tool's arguments, the answer is HTTP 200 with the dict of §8.3 —
# a business error is `{ok: false, code, …}`, not an HTTP error. What answers with an HTTP error is the
# authentication alone: 401 · 403 (`authenticate`) and 503. The tools are studio/tools.py; a refusal of the
# machine or the inbox is turned into a result there, so the error handlers above never see one from here.
class McpTaskPropose(BaseModel):
    """§8.4. Every argument has a default: a missing one is the result `missing_fields`, not a 422."""
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    reason: str = ""
    spec: str = ""
    done_criteria: str = ""
    type: str = ""                      # feature · fix · chore · doc · data
    suggested_assignee_id: str = ""
    depends_on: list[str] = Field(default_factory=list)
    campaign_id: str = ""
    project_id: str = ""                # default: this turn's task's, then the caller's (HR must give it)


class McpFollowupPropose(McpTaskPropose):
    parent_task_id: str = ""            # default: the task of this turn


class McpTaskGet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = ""                   # default: the task of this turn


class McpTaskList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = ""
    assignee: str = ""                  # `me` or an employee id
    campaign_id: str = ""
    limit: int = 50


class McpTaskProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = ""
    percent: float | None = None        # 0–100


class McpTaskRequestReview(BaseModel):
    """The shapes are §5.4: checklist `{criterion, status, note}`, evidence `{kind, ref, caption}`,
    judgment_calls `{text, why}`."""
    model_config = ConfigDict(extra="forbid")

    summary: str = ""
    checklist: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    judgment_calls: list[dict[str, Any]] = Field(default_factory=list)


class McpAskOwner(BaseModel):
    """`kind` is question · choice · approval (§1.5). question: body, options `{id, label}`. choice: body,
    options `{id, label, detail, figure}` (two or more), recommended, fix_points. approval: what, why, if_not,
    policy_ref."""
    model_config = ConfigDict(extra="forbid")

    kind: str = ""
    title: str = ""
    body: str = ""
    options: list[dict[str, Any]] = Field(default_factory=list)
    recommended: str = ""
    fix_points: str = ""
    what: str = ""
    why: str = ""
    if_not: str = ""
    policy_ref: str = ""


class McpRoomPost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    mentions: list[str] = Field(default_factory=list)
    room: str = ""                      # default: the room of this turn
    attachments: list[dict[str, Any]] = Field(default_factory=list)  # each {path, name=""} (§6.6)
    handoff: dict[str, Any] | None = None  # H-5: checked by studio/handover.py, then stored in meta.handoff as it is


class McpEmployeeList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = ""
    department_id: str = ""


class McpHireRecommend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = ""
    role: str = ""
    model: str = ""
    effort: str = ""
    department_id: str = ""             # an existing part ... or
    part_name: str = ""                 # ... a new one (with part_job): exactly one of the two
    part_job: str = ""
    part_relay: str = ""
    part_models: str = ""
    project_id: str = ""
    task_ids: list[str] = Field(default_factory=list)
    signal_key: str = ""                # the key of the trigger this answers


class McpStaffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = ""
    role: str = ""
    task_id: str = ""                   # empty: the task the turn is of
    department_id: str = ""
    project_id: str = ""


class McpEmployeeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str = ""                  # given: change that draft, an empty argument keeps what it has
    name: str = ""
    department_id: str = ""
    project_id: str = ""
    persona_md: str = ""
    guide_md: str = ""
    employee_yaml: str = ""
    first_message: str = ""
    part_name: str = ""                 # given: a new part is made with the hire (department_id is then the server's)
    part_job: str = ""
    part_relay: str = ""
    part_models: str = ""               # YAML of {chat, work, cap}
    target_employee_id: str = ""        # given: an amendment of `file` of that employee
    file: str = ""                      # PERSONA.md · GUIDE.md · employee.yaml


class McpEmployeeSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str = ""
    reason: str = ""
    source_inbox_id: str = ""


class McpPolicyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = ""
    policy_md: str = ""
    reason: str = ""
    submit: bool = False
    source_inbox_id: str = ""


def get_caller(request: Request, store: StudioStore = Depends(get_store)) -> tools.Caller:
    """The employee behind a request on `/studio/mcp/*`: the subject `authenticate` established, with the
    employees row of now (§8.5 looks at the DB, not at the token). Reached without the guard — this router on
    an app of its own — the token is checked here; 401 `actor_invalid` when it establishes no employee."""
    who = getattr(request.state, "studio_subject", None)
    if who is None:
        who = identify(_bearer(request) or "", _config())
    employee = store.get_employee(who["employee_id"]) if who and who.get("kind") == "employee" else None
    if employee is None or employee["retired_at"] is not None:
        raise StudioError("actor_invalid", "행위자 토큰으로 호출자를 정하지 못했습니다")
    return tools.Caller(employee=employee, run_id=who["run_id"], room_id=who["room_id"],
                        task_id=who["task_id"], mode=who["mode"])


def answered(rule: str, task_id: str | None, *, caller_run_id: str | None, inbox_id: str | None) -> str:
    """The one way into what the server does once the item of a T4 · T6 has been filed and the answer is made
    (§8.3 '서버의 뒤처리'; studio/cutoff.py `Cutoff.after`): the two tools below call it, and the guard's T4
    (S-10) is to call it with what it filed. A task run that asked is cut when it is still at work after the
    wait; a T4 from another turn cuts the task's run at once (§5.7); a T6 cuts nothing. Returns `none` · `cut`
    · `timed` — `none` while no cutoff is connected — and never raises: the answer goes out as it is."""
    if _cutoff is None or not task_id or not inbox_id:
        return "none"
    return _cutoff.after(rule, task_id, caller_run_id=caller_run_id, inbox_id=inbox_id)


def _tool(name: str, body: BaseModel, caller: tools.Caller, machine: TaskMachine, inbox: Inbox, *,
          rule: str | None = None) -> dict:
    """Run a tool. `rule` is the transition its success is (T4 · T6): what follows that answer is `answered`'s."""
    out = tools.call(name, caller, body.model_dump(), machine=machine, inbox=inbox)
    if rule is not None and out.get("ok"):
        answered(rule, caller.task_id, caller_run_id=caller.run_id, inbox_id=out.get("inbox_id"))
    return out


# --- the guard (§6.4 '가드', §10.5): the hook's question about a command of the ask list -------------------------
class GuardCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    command: str
    rule_id: str


def _stage_now(run_id: str) -> str:
    """The stage that run is in — `stage` of a risky_command's payload (§5.3). "" when the engine does not say."""
    try:
        return str((_liveness(run_id) if _liveness is not None else {}).get("stage") or "")
    except Exception:  # noqa: BLE001 - the item is filed without it
        return ""


@router.post("/guard/check")
def guard_check(body: GuardCheck, caller: tools.Caller = Depends(get_caller),
                machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """The guard hook asks about a command of the ask list (§10.4). `{decision, message, inbox_id, end_turn}`."""
    # the four rows of §6.4 are studio/guard.py `check`; a filed item is a T4, followed as `ask_owner`'s is
    out = guard.check(caller, body.tool, body.command, body.rule_id, machine=machine, inbox=inbox,
                      stage=_stage_now(caller.run_id))
    if out["inbox_id"]:
        answered("T4", caller.task_id, caller_run_id=caller.run_id, inbox_id=out["inbox_id"])
    return out


@router.post("/mcp/task_propose")
def mcp_task_propose(body: McpTaskPropose, caller: tools.Caller = Depends(get_caller),
                     machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """C1: propose a task. `{task_id, inbox_id}`."""
    return _tool("task_propose", body, caller, machine, inbox)


@router.post("/mcp/followup_propose")
def mcp_followup_propose(body: McpFollowupPropose, caller: tools.Caller = Depends(get_caller),
                         machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """C1 with a parent (default: the task of this turn). `{task_id, inbox_id, parent_id}`."""
    return _tool("followup_propose", body, caller, machine, inbox)


@router.post("/mcp/task_get")
def mcp_task_get(body: McpTaskGet, caller: tools.Caller = Depends(get_caller),
                 machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """Read a task. `{task, depends_on[], dependents[], pending_inbox, recent_events[]}`."""
    return _tool("task_get", body, caller, machine, inbox)


@router.post("/mcp/task_list")
def mcp_task_list(body: McpTaskList, caller: tools.Caller = Depends(get_caller),
                  machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """The tasks of the caller's project (HR: all). `{items[]}`."""
    return _tool("task_list", body, caller, machine, inbox)


@router.post("/mcp/task_progress")
def mcp_task_progress(body: McpTaskProgress, caller: tools.Caller = Depends(get_caller),
                      machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """Leave a `progress` event on the task of this turn. `{event_id}`."""
    return _tool("task_progress", body, caller, machine, inbox)


@router.post("/mcp/task_request_review")
def mcp_task_request_review(body: McpTaskRequestReview, caller: tools.Caller = Depends(get_caller),
                            machine: TaskMachine = Depends(get_machine),
                            inbox: Inbox = Depends(get_inbox)) -> dict:
    """T6: file the final review. `{inbox_id}` and `end_turn: true`."""
    # the same way in as T4, and nothing is cut: T6 is not in `cutoff.TIMED` (§8.3). The docstring is the saved
    # contract's description (design/studio/openapi.json), so this is said here.
    if body.evidence:  # §6.6 '직원의 첨부': a path of evidence is checked against the folder this run works in
        caller = dataclasses.replace(caller, workdir=run_workdir(caller.run_id))
    return _tool("task_request_review", body, caller, machine, inbox, rule="T6")


@router.post("/mcp/ask_owner")
def mcp_ask_owner(body: McpAskOwner, caller: tools.Caller = Depends(get_caller),
                  machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """T4: file a blocking item (§5.7). `{inbox_id}` and `end_turn: true`."""
    # the task's run is cut when it is still at work after the wait — at once when this turn is not that run
    # (§8.3 '서버의 뒤처리', `answered`)
    if body.options:  # §6.6 '직원의 첨부': a figure's path is checked against the folder this run works in
        caller = dataclasses.replace(caller, workdir=run_workdir(caller.run_id))
    return _tool("ask_owner", body, caller, machine, inbox, rule="T4")


@router.post("/mcp/room_post")
def mcp_room_post(body: McpRoomPost, caller: tools.Caller = Depends(get_caller),
                  machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """Write in a room (default: the room of this turn). `{message_id, queued[]}`."""
    if body.attachments:  # §6.6: a path is checked against the folder this run works in — asked only then
        caller = dataclasses.replace(caller, workdir=run_workdir(caller.run_id))
    out = _tool("room_post", body, caller, machine, inbox)
    if out.get("code") == "exchange_limit" and out.get("inbox_id"):
        # §4.6: this call had the system ask the owner (T4) — the same way in as `ask_owner`: the task's run is cut
        # when it is still at work after the wait, at once when the call came from a read turn (§5.7). A call that
        # filed nothing (the task waits already) starts no wait: the one that is running must not begin again.
        item = inbox.store.get_inbox_item(out["inbox_id"])
        answered("T4", item["task_id"] if item else None, caller_run_id=caller.run_id, inbox_id=out["inbox_id"])
    return out


@router.post("/mcp/employee_list")
def mcp_employee_list(body: McpEmployeeList, caller: tools.Caller = Depends(get_caller),
                      machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """The employees of the caller's project (HR: all), the retired left out. `{items[]}`."""
    return _tool("employee_list", body, caller, machine, inbox)


@router.post("/mcp/hire_recommend")
def mcp_hire_recommend(body: McpHireRecommend, caller: tools.Caller = Depends(get_caller),
                       machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """HR only: raise a recommendation to hire (an inbox item for the owner). `{inbox_id}`."""
    return _tool("hire_recommend", body, caller, machine, inbox)


@router.post("/mcp/staff_request")
def mcp_staff_request(body: McpStaffRequest, caller: tools.Caller = Depends(get_caller),
                      machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """An employee asks HR to judge whether more staff is needed. `{asked, why}`; HR may recommend nothing."""
    return _tool("staff_request", body, caller, machine, inbox)


@router.post("/mcp/employee_draft")
def mcp_employee_draft(body: McpEmployeeDraft, caller: tools.Caller = Depends(get_caller),
                       machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """HR only: make or change a draft of a hire or of an amendment. `{draft_id, action, problems[]}`."""
    return _tool("employee_draft", body, caller, machine, inbox)


@router.post("/mcp/employee_submit")
def mcp_employee_submit(body: McpEmployeeSubmit, caller: tools.Caller = Depends(get_caller),
                        machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """HR only: file the `hire` item of a draft. `{inbox_id}`."""
    return _tool("employee_submit", body, caller, machine, inbox)


@router.post("/mcp/policy_draft")
def mcp_policy_draft(body: McpPolicyDraft, caller: tools.Caller = Depends(get_caller),
                     machine: TaskMachine = Depends(get_machine), inbox: Inbox = Depends(get_inbox)) -> dict:
    """HR only: save a policy draft and, with `submit`, file its `hire` item. `{draft_id, diff, inbox_id}`."""
    return _tool("policy_draft", body, caller, machine, inbox)


# --- attachments (§6.6) -----------------------------------------------------------------------------------
MAX_ATTACHMENT_MB = files.MAX_ATTACHMENT_MB                              # the limit of POST /attachments (server.py)
STAGED = "_staged"                                  # uploads not attached to a message yet
STAGED_ID = re.compile(r"att_[0-9a-f]{12}")
TAKEN = ".taken"                                    # suffix of a staged folder that a message has claimed
ATTACHED_ID = re.compile(r"\d+|inb_[0-9a-f]{8}|shot_[0-9a-f]{12}")  # a message id, an inbox id, a screenshot (M-7)
UNSAFE_NAME = files.UNSAFE_NAME  # the cleaning of POST /attachments (server.py)


class AttachmentUpload(BaseModel):
    name: str
    data_b64: str


def _attachment_root() -> Path:
    return _config().studio_dir / "attachments"


_mime = files.mime


def _staged_file(attachment_id: str) -> Path:
    """The one file of a staged upload. The id pattern keeps the lookup inside `_staged/`."""
    folder = _attachment_root() / STAGED / attachment_id
    known = STAGED_ID.fullmatch(attachment_id) is not None and folder.is_dir()
    files = [p for p in folder.iterdir() if p.is_file()] if known else []
    if len(files) != 1:
        raise StudioError("precondition_failed", f"첨부를 찾을 수 없습니다: {attachment_id} "
                          "(POST /studio/attachments 가 돌려준 id 를 한 번만 쓸 수 있습니다)")
    return files[0]


def _claim(file: Path) -> Path:
    """Take a staged upload for one message; the file's new path. Its folder becomes `<id>.taken`, which
    `_staged_file` does not find: the id is used up from here on, also when deleting the folder fails later."""
    folder = file.parent
    taken = folder.with_name(folder.name + TAKEN)
    folder.rename(taken)
    return taken / file.name


def _release(files: list[Path]) -> None:
    """Give claimed uploads back after a rollback: each folder gets its id again."""
    for file in files:
        folder = file.parent
        try:
            folder.rename(folder.with_name(folder.name.removesuffix(TAKEN)))
        except OSError:  # it stays claimed: the upload is lost to this id, never attached twice
            pass


def _copy_in(folder: Path, staged: list[Path]) -> list[dict]:
    """Copy claimed files into a message's folder; the `[{name, path, mime, size}]` of attachments_json.

    Copied, not moved: when the transaction rolls back the originals are still there to be released."""
    return files.copy([(src, src.name) for src in staged], folder)  # two uploads with one name: `-2`


@router.post("/attachments", status_code=201, dependencies=[Depends(get_store)])
def upload_attachment(body: AttachmentUpload) -> dict:
    """Stage a file for a message (base64 JSON, like POST /attachments). Its `id` goes in `attachments[]`."""
    try:
        data = base64.b64decode(body.data_b64.split(",", 1)[-1])
    except (binascii.Error, ValueError):
        raise HTTPException(422, "data_b64 가 base64 가 아닙니다")
    if len(data) > MAX_ATTACHMENT_MB * 1024 * 1024:
        raise StudioError("payload_too_large", f"첨부는 파일당 {MAX_ATTACHMENT_MB}MB 까지입니다")
    name = UNSAFE_NAME.sub("_", Path(body.name).name).strip(". ") or "file"
    attachment_id = "att_" + uuid.uuid4().hex[:12]
    folder = _attachment_root() / STAGED / attachment_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return {"id": attachment_id, "name": name, "mime": _mime(name), "size": len(data)}


@router.get("/attachments/{id}/{name}", response_class=FileResponse, dependencies=[Depends(get_store)])
def download_attachment(id: str, name: str):
    """A file attached to a message (`id` = message id) or to an inbox item (`inb_…`). Staged uploads are
    not served."""
    missing = StudioError("not_found", f"첨부를 찾을 수 없습니다: {id}/{name}")
    if not ATTACHED_ID.fullmatch(id) or name != Path(name).name or name in (".", ".."):
        raise missing
    folder = (_attachment_root() / id).resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        raise missing
    return FileResponse(path, media_type=_mime(name), headers={"X-Content-Type-Options": "nosniff"})


# --- app update (§6.4, A-13) --------------------------------------------------------------------------------
@router.get("/app/latest")
def app_latest(abi: str | None = None) -> dict:
    """The app update on offer: version name, build number, size, sha256, when it was put there, what changed.
    `abi` = the phone's CPU (Build.SUPPORTED_ABIS[0]); it picks the APK. No folder or file is not an error:
    `{available: false, reason}`. The server only reads `Config.studio_app_dir`."""
    return app_release.latest(_config().studio_app_folder, (abi or "").strip() or app_release.DEFAULT_ABI)


@router.get("/app/files/{name}", response_class=FileResponse)
def download_app(name: str):
    """The APK that `GET /studio/app/latest` names (`url`). A name that is not a plain `.apk` file of the folder
    (`..`, a path, a link) is `not_found`, like an attachment."""
    path = app_release.safe_file(_config().studio_app_folder, name)
    if path is None:
        raise StudioError("not_found", f"앱 파일을 찾을 수 없습니다: {name}")
    return FileResponse(path, media_type=app_release.APK_MIME, filename=path.name,
                        headers={"X-Content-Type-Options": "nosniff"})


# --- this computer (§6.4, M-1) ----------------------------------------------------------------------------
_machine = machine_info.Sources()  # where the diagnosis reads from; server.py adds what only it knows


def use_machine(**parts: Any) -> None:
    """Set where `GET /studio/machine` reads from (studio/machine.py `Sources`), after install(): server.py
    gives `doctor` (cli.py `doctor_checks`), `providers` (its `GET /providers`) and `version` (the hash of the
    code it loaded). Only the parts that are named change."""
    global _machine
    _machine = dataclasses.replace(_machine, **parts)


def machine_brief() -> dict:
    """What the stream's `hello` says of this computer (§7.1): `{name, server_version, schema_version,
    utc_offset_minutes}` — the values of `GET /studio/machine` that cost nothing to read."""
    return machine_info.brief(_machine)


# §6.4: is this computer fit to work? The checks of `relay doctor`, the installed engines (registry), the disk of
# the runs folder, the AIs (`GET /providers`), the running editors and the limits. A source that fails empties
# its own field and the answer stays 200. No docstring: the saved contract would carry it as the description.
# No cache (provisional, M-1): every call runs `claude --version` once and one PowerShell query (up to 8 s), so
# the app asks only when it opens or refreshes the screen; the four cheap values ride on `hello` (machine_brief).
@router.get("/machine")
def read_machine(store: StudioStore = Depends(get_store)) -> dict:
    return machine_info.report(_config(), store.list_projects(), _machine)


class MachineLimits(BaseModel):
    """V-4: the limits the owner may change, each a whole number in its range — a text, a bool, 0 or a negative
    one is 422. At least one is named."""
    model_config = ConfigDict(extra="forbid")

    max_runs: StrictInt | None = Field(default=None, ge=1, le=20)
    max_exchanges: StrictInt | None = Field(default=None, ge=1, le=200)
    group_max_exchanges: StrictInt | None = Field(default=None, ge=1, le=100)


@router.get("/machine/limits")
def read_machine_limits() -> dict:
    """The three limits the owner may change, as the running server holds them (§6.4; `GET /studio/machine` has them too, but runs the checks of the computer)."""
    return machine_limits.read(_config())


@router.patch("/machine/limits")
def change_machine_limits(body: MachineLimits) -> dict:
    """V-4 (§4.6 · §6.4): the named limits take effect at once — the line and the exchange checks read them at every use — and are kept in this machine's settings file (`relay.config.local.yaml`), so they hold after a restart. The answer is the three limits afterwards. None named: 422. The file cannot be written: 500 and nothing changed."""
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(422, "바꿀 한도가 없습니다 (max_runs · max_exchanges · group_max_exchanges)")
    try:
        done = machine_limits.save(_config(), changes)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except OSError as e:
        raise HTTPException(500, f"한도를 설정 파일에 적지 못했습니다: {e}")
    kick = getattr(_queue, "kick", None)
    if kick is not None and "max_runs" in changes:
        kick()  # a larger number of slots starts what waited
    return done


# --- devices: pairing · list · revoke (§6.5, M-2) ---------------------------------------------------------
_pairings = devices.Pairings()  # the one-time codes: this process's memory only, so a restart forgets them (§3.14)
DeviceName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
DevicePlatform = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]


class Pair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=200)
    name: DeviceName
    platform: DevicePlatform = "android"
    fcm_token: str | None = Field(default=None, max_length=4096)  # null or '' = none yet


# §6.5 의 1: a code for one phone. The owner's (the guard has checked the token); nothing is written to the DB.
# `qr.url` is worked out per call (devices.reach_url) — with remote access on it asks the Tailscale CLI once.
# No docstrings in this section: the saved contract would carry them as descriptions.
@router.post("/devices/pairing")
def new_pairing(request: Request) -> dict:
    code, expires_at = _pairings.issue()
    url = devices.reach_url(_config(), str(request.base_url).rstrip("/"))
    return {"code": code, "expires_at": expires_at,
            "qr": {"v": 1, "url": url, "code": code, "name": machine_brief()["name"]}}


# §6.5 의 2–4: the one path without a token (TOKENLESS) — the code is what is checked, after the guard's origin
# and network checks. A body that fails validation (422) and a DB that is not open (503) leave the code unused;
# from `redeem` on it is spent. Only the token's hash is stored; its text is in this answer and nowhere else.
# A push token belongs to one device: an unrevoked row that holds the same one gives it up (it is not revoked).
@router.post("/devices/pair", status_code=201)
def pair_device(body: Pair, store: StudioStore = Depends(get_store)) -> dict:
    if not _pairings.redeem(body.code):
        raise StudioError("pairing_invalid", "페어링 코드가 틀렸거나 만료됐거나 이미 썼습니다. "
                                             "대시보드에서 새 코드를 받으세요 (코드는 5분 동안, 한 번만 통합니다).")
    token, token_hash = devices.new_token()
    fcm = (body.fcm_token or "").strip() or None
    with store.transaction() as tx:
        fields: dict[str, Any] = {"platform": body.platform}
        if fcm:
            for other in store.list_devices(tx=tx):
                if other["fcm_token"] == fcm:
                    store.update_device(other["id"], fcm_token=None, tx=tx)
            fields["fcm_token"] = fcm
        row = store.create_device(body.name, token_hash, tx=tx, **fields)
    log.info("기기를 페어링했습니다: %s (%s)", row["id"], row["name"])
    return {"device": devices.public(row), "token": token, "machine": machine_brief()}


# The devices that can still come in (a revoked one is left out), without `token_hash`.
@router.get("/devices")
def list_devices(store: StudioStore = Depends(get_store)) -> dict:
    return {"items": [devices.public(row) for row in store.list_devices()]}


QuietTime = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]  # HH:MM, 00:00–23:59


class QuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: QuietTime  # on the device's own clock: from `start` until just before `end`, over midnight too
    end: QuietTime
    utc_offset_min: int | None = Field(default=None, ge=-720, le=840)  # that clock's minutes ahead of UTC


class DeviceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quiet_hours: QuietHours | None = None  # null or left out = no quiet hours


class DevicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: DeviceName | None = None  # null = as it is
    fcm_token: str | None = Field(default=None, max_length=4096)  # sent as null or '' = none any more
    settings: DeviceSettings | None = None  # replaced as a whole; null = {}


# N-2 · A-11 (§6.4, §5.9): only the keys that were sent change. `me` is the device of the token that asks — the
# shared token is no device. A push token belongs to one device, as at pairing: an unrevoked row that holds the
# same one gives it up. `settings` is replaced as a whole (the quiet hours: studio/push.py `quiet_now`; without
# `utc_offset_min` this PC's clock is read). A revoked device is not there any more. No stream event: no client
# draws a device from the stream.
@router.patch("/devices/{id}")
def patch_device(id: str, body: DevicePatch, request: Request, store: StudioStore = Depends(get_store)) -> dict:
    if id == "me":
        who = getattr(request.state, "studio_subject", None) or {}
        if not who.get("device_id"):
            raise StudioError("precondition_failed", "`me` 는 기기 토큰으로 부를 때만 씁니다 — 공용 토큰에는 기기가 "
                                                     "없습니다. 기기 id 를 적으세요 (GET /studio/devices).")
        id = who["device_id"]
    sent = body.model_fields_set
    with store.transaction() as tx:
        row = store.get_device(id, tx=tx)
        row = _need(row if row is not None and row["revoked_at"] is None else None, "기기", id)
        fields: dict[str, Any] = {}
        if body.name is not None:
            fields["name"] = body.name
        if "fcm_token" in sent:
            fcm = (body.fcm_token or "").strip() or None
            if fcm:
                for other in store.list_devices(tx=tx):
                    if other["id"] != id and other["fcm_token"] == fcm:
                        store.update_device(other["id"], fcm_token=None, tx=tx)
            fields["fcm_token"] = fcm
        if "settings" in sent:
            fields["settings"] = body.settings.model_dump(exclude_none=True) if body.settings is not None else {}
        if fields:
            row = store.update_device(id, tx=tx, **fields) or row
    return devices.public(row)


# §6.5 의 5: the token is 401 from the next request (owner_device reads revoked_at), and the listeners of
# on_revoke are told after the commit — ws.py closes the device's connections with 4401. A connection that was
# between its token check and the hub at that moment is closed by ws.py itself: it reads revoked_at again once
# it is attached (§7.1). Revoking twice is the same answer; telling the listeners again does no harm.
@router.post("/devices/{id}/revoke")
def revoke_device(id: str, store: StudioStore = Depends(get_store)) -> dict:
    row = _need(store.get_device(id), "기기", id)
    if row["revoked_at"] is None:
        row = store.update_device(id, revoked_at=now()) or row
        log.info("기기를 폐기했습니다: %s (%s)", row["id"], row["name"])
    for listener in list(_revoke_listeners):
        try:
            listener(id)
        except Exception:  # the revoke is committed either way; a listener must not turn it into a 500
            log.exception("기기 폐기를 알리지 못했습니다: %s", id)
    return devices.public(row)
