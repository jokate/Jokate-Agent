"""An employee's memory (H-4): after the owner's final decision, a light model picks out 'what to do differently
next time' and the server piles it up as lines in that employee's MEMORY.md (ARCHITECTURE.md §9.1 · §5.5 · §6.4).

A line is one item: `- <text> (출처: <task id> · YYYY-MM-DD)`. A merged line says `합침` in place of the task. A line
that does not start with `- ` is no item and is left as it is; a line without the tail (written by hand) is an
item whose source is unknown. The id of an item is the first 12 hex digits of the SHA-256 of its line (both ends
trimmed) — a line the server moved is the same item, a line that was changed in the meantime is not found.

The model is `ask(system, facts) -> text | (text, usage)`; `writer()` makes the real one (`handoff.HandoffWriter`).
A call that fails leaves the decision and the file as they were. The file is over `MEMORY_LIMIT` -> the lines
are merged by the model (aiming at half of the limit); if that fails too, the oldest lines go.

No FastAPI here: api.py (the paths, the thread) and inbox.py (`wanted`) use it. `employee_git.lock` is held for
the read-write-commit, never for a model call.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Any, Callable

from . import employee_git, employees
from .store import StudioStore

log = logging.getLogger(__name__)

Ask = Callable[[str, str], Any]  # (system, facts) -> the text, or (text, usage) as HandoffWriter answers
NAME = "MEMORY.md"
MAX_NEW = 3  # items one decision adds, at most
ITEM_CHARS = 200
MERGE_TARGET = employees.MEMORY_LIMIT // 2
TIMEOUT_S = 120
MERGED = "합침"
TAIL = re.compile(r"\s*\(출처: (?P<source>[^()]+?) · (?P<date>\d{4}-\d{2}-\d{2})\)$")

EXTRACT_SYSTEM = """너는 직원의 '배운 점' 메모를 쓴다. 아래 사실은 owner 가 이 직원의 작업을 최종 검수하고 내린 결정이다.
다음에 같은 일을 할 때 다르게 해야 할 점만 뽑아라.
- 한 줄에 하나, 줄마다 `- ` 로 시작하고 200자 이내, 최대 3줄.
- 이 작업에서만 맞는 이야기가 아니라 다음 작업에도 쓰이는 행동 지침으로 쓴다.
- '지금 항목'에 이미 있는 내용은 다시 쓰지 않는다.
- 배울 것이 없으면 `없음` 한 단어만 쓴다. 다른 설명은 붙이지 않는다."""

MERGE_SYSTEM = f"""너는 직원의 '배운 점' 메모를 줄인다. 아래 줄들에서 같은 뜻은 하나로 합치고 낡았거나 사소한 것은 버려라.
- 한 줄에 하나, 줄마다 `- ` 로 시작하고 200자 이내.
- 전체가 UTF-8 로 {MERGE_TARGET}바이트 이하여야 한다.
- 줄 끝의 `(출처: … · 날짜)` 는 붙어 있으면 그대로 두고, 합친 줄에는 붙이지 않는다.
- 결과 줄만 쓴다. 설명은 붙이지 않는다."""


class MemoryRefused(Exception):
    """An item change that cannot be made. `failed` is the code for `precondition_failed.failed[]` (§12):
    `item_unknown` · `memory_too_large`."""

    def __init__(self, failed: str, message: str):
        super().__init__(message)
        self.failed = failed


# --- the lines ------------------------------------------------------------------------------------------------
def is_item(line: str) -> bool:
    return line.lstrip("﻿").startswith("- ")


def line_id(line: str) -> str:
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:12]


def tail(source: str, day: str) -> str:
    return f" (출처: {source} · {day})"


def line_of(text: str, source: str, day: str) -> str:
    """One item line, without the line end."""
    return f"- {one_line(text)}{tail(source, day)}"


def one_line(text: str) -> str:
    body = " ".join(str(text).split())
    while body.startswith("- "):
        body = body[2:].lstrip()
    return body


def parse(text: str) -> list[dict]:
    """`[{id, text, source, date}]` for the item lines of a MEMORY.md body, in the order of the file. `source`
    and `date` are None for a line without the tail."""
    items = []
    for line in (text or "").splitlines():
        if not is_item(line):
            continue
        body = line.strip()[2:]
        found = TAIL.search(body)
        items.append({"id": line_id(line), "text": body[:found.start()].strip() if found else body.strip(),
                      "source": found["source"] if found else None, "date": found["date"] if found else None})
    return items


def view(studio_dir: Path | str, employee_id: str) -> dict:
    """`{items, bytes, limit, sha256}` of the employee's MEMORY.md now (a file that is not there = empty)."""
    raw = _read(employees.folder(studio_dir, employee_id))
    return {"items": parse(raw.decode("utf-8", errors="replace")), "bytes": len(raw),
            "limit": employees.MEMORY_LIMIT, "sha256": hashlib.sha256(raw).hexdigest()}


