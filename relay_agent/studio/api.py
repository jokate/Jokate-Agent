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

Not here yet, on purpose: actor tokens (`sat1.`, §8.2) are verified by S-6, so until then they establish
nobody and get 401 everywhere. An owner's message is stored and announced; the turns it starts belong to
S-7 · S-9. Tasks are not started or stopped here (S-8), campaigns not created or driven (S-14).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import mimetypes
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection

from .inbox import Hooks, Inbox, InboxError
from .store import StudioStore, StudioStoreError, Tx, now
from .tasks import OWNER, STATES, TERMINAL, TEXT_FIELDS, Effects, TaskMachine, TaskNotFound, TransitionError

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


async def _on_inbox_error(request: Request, exc: InboxError) -> JSONResponse:
    return JSONResponse(exc.as_dict(), STATUS[exc.code])  # §5.1: `inbox` · `allowed` · `fields` ride along


def add_error_handlers(app: FastAPI) -> None:
    """Request validation errors are left alone: FastAPI's 422 with a list in `detail`, no `code` (§6.3)."""
    app.add_exception_handler(StudioError, _on_studio_error)
    app.add_exception_handler(TransitionError, _on_transition_error)
    app.add_exception_handler(TaskNotFound, _on_task_not_found)
    app.add_exception_handler(InboxError, _on_inbox_error)


# --- the server's config and the one store ----------------------------------------------------------------
_cfg: Config | None = None
_lock = threading.Lock()
_store: StudioStore | None = None
_failure: str | None = None  # why the store could not start; kept so the start is not retried per request
_effects: Effects | None = None  # what a task change does outside the DB (runs, gates, apply); None = nothing
_hooks: Hooks | None = None  # what a decision asks of the engine and the turns (interject, commit, chat turn)
_run_view: Callable[[str], dict | None] | None = None  # run id -> {id, status, changes_status}; S-8 sets it
_liveness: Callable[[str], dict] | None = None  # run id -> RelayEngine.liveness(run id); None = no engine connected
_commit_listeners: list[Callable[[tuple[int, ...]], None]] = []  # see on_commit
_pruned_at = 0.0  # time.monotonic() of the last look at the retention of stream_events
PRUNE_EVERY = 24 * 3600.0  # §7.2: at the start and once a day


def install(app: FastAPI, cfg: Config, effects: Effects | None = None, *,
            liveness: Callable[[str], dict] | None = None, hooks: Hooks | None = None) -> None:
    """Connect the studio layer to the server: remember its config (and the effects of task changes, once
    S-8 has them), add the router and the error bodies. `liveness` is the engine's — what `run.live` and
    `GET /studio/runs/{run_id}/live` report (§7.4). `hooks` is what an inbox decision asks of the engine and
    the turns (studio/inbox.py); without it a decision is stored and its text waits to be delivered."""
    global _cfg, _effects, _liveness, _hooks
    _cfg, _effects, _liveness, _hooks = cfg, effects, liveness, hooks
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
DEVICE_PREFIX = "sdt_"
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


def identify(token: str, cfg: Config) -> dict | None:
    """Who a token is: `{"kind": "owner", "device_id": id | None}`, or None when it establishes nobody.

    The shared token is checked first and needs no DB. A device token is accepted with or without a shared
    token configured. Raises StudioError `unavailable` when a device token cannot be looked up."""
    if not token:
        return None
    if cfg.auth_token and hmac.compare_digest(token.encode(), cfg.auth_token.encode()):
        return {"kind": "owner", "device_id": None}
    if token.startswith(DEVICE_PREFIX):
        device = owner_device(token)
        if device is not None:
            return {"kind": "owner", "device_id": device["id"]}
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
        return error_response(code, "토큰이 맞지 않습니다 (대소문자 구분)." + hint)
    if actor_path:
        return error_response("actor_not_allowed", "이 경로는 직원(행위자) 토큰만 받습니다")
    request.state.studio_subject = who  # `devices/me` (A-11) reads the device id from here
    return None


# --- shared helpers ---------------------------------------------------------------------------------------
MAX_PAGE = 200  # the largest page of a list with a cursor (messages, task events)


def _need(row: dict | None, what: str, id: Any) -> dict:
    if row is None:
        raise StudioError("not_found", f"{what}을(를) 찾을 수 없습니다: {id}")
    return row


def _with_dm_room(store: StudioStore, employees: list[dict]) -> list[dict]:
    """Employee rows with `dm_room_id` (None while the 1:1 room does not exist)."""
    rooms = {r["employee_id"]: r["id"] for r in store.list_rooms(kind="dm", include_archived=True)}
    return [{**e, "dm_room_id": rooms.get(e["id"])} for e in employees]


