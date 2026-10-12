"""The final review (S-12): what the owner reads before a pass, a rework or a revert (part 1), and what the
decision then does outside the studio DB (part 2).

Contract: design/studio/ARCHITECTURE.md §5.4 (`package`), §4.3 T6–T9 · T11 and §4.5 (the conditions, the order of
a pass and of a revert, the codes of `failed[]`), §6.4 (`run` of a task, the aliases of a run's patch and
tokens), §11.2 (`cleanup_workspaces`, the commit of a pass).

Part 1. T6 stores what the employee handed in — the summary, the checklist, the evidence, the judgment calls (the
payload; studio/inbox.py). Everything else is computed when the item is read, from the run as it is then: after
T6 the run still does its remaining stages, so its patch, its cost and the rows of the guard go on changing.

- `run_view` — `{id, status, changes_status}`: the `run` of `GET /studio/tasks/{id}`.
- `package` — the eleven keys of the second table of §5.4, for `GET /studio/inbox/{id}` of a `final_review`.
- `held_runs` · `held_runs_ro` — the runs of the tasks under review or blocked: `RelayEngine.cleanup_workspaces`
  keeps their workspaces past the retention period, so a review — or an approval — that takes long can still be
  reverted (T9 · T11).

Part 2. One judge, `gate`: does the task take a pass, a rework, a revert now — and if not, by which code of
`failed[]`. `can` of the package reads it, and the decision asks it again where it is made (studio/work.py
`WorkEffects.check` · `before`). What a decision then does to the run:
- `close_run` — first: a run that is neither `done` nor `cancelled` is cancelled (§4.3 '런 정리');
- `passed` (T7) — the changes of a `copy` run are applied to the original, then `commit`: one git commit of the
  project folder, never a push. An `inplace` run is marked kept after it (`RelayEngine.keep_changes`);
- `reverted` (T9 · T11) — `discard_changes` of a `copy` run, `rollback_changes` of an `inplace` one. A run the
  engine has no more (`lost`) is not reverted: the revert · cancel is taken as it is and says so (`LOST`).
A rework (T8) does nothing here: its text goes into the run by `Inbox.deliver`, and a run that is `done` is
opened again where the text goes in (studio/runs.py `EngineHooks.interject`, `RelayEngine.reopen`).

No FastAPI here and no engine of its own: every function is handed the engine it reads (as studio/runs.py
`workdir` is) — the server's engine is a module variable a test replaces, so nothing here keeps the object.
Part 1 writes nothing.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

from ..journal import is_asset
from .store import StudioStore
from .tasks import TransitionError

log = logging.getLogger(__name__)

PACKAGE_KEYS = ("run", "run_stopped", "changes", "assets", "screenshots", "harness_checks", "out_of_scope",
                "cost", "incidents", "can", "followups")  # §5.4, in the order of its table
FOLLOWUP = ("id", "title", "status", "type", "assignee_id", "is_milestone")  # tools.LISTED: a row of task_list
INCIDENT_KINDS = ("guard", "limit", "run")  # §4.5: the task_events the package carries
SKIPPED = "stage_skipped"  # work.SKIPPED: the engine's event a `run` row of a skipped stage names
DECISIONS = ("pass", "rework", "revert")
# §4.3 'T5 · T7 · T8 · T9 · T11 의 failed[]': what `gate` and `close_run` name, in the order they are looked at
CODES = ("run_gone", "run_running", "run_held", "changes_applied", "changes_reverted", "snapshot_gone",
         "folder_busy", "others_started")
ENDED = ("done", "cancelled")           # the runs whose changes the engine applies and reverts (`_changes_action`)
REVERTED = ("discarded", "rolled_back")
HELD = ("review", "blocked")            # the states whose run the cleanup keeps (`held_runs`)
REWORK = "재작업으로 고친다"             # what `gate` points to instead of a revert that cannot be done
GONE = "런을 읽을 수 없다 — 엔진에 그 런이 없거나 엔진이 연결되지 않았다"
# what the transition row of a revert · cancel says after its reason when the run was `lost` (§4.3 `run_gone` 의 예외)
LOST = "런이 엔진에 없어 변경을 되돌리지 못했다 — 폴더에 남은 변경은 직접 확인한다"
GIT_TIMEOUT_S = 120
_ESCAPES = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r"}


# --- the run --------------------------------------------------------------------------------------------------
def _load(engine: Any, run_id: str | None) -> Any:
    """The run as the engine has it now. None: no engine, no id, no such run, or a run.json nobody can read."""
    if engine is None or not run_id:
        return None
    try:
        return engine.load(run_id)
    except (OSError, ValueError):  # FileNotFoundError: no run.json under that id; ValueError: not a RunState
        return None


def lost(engine: Any, run_id: str | None) -> bool:
    """Is that run gone from the engine for good — no run.json under its id (`RelayEngine.load` raises
    FileNotFoundError)? Nothing can apply or revert the changes of such a run, so its task is let out of
    `review` · `blocked` by a revert · cancel that reverts nothing (§4.3 `run_gone` 의 예외). A run.json that is
    there and cannot be read — locked, broken — is not lost: its snapshot may well be there. False as well
    without an engine or an id."""
    if engine is None or not run_id:
        return False
    try:
        engine.load(run_id)
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return False


def run_view(engine: Any, run_id: str | None) -> dict | None:
    """`run` of `GET /studio/tasks/{id}` (§6.4): three values of the RunState, or None when the engine cannot
    read that run."""
    run = _load(engine, run_id)
    return None if run is None else {"id": run.id, "status": run.status, "changes_status": run.changes_status}


def run_cost(engine: Any, run_id: str | None) -> float:
    """What that run has cost so far in USD (`RunState.cost_usd`), for `GET /studio/stats` (§6.4). 0.0 when
    the engine cannot read the run."""
    run = _load(engine, run_id)
    try:
        return float(run.cost_usd or 0.0) if run is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# --- result.patch -> files[] ------------------------------------------------------------------------------------
def _unquote(text: str) -> str:
    """A path as git writes it when it holds a quote, a control character or a byte outside ASCII: in double
    quotes, C-escaped, the bytes of UTF-8 in octal (`core.quotePath`). Any other text comes back as it is."""
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    body, out, i = text[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 >= len(body):
            out += ch.encode("utf-8")
            i += 1
        elif body[i + 1] in "01234567":
            j = i + 1
            while j < len(body) and j < i + 4 and body[j] in "01234567":
                j += 1
            out.append(int(body[i + 1:j], 8) & 0xFF)
            i = j
        else:
            out += _ESCAPES.get(body[i + 1], body[i + 1]).encode("utf-8")
            i += 2
    return out.decode("utf-8", "replace")


def _side(text: str) -> str | None:
    """One side of a block — after `--- ` · `+++ `, or a half of the header: unquoted, without the `a/` · `b/`
    in front. None for /dev/null. git ends an unquoted name that holds a space with a tab."""
    text = text.strip() if text.lstrip().startswith('"') else text.split("\t", 1)[0]
    path = _unquote(text)
    if path == "/dev/null":
        return None
    return path[2:] if path[:2] in ("a/", "b/") else path


def _header_path(rest: str) -> str | None:
    """The path `diff --git <a> <b>` names. The block of a file that only came or went empty, or only changed
    its mode, has no `---` · `+++` line, so this is all there is. An unquoted name may hold spaces — the two
    sides are the same path, so the split that makes them equal is the one."""
    rest = rest.strip()
    if rest.startswith('"'):
        end = 1
        while end < len(rest) and rest[end] != '"':
            end += 2 if rest[end] == "\\" else 1
        second = rest[end + 1:].strip()
        return _side(second or rest[:end + 1])
    at = rest.find(" b/")
    while at >= 0:
        if rest[2:at] == rest[at + 3:]:
            return rest[at + 3:]
        at = rest.find(" b/", at + 1)
    return _side(rest.rpartition(" ")[2]) if rest else None


def patch_files(patch: str) -> list[dict]:
    """The files of a result.patch, one per `diff --git` block and in its order: `{path, status, insertions,
    deletions, binary}`, `status` one of added · modified · deleted. Both kinds of workspace write the same
    headers (`Workspace.collect`, `JournalWorkspace.collect`); `RunState.changes` has the count alone, and its
    `stat` text differs between the two. A renamed file is `modified` under its new name."""
    files: list[dict] = []
    cur: dict | None = None
    in_hunk = False
    for raw in patch.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if line.startswith("diff --git "):
            cur = {"path": _header_path(line[len("diff --git "):]) or "", "status": "modified",
                   "insertions": 0, "deletions": 0, "binary": False}
            files.append(cur)
            in_hunk = False
        elif cur is None:
            continue
        elif in_hunk:  # every line down to the next block is the file's text: `+++ x` is an added `++ x` here
            if raw.startswith("+"):
                cur["insertions"] += 1
            elif raw.startswith("-"):
                cur["deletions"] += 1
        elif line.startswith("@@"):
            in_hunk = True
        elif line.startswith("new file mode"):
            cur["status"] = "added"
        elif line.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif line.startswith("rename to "):
            cur["path"] = _unquote(line[len("rename to "):].strip()) or cur["path"]
        elif line.startswith("+++ "):
            cur["path"] = _side(line[4:]) or cur["path"]
        elif line.startswith("--- "):
            if cur["status"] == "deleted":
                cur["path"] = _side(line[4:]) or cur["path"]
        elif line.startswith("GIT binary patch") or (line.startswith("Binary files ") and line.endswith(" differ")):
            cur["binary"] = True
    return files


def _journal_list(run: Any) -> list[dict] | None:
    """`RunState.changes["binary"]`: what `JournalWorkspace.collect` (journal.py) listed and did not diff — a
    binary file, or one whose original it has not. None for a run whose workspace keeps no journal."""
    listed = (getattr(run, "changes", None) or {}).get("binary")
    return [entry for entry in listed if isinstance(entry, dict) and entry.get("path")] \
        if isinstance(listed, list) else None


def changed_files(run: Any, patch: str) -> list[dict]:
    """`changes.files[]`: the blocks of the patch, then — an `inplace` run — what its journal listed without a
    diff, as `binary`."""
    files = patch_files(patch)
    seen = {f["path"] for f in files}
    for entry in _journal_list(run) or []:
        if entry["path"] in seen:
            continue
        status = ("deleted" if entry.get("after_bytes") is None
                  else "added" if entry.get("source") == "absent" else "modified")
        files.append({"path": entry["path"], "status": status, "insertions": 0, "deletions": 0, "binary": True})
    return files


def assets(run: Any, files: list[dict]) -> list[dict]:
    """`assets[]`, each `{path, asset, before_bytes, after_bytes, restorable, source}`. An `inplace` run: its
    journal's list as it is. A run without a journal (`copy`): the files of the patch that are binary or have
    an asset's extension — no sizes, and the original is the snapshot's."""
    listed = _journal_list(run)
    if listed is not None:
        return [dict(entry) for entry in listed]
    return [{"path": f["path"], "asset": is_asset(f["path"]), "before_bytes": None, "after_bytes": None,
             "restorable": True, "source": "snapshot"} for f in files if f["binary"] or is_asset(f["path"])]


# --- 담당 밖 변경 ------------------------------------------------------------------------------------------------
def _norm(text: str) -> str:
    text = text.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lower()


def out_of_scope(paths: Iterable[str], patterns: Iterable[str] | None) -> list[dict]:
    """`out_of_scope[]`: the paths that fit none of the part's globs, each `{path}`, once. Compared as posix
    paths from the project folder, whatever the case; a pattern that ends with `/` is everything under that
    folder. No patterns = the part is not judged: []."""
    wanted = [p for p in (_norm(p) for p in patterns or [] if isinstance(p, str)) if p]
    if not wanted:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        name = _norm(path)
        if not any(name.startswith(p) if p.endswith("/") else fnmatch.fnmatchcase(name, p) for p in wanted):
            out.append({"path": path})
    return out


def _scope(store: StudioStore, task: dict | None, scopes: dict | None) -> list[str] | None:
    """The globs of the assignee's part in `Config.studio_scopes` (department id -> globs); None = none."""
    if not scopes or task is None or not task.get("assignee_id"):
        return None
    employee = store.get_employee(task["assignee_id"])
    listed = scopes.get(employee["department_id"]) if employee else None
    return list(listed) if isinstance(listed, (list, tuple)) else None


