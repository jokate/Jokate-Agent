"""Making a new UE project as a job (M-8): what the making screen may choose (`options`), the check before
anything is started (`admit`), the job's file, the thread that runs the five steps one after the other and tells
of each (`Jobs`), and what becomes of a job that failed — run again from the step that failed (`Jobs.retry`),
or what it made taken away (`Jobs.cleanup`).

Contract: design/studio/ARCHITECTURE.md §6.4 (`POST /studio/projects/create`, `GET /studio/projects/create/options`,
`GET /studio/jobs/{id}`, `POST /studio/jobs/{id}/retry` · `/cleanup`) and §7.4 (`job.step` · `job.log` ·
`job.finished`). The routes are api.py's.

Nothing here imports api, cli or server. A job is the file `<studio_dir>/jobs/<job_id>.json` (`job_<hex8>`) —
it is not in the DB — written (a temporary file replaced into place) before the event that tells of the change.
The steps are the functions of creator.py · plugins.py · harness.py, called by their names in the order of
`STEPS`: changing that tuple changes the order. One job runs at a time (`Jobs._turn`); the others wait as
`pending`. A step that raises is a failed step, not a dead thread.

`job.step` and `job.finished` are stored (`store.emit`); `job.log` is not: it goes to `live(type, data)` — the
hub of ws.py, through api.py — as the whole tail of the step's log, the first line at once and then at most
once every `LOG_EVERY` seconds, and once more at the step's end when something was not sent yet.

A retry makes the request again from the file (`readmit`), so it works after a restart as well; the steps that
are `done` or `skipped` are passed over, and a step that runs again first takes away what its earlier try left
(`created`). A cleanup takes away only what this job made: the paths its steps listed — inside the job's folder,
a link unlinked and never followed — the repository when the name was not registered before the job
(`repo_new`), and the project's row (`project_id`). The job's folder itself goes only when nothing is left in
it (`_delete_root`): what no step listed — a file a person put in — stays, and the folder with it. A file left
`pending` or `running` by a server that stopped is made `failed` by `Jobs.recover`, which the server calls once
when it starts. What a step that was cut off left right under the folder is written as that step's
(`Jobs._leftover`), so a retry or a cleanup can take it away.

The time limit of a command is the runner's (`creator.run_process`). Not here yet: ending the commands a server
that stopped left running.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ..repos import save_local_repo
from . import creator, harness, importer, machine, plugins
from .creator import Refused, Spec, StepResult
from .store import StudioStoreError, now

log = logging.getLogger(__name__)

KIND = "project_create"
STEPS = ("folder", "build", "plugins", "harness", "register")  # §7.4: the order the steps are run in
JOB_ID = re.compile(r"job_[0-9a-f]{8}")
LOG_EVERY = 1.0  # the least seconds between two `job.log` of one step
OPEN = ("pending", "running")  # a job that is not over
SETTLED = ("done", "cleaned")  # a job whose port is no longer its own: a project row has it, or nothing has
PASSED = ("done", "skipped")  # a step that is over: a retry does not run it again
SETTLE = 5.0  # the most seconds a retry or a cleanup waits for the job's own thread, when the file says it is over
RESTARTED = "생성 작업이 중단됐다 (서버가 다시 시작됐다)"  # the error of a job a server that stopped left open
# The reasons this module adds to creator.REASONS and plugins.REASONS (the `reason` of a refusal, §6.4)
REASONS = ("path_taken", "name_taken", "repo_taken", "job_running", "plugins_misconfigured", "harness_unavailable",
           "job_not_failed", "project_in_use")

Live = Callable[..., Any]  # live(type, data): an event that is not stored


# --- the file -----------------------------------------------------------------------------------------------
def folder(studio_dir: Path | str) -> Path:
    return Path(studio_dir) / "jobs"


def new_id(studio_dir: Path | str) -> str:
    while True:
        job_id = "job_" + uuid.uuid4().hex[:8]
        if not (folder(studio_dir) / f"{job_id}.json").exists():
            return job_id


def load(studio_dir: Path | str, job_id: object) -> dict | None:
    """The job, or None: an id of another shape, no such file, a file that is not a job."""
    if not isinstance(job_id, str) or JOB_ID.fullmatch(job_id) is None:
        return None
    try:
        job = json.loads((folder(studio_dir) / f"{job_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return job if isinstance(job, dict) and job.get("id") == job_id else None


def _save(studio_dir: Path | str, job: dict) -> dict:
    """Write the job (UTF-8, a temporary file replaced into place) and return it with `updated_at`."""
    if JOB_ID.fullmatch(str(job.get("id"))) is None:
        raise ValueError(f"a job id is job_<hex8>: {job.get('id')!r}")
    out = {**job, "updated_at": now()}
    path = folder(studio_dir) / f"{out['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8"))
    os.replace(tmp, path)
    return out


def every(studio_dir: Path | str) -> list[dict]:
    """Every job there is a file of, in the order of the ids."""
    try:
        names = sorted(file.stem for file in folder(studio_dir).glob("job_*.json"))
    except OSError:
        return []
    return [job for job in (load(studio_dir, name) for name in names) if job is not None]


def step_event(job: dict, index: int) -> tuple[str, dict]:
    """`job.step` of the job's step `index` (1 and up) as it is in the file (§7.4)."""
    entry = job["steps"][index - 1]
    return "job.step", {"job_id": job["id"], "kind": job["kind"], "step": entry["step"], "index": index,
                        "total": len(job["steps"]), "status": entry["status"], "error": entry["error"]}