EMPLOYEE_FILES = {"persona_md": "PERSONA.md", "guide_md": "GUIDE.md", "memory_md": "MEMORY.md",
                  "employee_yaml": "employee.yaml"}


def _employee_files(employee_id: str) -> dict:
    """The four files of runs/studio/employees/<id>/ (§2.2) as text; None for a file that is not there."""
    folder = _config().studio_dir / "employees" / employee_id
    out: dict[str, str | None] = {}
    for key, name in EMPLOYEE_FILES.items():
        try:
            out[key] = (folder / name).read_text(encoding="utf-8") if (folder / name).is_file() else None
        except (OSError, UnicodeDecodeError):
            out[key] = None
    return out


def _room_project(store: StudioStore, room: dict, tx: Tx) -> str | None:
    """The project a room's events are filed under: the task's for a task room, the employee's for a DM."""
    if room["task_id"]:
        task = store.get_task(room["task_id"], tx=tx)
        return task["project_id"] if task else None
    if room["employee_id"]:
        employee = store.get_employee(room["employee_id"], tx=tx)
        return employee["project_id"] if employee else None
    return None


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


@router.get("/projects/{id}")
def get_project(id: str, store: StudioStore = Depends(get_store)) -> dict:
    row = _need(store.get_project(id), "프로젝트", id)
    employees = _with_dm_room(store, store.list_employees(project_id=id))
    # `editor` is filled by M-7; the key is there so the shape does not change then
    return {**row, "employees": employees, "task_counts": store.count_tasks_by_status(id), "editor": None}


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


@router.get("/employees")
def list_employees(project_id: str | None = None, department_id: str | None = None,
                   retired: int = Query(0, ge=0, le=1), store: StudioStore = Depends(get_store)) -> dict:
    """`retired=1` adds the retired employees to the list."""
    rows = store.list_employees(project_id=project_id, department_id=department_id,
                                include_retired=bool(retired))
    return {"items": _with_dm_room(store, rows)}


@router.get("/employees/{id}")
def get_employee(id: str, store: StudioStore = Depends(get_store)) -> dict:
    row = _with_dm_room(store, [_need(store.get_employee(id), "직원", id)])[0]
    tasks = [t for t in store.list_tasks(assignee_id=id) if t["status"] not in TERMINAL]
    # `runs` (the employee's studio runs going on now) is filled by S-7 · S-8
    return {**row, "files": _employee_files(id), "tasks": tasks, "runs": []}


# --- campaigns · tasks (§6.4) -----------------------------------------------------------------------------
TaskStatus = Literal["proposed", "backlog", "working", "blocked", "review", "done", "rejected", "cancelled"]
TaskType = Literal["feature", "fix", "chore", "doc", "data"]
CampaignStatus = Literal["draft", "running", "paused", "done", "cancelled"]
NEW_TASK_REQUIRED = ("project_id", "title", "reason", "spec", "done_criteria", "type")
TASK_NOT_NULL = TEXT_FIELDS + ("type", "is_milestone", "sort_order")  # PATCH: null clears relay · campaign_id only


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


def get_machine(store: StudioStore = Depends(get_store)) -> TaskMachine:
    """The task machine of a request: the one store, and the effects install() was given (none before S-8)."""
    return TaskMachine(store, _effects)


def _empty(value: Any) -> bool:
    return not (isinstance(value, str) and value.strip())


def _clean(value: str | None) -> str | None:
    """An optional text: blank is NULL."""
    return (value or "").strip() or None


def _progress(counts: dict[str, int]) -> dict:
    """`progress` of a campaign: every status of §4.1 is a key; done_ratio = done / total (provisional)."""
    by_status = {status: counts.get(status, 0) for status in STATES}
    total = sum(by_status.values())
    return {"by_status": by_status, "total": total, "done_ratio": by_status["done"] / total if total else 0.0}


@router.get("/campaigns")
def list_campaigns(project_id: str | None = None, status: CampaignStatus | None = None,
                   store: StudioStore = Depends(get_store)) -> dict:
    counts = store.count_tasks_by_campaign()
    rows = store.list_campaigns(project_id=project_id, status=status)
    return {"items": [{**row, "progress": _progress(counts.get(row["id"], {}))} for row in rows]}


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
    line (provisional); `run` stays null until S-8 sets `_run_view`."""
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
                    "budget", "risky_command", "final_review"]


class Decide(BaseModel):
    """§5.1. `decision` and `via` are plain text here: a value that does not fit the item is 422
    `invalid_decision` with `allowed[]`, not the 422 of the request validation."""
    model_config = ConfigDict(extra="forbid")

    decision: str
    comment: str | None = None
    data: dict[str, Any] | None = None
    via: str | None = None  # app (default) · notification · dashboard


def get_inbox(machine: TaskMachine = Depends(get_machine)) -> Inbox:
    """The inbox of a request: the request's machine, and the hooks the server gave (install() · use_hooks() —
    studio/runs.py for the runs that are no task's; the turns come with S-7 · S-8)."""
    return Inbox(machine.store, machine, _hooks, studio_dir=_config().studio_dir)