# --- incidents ----------------------------------------------------------------------------------------------------
def incidents(events: list[dict], item: dict, run_id: str | None) -> list[dict]:
    """`incidents[]`, each `{id, at, kind, reason, detail}` in the order they were written: the `run` rows of the
    stages that run skipped (also the ones before T6), and the `guard` · `limit` · `run` rows between the T6
    that filed this item and the transition that takes the task out of `review`."""
    filed = next((e["id"] for e in events if e["kind"] == "transition" and e["rule"] == "T6"
                  and (e["detail"] or {}).get("inbox_id") == item["id"]), None)
    left = None if filed is None else next(
        (e["id"] for e in events if e["id"] > filed and e["kind"] == "transition" and e["from_status"] == "review"),
        None)
    out = []
    for e in events:
        if e["kind"] not in INCIDENT_KINDS:
            continue
        detail = e["detail"] or {}
        skipped = bool(run_id) and e["kind"] == "run" and detail.get("event") == SKIPPED \
            and detail.get("run_id") == run_id
        after = filed is not None and e["id"] > filed and (left is None or e["id"] < left)
        if skipped or after:
            out.append({"id": e["id"], "at": e["at"], "kind": e["kind"], "reason": e["reason"], "detail": detail})
    return out


# --- can ----------------------------------------------------------------------------------------------------------
def _verdict(why: str = "") -> dict:
    return {"ok": not why, "why": why}