def finished_event(job: dict) -> tuple[str, dict]:
    """`job.finished` of the job as it is in the file (§7.4; what studio/push.py reads)."""
    return "job.finished", {"job_id": job["id"], "kind": job["kind"], "status": job["status"],
                            "project_id": job["project_id"], "error": job["error"]}


# --- what may be chosen, and the check ------------------------------------------------------------------------
@dataclass(frozen=True)
class Admitted:
    """A request `admit` let through: the project, the plugins it is given, whether the harness is installed,
    and the request as it is kept in the job's file (what was left out filled in)."""
    spec: Spec
    chosen: list
    harness: bool
    request: dict


def _catalog(entries: Iterable[Any], plugin_root: Any) -> list:
    try:
        return plugins.catalog(entries, plugin_root)
    except ValueError as e:  # the settings are wrong: the owner is the one who can correct them, so the text is theirs
        raise Refused("precondition_failed", "plugins", "plugins_misconfigured", f"플러그인 설정이 틀렸다: {e}") from e


def _harness_trouble(harness_root: Any) -> str:
    """'' when the harness can be installed from `harness_root`, else why not (`harness.locate`)."""
    try:
        harness.locate(harness_root)
    except ValueError as e:
        return str(e)
    return ""


def options(engine_version: Any, *, engines: list[dict], entries: Iterable[Any], plugin_root: Any,
            harness_root: Any) -> dict:
    """What the making screen offers: `{engines[], engine_version, plugins[], harness}`.

    `engines[]`: `machine.engines()` with `usable` and `warning` (why not, '' when usable —
    `creator.engine_problem`). `engine_version`: the engine `plugins[]` is worked out for — the one asked for,
    else the highest usable one, else ''. `plugins[]`: `plugins.choices`. `harness`: `{available, checked,
    warning}` — can it be installed, is it on unless the person says otherwise, and why it cannot be.
    `Refused` (`plugins_misconfigured`) when the list of plugins in the settings is wrong."""
    listed = []
    for engine in engines:
        problem = creator.engine_problem(engine)
        listed.append({"version": engine.get("version"), "path": engine.get("path"),
                       "exists": bool(engine.get("exists")), "usable": problem is None,
                       "warning": problem[1] if problem else ""})
    version = engine_version if isinstance(engine_version, str) and engine_version else next(
        (engine["version"] for engine in reversed(listed) if engine["usable"]), "")
    trouble = _harness_trouble(harness_root)
    return {"engines": listed, "engine_version": version,
            "plugins": plugins.choices(_catalog(entries, plugin_root), version),
            "harness": {"available": not trouble, "checked": not trouble, "warning": trouble}}


def admit(name: Any, engine_version: Any, parent_dir: Any, wanted: Any = None, harness_on: bool | None = None, *,
          engines: list[dict], entries: Iterable[Any], plugin_root: Any, harness_root: Any,
          registry: Any = None, projects: Iterable[dict] = ()) -> Admitted:
    """A request to make a project, checked before anything is made or started; `Refused` for what cannot be.

    In this order: `creator.check` (the name, the parent folder, the folder, the engine); the plugins
    (`plugins.select` — `wanted` None, left out of the request, is what `choices` has checked for the engine;
    `[]` is none); the harness (`harness_on` None is "when it can be installed", True with a harness folder
    that cannot be used is refused: the failure would come minutes later, after the build); then the clashes —
    a project (an archived one too) that has the folder, the name or the repository (`importer.conflict`), and
    a registered repository of that name or that folder (`harness.repo_clash`, when a `registry` is given).
    A clash is `conflict` with the field `path` · `name` · `repo` and the reason `<field>_taken`. The
    `register` step asks the same again when its turn comes."""
    spec = creator.check(name, engine_version, parent_dir, engines=engines)
    listed = _catalog(entries, plugin_root)
    if wanted is None:
        wanted = [choice["id"] for choice in plugins.choices(listed, spec.engine_version) if choice["checked"]]
    chosen = plugins.select(listed, wanted)
    trouble = _harness_trouble(harness_root)
    if harness_on is None:
        harness_on = not trouble
    elif harness_on and trouble:
        raise Refused("precondition_failed", "harness", "harness_unavailable", trouble)
    root = spec.root.resolve()
    clash = importer.conflict(projects, name=spec.name, folder=root, repo=spec.name)
    if clash is None and registry is not None:
        clash = harness.repo_clash(registry, spec.name, root)
    if clash is not None:
        raise Refused("conflict", clash[0], f"{clash[0]}_taken", clash[1])
    request = {"name": spec.name, "engine_version": spec.engine_version, "parent_dir": str(spec.parent_dir),
               "plugins": [plugin.id for plugin in chosen], "harness": bool(harness_on)}
    return Admitted(spec, chosen, bool(harness_on), request)