def _read(folder: Path) -> bytes:
    try:
        return (folder / NAME).read_bytes()
    except OSError:
        return b""


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def fit(text: str) -> str:
    """The text within `MEMORY_LIMIT`: the oldest item lines (the top of the file) go first. No item left and
    still over — lines that are no items — `clip_memory` keeps the end."""
    lines = text.splitlines(keepends=True)
    while _size("".join(lines)) > employees.MEMORY_LIMIT:
        at = next((i for i, line in enumerate(lines) if is_item(line)), None)
        if at is None:
            return employees.clip_memory("".join(lines))
        del lines[at]
    return "".join(lines)


# --- what asks the model --------------------------------------------------------------------------------------
def wanted(item: dict) -> bool:
    """A final review that is decided and carries a comment — a rework always does (§5.5). A pass or a revert
    without a reason has nothing to learn from, so no model is called for it."""
    return (item.get("kind") == "final_review" and item.get("status") == "decided"
            and bool((item.get("decision_comment") or "").strip()))


def writer(model: str | None) -> Ask | None:
    """The light model: one call without tools from a neutral folder (`HandoffWriter`), the system prompt per
    call. None for a blank model or a PC without `claude` — nothing is learned then."""
    name = (model or "").strip()
    exe = shutil.which("claude") if name else None
    if not exe:
        return None
    from ..handoff import HandoffWriter  # at the call: the file lines need neither the runners nor a model

    def ask(system: str, facts: str) -> Any:
        return HandoffWriter(name, exe=exe, timeout_s=TIMEOUT_S, system=system)(facts)

    return ask


def _answer(raw: Any) -> str:
    text = raw[0] if isinstance(raw, tuple) and raw else raw
    return str(text or "").strip()


def _facts(task: dict | None, item: dict, current: str) -> str:
    payload = item.get("payload") or {}
    checklist = payload.get("checklist") if isinstance(payload, dict) else None
    marks = []
    for entry in checklist if isinstance(checklist, list) else []:
        if isinstance(entry, dict):
            marks.append(f"{entry.get('criterion') or entry.get('text') or entry} ({entry.get('status') or '?'})")
        else:
            marks.append(str(entry))
    parts = [f"작업: {(task or {}).get('title') or item.get('title') or ''}",
             f"결정: {item.get('decision')}", f"사유: {item.get('decision_comment')}"]
    if marks:
        parts.append("완료 기준:\n" + "\n".join(f"- {m}" for m in marks))
    parts.append("지금 항목:\n" + ("\n".join(i["text"] for i in parse(current)) or "(없음)"))
    return "\n".join(parts)


def _lines_of(answer: str, current: str) -> list[str]:
    """The item texts in the model's answer: bullets off, `없음` and blank lines out, at most MAX_NEW, each cut
    at ITEM_CHARS, the ones the file has already out."""
    known = {i["text"] for i in parse(current)}
    found: list[str] = []
    for raw in answer.splitlines():
        text = one_line(raw)
        if not text or text.strip("`'\".") == "없음":
            continue
        text = text[:ITEM_CHARS].rstrip()
        if text in known or text in found:
            continue
        found.append(text)
    return found[:MAX_NEW]


def _write(store: StudioStore, studio_dir: Path | str, employee_id: str, text: str, message: str) -> str | None:
    """Write MEMORY.md, commit the folder, tell the app. The commit that fails is logged and the file stays;
    its sha is None then. Under `employee_git.lock` (the caller's)."""
    folder = employees.folder(studio_dir, employee_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / NAME).write_bytes(text.encode("utf-8"))
    commit = None
    try:
        commit = employee_git.commit(folder, message) or employee_git.head(folder)
    except Exception:  # noqa: BLE001 - the file is written: what follows must not hide or undo that
        log.exception("studio: 직원 폴더를 커밋하지 못했다 (%s, '%s') — 파일은 그대로다", folder, message)
    row = store.get_employee(employee_id)
    if row is not None:
        with store.transaction() as tx:
            store.emit(tx, "employee.updated", {"employee": row, "change": "file"}, project_id=row["project_id"])
    return commit


def _appended(current: str, lines: list[str]) -> str:
    base = current if not current or current.endswith("\n") else current + "\n"
    return base + "".join(line + "\n" for line in lines)