def _snapshot_left(engine: Any, run: Any) -> bool:
    """T9 의 조건 1: is what the run's changes are reverted from still there — the snapshot of an `inplace` run
    (or its journal), the copy of a `copy` run?"""
    if run.workspace_cleaned:
        return False
    try:
        ws = engine.workspace(run)
        return ws is not None and bool(ws.prepared)
    except Exception:  # noqa: BLE001 - a workspace nobody can read reverts nothing
        return False


def _holder(engine: Any, run: Any) -> str | None:
    """Another run that writes in that folder in place: the engine applies and reverts nothing meanwhile."""
    try:
        return engine.folder_holder(run.workdir, exclude=run.id) or None
    except Exception:  # noqa: BLE001
        return None


def _others_started(store: StudioStore, task: dict, events: list[dict]) -> bool:
    """T9 의 조건 2: has another task of the project gone into `working` since this one first did (by the ids
    of `task_events`)? `rollback` of an `inplace` run puts the whole folder back, their changes with it."""
    first = next((e["id"] for e in events if e["kind"] == "transition" and e["to_status"] == "working"), None)
    if first is None:
        return False
    for other in store.list_tasks(project_id=task["project_id"]):
        if other["id"] != task["id"] and any(
                e["kind"] == "transition" and e["to_status"] == "working"
                for e in store.list_task_events(other["id"], after_id=first)):
            return True
    return False


