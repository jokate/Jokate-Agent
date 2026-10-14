"""The policy of a project (H-6; ARCHITECTURE §2.2 · §6.4 · §8.4): `studio/projects/<id>/POLICY.md`, its reading,
and the start of the talk in which HR writes it with the owner.

`begin_interview` is asked by studio/api.py (a project that was brought in) and by studio/jobs.py (one that was
made) once the project's row is committed. It writes a system message in HR's 1:1 room and tells the caller to
ask for HR's chat turn; a studio without an HR employee or its room is left as it is.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path

from . import employees, importer
from .store import StudioStore

log = logging.getLogger(__name__)

INTERVIEW_HEAD = "[정책 면담]"
RULES_LIMIT = 8000  # characters of the rules files a message carries, all of them together


def path(studio_dir: Path | str, project_id: str) -> Path:
    return employees.policy_path(studio_dir, project_id)


def read(studio_dir: Path | str, project_id: str) -> dict:
    """`{text, sha256, updated_at}` of the file. No file: `""` · `""` · None (the base of a first write is `""`)."""
    target = path(studio_dir, project_id)
    try:
        if not target.is_file():
            return {"text": "", "sha256": "", "updated_at": None}
        raw, stamp = target.read_bytes(), target.stat().st_mtime
    except OSError:
        return {"text": "", "sha256": "", "updated_at": None}
    return {"text": raw.decode("utf-8", errors="replace"), "sha256": hashlib.sha256(raw).hexdigest(),
            "updated_at": datetime.fromtimestamp(stamp).isoformat(timespec="seconds")}


def _rules_text(folder: Path | str) -> str:
    """The rules files of an imported folder, read and never written: name, then content, within RULES_LIMIT."""
    left, blocks = RULES_LIMIT, []
    for name in importer.rules(folder):
        if left <= 0:
            blocks.append(f"--- {name} --- (잘림: 길이 상한을 넘어 읽지 않았다)")
            continue
        try:
            text = (Path(folder) / name).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        cut = len(text) > left
        blocks.append(f"--- {name} ---\n{text[:left]}" + ("\n(잘림: 길이 상한을 넘는 뒷부분은 싣지 않았다)" if cut else ""))
        left -= len(text)
    return "\n\n".join(blocks)


def interview_text(project: dict, rules_from: Path | str | None = None) -> str:
    lines = [f"{INTERVIEW_HEAD} 프로젝트 '{project['name']}' (id `{project['id']}`) 이(가) 등록됐다. 이 프로젝트의 정책"
             "(POLICY.md)을 owner 와 함께 정한다. 한 번에 한 질문으로 아래 셋을 묻는다.",
             "1. 장르 · 규모 · 지금 단계",
             "2. 누가 무엇을 정하는가 (owner 가 정하는 것, 직원이 알아서 하는 것, 결재를 받고 하는 것)",
             "3. 건드리면 안 되는 것"]
    rules = _rules_text(rules_from) if rules_from is not None else ""
    if rules:
        lines += ["", "이 프로젝트 폴더에는 이미 규칙 파일이 있다. 읽고 초안에 반영해, owner 에게는 확인만 받는다:", "", rules]
    return "\n".join(lines)


def begin_interview(store: StudioStore, studio_dir: Path | str, project: dict, *,
                    rules_from: Path | str | None = None) -> tuple[str, str] | None:
    """Write the system message that opens the policy talk in HR's 1:1 room (`message.created` in the same
    transaction). `(hr_id, room_id)` for the caller to ask for HR's chat turn. The employee is the first of the
    HR part who is not on leave and has a 1:1 room that is not archived (a message in an archived room is not
    shown to the owner); an employee on leave comes only after those. None — and a warning — when there is no
    such employee. Reads `rules_from` and writes nothing there."""
    body = interview_text(project, rules_from)
    with store.transaction() as tx:
        staff = store.list_employees(department_id=employees.HR_DEPARTMENT, tx=tx)
        staff.sort(key=lambda row: row.get("status") == "on_leave")  # stable: on leave last
        chosen = None
        for person in staff:
            rooms = store.list_rooms(kind="dm", employee_id=person["id"], tx=tx)
            if rooms:
                chosen = person, rooms[0]
                break
        if chosen is None:
            if staff:
                log.warning("no 1:1 room that is not archived for an HR employee: no policy talk for %s",
                            project.get("id"))
            return None
        person, room = chosen
        message = store.create_message(room["id"], "system", body=body,
                                       meta={"event": "policy_interview", "project_id": project["id"]}, tx=tx)
        store.emit(tx, "message.created", {"message": message}, project_id=person["project_id"],
                   room_id=room["id"], task_id=None)
    return person["id"], room["id"]
