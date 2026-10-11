"""Files that become attachments (§6.6): what the owner's upload and an employee's file argument share, and
the check of a path an employee hands in ('직원의 첨부').

Contract: design/studio/ARCHITECTURE.md §6.6 — the limit, the cleaning of a name, `[{name, path, mime, size}]`,
and for an employee's path the two allowed folders and the three codes of `failed[]`.

No FastAPI and no api.py: studio/tools.py uses it for `room_post`, api.py for the owner's uploads.
"""

from __future__ import annotations

import mimetypes
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from .store import StudioStore, Tx

MAX_ATTACHMENT_MB = 25                              # the limit of POST /attachments (server.py)
UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')  # the cleaning of POST /attachments (server.py)
NOT_FOUND = "attachment_not_found"                  # no such file, or not a file
OUTSIDE = "attachment_outside_workspace"            # not inside an allowed folder
TOO_LARGE = "attachment_too_large"                  # over MAX_ATTACHMENT_MB


def mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def clean_name(name: str) -> str:
    return UNSAFE_NAME.sub("_", Path(name).name).strip(". ") or "file"


def room_project(store: StudioStore, room: dict, tx: Tx | None = None) -> str | None:
    """The project a room's events are filed under: the task's for a task room, the employee's for a DM."""
    if room["task_id"]:
        task = store.get_task(room["task_id"], tx=tx)
        return task["project_id"] if task else None
    if room["employee_id"]:
        employee = store.get_employee(room["employee_id"], tx=tx)
        return employee["project_id"] if employee else None
    return None


def _locate(raw: str, allowed: list[Path], base: Path | None) -> tuple[str | None, Path | None]:
    """(code, None) for a path that cannot be attached, (None, the resolved file) for one that can.

    The order is the point: inside an allowed folder first, then "is it a file", then the size — a path
    outside is `OUTSIDE` whether a file is there or not, so the answer says nothing about other folders."""
    try:
        path = Path(raw)
        if not path.is_absolute():
            if base is None:  # a relative path needs the folder the run works in
                return OUTSIDE, None
            path = Path(base) / path
        path = path.resolve()
    except (OSError, ValueError, RuntimeError):
        return OUTSIDE, None  # it cannot be placed inside an allowed folder
    if not any(root == path or root in path.parents for root in allowed):
        return OUTSIDE, None
    try:
        if not path.is_file():
            return NOT_FOUND, None
        if path.stat().st_size > MAX_ATTACHMENT_MB * 1024 * 1024:
            return TOO_LARGE, None
    except OSError:
        return NOT_FOUND, None
    return None, path


def check(specs: Iterable[dict], roots: Iterable[Any], base: Path | None) -> tuple[list[tuple[Path, str]],
                                                                                 list[tuple[str, str]]]:
    """§6.6 '직원의 첨부': each `{path, name=""}` against the allowed folders `roots`; a relative path is taken
    from `base`, the folder the run works in. Returns (the files to copy as (source, name), the refused ones
    as (code, the path as it was given)). Nothing is copied here: every check ends before anything is made."""
    allowed: list[Path] = []
    for root in roots:
        if not root:
            continue
        try:
            allowed.append(Path(root).resolve())
        except (OSError, ValueError, RuntimeError):
            pass
    found: list[tuple[Path, str]] = []
    refused: list[tuple[str, str]] = []
    for spec in specs:
        raw = str(spec.get("path") or "")
        code, path = _locate(raw, allowed, base)
        if path is None:
            refused.append((code or NOT_FOUND, raw))
        else:
            found.append((path, clean_name(spec.get("name") or path.name)))
    return found, refused


def copy(files: Iterable[tuple[Path, str]], folder: Path) -> list[dict]:
    """Copy (source, name) pairs into a message's or an item's folder; the `[{name, path, mime, size}]` of
    attachments_json. Two files with one name in the same folder: the later one gets `-2`.

    Copied, not moved: the originals stay where they were."""
    folder.mkdir(parents=True, exist_ok=True)
    refs: list[dict] = []
    used: set[str] = set()
    for src, wanted in files:
        name, n = wanted, 1
        stem, suffix = Path(wanted).stem, Path(wanted).suffix
        while name.lower() in used:
            n += 1
            name = f"{stem}-{n}{suffix}"
        used.add(name.lower())
        shutil.copy2(src, folder / name)
        refs.append({"name": name, "path": f"{folder.name}/{name}", "mime": mime(name),
                     "size": (folder / name).stat().st_size})
    return refs
