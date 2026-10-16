"""The harness gates of S-11 ② (CAMPAIGN 219–222행, ARCHITECTURE §4.3 T6 · §5.4 `harness_checks` · §5.3).

Three judgements live here, so that no other module has its own:

  * a program-natured employee is placed only in a project the harness is laid in (`placement_problem`; the
    judgement of "the harness is laid" is `importer.harness_ready`). A part is program-natured when its id is
    `program` or its default relay is `auto` (`needs_harness`; schema.py seeds `auto` for `program` only, the other
    four are `doc-write` · `editor-task` · NULL), so no column is added to the departments table. Two limits: an
    `employee.yaml` that overrides the relay to `auto` is not looked at (the T6 gate below stops feature code
    whatever the part), and an employee already placed is not checked again;
  * a `feature` task whose run changed code asks for the review (T6) only after a `structure_choice` of that task
    was decided `choose` (`judge`);
  * the `harness_checks[]` of a review package (`checks`).

What "the code was changed" means. A file counts as code when its extension is one of `CODE_EXTS` (C / C++ /
C# / Python sources and headers, Unreal shaders). A run's changed files are the targets of its edit tools
(`RelayEngine.EDIT_TOOLS`: Edit · Write · MultiEdit · NotebookEdit) in the run's event log — the run is still at
work at T6, so it has no `result.patch` yet — plus the files of the patch when there is one. What the gate does
not see: a file written through Bash or through the editor's MCP tools (these show in the package's
`harness_checks` after the run has ended), and a Blueprint (`.uasset` is a binary and is not counted as code; a feature that changed only Blueprints (`ASSET_EXTS`) makes
`harness_checks.structure_choice` a `warning`, not a refusal).
Documents and settings (`.md` · `.ini` · `.json` …) are not code, so a feature that changed only those is not
asked for a structure choice.

This module imports neither `review` nor `inbox`, so both may import it."""
from __future__ import annotations

from pathlib import PurePath
from typing import Any, Iterable

from ..pipeline import RelayEngine
from . import importer

PROGRAM = "program"          # the id of the program part (the part a harness is laid for)
CODE_RELAY = "auto"          # the default relay of a program-natured part (schema.py seeds it for `program`)
ASSET_EXTS = (".uasset", ".umap")   # Blueprints / maps: binaries, not counted as code; shown as a warning
FEATURE = "feature"          # the task type whose code asks for a structure choice
CODE_EXTS = (".h", ".hpp", ".hh", ".inl", ".c", ".cc", ".cpp", ".cxx", ".cs", ".py", ".usf", ".ush")
MISSING = "structure_choice_missing"   # the code in `failed` of the T6 refusal
NO_HARNESS = "harness_missing"         # the code in `failed` of the refused placement
LISTED = 20                  # files / warnings kept in `data` of a check
STATUSES = ("passed", "failed", "warning", "info")


def is_code(path: str) -> bool:
    return PurePath(str(path).replace("\\", "/")).suffix.lower() in CODE_EXTS


def code_paths(paths: Iterable[str]) -> list[str]:
    """The code files among `paths`, each once, in order."""
    seen: dict[str, None] = {}
    for path in paths:
        if isinstance(path, str) and path and is_code(path):
            seen.setdefault(path.replace("\\", "/"), None)
    return list(seen)


def asset_paths(paths: Iterable[str]) -> list[str]:
    """The Blueprint / map files among `paths`, each once, in order."""
    seen: dict[str, None] = {}
    for path in paths:
        if isinstance(path, str) and path:
            norm = path.replace("\\", "/")
            if PurePath(norm).suffix.lower() in ASSET_EXTS:
                seen.setdefault(norm, None)
    return list(seen)


# --- placement ----------------------------------------------------------------------------------------------
def needs_harness(part: dict | None) -> bool:
    """Whether the employees of that part (a departments row, or the row a hire draft proposes) are program-natured."""
    if not part:
        return False
    return part.get("id") == PROGRAM or part.get("default_relay") == CODE_RELAY


def placement_problem(part: dict | None, project: dict | None) -> str | None:
    """Why an employee of that part cannot be placed in that project, None when they can. Only a program-natured
    part needs the harness; no project (placing nowhere) and the other parts are not asked."""
    if not needs_harness(part) or project is None:
        return None
    path = (project.get("path") or "").strip()
    state = importer.harness_state(path)
    if all(state.values()):
        return None
    missing = [label for name, label in (("claude_md", "CLAUDE.md"), ("docs", "Docs 폴더")) if not state[name]]
    who = ("프로그램 파트 직원" if part.get("id") == PROGRAM
           else f"코드 릴레이({CODE_RELAY})를 쓰는 파트 '{part.get('name') or part.get('id')}' 직원")
    return (f"프로젝트 '{project.get('name')}' 에 하네스가 깔려 있지 않아(없는 것: {', '.join(missing)}) "
            f"{who}을(를) 배치할 수 없다. 하네스를 먼저 깐다")