def readmit(job: dict, *, entries: Iterable[Any], plugin_root: Any) -> Admitted:
    """The `Admitted` of a job that was let through once, made again from its file for a retry: the `request`
    and `engine_dir` it was written with. `creator.check` is not asked again — the folder is there by now, and
    it would refuse it — so this works after a restart as well. `Refused` when the settings no longer have a
    plugin the job chose (M-4's reasons) or are wrong (`plugins_misconfigured`)."""
    request = dict(job["request"])
    spec = Spec(request["name"], request["engine_version"], Path(job["engine_dir"]), Path(request["parent_dir"]))
    chosen = plugins.select(_catalog(entries, plugin_root), request["plugins"])
    return Admitted(spec, chosen, bool(request["harness"]), request)


# --- taking away what was made --------------------------------------------------------------------------------
_JUNCTION = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def _is_link(path: Any) -> bool:
    """A symbolic link or a junction, told by `lstat` (`os.path.isjunction` is 3.12's; this runs on 3.11 too).
    What is not there is no link."""
    try:
        seen = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(seen.st_mode) or getattr(seen, "st_reparse_tag", 0) == _JUNCTION


def _delete(path: Path) -> None:
    """Take one thing away. A link (a junction too) is unlinked and what it points at is left; a folder goes
    with all that is in it, a read-only file as well; what is not there is gone already. `OSError` when it
    could not be."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if _is_link(path):
        os.unlink(path)
    elif stat.S_ISDIR(mode):
        try:
            shutil.rmtree(path)
        except OSError:  # a read-only file (a copied plugin, an object of `.git`): the mark off, and once more
            plugins._unlock(path)
            shutil.rmtree(path)
    else:
        try:
            path.unlink()
        except PermissionError:
            os.chmod(path, stat.S_IMODE(mode) | stat.S_IWRITE)
            path.unlink()


def _delete_root(root: Path) -> None:
    """Take the job's own folder away — only when nothing is left in it. The folders in it that are empty go
    first, the deepest first (a step lists the files it wrote, not the folders they are in); a link is not
    gone into. `OSError`, naming up to five of them, when something is left: no step listed it, so it is not
    this job's to delete — a file a person put in, what the editor wrote — and the folder stays with it."""
    def prune(folder: Path) -> None:
        for item in creator._entries(folder):
            if not _is_link(item) and item.is_dir():
                prune(item)
                try:
                    item.rmdir()
                except OSError:  # something is in it
                    pass

    prune(root)
    rest = sorted(item.name for item in creator._entries(root))
    if rest:
        more = f" 외 {len(rest) - 5}개" if len(rest) > 5 else ""
        raise OSError("이 생성 작업이 만들지 않은 것이 남아 있어 폴더를 지우지 않았다 "
                      f"(남은 것: {', '.join(rest[:5])}{more})")
    try:
        root.rmdir()
    except PermissionError:  # a folder marked read-only
        os.chmod(root, stat.S_IMODE(root.lstat().st_mode) | stat.S_IWRITE)
        root.rmdir()


def _with(listed: Iterable[Any], more: Iterable[Any]) -> list[str]:
    """`listed`, and after it what of `more` is not in it (compared as paths are)."""
    both = [str(path) for path in listed]
    seen = {machine._same(path) for path in both}
    return both + [str(path) for path in more if machine._same(path) not in seen]


def _remove(paths: Iterable[Any], root: Any) -> tuple[list[str], list[str]]:
    """Take away what a step said it made, the last first; the answer is `(what is left, why)`, both empty
    when all is gone. Only `root` — the job's folder, as written or with its links resolved, which is how
    `register` names what it made — and what is under it is touched: the paths are compared as text and a
    path with `..` is left, so nothing outside the folder is ever deleted. A listed folder goes with all that
    is in it; `root` itself only when it is empty (`_delete_root`). A path outside and one that could not be
    deleted stay in the list."""
    homes = {machine._same(root), machine._same(Path(str(root)).resolve())} - {""} if root else set()
    left, errors = [], []
    for text in reversed([str(path) for path in paths]):
        here = machine._same(text)
        if ".." in Path(text).parts or not any(here == home or here.startswith(home + "/") for home in homes):
            left.append(text)
            errors.append(f"생성 작업의 폴더 밖이라 지우지 않는다: {text}")
            continue
        target = Path(text)
        try:
            if here in homes and not _is_link(target) and target.is_dir():
                _delete_root(target)
            else:
                _delete(target)
        except OSError as e:
            left.append(text)
            errors.append(f"{text}: {e}")
    return left[::-1], errors


