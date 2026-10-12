"""Drafts HR writes before the owner sees them (§8.4): `runs/studio/drafts/<draft_id>.json`.

Contract: design/studio/ARCHITECTURE.md §8.4 (인사 전용) — a draft is saved even with problems and
`employee_submit` refuses it; submitting copies the draft's body into the item's payload (§5.3), so a later
change of the draft does not change what was filed.

A draft: `{id, kind: employee | policy, action: hire | guide_amend | policy, author_id, created_at,
updated_at, problems[], …body}`. The body is `draft` (the DRAFT_KEYS of inbox.py) for a hire, and `new_text` ·
`diff` · `base_sha256` (+ `target_employee_id` · `file`, or `project_id` · `reason`) for the two that replace a
file. `problems[]` is `[{code, message}]`.

No FastAPI and no api.py. The id is matched before a path is made from it, so no id leaves the folder.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path

from . import employees
from .inbox import HR
from .store import StudioStore, now

DRAFT_ID = re.compile(r"drf_[0-9a-f]{8}")


def folder(studio_dir: Path | str) -> Path:
    return Path(studio_dir) / "drafts"


def new_id(studio_dir: Path | str) -> str:
    while True:
        draft_id = "drf_" + uuid.uuid4().hex[:8]
        if not (folder(studio_dir) / f"{draft_id}.json").exists():
            return draft_id


def policy_id(project_id: str) -> str:
    """One policy draft per project: its id follows from the project, so a second draft replaces the first
    and handing it in again replaces the pending item (Inbox._create_hire looks at `draft_id`)."""
    return "drf_" + hashlib.sha256(f"policy:{project_id}".encode("utf-8")).hexdigest()[:8]


def load(studio_dir: Path | str, draft_id: object) -> dict | None:
    """The draft, or None: an id of another shape, no such file, a file that is not a draft."""
    if not isinstance(draft_id, str) or DRAFT_ID.fullmatch(draft_id) is None:
        return None
    try:
        draft = json.loads((folder(studio_dir) / f"{draft_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return draft if isinstance(draft, dict) and draft.get("id") == draft_id else None


def save(studio_dir: Path | str, draft: dict, old: dict | None = None) -> dict:
    """Write the draft (UTF-8, a temporary file replaced into place) and return it with its two times."""
    if DRAFT_ID.fullmatch(str(draft.get("id"))) is None:
        raise ValueError(f"a draft id is drf_<hex8>: {draft.get('id')!r}")
    at = now()
    out = {**draft, "created_at": (old or {}).get("created_at") or at, "updated_at": at}
    path = folder(studio_dir) / f"{out['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8"))
    os.replace(tmp, path)
    return out


def problem(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def yaml_problems(text: object, *, department_id: str | None, department: dict | None,
                  checks: employees.Checks) -> list[dict]:
    """What is wrong with an `employee.yaml` text: the checks of §9.4 (studio/employees.py), the same ones an
    approval runs. `department_id` is the part the file must name, `department` that part's row or None."""
    return employees.check(text, department_id=department_id, department=department, checks=checks)


def amend_problems(store: StudioStore, target: dict | None, file: str, new_text: object,
                   checks: employees.Checks) -> list[dict]:
    """What is wrong with the new text of an amendment. Only an `employee.yaml` is checked, against the part
    of the employee it is for; [] when there is no such employee — the caller reports that one."""
    if file != "employee.yaml" or target is None:
        return []
    return yaml_problems(new_text, department_id=target["department_id"],
                         department=store.get_department(target["department_id"]), checks=checks)


def hire_problems(store: StudioStore, body: dict, checks: employees.Checks) -> list[dict]:
    """What would refuse the approval of this hire now — the rules of Inbox._check_hire (§5.5) — and the
    check of its employee.yaml (§9.4)."""
    name = (body.get("name") or "").strip()
    department_id = (body.get("department_id") or "").strip()
    project_id = (body.get("project_id") or "").strip()
    out: list[dict] = []
    if any(row["name"] == name for row in store.list_employees(include_retired=True)):
        out.append(problem("name_taken", f"같은 이름의 직원이 이미 있다 (퇴사한 직원 포함): {name}"))
    if employees.proposes_part(body):  # a new part with the hire: it is not in the DB, so it is checked as proposed
        department, _ = employees.proposed_part(body)
        out += employees.part_proposal_problems(body, store.list_departments(include_archived=True), checks)
    else:
        department = store.get_department(department_id)
    if employees.proposes_part(body):
        pass
    elif department is None or department["archived_at"] is not None:
        out.append(problem("department_not_found", f"파트가 없다: {department_id}"))
    elif department["id"] == HR:
        if store.list_employees(department_id=HR):
            out.append(problem("hr_exists", "인사 직원은 한 명뿐이다"))
        if project_id:
            out.append(problem("hr_has_project", "인사 직원은 프로젝트에 배치하지 않는다"))
    if project_id and store.get_project(project_id) is None:
        out.append(problem("project_not_found", f"배치할 프로젝트가 없다: {project_id}"))
    return out + yaml_problems(body.get("employee_yaml"), department_id=department_id, department=department,
                               checks=checks)


def file_base(path: Path) -> tuple[str, str]:
    """(text, sha256) of the file a draft is made from; ("", "") when there is none — the value
    Inbox._replace_file compares `base_sha256` with at the approval. OSError when it cannot be read."""
    if not path.is_file():
        return "", ""
    data = path.read_bytes()
    return data.decode("utf-8", errors="replace"), hashlib.sha256(data).hexdigest()


def diff(old: str, new: str, name: str) -> str:
    """A unified diff of the two texts; "" when they are the same."""
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=name, tofile=name,
                                          lineterm=""))
