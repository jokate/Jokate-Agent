"""Lay the harness into a project folder (T3): `skills/game-bootstrap/scripts/bootstrap.py --auto <folder>` of the
harness (`harness_root.resolve_root`) is run once, for a project brought in or made while `importer.harness_ready`
is false. `--auto` is meant not to overwrite a file that is there, and the server keeps that promise itself: the
hand-written rule files (`KEEP`) are saved before the run and put back if they differ after it. Whatever happens is told in one system message in HR's
1:1 room (`note`); nothing here raises — a setting that fails does not fail the import.

Not `harness.py`: that is the `harness` step of a new project (a .bat of the harness, M-5). This is the one pass
every project gets when it is registered, and what `POST /studio/projects/{id}/harness` runs for one that was
registered before."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from . import harness_root, importer, policy
from .creator import run_process  # a module global: a test puts a fake in its place

log = logging.getLogger(__name__)

SCRIPT = Path("skills", "game-bootstrap", "scripts", "bootstrap.py")
TIMEOUT = 300
KEEP = ("CLAUDE.md", "CLAUDE.local.md")  # the rule files a person writes by hand, right under the project folder
EVENT = "harness_setup"


def script(setting: str | None) -> Path | None:
    """The bootstrap script of the harness, or None when there is no harness or it has no such script."""
    root = harness_root.resolve_root(setting)
    if root is None:
        return None
    found = root / SCRIPT
    try:
        return found if found.is_file() else None
    except OSError:
        return None


def _snapshot(folder: Path) -> dict[str, bytes]:
    """The content of each `KEEP` file that is there; one that cannot be read is left out."""
    saved: dict[str, bytes] = {}
    for name in KEEP:
        try:
            if (folder / name).is_file():
                saved[name] = (folder / name).read_bytes()
        except OSError:
            continue
    return saved


def _restore(folder: Path, saved: dict[str, bytes]) -> list[str]:
    """Put back each saved file that is changed or gone; the names put back. Never raises."""
    restored: list[str] = []
    for name, data in saved.items():
        try:
            target = folder / name
            if not target.is_file() or target.read_bytes() != data:
                target.write_bytes(data)
                restored.append(name)
        except OSError:
            log.warning("studio: %s 를 되돌리지 못했다", name)
    return restored


def auto(folder: Path | str, setting: str | None, *, force: bool = False, timeout: float = TIMEOUT) -> dict:
    """`{status, detail}`: status is `ready` (already laid, nothing run), `done`, `skipped` (no harness) or
    `failed`. `force` runs it for a folder that is ready as well. Never raises."""
    try:
        folder = Path(folder)
        if not folder.is_dir():
            return {"status": "failed", "detail": f"프로젝트 폴더가 없다: {folder}"}
        if not force and importer.harness_ready(folder):
            return {"status": "ready", "detail": "이미 깔려 있다"}
        found = script(setting)
        if found is None:
            return {"status": "skipped", "detail": "하네스가 없어 세팅을 건너뜀"}
        lines: list[str] = []

        def keep(line: str) -> None:
            if str(line).strip():
                lines.append(str(line).strip())
                del lines[:-5]

        saved = _snapshot(folder)
        try:
            code = run_process([sys.executable, str(found), "--auto", str(folder)], cwd=found.parents[3],
                               log=keep, timeout=timeout)
        finally:  # whatever the run did, a file a person wrote stays as it was
            restored = _restore(folder, saved)
        back = f" (기존 파일 되돌림: {', '.join(restored)})" if restored else ""
        if code != 0:
            tail = lines[-1] if lines else ""
            return {"status": "failed", "detail": f"exit {code}" + (f": {tail}" if tail else "") + back}
        if not importer.harness_ready(folder):
            return {"status": "failed", "detail": "bootstrap.py 가 끝났지만 CLAUDE.md · Docs 가 아직 없다" + back}
        return {"status": "done", "detail": "bootstrap.py --auto" + back}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"status": "failed", "detail": f"{type(e).__name__}: {e}"}
    except Exception as e:  # noqa: BLE001 — nothing here may stop an import
        log.exception("studio: 하네스 세팅이 예외로 끝났다")
        return {"status": "failed", "detail": f"{type(e).__name__}: {e}"}


def _text(result: dict) -> str:
    status, detail = result["status"], result.get("detail") or ""
    if status == "done":
        return "하네스 세팅 완료(bootstrap.py --auto)" + (detail[len("bootstrap.py --auto"):] if "되돌림" in detail else "")
    if status == "skipped":
        return detail or "하네스가 없어 세팅을 건너뜀"
    return f"하네스 세팅 실패 — 가져오기는 됐다: {detail}"


def note(store, project: dict, result: dict) -> bool:
    """One system message about `result` in HR's 1:1 room (`message.created` in the same transaction). Nothing
    for a project that was ready. False when nothing was written (ready, or no HR room)."""
    if result.get("status") == "ready":
        return False
    with store.transaction() as tx:
        chosen = policy.hr_room(store, tx, project)
        if chosen is None:
            return False
        person, room = chosen
        message = store.create_message(
            room["id"], "system", body=_text(result),
            meta={"event": EVENT, "project_id": project["id"], "status": result["status"]}, tx=tx)
        store.emit(tx, "message.created", {"message": message}, project_id=person["project_id"],
                   room_id=room["id"], task_id=None)
    return True