def gate(store: StudioStore | None, task: dict | None, engine: Any, run: Any, stopped: bool,
         events: list[dict] | None = None, want: Iterable[str] = DECISIONS,
         instead: str = REWORK, *, lost: bool = False) -> dict[str, tuple[str, str]]:
    """The conditions of §4.3 T7 · T8 · T9 (T11 is a revert, T5 a rework), judged on the run as it is now:
    for each decision of `want` a `(code, why)` — the code of `failed[]` (CODES) and what is in the way, both
    empty when the decision is taken. Read-only. `run` None = the engine cannot read the task's run — `lost`
    when it has that run no more (the function `lost`): a revert is then taken without one, as nothing could be
    put back, and a pass stays refused; `stopped`
    = the run is not at work and its task holds no slot (§4.6); `events` the task's `task_events` (read here
    when not given); `instead` what a revert that cannot be done is told to do — a rework, or for T11 the
    decision of the item.

    What has been done to the changes is read from `RunState.changes_status` alone (§4.5): the same decision
    again is taken — a pass of what is applied, a revert of what is reverted —, another one is refused."""
    status = run.changes_status if run is not None else None
    running = "런이 아직 돌고 있다. 멈춘 뒤 다시 결정한다"
    # another run writing there in place: nothing is applied, committed or reverted in that folder meanwhile —
    # `applied` as well: the pass that is decided again commits that folder
    holder = _holder(engine, run) if run is not None and stopped and status in ("ready", "applied") else None
    busy = f"다른 실행({holder})이 이 폴더를 원본에서 쓰는 중이다. 끝난 뒤 다시 결정한다"

    def passing() -> tuple[str, str]:
        if run is None:
            return "run_gone", GONE + (". 되돌리기 · 취소로 닫는다" if lost else "")
        if not stopped:
            return "run_running", running
        if status in REVERTED:
            return "changes_reverted", f"변경을 이미 되돌렸다 ({status}) — 통과시킬 변경이 없다. 되돌리기를 다시 한다"
        if holder:
            return "folder_busy", busy
        return "", ""

    def rework() -> tuple[str, str]:
        if run is None:  # the text waits (`delivery = 'pending'`): no run is there to take it
            return "", ""
        if status == "applied":  # a copy that was applied, a run in place that was committed (T7 의 (나))
            return "changes_applied", "변경이 이미 원본에 반영됐다 (applied). 통과만 받는다"
        if status in REVERTED:
            return "changes_reverted", f"변경을 이미 되돌렸다 ({status}) — 그 런을 이을 수 없다. 같은 되돌리기 · 취소를 다시 한다"
        return "", ""

    def revert() -> tuple[str, str]:
        if run is None:  # gone for good: taken, with nothing reverted — or the task never leaves `review` · `blocked`
            return ("", "") if lost else ("run_gone", GONE)
        if not stopped:
            return "run_running", running
        if status == "none" or status in REVERTED:  # nothing (left) to revert: neither condition is looked at
            return "", ""
        if status == "applied":
            return "changes_applied", "변경을 이미 원본에 반영해 되돌릴 수 없다"
        # a cleaned `copy` run is refused as well: the engine's `_changes_action` takes no discard · rollback of one
        if not _snapshot_left(engine, run):
            return "snapshot_gone", f"스냅샷이 정리돼 되돌릴 수 없다. {instead}"
        if holder:
            return "folder_busy", busy
        if run.workspace_mode != "copy" and task is not None and store is not None and _others_started(
                store, task, events if events is not None else store.list_task_events(task["id"])):
            return "others_started", ("이 작업이 시작된 뒤 같은 프로젝트의 다른 작업이 시작됐다 — 되돌리면 폴더 전체가 "
                                      f"스냅샷으로 돌아가 그 작업의 변경까지 지워진다. {instead}")
        return "", ""

    judges = {"pass": passing, "rework": rework, "revert": revert}
    wanted = set(want)
    return {name: judges[name]() for name in DECISIONS if name in wanted}