def _merged(current: str, answer: str, day: str) -> str | None:
    """The file with its item lines replaced by the model's: lines that are no items stay on top as they were.
    None when the answer has no item line."""
    kept = [line for line in current.splitlines(keepends=True) if not is_item(line)]
    lines = []
    for raw in answer.splitlines():
        text = one_line(raw)
        if not text:
            continue
        found = TAIL.search(text)
        lines.append(f"- {text}" if found else line_of(text[:ITEM_CHARS], MERGED, day))
    return "".join(kept) + "".join(line + "\n" for line in lines) if lines else None


def learn(store: StudioStore, studio_dir: Path | str, item: dict, ask: Ask, today: date | None = None) -> str | None:
    """Pick the lessons out of a decided final review and add them to the assignee's MEMORY.md. The sha of the
    commit (or the head, if the folder had nothing new to commit); None = nothing was written. A model that
    fails is logged and nothing is written — nothing here raises for it."""
    task = store.get_task(item["task_id"]) if item.get("task_id") else None
    employee = store.get_employee(task["assignee_id"]) if task and task.get("assignee_id") else None
    if employee is None:
        return None
    day = (today or date.today()).isoformat()
    folder = employees.folder(studio_dir, employee["id"])
    current = _read(folder).decode("utf-8", errors="replace")
    try:
        lines = _lines_of(_answer(ask(EXTRACT_SYSTEM, _facts(task, item, current))), current)
    except Exception:  # noqa: BLE001 - the decision is made: a model that fails teaches nothing, that is all
        log.exception("studio: 메모리 추출 호출이 실패했다 (작업 %s)", item.get("task_id"))
        return None
    if not lines:
        return None
    new_lines = [line_of(text, task["id"], day) for text in lines]
    message = f"memory: learn {task['id']}"
    with employee_git.lock:
        basis = _read(folder).decode("utf-8", errors="replace")
        full = _appended(basis, new_lines)
        if _size(full) <= employees.MEMORY_LIMIT:
            return _write(store, studio_dir, employee["id"], full, message)
    merged = None  # over the limit: the model merges, outside the lock
    try:
        merged = _merged(full, _answer(ask(MERGE_SYSTEM, full)), day)
    except Exception:  # noqa: BLE001 - the oldest lines go instead
        log.exception("studio: 메모리 합치기 호출이 실패했다 (직원 %s) — 오래된 항목부터 버린다", employee["id"])
    with employee_git.lock:
        fresh = _read(folder).decode("utf-8", errors="replace")
        if merged is not None and fresh == basis:
            text = merged
        else:  # the file changed while the model worked (or the merge failed): what is there now plus the new
            text = _appended(fresh, new_lines)
        return _write(store, studio_dir, employee["id"], fit(text), message)


# --- the owner's change of one item (§6.4) --------------------------------------------------------------------
def _change(store: StudioStore, studio_dir: Path | str, employee: dict, item_id: str,
            make: Callable[[str], str | None], message: str) -> dict:
    folder = employees.folder(studio_dir, employee["id"])
    with employee_git.lock:
        current = _read(folder).decode("utf-8", errors="replace")
        lines = current.splitlines(keepends=True)
        at = next((i for i, line in enumerate(lines) if is_item(line) and line_id(line) == item_id), None)
        if at is None:
            raise MemoryRefused("item_unknown", f"그 메모리 항목이 없습니다: {item_id}")
        line = lines[at]
        end = line[len(line.rstrip("\r\n")):]
        replaced = make(line.rstrip("\r\n"))
        lines[at:at + 1] = [] if replaced is None else [replaced + (end or "\n")]
        new = "".join(lines)
        problems = employees.memory_problems(new)
        if problems and _size(new) > _size(current):  # a file the owner already left over the limit may shrink
            raise MemoryRefused("memory_too_large", " / ".join(p["message"] for p in problems))
        commit = _write(store, studio_dir, employee["id"], new, message)
    return {**view(studio_dir, employee["id"]), "commit": commit}


def edit(store: StudioStore, studio_dir: Path | str, employee: dict, item_id: str, text: str) -> dict:
    """Replace the text of one item and keep its tail. `view()` + `commit`. MemoryRefused when the item is not
    there (`item_unknown`) or the file would grow over the limit (`memory_too_large`; a file that is over it already
    may be made shorter or kept as long)."""
    body = one_line(text)

    def make(line: str) -> str:
        found = TAIL.search(line.strip()[2:])
        return f"- {body}{found.group(0) if found else ''}"

    return _change(store, studio_dir, employee, item_id, make, "memory: edit")


def remove(store: StudioStore, studio_dir: Path | str, employee: dict, item_id: str) -> dict:
    """Delete one item line. `view()` + `commit`. MemoryRefused(`item_unknown`) when it is not there. A file that is
    over the limit already is shortened all the same."""
    return _change(store, studio_dir, employee, item_id, lambda line: None, "memory: delete")