# --- the structure choice ------------------------------------------------------------------------------------
def chosen(store: Any, task_id: str, tx: Any = None) -> dict | None:
    """The newest `structure_choice` of that task that was decided `choose` — `revise` is no ground (§5.5)."""
    items = store.list_inbox(kind="structure_choice", task_id=task_id, status="decided", tx=tx)
    found = [item for item in items if item.get("decision") == "choose"]
    return found[-1] if found else None


def option_of(item: dict | None) -> str | None:
    data = (item or {}).get("decision_data")
    return data.get("option_id") if isinstance(data, dict) else None


def edited(engine: Any, run_id: str | None) -> list[str]:
    """The targets of the edit tools of that run, from its event log. [] when it cannot be read."""
    if engine is None or not run_id:
        return []
    try:
        events = engine.history.events(run_id)
    except Exception:  # noqa: BLE001 - a log that cannot be read adds nothing
        return []
    out: list[str] = []
    for e in events:
        d = e.get("detail")
        if e.get("kind") == "tool_use" and isinstance(d, dict) and d.get("tool") in RelayEngine.EDIT_TOOLS:
            target = d.get("target")
            if isinstance(target, str) and target and target not in out:
                out.append(target)
    return out


def judge(task: dict, paths: Iterable[str], done: dict | None) -> tuple[str | None, str]:
    """(code, why) of the T6 gate: a refusal when a feature changed code without a decided structure choice."""
    if task.get("type") != FEATURE or done is not None:
        return None, ""
    code = code_paths(paths)
    if not code:
        return None, ""
    shown = ", ".join(code[:3]) + (f" 외 {len(code) - 3}개" if len(code) > 3 else "")
    return (MISSING,
            f"feature 작업에서 코드가 바뀌었는데({shown}) 결정된 구조 선택 결재가 없다. "
            "`ask_owner(kind = choice)` 로 선택지를 올려 결정받은 뒤 다시 검수를 요청한다")


# --- the package ---------------------------------------------------------------------------------------------
def _check(id: str, status: str, note: str, data: dict | None = None) -> dict:
    return {"id": id, "status": status, "note": note, "data": data or {}}


def checks(task: dict | None, payload: dict, files: list[dict], done: dict | None, record: dict | None) -> list[dict]:
    """`harness_checks[]` of a review package: `[{id, status, note, data}]`, five of them — structure_choice ·
    checklist · skills · mcp · guard_warnings. `files` is `changes.files`, `done` the decided structure choice
    (`chosen`), `record` the run's `runlog.snapshot` (None = it could not be read)."""
    out: list[dict] = []
    listed = [f.get("path") for f in files if isinstance(f, dict)]
    code, assets = code_paths(listed), asset_paths(listed)
    feature = bool(task) and task.get("type") == FEATURE
    if not feature:
        out.append(_check("structure_choice", "passed", "feature 작업이 아니라 구조 선택을 요구하지 않는다"))
    elif not code and done is None and assets:
        out.append(_check("structure_choice", "warning", "블루프린트가 바뀌었다 — 구조 선택이 필요한 변경인지 사람이 본다",
                          {"inbox_id": None, "option_id": None, "code_files": [], "asset_files": assets[:LISTED]}))
    elif not code:
        out.append(_check("structure_choice", "passed", "코드가 바뀌지 않았다"))
    elif done is not None:
        out.append(_check("structure_choice", "passed", f"구조 선택이 결정됐다 (결재 {done['id']})",
                          {"inbox_id": done["id"], "option_id": option_of(done), "code_files": code[:LISTED]}))
    else:
        out.append(_check("structure_choice", "failed", "코드가 바뀌었는데 결정된 구조 선택 결재가 없다",
                          {"inbox_id": None, "option_id": None, "code_files": code[:LISTED]}))
    marks: dict[str, int] = {}
    for row in payload.get("checklist") or []:
        if isinstance(row, dict):
            marks[str(row.get("status"))] = marks.get(str(row.get("status")), 0) + 1
    counts = {key: marks.get(key, 0) for key in ("verified_auto", "verified_manual", "unverified")}
    out.append(_check("checklist", "warning" if counts["unverified"] else "passed",
                      f"완료 기준 {sum(counts.values())}개 — 자동 확인 {counts['verified_auto']} · 직접 확인 "
                      f"{counts['verified_manual']} · 미확인 {counts['unverified']}", counts))
    if record is None:
        for id in ("skills", "mcp", "guard_warnings"):
            out.append(_check(id, "info", "런 기록을 읽지 못했다"))
        return out
    skills = [s.get("name") for s in record.get("skills") or [] if isinstance(s, dict)]
    out.append(_check("skills", "info", f"스킬 호출 {len(skills)}번", {"names": skills[:LISTED]}))
    counts_in = record.get("counts") or {}
    calls, errors = counts_in.get("mcp_calls", 0), counts_in.get("mcp_errors", 0)
    out.append(_check("mcp", "warning" if errors else "info", f"MCP 호출 {calls}번, 오류 {errors}번",
                      {"calls": calls, "errors": errors}))
    guards = [g for g in record.get("guards") or [] if isinstance(g, dict)]
    out.append(_check("guard_warnings", "warning" if guards else "info",
                      f"가드 경고 {len(guards)}건 (판정 {counts_in.get('guard_judged', 0)}건)",
                      {"warnings": guards[:LISTED]}))
    return out