def can(store: StudioStore, item: dict, task: dict | None, engine: Any, run: Any, stopped: bool,
        events: list[dict]) -> dict:
    """`can`: `{pass, rework, revert}`, each `{ok, why}` — does the item take that decision now (§4.3 T7 · T8 ·
    T9)? `why` says what is in the way and is empty when `ok`. It is `gate`, a moment old when it is read: the
    decision asks `gate` again."""
    if item["status"] != "pending":
        return {name: _verdict("이미 처리된 결재다") for name in DECISIONS}
    gone = run is None and task is not None and lost(engine, task.get("run_id"))
    return {name: _verdict(why)
            for name, (_, why) in gate(store, task, engine, run, stopped, events, lost=gone).items()}


# --- part 2: what a decision does to the run (§4.3 T7 · T9 · T11, §4.5) ------------------------------------------------
class CommitError(ValueError):
    """git did not make the commit of a pass. A ValueError: the decision is refused as `engine_error`, nothing
    of the transition is written, and the pass can be decided again (§4.3 'T7 이 실패했을 때')."""


def close_run(engine: Any, run: Any) -> Any:
    """§4.3 '런 정리': the engine applies and reverts the changes of a `done` · `cancelled` run alone, so a run
    that stopped any other way (`failed` · `awaiting_approval` · `pending`) is cancelled first. The run as it
    is then. `precondition_failed`: `run_held` when it is still not closed — a thread outside the studio holds
    it, and `cancel` could only leave its mark —, `run_gone` when the engine has it no more."""
    if run.status in ENDED:
        return run
    try:
        try:
            run = engine.cancel(run.id)
        except ValueError:  # closed between the look and the cancel
            run = engine.load(run.id)
    except OSError as e:
        raise TransitionError("precondition_failed", GONE, failed=["run_gone"]) from e
    if run.status not in ENDED:
        raise TransitionError(
            "precondition_failed", f"런이 아직 닫히지 않았다 ({run.status}) — 다른 곳에서 그 런을 쥐고 있다. "
            "취소 표시는 남겼으니 잠시 뒤 다시 결정한다", failed=["run_held"])
    return run