# --- the log of a step ----------------------------------------------------------------------------------------
class _Tail:
    """The last lines of a step's log, and when they were last sent as `job.log`."""

    def __init__(self, live: Live | None, job_id: str, step: str):
        self.live, self.job_id, self.step = live, job_id, step
        self.lines: deque[str] = deque(maxlen=creator.TAIL_LINES)
        self.sent_at: float | None = None
        self.unsent = False

    def hear(self, line: str) -> None:
        self.lines.append(str(line))
        self.unsent = True
        if self.sent_at is None or time.monotonic() - self.sent_at >= LOG_EVERY:
            self.flush()

    def flush(self) -> None:
        if not self.unsent:
            return
        self.unsent, self.sent_at = False, time.monotonic()
        if self.live is None:
            return
        try:
            self.live("job.log", {"job_id": self.job_id, "step": self.step, "lines": list(self.lines)})
        except Exception:  # noqa: BLE001 - a log nobody could be told of does not stop the step
            log.debug("studio: 생성 작업 %s 의 job.log 를 보내지 못했다", self.job_id, exc_info=True)


# --- the jobs -------------------------------------------------------------------------------------------------
class Jobs:
    """The jobs of this server. `local_file` and `reload` are `harness.register`'s — the settings file a
    repository is written in and the function that makes the server read it again and gives its RepoRegistry;
    `harness_root` is a function giving the setting as it is now; `run` runs a command (`creator.run_process`);
    `is_open` says whether a port is in use on this computer; `live(type, data)` hears `job.log`."""

    def __init__(self, studio_dir: Path | str, *, local_file: Path | str, reload: Callable[[], Any],
                 harness_root: Callable[[], Any], run: creator.Run = creator.run_process,
                 is_open: Callable[[int], bool] = machine.port_open, live: Live | None = None,
                 on_registered: Callable[[Any, dict], None] | None = None):
        self.studio_dir = Path(studio_dir)
        self.on_registered = on_registered  # (store, project row): asked once for a row this job just made
        self.local_file, self.reload, self.harness_root = Path(local_file), reload, harness_root
        self.run, self.is_open, self.live = run, is_open, live
        self._turn = threading.Lock()  # held by the job that runs: the UE build is heavy, and one port at a time
        self._lock = threading.Lock()  # the two dicts below
        self._threads: dict[str, threading.Thread] = {}
        self._open: dict[str, tuple[str, str]] = {}  # job id -> (the name in lower case, the folder as compared)

    # --- reading
    def get(self, job_id: object) -> dict | None:
        return load(self.studio_dir, job_id)

    def unfinished(self) -> list[dict]:
        """The jobs of this process that wait or run."""
        with self._lock:
            ids = list(self._open)
        return [job for job in (load(self.studio_dir, job_id) for job_id in ids) if job is not None]

    def ports(self, but: str | None = None) -> list[int]:
        """The MCP ports the jobs hold: of every job file that is not `done` (the project's row has it then)
        or `cleaned`, the job `but` aside. A failed job keeps its port — it is written in its folder, and a
        retry registers the project with it."""
        return [job["mcp_port"] for job in every(self.studio_dir)
                if job["id"] != but and job.get("status") not in SETTLED and isinstance(job.get("mcp_port"), int)]

    def wait(self, job_id: str | None = None, timeout: float = 30.0) -> bool:
        """Wait until the job's thread (every thread, when no job is named) has ended. False: it has not."""
        with self._lock:
            threads = [thread for name, thread in self._threads.items() if job_id in (None, name)]
        end = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, end - time.monotonic()))
        return not any(thread.is_alive() for thread in threads)

    # --- starting
    def create(self, store: Any, admitted: Admitted) -> dict:
        """Write the job as `pending`, start its thread and return it. `store` is the one the thread writes
        its events and the project's row with, to its end. `Refused` (`conflict`, `job_running`) when a job
        that is not over makes a project of the same name or folder."""
        spec = admitted.spec
        mine = (spec.name.lower(), machine._same(spec.root))
        with self._lock:
            self._claim(None, mine)
            at = now()
            job = _save(self.studio_dir, {
                "id": new_id(self.studio_dir), "kind": KIND, "status": "pending",
                "steps": [{"step": step, "status": "pending", "error": None, "tail": [], "created": [],
                           "started_at": None, "ended_at": None} for step in STEPS],
                "project_id": None, "error": None, "request": dict(admitted.request),
                "engine_dir": str(spec.engine_dir), "path": str(spec.root), "repo": None, "repo_new": None,
                "mcp_port": None, "created_at": at, "updated_at": at, "finished_at": None})
            thread = threading.Thread(target=self._work, args=(store, job["id"], admitted), daemon=True,
                                      name=f"katae-studio-{job['id']}")
            self._open[job["id"]], self._threads[job["id"]] = mine, thread
        thread.start()
        return job

    def _claim(self, job_id: str | None, mine: tuple[str, str]) -> None:
        """`Refused` (`conflict`, `job_running`) when the job itself or another job of that name or folder is
        not over. Called with `_lock` held."""
        for other, (name, root) in self._open.items():
            if other == job_id or name == mine[0] or root == mine[1]:
                raise Refused("conflict", "name", "job_running",
                              f"같은 이름 · 폴더의 프로젝트를 만드는 작업이 아직 끝나지 않았다: {other}")

    def _settle(self, job: dict) -> None:
        """Wait a little for the thread of a job whose file says it is over: it still has its last events to
        write and its place in `_open` to leave."""
        if job.get("status") in OPEN:
            return
        with self._lock:
            thread = self._threads.get(job["id"])
        if thread is not None and thread is not threading.current_thread():
            thread.join(SETTLE)

    @staticmethod
    def _failed(job: dict, what: str) -> None:
        if job.get("status") != "failed":
            raise Refused("precondition_failed", "status", "job_not_failed",
                          f"실패한 생성 작업만 {what}할 수 있다 (지금 상태: {job.get('status')})")

    # --- a job that failed
    def retry(self, store: Any, job_id: object, *, entries: Iterable[Any], plugin_root: Any) -> dict | None:
        """Run a failed job again from the step that failed: the job as `pending`, its thread started — or
        None when there is no such job. The steps keep what they have; `_run` passes over those that are over.
        `Refused`: the job is not `failed` (`precondition_failed`, `job_not_failed`), the settings no longer
        have what it chose (`readmit`), or it — or another job of its name or folder — is not over, a cleanup
        of it included (`conflict`, `job_running`)."""
        job = load(self.studio_dir, job_id)
        if job is None:
            return None
        self._settle(job)
        self._failed(job, "다시 시도")
        admitted = readmit(job, entries=entries, plugin_root=plugin_root)
        mine = (admitted.spec.name.lower(), machine._same(admitted.spec.root))
        with self._lock:
            self._claim(job["id"], mine)
            job = load(self.studio_dir, job_id)  # as it is now: another retry may have come and gone
            if job is None:
                return None
            self._failed(job, "다시 시도")
            job = self._adopt(store, job)  # after the claim, in the lock: a cleanup's file is never written over
            job = _save(self.studio_dir, {**job, "status": "pending", "error": None, "finished_at": None})
            thread = threading.Thread(target=self._work, args=(store, job["id"], admitted), daemon=True,
                                      name=f"katae-studio-{job['id']}")
            self._open[job["id"]], self._threads[job["id"]] = mine, thread
        thread.start()
        return job

    def cleanup(self, store: Any, job_id: object) -> dict | None:
        """Take away what a failed job made, on the caller's thread, and answer the job — None when there is
        no such job. Only what is this job's: the project's row (`project_id`), the repository when the name
        was not registered before the job (`repo_new`) and still names this folder, and the paths its steps
        listed (`_remove`) — a folder among them goes with all that is in it, and a file a step listed goes
        even when a person changed it since. The project's folder itself goes only when it is empty: what no
        step listed (a file a person put in after the failure, what the editor wrote) is left, and the folder
        with it.

        When all is gone the job is `cleaned` and `job.finished` tells of it, its `project_id` the id of the
        row that was taken out. When something could not be taken away — or was left in the project's folder,
        which then stays — the job stays `failed`, `error` says
        what is left, `created` keeps it, every step is `pending` again — a retry then makes all of it anew —
        and no event is written. `Refused`: the job is not `failed` (`precondition_failed`, `job_not_failed`);
        something still names the project's row (`precondition_failed`, `project_in_use` — nothing was taken
        away); a job is running, or this one is being retried or cleaned (`conflict`, `job_running`): a
        cleanup does not wait for its turn, and none runs beside a step."""
        job = load(self.studio_dir, job_id)
        if job is None:
            return None
        self._settle(job)
        self._failed(job, "정리")
        if not self._turn.acquire(blocking=False):
            raise Refused("conflict", "status", "job_running", "생성 작업이 돌고 있어 지금은 정리할 수 없다")
        try:
            with self._lock:
                if job["id"] in self._open:
                    raise Refused("conflict", "status", "job_running",
                                  f"이 생성 작업은 아직 끝나지 않았다: {job['id']}")
                job = load(self.studio_dir, job_id)
                if job is None:
                    return None
                self._failed(job, "정리")
                self._open[job["id"]] = (str(job["request"]["name"]).lower(), machine._same(job["path"]))
            try:
                return self._clean(store, job)
            finally:
                with self._lock:
                    self._open.pop(job["id"], None)
        finally:
            self._turn.release()

    def _clean(self, store: Any, job: dict) -> dict:
        job = self._adopt(store, job)
        if job.get("project_id"):
            try:
                store.delete_project(job["project_id"])  # False: taken out by an earlier cleanup
            except sqlite3.IntegrityError as e:
                raise Refused("precondition_failed", "project_id", "project_in_use",
                              f"프로젝트 {job['project_id']} 에 딸린 것(직원 · 작업 · 방 등)이 있어 아무것도 "
                              f"지우지 않았다") from e
        errors = []
        if job.get("repo_new") is True:
            trouble = self._unregister(job)
            if trouble:
                errors.append(trouble)
        for entry in reversed(job["steps"]):
            entry["created"], said = _remove(entry["created"], job["path"])
            errors += said
        if errors:
            for entry in job["steps"]:  # nothing counts as made any more: a retry starts from the first step
                entry["status"] = "pending"
            return _save(self.studio_dir, {**job, "error": "정리하지 못한 것이 있다: " + " / ".join(errors)})
        job = _save(self.studio_dir, {**job, "status": "cleaned", "error": None, "finished_at": now()})
        self._emit(store, job, [finished_event(job)])
        return job

    def _unregister(self, job: dict) -> str:
        """Take the job's repository out of the settings file; '' or why it is still there. One that is not
        registered, or names another folder by now, is not this job's to take out."""
        name = job["request"]["name"]
        try:
            seen = harness._reloaded(self.reload).repos.get(name)
            if seen is None or machine._same(seen.resolved) != machine._same(Path(job["path"]).resolve()):
                return ""
            save_local_repo(self.local_file, name, None, remove=True)
            if name in harness._reloaded(self.reload).repos:
                return f"저장소 등록 '{name}' 이(가) 남아 있다 ({self.local_file} 밖에 적힌 등록이다)"
        except Exception as e:  # noqa: BLE001 - `reload` is the caller's code, and the file is the person's
            return f"저장소 등록 '{name}' 을(를) 지우지 못했다 ({type(e).__name__}): {e}"
        return ""

    def _adopt(self, store: Any, job: dict) -> dict:
        """A job cut off in `register` may have put its row in without the file saying so. That row — of the
        job's name and folder, made after the step started — is written in the file as the job's (`project_id`,
        `repo`). Only for a step that was cut off (its error is the job's own): one that failed by itself put
        no row in, and a row of that name is somebody else's. It writes the file, so only who holds the job
        calls it: a retry after its `_claim`, in `_lock`; a cleanup once the job is in `_open`."""
        entry = job["steps"][STEPS.index("register")]
        if job.get("project_id") or entry["status"] != "failed" or not entry["started_at"] \
                or entry["error"] != job.get("error"):
            return job
        name = str(job["request"]["name"])
        homes = {machine._same(job["path"]), machine._same(Path(job["path"]).resolve())}
        for row in store.list_projects(include_archived=True):
            if row["name"] == name and machine._same(row["path"]) in homes \
                    and str(row.get("created_at") or "") >= entry["started_at"]:
                job.update(project_id=row["id"], repo=row["repo"])
                job.update(_save(self.studio_dir, job))
                break
        return job

    def recover(self, store_factory: Callable[[], Any] | None = None) -> list[dict]:
        """What the server does once when it starts: every job file that is `pending` or `running` and is no
        job of this process was left by a server that stopped. It is made `failed` (`RESTARTED`; the step that
        was running too, with what it left in the folder as its `created` — `_interrupt`), so it can be
        retried or cleaned. The answer is the jobs that were changed. Their
        `job.step` and `job.finished` are written with the store `store_factory()` gives — asked for only when
        something was changed; without a store the files are changed all the same and it is logged."""
        changed: list[tuple[dict, list[int]]] = []
        for found in every(self.studio_dir):
            if found.get("status") not in OPEN:
                continue
            with self._lock:
                job = load(self.studio_dir, found["id"])
                if job is None or job.get("status") not in OPEN or job["id"] in self._open:
                    continue
                try:
                    changed.append(self._interrupt(job, RESTARTED))
                except Exception:  # noqa: BLE001 - one file that cannot be written does not keep the others open
                    log.exception("studio: 중단된 생성 작업 %s 을(를) 실패로 적지 못했다", job["id"])
        if changed and store_factory is not None:
            try:
                store = store_factory()
            except Exception:  # noqa: BLE001 - the files say it; the stream does not
                log.exception("studio: 중단된 생성 작업 %d개의 이벤트를 쓰지 못했다", len(changed))
            else:
                for job, stopped in changed:
                    self._emit(store, job, [step_event(job, index) for index in stopped] + [finished_event(job)])
        return [job for job, _ in changed]

    # --- the thread
    def _work(self, store: Any, job_id: str, admitted: Admitted) -> None:
        try:
            with self._turn:
                self._run(store, job_id, admitted)
        except Exception as e:  # noqa: BLE001 - e.g. the job's file could not be written: the job is over
            log.exception("studio: 생성 작업 %s 이(가) 중단됐다", job_id)
            self._abandon(store, job_id, e)
        finally:
            with self._lock:
                self._open.pop(job_id, None)
                self._threads.pop(job_id, None)

    def _run(self, store: Any, job_id: str, admitted: Admitted) -> None:
        job = load(self.studio_dir, job_id)
        if job is None:
            raise ValueError(f"생성 작업의 파일을 읽지 못했다: {job_id}")
        job = _save(self.studio_dir, {**job, "status": "running"})
        for index, name in enumerate(STEPS, 1):
            entry = job["steps"][index - 1]
            if entry["status"] in PASSED:  # a retry: the step is over, and nothing tells of it again
                continue
            earlier = list(entry["created"])  # what a try before this one left
            entry.update(status="running", error=None, tail=[], started_at=now(), ended_at=None)
            job = _save(self.studio_dir, job)
            self._emit(store, job, [step_event(job, index)])
            tail = _Tail(self.live, job_id, name)
            try:
                # a `register` whose row is in ran to its end (the row is the last thing it makes): what it
                # made — the `.git`, the two files — stays, and `_step` does not make it again
                kept = name == "register" and self._row(store, job) is not None
                left, said = ([], []) if kept else _remove(earlier, job["path"])
                if said:  # the step would fail on what is in its way, or leave a list that is not true
                    result = StepResult(name, created=left,
                                        error="앞선 시도가 만든 것을 지우지 못했다: " + " / ".join(said))
                    tail.hear(result.error)
                else:
                    if name == "folder" and not os.path.lexists(admitted.spec.root):
                        # written before the step: a step cut off while it writes has the folder as its own.
                        # One that is there already is somebody else's and is never listed
                        entry["created"] = [str(admitted.spec.root)]
                        job = _save(self.studio_dir, job)
                    if name == "register" and job.get("repo_new") is None:
                        # written before the step: a job cut off in it still knows whose the repository is
                        known = harness._reloaded(self.reload)
                        job["repo_new"] = not any(repo.name == admitted.spec.name for repo in known.repos.values())
                        job = _save(self.studio_dir, job)
                    result = self._step(name, store, job, admitted, tail.hear)
            except Exception as e:  # noqa: BLE001 - a bug of a step is that step's failure, not a dead thread
                log.exception("studio: 생성 작업 %s 의 %s 단계가 예외로 끝났다", job_id, name)
                result = StepResult(name, created=_with(entry["created"], self._leftover(job)),
                                    error=f"{name} 단계가 예외로 끝났다 ({type(e).__name__}): {e}")
                tail.hear(result.error)
            tail.flush()
            status = "failed" if result.error else "skipped" if result.skipped else "done"
            entry.update(status=status, error=result.error or None, tail=list(result.tail) or list(tail.lines),
                         created=[str(path) for path in result.created], ended_at=now())
            if getattr(result, "mcp_port", None) is not None:
                job["mcp_port"] = result.mcp_port
            if getattr(result, "repo", ""):
                job["repo"] = result.repo
            project = getattr(result, "project", None)
            if project:
                job["project_id"] = project["id"]
            events = [step_event(job, index)]
            if status == "failed" or index == len(STEPS):
                job.update(status="failed" if status == "failed" else "done", finished_at=now(),
                           error=f"{name}: {result.error}" if status == "failed" else None)
                events.append(finished_event(job))
            job = _save(self.studio_dir, job)
            self._emit(store, job, events)
            if name == "register" and status == "done" and project and not kept and self.on_registered is not None:
                try:  # the project is committed: what the owner asked for does not depend on this
                    self.on_registered(store, project)
                except Exception:  # noqa: BLE001
                    log.exception("studio: 생성 작업 %s 의 등록 뒤 처리가 실패했다", job_id)
            if status == "failed":
                return
        if job["status"] == "running":  # a retry that found the last step over already
            job = _save(self.studio_dir, {**job, "status": "done", "error": None, "finished_at": now()})
            self._emit(store, job, [finished_event(job)])

    def _step(self, name: str, store: Any, job: dict, admitted: Admitted, hear: creator.Log) -> StepResult:
        spec = admitted.spec
        if name == "folder":
            return creator.folder(spec, run=self.run, log=hear)
        if name == "build":
            return creator.build(spec, run=self.run, log=hear)
        if name == "plugins":
            taken = [row["mcp_port"] for row in store.list_projects(include_archived=True)]
            return plugins.step(spec, admitted.chosen, taken=taken + self.ports(but=job["id"]),
                                is_open=self.is_open, run=self.run, log=hear)
        if name == "harness":
            return harness.harness(spec, self.harness_root(), enabled=admitted.harness, run=self.run, log=hear)
        if name == "register":
            row = self._row(store, job)
            if row is not None:  # a try that was cut off after the row went in (`_adopt`): it is not made twice
                line = f"프로젝트 등록: {row['id']} {row['name']} (앞선 시도가 넣은 행)"
                hear(line)
                made = list(job["steps"][STEPS.index(name)]["created"])  # what that try left: `_run` kept it
                return harness.RegisterResult(name, created=made, tail=[line], repo=row["repo"], project=row)
            server = next((plugin.mcp.name for plugin in admitted.chosen if plugin.mcp is not None),
                          plugins.DEFAULT_SERVER)
            return harness.register(spec, store, local_file=self.local_file, reload=self.reload, mcp=(server,),
                                    run=self.run, log=hear)
        raise ValueError(f"모르는 단계다: {name}")

    def _emit(self, store: Any, job: dict, events: list[tuple[str, dict]]) -> None:
        """The events of one change, in one transaction, after the file was written. A store that cannot be
        written (closed under the job) is logged: the file is what `GET /studio/jobs/{id}` reads."""
        try:
            with store.transaction() as tx:
                for kind, data in events:
                    store.emit(tx, kind, data, project_id=job["project_id"])
        except (StudioStoreError, sqlite3.Error):
            log.exception("studio: 생성 작업 %s 의 이벤트를 쓰지 못했다", job["id"])

    def _abandon(self, store: Any, job_id: str, error: Exception) -> None:
        """The job could not go on (not a step's failure — that is `_run`'s): it is `failed`, as far as the
        file can still be written. A step that was running is `failed` with the job's own error, and its
        `job.step` goes before `job.finished` in one transaction (§7.4: no step is left `running`)."""
        try:
            job = load(self.studio_dir, job_id)
            if job is None or job.get("status") not in OPEN:
                return
            job, stopped = self._interrupt(job, f"생성 작업이 중단됐다 ({type(error).__name__}): {error}")
        except Exception:  # noqa: BLE001 - nothing more can be done for it here
            log.exception("studio: 중단된 생성 작업 %s 을(를) 실패로 적지 못했다", job_id)
            return
        self._emit(store, job, [step_event(job, index) for index in stopped] + [finished_event(job)])

    @staticmethod
    def _row(store: Any, job: dict) -> dict | None:
        """The project's row the job's file names, when it is there."""
        return store.get_project(job["project_id"]) if job.get("project_id") else None

    def _leftover(self, job: dict) -> list[str]:
        """What is right under the job's folder and in no step's `created` (nor above a path that is) — what
        a step that was cut off left, as far as it can be told. Only when the `folder` step listed the folder
        as made by this job (`_run` writes that before the step): in a folder that was there before, nothing
        is taken for the job's. [] when the folder cannot be read."""
        try:
            root = Path(job["path"])
            homes = {machine._same(root), machine._same(root.resolve())} - {""}
            if not any(machine._same(path) in homes for path in job["steps"][STEPS.index("folder")]["created"]):
                return []
            listed = [machine._same(path) for entry in job["steps"] for path in entry["created"]]
            heads = {here[len(home) + 1:].split("/")[0] for here in listed for home in homes
                     if here.startswith(home + "/")}
            return sorted(str(item) for item in creator._entries(root) if machine._same(item.name) not in heads)
        except Exception:  # noqa: BLE001 - a list that could not be made keeps no job from being written as failed
            log.exception("studio: 생성 작업 %s 의 폴더에 남은 것을 읽지 못했다", job.get("id"))
            return []

    def _interrupt(self, job: dict, text: str) -> tuple[dict, list[int]]:
        """Write the job as `failed` with `text`, the step that was running too; `(the job, the indexes of
        the steps that were running)`. That step's `created` gets what it left (`_leftover`): its own list
        was never written."""
        at, stopped = now(), []
        for index, entry in enumerate(job["steps"], 1):
            if entry["status"] == "running":
                entry.update(status="failed", error=text, ended_at=at,
                             created=_with(entry["created"], self._leftover(job)))
                stopped.append(index)
        return _save(self.studio_dir, {**job, "status": "failed", "error": text, "finished_at": at}), stopped