# --- the engine's side of the inbox (§5.8) ----------------------------------------------------------------
# Called from outside a /studio request: server.py's old /runs endpoints and the thread that advanced a run
# (studio/runs.py). They use the store only when it is open — it is, from the server's start on — and the two
# the old endpoints call never raise: the dashboard and a run must not depend on the studio layer.
def use_hooks(hooks: Hooks | None) -> None:
    """Set what an inbox decision asks of the engine, after install(): server.py builds it from what it
    defines further down (`_advance_bg`)."""
    global _hooks
    _hooks = hooks


def run_inbox() -> Inbox | None:
    """An inbox outside a request, with the effects and hooks the server gave. None while the store is not
    open (it could not start, or nothing has opened it yet): then nothing is filed or closed."""
    with _lock:
        store = _store
    if store is None or _cfg is None:
        return None
    return Inbox(store, TaskMachine(store, _effects), _hooks, studio_dir=_cfg.studio_dir)


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
    has `package` (§5.4): null until S-12 computes it."""
    return inbox.view(id)


@router.post("/inbox/{id}/decide")
def decide_inbox(id: str, body: Decide, inbox: Inbox = Depends(get_inbox)) -> dict:
    """The one way a decision is made (§5.1): `{inbox, task}`. A second decision of the same item is 409
    `already_decided` with the item as it is now."""
    return inbox.decide(id, body.decision, comment=body.comment, data=body.data, via=body.via)


# --- rooms ------------------------------------------------------------------------------------------------
RoomKind = Literal["dm", "task", "department", "system"]


class ReadMark(BaseModel):
    message_id: int = Field(ge=0)


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
    except BaseException:
        if copied is not None:  # rolled back: the id may be handed out again, so its folder must not stay
            shutil.rmtree(copied, ignore_errors=True)
        _release(taken)  # and its uploads are staged again, for the next try
        raise
    for file in taken:  # committed: the originals are no longer needed (a folder left behind stays claimed)
        shutil.rmtree(file.parent, ignore_errors=True)
    return message


# --- runs: liveness (§6.4 의 별칭, §7.4 `run.live`) ---------------------------------------------------------
LIVE_HEAD = ("run_id", "employee_id", "room_id", "task_id", "mode")  # what `run.live` puts before the engine's fields
_runs_lock = threading.Lock()
_tracked: dict[str, dict] = {}  # runs that are no task's run: chat · read turns, by run id (see track_run)


def track_run(run_id: str, *, mode: Literal["chat", "read"], employee_id: str, room_id: str,
              task_id: str | None = None, project_id: str | None = None) -> None:
    """Have a run that is no task's run — a chat turn of a 1:1 or department room, a read-only turn of a
    task room — reported as `run.live` while it runs. S-7 · S-9 call this when they start one and
    `untrack_run` when it has ended. A task run needs neither: tasks.run_id of a working task is enough."""
    with _runs_lock:
        _tracked[run_id] = {"run_id": run_id, "employee_id": employee_id, "room_id": room_id,
                            "task_id": task_id, "project_id": project_id, "mode": mode}


def untrack_run(run_id: str) -> None:
    with _runs_lock:
        _tracked.pop(run_id, None)


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
    with _runs_lock:
        head = _tracked.get(run_id)
    if head is not None:
        return dict(head)
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


# --- attachments (§6.6) -----------------------------------------------------------------------------------
MAX_ATTACHMENT_MB = 25                              # the limit of POST /attachments (server.py)
STAGED = "_staged"                                  # uploads not attached to a message yet
STAGED_ID = re.compile(r"att_[0-9a-f]{12}")
TAKEN = ".taken"                                    # suffix of a staged folder that a message has claimed
ATTACHED_ID = re.compile(r"\d+|inb_[0-9a-f]{8}")    # a folder is a message id or an inbox id
UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')  # the cleaning of POST /attachments (server.py)


class AttachmentUpload(BaseModel):
    name: str
    data_b64: str


def _attachment_root() -> Path:
    return _config().studio_dir / "attachments"


def _mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


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
    folder.mkdir(parents=True, exist_ok=True)
    refs: list[dict] = []
    used: set[str] = set()
    for src in staged:
        name, n = src.name, 1
        while name.lower() in used:  # two uploads with one name in the same message
            n += 1
            name = f"{src.stem}-{n}{src.suffix}"
        used.add(name.lower())
        shutil.copy2(src, folder / name)
        refs.append({"name": name, "path": f"{folder.name}/{name}", "mime": _mime(name),
                     "size": (folder / name).stat().st_size})
    return refs


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