def _git(folder: Path | str, *args: str) -> subprocess.CompletedProcess:
    """git in that folder: it asks nothing (no terminal, no prompt, no window) and is given GIT_TIMEOUT_S."""
    try:
        return subprocess.run(
            ["git", "-C", str(folder), *args], capture_output=True, stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_S,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as e:
        raise CommitError(f"git {args[0]} 이 {GIT_TIMEOUT_S}초 안에 끝나지 않았다") from e


def _must(folder: Path | str, *args: str) -> str:
    done = _git(folder, *args)
    if done.returncode != 0:
        said = (done.stderr or done.stdout or b"").decode("utf-8", "replace").strip()
        raise CommitError(f"git {args[0]} 실패: {said[-300:] or f'exit {done.returncode}'}")
    return done.stdout.decode("utf-8", "replace").strip()


def commit(folder: Path | str, message: str) -> str | None:
    """One git commit of everything under that folder as it is now (`add -A -- .`, `commit -- .`), and its
    sha. None when nothing was committed: the folder is in no git repository (or git cannot be run), or
    nothing there differs from HEAD — the same pass decided again. There is no push, here or anywhere.
    CommitError when git refuses (no `user.name`, a hook, a lock) or does not come back in time; the index is
    then put back as it was before `add` (`_unstage`): nothing of the pass stays staged, and what the owner had
    staged does."""
    try:
        if _git(folder, "rev-parse", "--show-toplevel").returncode != 0:
            return None
    except OSError:
        return None
    index = _git(folder, "write-tree")  # the index as it is before `add`; not a tree while paths are unmerged
    tree = index.stdout.decode("ascii", "replace").strip() if index.returncode == 0 else None
    try:
        _must(folder, "add", "-A", "--", ".")
        staged = _git(folder, "diff", "--cached", "--quiet", "--", ".")
        if staged.returncode == 0:
            return None
        if staged.returncode != 1:
            raise CommitError("git diff 실패: " + (staged.stderr or b"").decode("utf-8", "replace").strip()[-300:])
        _must(folder, "commit", "-q", "-m", message, "--", ".")
    except CommitError:
        _unstage(folder, tree)
        raise
    return _must(folder, "rev-parse", "HEAD")


def _unstage(folder: Path | str, tree: str | None) -> None:
    """After a commit that failed: the index as it was before its `add`. The tree written then is read back
    (`read-tree`), so what the owner had staged stays staged — a `reset` would unstage that as well. Without a
    tree, or when it cannot be read back, the paths under the folder are unstaged. The files are not touched
    either way. What fails here is logged: the CommitError is what goes up."""
    try:
        if tree and _git(folder, "read-tree", tree).returncode == 0:
            return
        if _git(folder, "reset", "-q", "--", ".").returncode != 0:
            log.warning("studio: 커밋 실패 뒤 %s 의 스테이징을 되돌리지 못했다", folder)
    except (OSError, CommitError):
        log.exception("studio: 커밋 실패 뒤 %s 의 스테이징을 되돌리지 못했다", folder)


def _record(engine: Any, run_id: str, kind: str, detail: dict) -> None:
    try:
        engine.history.add_event(run_id, "-", kind, detail)
    except Exception:  # noqa: BLE001 - the commit is made: a line of the record does not refuse the pass
        log.exception("studio: 런 %s 의 %s 를 적지 못했다", run_id, kind)


def passed(engine: Any, run: Any, task: dict) -> Any:
    """T7, before anything of it is written (§4.5): the run is closed, the changes of a `copy` run are applied
    to the original (not again when they are `applied` already: T7 의 (나)), and the project folder is
    committed — only when the run changed something (`ready` · `applied`): a run that changed nothing does not
    sweep up whatever else lies in the folder. An `inplace` run is marked kept after the commit, so a commit
    that fails leaves the rework and the revert open (T7 의 (다)); a mark that fails is logged, the pass stands.
    Whatever the engine or git refuses goes up as it is (ValueError): then nothing is written."""
    run = close_run(engine, run)
    if run.workspace_mode == "copy" and run.changes_status == "ready":
        run = engine.apply_changes(run.id)
    if run.changes_status not in ("ready", "applied"):
        return run
    title = " ".join(str(task.get("title") or "").split()) or str(task["id"])
    sha = commit(run.workdir, f"{title}\n\n작업 {task['id']} · 런 {run.id} · 검수 통과")
    _record(engine, run.id, "changes_committed" if sha else "commit_skipped", {"sha": sha} if sha else {})
    if run.workspace_mode == "inplace" and run.changes_status == "ready":
        try:
            run = engine.keep_changes(run.id)
        except (OSError, ValueError):
            log.exception("studio: 통과한 런 %s 의 변경을 '그대로 둔다'로 표시하지 못했다", run.id)
    return run


def reverted(engine: Any, run: Any) -> Any:
    """T9 · T11, before anything of them is written: the run is closed and its undecided changes are taken
    back — the copy of a `copy` run is dropped, the folder of an `inplace` run goes back to its snapshot. A
    run with nothing to revert (`none`, or reverted by the same decision before) is left as it is."""
    run = close_run(engine, run)
    if run.changes_status != "ready":
        return run
    return engine.discard_changes(run.id) if run.workspace_mode == "copy" else engine.rollback_changes(run.id)


# --- the package --------------------------------------------------------------------------------------------------
def package(store: StudioStore, item: dict, task: dict | None, engine: Any, *,
            holds: Callable[[str], bool] | None = None, scopes: dict | None = None) -> dict:
    """`package` of a `final_review` item (§5.4), computed now. `engine` is the RelayEngine to read (None =
    none is connected), `holds` the queue's "does that task hold a slot" (`TaskQueue.holds`; None = no queue,
    so nothing holds one), `scopes` `Config.studio_scopes`.

    The run is the one the item was filed for — a task that was reverted has none any more. A run the engine
    cannot read: `run` is null, the lists are empty, the cost is 0, and neither a pass nor a revert is `ok`."""
    run_id = item.get("run_id") or (task or {}).get("run_id")
    run = _load(engine, run_id)
    payload = item.get("payload") or {}
    events = store.list_task_events(task["id"]) if task else []
    held = bool(task and holds is not None and holds(task["id"]))
    stopped = run is not None and run.status != "running" and not held
    files: list[dict] = []
    listed: list[dict] = []
    if run is not None:
        try:
            patch = engine.patch_text(run.id)
        except OSError:
            patch = ""
        files = changed_files(run, patch)
        listed = assets(run, files)
    stop = run.baton.stop if run is not None else None
    return {
        "run": None if run is None else {"status": run.status, "error": run.error,
                                         "stop": stop.model_dump() if stop is not None else None},
        "run_stopped": stopped,
        "changes": {"status": run.changes_status if run is not None else None,
                    "workspace_mode": run.workspace_mode if run is not None else None,
                    "files": files,
                    "patch_url": f"/studio/runs/{run.id}/patch" if run is not None else None},
        "assets": listed,
        "screenshots": [e["ref"] for e in payload.get("evidence") or []
                        if isinstance(e, dict) and e.get("kind") == "screenshot" and isinstance(e.get("ref"), dict)],
        "harness_checks": [],  # S-11
        "out_of_scope": out_of_scope([f["path"] for f in files] + [a["path"] for a in listed],
                                     _scope(store, task, scopes)),
        "cost": {"usd": round(run.cost_usd, 4) if run is not None else 0},
        "incidents": incidents(events, item, run_id),
        "can": can(store, item, task, engine, run, stopped, events),
        "followups": [{key: row[key] for key in FOLLOWUP} for row in store.list_tasks(parent_id=task["id"])]
                     if task else [],
    }


# --- the runs the cleanup keeps -------------------------------------------------------------------------------------
def held_runs(store: StudioStore) -> set[str]:
    """The runs of the tasks under review or blocked. `RelayEngine.cleanup_workspaces` does not let their
    workspaces expire: such a run is `ready` — undecided — for as long as the owner has not decided, and
    without its snapshot the review could not be reverted any more (§4.3 T9 의 조건 1), nor the blocked task
    be cancelled (T11)."""
    return {task["run_id"] for status in HELD for task in store.list_tasks(status=status) if task["run_id"]}


def held_runs_ro(db: Path | str) -> set[str]:
    """`held_runs` for a process that does not open the studio store (the CLI, studio/runs.py): the same rows,
    read from the DB file through a read-only connection. No file, or no `tasks` table yet: no run is held.
    Any other failure is raised — whoever asks must not take "cannot say" for "none"."""
    path = Path(db)
    if not path.is_file():
        return set()
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'").fetchone() is None:
            return set()
        rows = conn.execute(f"SELECT run_id FROM tasks WHERE status IN ({', '.join('?' * len(HELD))})"
                            " AND run_id IS NOT NULL", HELD).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()
