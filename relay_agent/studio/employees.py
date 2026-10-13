"""Employee folders, `employee.yaml`, the seed employee (S-7, 1/2) and the HR employee every studio has (H-2).

Contract: design/studio/ARCHITECTURE.md §9.1 (the four files, the order of the prompt, the 4 KB of MEMORY.md),
§9.2 (the eight keys and the part's defaults), §9.4 (the eleven checks and their `failed[]` codes), §9.5 (the
values of a chat turn, the cap), §9.6 (a file that cannot be read), §4.9 (the derived status), §2.2 · §3.15
(the seed employee), §3.5 · §3.15 (the HR employee and the HR part's defaults).

Every way an `employee.yaml` is written goes through `check()`: the two approvals of inbox.py (hire ·
guide_amend) and HR's drafts (drafts.py · tools.py). What the checks must know of the server — the relay
folder, the MCP names, the providers — comes in as `Checks` (`context(cfg)` makes one), so no check is
skipped for want of it. This module reads no Config and imports none of inbox · drafts · tools · tasks · api.

A key written without a value (`mcp:`) counts as a key that is not written: the part's default stands (§9.2).

Provisional (S-7): the summary of a room is its newest messages listed, each cut short — no model is called.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from ..pipeline import EFFORT_ORDER, MODEL_ORDER, cap_effort, cap_model
from ..providers import ProviderRegistry
from .store import StudioStore

FILES = ("PERSONA.md", "GUIDE.md", "MEMORY.md", "employee.yaml")  # §9.1, in the order they are written
FILE_KEYS = {"persona_md": "PERSONA.md", "guide_md": "GUIDE.md", "memory_md": "MEMORY.md",
             "employee_yaml": "employee.yaml"}  # the keys of `files` (§6.4) -> the file
KEYS = ("version", "department", "relay", "mcp", "chat_mcp", "chat", "work", "cap")  # §9.2: these eight only
SLOT_KEYS = ("provider", "model", "effort")
CAP_KEYS = ("model", "effort")
CAP_ORDER = {"model": MODEL_ORDER, "effort": EFFORT_ORDER}
CODES = ("yaml_invalid", "unknown_key", "version_unsupported", "department_mismatch", "relay_unknown",
         "mcp_unknown", "chat_mcp_not_subset", "provider_unguarded", "effort_unsupported", "cap_invalid",
         "over_cap")  # §9.4, by the number of the check
READ_CODES = CODES[:3]  # §9.6: a file with one of these cannot be read — no turn and no run for the employee
VERSION = 1
AUTO = "auto"
STUDIO_MCP = "studio"  # the server always attaches it; an employee.yaml does not name it
GUARDED_KIND = "claude_cli"  # the only runner a guard hook can be put on (§9.4 의 8)
RELAY_NAME = re.compile(r"[A-Za-z0-9_-]+")  # a name, never a path
MEMORY_LIMIT = 4096  # bytes of UTF-8 (§9.1)
SUMMARY_MESSAGES = 20
SUMMARY_CHARS = 200
SECTIONS = ("PERSONA.md", "GUIDE.md", "MEMORY.md", "프로젝트 정책 (POLICY.md)", "방의 최근 글")  # §9.1: this order
SEED_ID = "emp_5eed0001"
SEED_NAME = "시드"  # provisional, as are the files of SEED_DIR
SEED_DEPARTMENT = "program"
SEED_DIR = Path(__file__).with_name("seed")
HR_DIR = Path(__file__).with_name("hr")  # the HR employee's own files (H-1 · H-2): the original, not provisional
HR_NAME = "인사"  # the title of hr/PERSONA.md; the HR employee's row takes this name
HR_ID = "emp_68720001"  # the HR employee a start makes (§3.15); fixed, as the seed's
HR_DEPARTMENT = "hr"
PARTS_DIR = Path(__file__).with_name("parts")  # the originals of the four other parts (H-1 · T3): <id>.md · defaults.yaml
PART_IDS = ("design_doc", "program", "design_setup", "art")  # the parts `fill_parts` fills, in the order of the seed
MODEL_KEYS = ("chat", "work", "cap")  # the keys of `departments.default_models` (§9.2)


def _problem(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _names(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(name, str) for name in value)


def _name_list(value: Any) -> list[str]:
    """The MCP names of a list, once each and without `studio`; [] for what is not a list."""
    out: list[str] = []
    for name in value if isinstance(value, list) else []:
        if isinstance(name, str) and name and name != STUDIO_MCP and name not in out:
            out.append(name)
    return out


def _slot(value: Any) -> dict:
    """A slot as it is used: its three keys, only where a value is written."""
    return {key: value[key] for key in SLOT_KEYS if isinstance(_mapping(value).get(key), str) and value[key]}


# --- what the checks know of the server ---------------------------------------------------------------------
@dataclass(frozen=True)
class Checks:
    """The relay folder, the names of the MCP registry and the providers (§9.4 의 5 · 6 · 8 · 9)."""

    relays_dir: Path
    mcp_names: frozenset
    providers: ProviderRegistry

    def relay_known(self, relay: object) -> bool:
        if not isinstance(relay, str):
            return False
        return relay == AUTO or (RELAY_NAME.fullmatch(relay) is not None
                                 and (self.relays_dir / f"{relay}.yaml").is_file())

    def guarded(self, provider: object) -> bool:
        """Empty, or a Claude Code runner. A name nobody knows is not one."""
        if provider is None or provider == "":
            return True
        spec = self.providers.specs.get(provider) if isinstance(provider, str) else None
        return spec is not None and spec.kind == GUARDED_KIND

    def efforts(self, provider: object, model: str) -> list[str]:
        """`efforts_for` of the slot. An empty provider is the Claude Code runner: the only one 8 lets through."""
        if provider is None or provider == "":
            provider = next((name for name, spec in self.providers.specs.items() if spec.kind == GUARDED_KIND), None)
        return self.providers.efforts_for(provider, model) if isinstance(provider, str) else []


def context(cfg: Any) -> Checks:
    """The Checks of a server Config. Made anew each time: the config is the one thing that says."""
    return Checks(Path(cfg.relays_dir), frozenset(cfg.mcp_registry),
                  ProviderRegistry(cfg.providers, cfg.fallback_chain))


def config_cap(cfg: Any) -> dict:
    """The last cap of §9.5 의 6 as `{model, effort}`: `Config.studio_model_cap` · `studio_effort_cap`. What
    `resolve()` · `read_settings()` take as `config_cap` — for a task's run and a chat turn alike."""
    return {"model": getattr(cfg, "studio_model_cap", None), "effort": getattr(cfg, "studio_effort_cap", None)}


# --- the checks (§9.4) --------------------------------------------------------------------------------------
def parse(text: object) -> tuple[dict | None, str]:
    """Check 1: (the mapping, "") or (None, why)."""
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError:
            return None, "employee.yaml 이 UTF-8 이 아니다"
    if not isinstance(text, str):
        return None, "employee.yaml 의 본문이 없다"
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, RecursionError) as e:  # ValueError: a date that is none (2026-13-45)
        return None, f"employee.yaml 을 YAML 로 읽지 못했다: {' '.join(str(e).split())[:200]}"
    if not isinstance(data, dict):
        return None, "employee.yaml 은 키와 값의 묶음(매핑)이어야 한다"
    return data, ""


def _inspect(data: dict, flag: Callable[[str, str], None]) -> list[tuple[str, dict]]:
    """What needs nothing but the file: checks 2 · 3 and the form of `chat` · `work`. Returns the slots that
    can be looked at as (where, slot); one of the wrong form is flagged `yaml_invalid` and left out."""
    extra = [str(key) for key in data if key not in KEYS]
    if extra:
        flag("unknown_key", f"모르는 키가 있다: {', '.join(extra)}")
    version = data.get("version")
    if type(version) is not int or version != VERSION:  # not True, not "1"
        flag("version_unsupported", f"version 은 {VERSION} 이어야 한다 (받음: {version!r})")
    extra = [str(key) for key in _mapping(data.get("cap")) if key not in CAP_KEYS]
    if extra:
        flag("unknown_key", f"cap 에 모르는 키가 있다: {', '.join(extra)}")

    slots: list[tuple[str, dict]] = []

    def take(where: str, slot: Any) -> None:
        if slot is None:  # nothing written: the relay file's values
            return
        if not isinstance(slot, dict):
            flag("yaml_invalid", f"{where} 는 칸({{provider, model, effort}})이어야 한다")
            return
        extra = [str(key) for key in slot if key not in SLOT_KEYS]
        if extra:
            flag("unknown_key", f"{where} 에 모르는 키가 있다: {', '.join(extra)}")
        if slot.get("model") is not None and not isinstance(slot["model"], str):
            flag("yaml_invalid", f"{where}.model 은 문자열이어야 한다")
            return
        slots.append((where, slot))

    take("chat", data.get("chat"))
    work = data.get("work")
    if work is not None and not isinstance(work, dict):
        flag("yaml_invalid", 'work 는 {단계 이름 또는 "*": 칸} 이어야 한다')
    for stage, slot in _mapping(work).items():
        if isinstance(stage, str) and stage:
            take(f"work.{stage}", slot)
        else:
            flag("unknown_key", f"work 의 단계 이름은 문자열이어야 한다 (받음: {stage!r})")
    return slots


def check(text: object, *, department_id: str | None, department: dict | None, checks: Checks) -> list[dict]:
    """The checks of §9.4 on the text of an `employee.yaml`: `[{code, message}]`, [] when it may be written.

    `department_id` is `employees.department_id` (for a hire draft, `draft.department_id`); `department` is
    that part's row, or None when there is none. A text that fails check 1 gets `yaml_invalid` alone. Else
    every failed check is there once, by its number. A value of the wrong type fails the check of its key
    (`relay: 3` -> `relay_unknown`); a `chat` · `work` · slot that is no mapping is `yaml_invalid`."""
    data, why = parse(text)
    if data is None:
        return [_problem("yaml_invalid", why)]
    found: dict[str, list[str]] = {}

    def flag(code: str, message: str) -> None:
        found.setdefault(code, []).append(message)

    slots = _inspect(data, flag)  # 2 · 3
    if not isinstance(department_id, str) or data.get("department") != department_id:  # 4
        flag("department_mismatch",
             f"department 는 직원의 파트와 같아야 한다 (파일: {data.get('department')!r}, 파트: {department_id})")
    relay = data.get("relay")
    if relay is not None and not checks.relay_known(relay):  # 5
        flag("relay_unknown", f"relay 는 auto 이거나 relays/ 에 있는 릴레이의 이름이어야 한다 (받음: {relay!r})")
    mcp = data.get("mcp")
    if mcp is not None and not _names(mcp):  # 6
        flag("mcp_unknown", "mcp 는 MCP 서버 이름의 목록이어야 한다")
    elif mcp:
        unknown = [name for name in mcp if name not in checks.mcp_names]
        if unknown:
            flag("mcp_unknown", f"MCP 레지스트리에 없는 이름: {', '.join(unknown)}")
        if STUDIO_MCP in mcp:
            flag("mcp_unknown", "studio 는 서버가 항상 붙이므로 mcp 에 적지 않는다")
    chat_mcp = data.get("chat_mcp")
    if chat_mcp is not None and not _names(chat_mcp):  # 7
        flag("chat_mcp_not_subset", "chat_mcp 는 MCP 서버 이름의 목록이어야 한다")
    elif chat_mcp:
        within = _mapping(department).get("default_mcp") if mcp is None else mcp
        outside = [name for name in chat_mcp if name not in (within if _names(within) else [])]
        if outside:
            flag("chat_mcp_not_subset", f"chat_mcp 가 mcp 밖의 이름을 가졌다: {', '.join(outside)}")
    cap, limit = data.get("cap"), {"model": None, "effort": None}
    if cap is not None and not isinstance(cap, dict):  # 10
        flag("cap_invalid", "cap 은 {model, effort} 여야 한다")
    for key, order in CAP_ORDER.items():
        value = _mapping(cap).get(key)
        if isinstance(value, str) and value in order:
            limit[key] = value
        elif value is not None:
            flag("cap_invalid", f"cap.{key} 는 {' · '.join(order)} 중 하나여야 한다 (받음: {value!r})")
    for where, slot in slots:  # 8 · 9 · 11
        provider, model, effort = slot.get("provider") or None, slot.get("model") or None, slot.get("effort")
        if not checks.guarded(provider):
            flag("provider_unguarded", f"{where}.provider 는 비우거나 Claude Code 러너여야 한다 (받음: {provider!r})")
        if effort is None or effort == "":
            pass
        elif not isinstance(effort, str) or effort not in EFFORT_ORDER:
            flag("effort_unsupported",
                 f"{where}.effort 는 {' · '.join(EFFORT_ORDER)} 중 하나여야 한다 (받음: {effort!r})")
        else:
            allowed = checks.efforts(provider, model) if model else []
            if allowed and effort not in allowed:
                flag("effort_unsupported",
                     f"{where}: {model} 은 노력치 {effort} 를 받지 않는다 (받는 값: {' · '.join(allowed)})")
            if cap_effort(effort, limit["effort"]) != effort:
                flag("over_cap", f"{where}.effort {effort} 가 cap.effort {limit['effort']} 를 넘는다")
        if model and cap_model(model, limit["model"]) != model:
            flag("over_cap", f"{where}.model {model} 이 cap.model {limit['model']} 을 넘는다")
    return [_problem(code, " / ".join(found[code])) for code in CODES if code in found]


def memory_problems(text: str) -> list[dict]:
    """§9.1: a MEMORY.md body over 4096 bytes is not written (`memory_too_large`). For the `PUT` of H-3 · H-4."""
    size = len(text.encode("utf-8"))
    if size <= MEMORY_LIMIT:
        return []
    return [_problem("memory_too_large", f"MEMORY.md 는 UTF-8 로 {MEMORY_LIMIT}바이트까지다 (받음: {size}바이트)")]


# --- the folder (§9.1) --------------------------------------------------------------------------------------
def folder(studio_dir: Path | str, employee_id: str) -> Path:
    return Path(studio_dir) / "employees" / employee_id


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else None
    except (OSError, UnicodeDecodeError):
        return None


def read_files(studio_dir: Path | str, employee_id: str) -> dict[str, str | None]:
    """The four files as text, by file name; None for one that is not there or cannot be read as UTF-8."""
    root = folder(studio_dir, employee_id)
    return {name: _read(root / name) for name in FILES}


def write_files(target: Path, texts: dict[str, str], *, keep: bool = False) -> None:
    """Make the folder and write the four files in the order of FILES (bytes: no CRLF on Windows). A folder
    that is there already is an OSError; with `keep` it is used and the files it has are left as they are."""
    target.mkdir(parents=True, exist_ok=keep)
    for name in FILES:
        if not (keep and (target / name).exists()):
            (target / name).write_bytes(texts[name].encode("utf-8"))


# --- reading: the part's defaults (§9.2 · §9.6) and the values of a turn (§9.5) -------------------------------
class Unreadable(Exception):
    """§9.6: the `employee.yaml` is missing or fails one of the checks 1–3 — no turn and no run is made for
    the employee. `problems` is `[{code, message}]`, `codes` its codes."""

    def __init__(self, problems: list[dict]):
        super().__init__(" / ".join(p["message"] for p in problems))
        self.problems = problems
        self.codes = [p["code"] for p in problems]


def resolve(data: dict, employee: dict, department: dict | None, config_cap: dict | None = None) -> dict:
    """The settings of an employee: `{department, relay, mcp, chat_mcp, chat, work, cap}`.

    `department` is the DB's, whatever the file says (§9.6). A key the file does not have takes the part's:
    `default_relay` · `default_mcp` · `default_models` (chat · work · cap); `chat_mcp` is [] and never wider
    than `mcp`. `cap` is `{model, effort}`, each looked for on its own: the file -> the part -> `config_cap`
    -> None (no cap). Nothing is written back. A value of the wrong type counts as not written — the checks
    refuse it when the file is written, and a file changed by hand must not stop the reading."""
    part = _mapping(department)
    models = _mapping(part.get("default_models"))
    relay = data.get("relay") if isinstance(data.get("relay"), str) and data["relay"] else part.get("default_relay")
    mcp = _name_list(data["mcp"] if _names(data.get("mcp")) else part.get("default_mcp"))
    work = data["work"] if isinstance(data.get("work"), dict) else models.get("work")
    caps = (_mapping(data.get("cap")), _mapping(models.get("cap")), _mapping(config_cap))
    return {
        "department": employee["department_id"], "relay": relay, "mcp": mcp,
        "chat_mcp": [name for name in _name_list(data.get("chat_mcp")) if name in mcp],
        "chat": _slot(data["chat"] if isinstance(data.get("chat"), dict) else models.get("chat")),
        "work": {stage: _slot(slot) for stage, slot in _mapping(work).items() if isinstance(stage, str)},
        "cap": {key: next((c[key] for c in caps if isinstance(c.get(key), str) and c[key] in order), None)
                for key, order in CAP_ORDER.items()},
    }


def read_settings(studio_dir: Path | str, employee: dict, department: dict | None,
                  config_cap: dict | None = None) -> dict:
    """`resolve()` of the employee's file as it is now (§9.1: read at the start of every turn). `Unreadable`
    when the file is missing or fails a check of 1–3."""
    text = read_files(studio_dir, employee["id"])["employee.yaml"]
    if text is None:
        raise Unreadable([_problem("yaml_invalid", "employee.yaml 이 없거나 UTF-8 로 읽지 못했다")])
    data, why = parse(text)
    if data is None:
        raise Unreadable([_problem("yaml_invalid", why)])
    found: dict[str, list[str]] = {}
    _inspect(data, lambda code, message: found.setdefault(code, []).append(message))
    problems = [_problem(code, " / ".join(found[code])) for code in READ_CODES if code in found]
    if problems:
        raise Unreadable(problems)
    return resolve(data, employee, department, config_cap)


def lower(slot: dict, cap: dict) -> dict:
    """§9.5 의 6 for a slot of an employee.yaml: a value over the cap becomes the cap. A model lowered to
    haiku loses its effort, as in the engine."""
    out = dict(slot)
    model = cap_model(out.get("model"), cap.get("model"))
    if model != out.get("model"):
        out["model"] = model
        if ProviderRegistry.tier(model) == "haiku":
            out.pop("effort", None)
    if out.get("effort"):
        out["effort"] = cap_effort(out["effort"], cap.get("effort"))
    return out


def chat_values(settings: dict, checks: Checks) -> dict:
    """What a chat or read turn is made with (§9.5): `{slot, mcp, model_cap, effort_cap, failed}`.

    `slot` is `chat` under the cap, `mcp` is `chat_mcp` + `studio`, the two caps go to the engine as well.
    `failed` is `["provider_unguarded"]` when the slot names a provider that is no Claude Code runner — the
    turn is not made then. The provider the relay's stage names is looked at where the turn is made."""
    slot, cap = lower(settings["chat"], settings["cap"]), settings["cap"]
    return {"slot": slot, "mcp": [*settings["chat_mcp"], STUDIO_MCP], "model_cap": cap["model"],
            "effort_cap": cap["effort"], "failed": [] if checks.guarded(slot.get("provider")) else ["provider_unguarded"]}


# --- the prompt (§9.1) --------------------------------------------------------------------------------------
def clip_memory(text: str) -> str:
    """MEMORY.md as it goes into a prompt: as it is up to 4096 bytes, else whole lines from the back up to
    that. When the last line alone is longer, the end of that line (cut between characters)."""
    raw = text.encode("utf-8")
    if len(raw) <= MEMORY_LIMIT:
        return text
    kept: list[str] = []
    size = 0
    for line in reversed(text.splitlines(keepends=True)):
        size += len(line.encode("utf-8"))
        if size > MEMORY_LIMIT:
            break
        kept.append(line)
    return "".join(reversed(kept)) if kept else raw[-MEMORY_LIMIT:].decode("utf-8", errors="ignore")


def room_summary(messages: Iterable[dict], names: dict[str, str] | None = None) -> str:
    """The newest SUMMARY_MESSAGES messages (given oldest first) as `author: body` lines, a body on one line
    and cut at SUMMARY_CHARS. `names` is employee id -> name."""
    lines = []
    for message in list(messages)[-SUMMARY_MESSAGES:]:
        body = " ".join((message.get("body") or "").split())
        if not body:
            continue
        who = {"owner": "owner", "system": "시스템"}.get(message.get("author_kind"))
        who = who or (names or {}).get(message.get("author_id")) or message.get("author_id") or "직원"
        lines.append(f"{who}: {body[:SUMMARY_CHARS]}{'…' if len(body) > SUMMARY_CHARS else ''}")
    return "\n".join(lines)


def assemble(persona: str | None, guide: str | None, memory: str | None, policy: str | None = None,
             summary: str | None = None) -> str:
    """The employee's part of a prompt, in the order of §9.1: PERSONA.md -> GUIDE.md -> MEMORY.md (clipped) ->
    the project's policy -> the room's recent messages. An empty part is left out."""
    texts = (persona, guide, clip_memory(memory or ""), policy, summary)
    return "\n\n".join(f"===== {title} =====\n{text.strip()}" for title, text in zip(SECTIONS, texts)
                       if text and text.strip())


def prompt_for(store: StudioStore, studio_dir: Path | str, employee: dict, room_id: str | None = None,
               project_id: str | None = None) -> str:
    """`assemble()` from what is on disk and in the room now. `employee.yaml` is not part of it (§9.1)."""
    texts = read_files(studio_dir, employee["id"])
    policy = _read(Path(studio_dir) / "projects" / project_id / "POLICY.md") if project_id else None
    summary = ""
    if room_id:
        names = {row["id"]: row["name"] for row in store.list_employees(include_retired=True)}
        summary = room_summary(store.list_messages(room_id, limit=SUMMARY_MESSAGES), names)
    return assemble(texts["PERSONA.md"], texts["GUIDE.md"], texts["MEMORY.md"], policy, summary)


# --- the derived status (§4.9) ------------------------------------------------------------------------------
def derive_status(current: str, held: Iterable[str]) -> str:
    """The first rule of §4.9 that fits. `current` counts only as the owner's leave; `held` is the statuses of
    the tasks the employee is the assignee of. `TaskMachine.recompute_employee` writes the status in the task's
    transaction with the same rules written out — tasks.py imports nothing but the store, and this module
    imports the engine's caps. tests/test_studio_employees.py holds the two together."""
    if current == "on_leave":
        return "on_leave"
    held = set(held)
    return "working" if "working" in held else "blocked" if "blocked" in held else "idle"


# --- the seed employee (§2.2 · §3.15) -----------------------------------------------------------------------
def seed_texts() -> dict[str, str]:
    """The four files of the seed employee from relay_agent/studio/seed/. MEMORY.md is empty, as at a hire."""
    return {name: "" if name == "MEMORY.md" else (SEED_DIR / name).read_text(encoding="utf-8") for name in FILES}


def hr_texts() -> dict[str, str]:
    """The four files of the HR employee from relay_agent/studio/hr/. MEMORY.md is empty, as at a hire."""
    return {name: "" if name == "MEMORY.md" else (HR_DIR / name).read_text(encoding="utf-8") for name in FILES}


def seed(store: StudioStore, studio_dir: Path | str) -> dict | None:
    """Make the seed employee — the folder with its four files, the row, the 1:1 room — in a DB that has no
    employee at all. Retired employees count, so a seed that was retired does not come back, and a second
    call makes nothing. Returns the row, or None when nothing was made."""
    if store.list_employees(include_retired=True):
        return None
    target = folder(studio_dir, SEED_ID)
    fresh = not target.exists()
    employee = None
    try:  # the files too: a folder made here goes when they cannot all be written
        write_files(target, seed_texts(), keep=True)  # a folder left by a start that broke off is used
        with store.transaction() as tx:
            if not store.list_employees(include_retired=True, tx=tx):  # the write lock is ours from here
                employee = _enroll(store, tx, SEED_ID, SEED_NAME, SEED_DEPARTMENT)
    except BaseException:
        employee = None
        raise
    finally:
        if employee is None and fresh:
            shutil.rmtree(target, ignore_errors=True)
    return employee


def _enroll(store: StudioStore, tx: Any, employee_id: str, name: str, department_id: str) -> dict:
    """What the seed and the HR employee write in their one transaction: the row (not placed), the 1:1 room,
    the membership of that room and of the part's room, and the three events in that order. Returns the row."""
    employee = store.create_employee(name, department_id, id=employee_id, tx=tx)
    dm = store.create_room("dm", title=name, employee_id=employee_id, tx=tx)
    store.create_room_member(dm["id"], employee_id, tx=tx)
    store.emit(tx, "employee.updated", {"employee": employee, "change": "created"})
    store.emit(tx, "room.updated", {"room": dm, "members": store.list_room_members(room_id=dm["id"], tx=tx)},
               room_id=dm["id"])
    for part in store.list_rooms(kind="department", department_id=department_id, tx=tx)[:1]:
        store.create_room_member(part["id"], employee_id, tx=tx)
        members = store.list_room_members(room_id=part["id"], tx=tx)
        store.emit(tx, "room.updated", {"room": part, "members": members}, room_id=part["id"])
    return employee


# --- the HR employee every studio has (H-2; §3.5 · §3.15) ---------------------------------------------------
def hr_defaults() -> dict:
    """What the HR part's row takes where it has nothing (§3.4): `guide_template` is hr/GUIDE.md and
    `default_models` is `{chat: …}` of hr/employee.yaml — the same originals the HR employee is made from."""
    texts = hr_texts()
    data, _ = parse(texts["employee.yaml"])
    chat = _mapping(_mapping(data).get("chat"))
    return {"guide_template": texts["GUIDE.md"], "default_models": {"chat": dict(chat)} if chat else {}}


HIRING_TITLE = "채용에 쓰는 값"  # the section `hiring_notes` makes: in the prompt of HR's chat turn only (§8.4)


def hiring_notes(store: StudioStore, checks: Checks, catalog: Iterable[dict] | None = None) -> str:
    """What HR writes in the draft of a hire and has no tool to ask for — the REST paths that list them are the
    owner's (§8.4 인사 전용): the projects that are not archived, the parts but HR's own with the `chat` default
    of each, and of `catalog` (`ProviderRegistry.catalog()`) the models of the Claude Code runners with the
    efforts each takes. A catalog that is empty or not given leaves the models out. One section, titled the way
    `assemble` titles its own; it is no part of `assemble` — a task's run does not get it.

    H-1: for each of those parts, also what a key that is not written takes (the relay, the MCP, `work`, `cap`)
    and the body of its `guide_template` — the frame HR fills for `guide_md`. A part without one says so."""
    lines: list[str] = []
    projects = store.list_projects()
    if projects:
        lines.append("프로젝트 — `project_id` 에 id 를 적는다. 비우면 미배치다:")
        lines += [f"- `{row['id']}` {row['name']}" for row in projects]
    else:
        lines.append("프로젝트: 아직 없다. `project_id` 를 비운다.")
    lines.append("파트 — `department_id` 와 `employee_yaml` 의 `department` 에 id 를 적는다:")
    parts = [part for part in store.list_departments() if part["id"] != HR_DEPARTMENT]
    for part in parts:
        slot = _mapping(_mapping(part.get("default_models")).get("chat"))
        default = " · ".join(str(slot[key]) for key in CAP_KEYS if slot.get(key)) or "기본값 없음"
        lines.append(f"- `{part['id']}` {part['name']} (chat 기본값: {default})")
    lines.append("파트 기본값 — `employee_yaml` 에 적지 않은 키가 따르는 값이다. owner 가 다르게 정했을 때만 적는다:")
    lines += [_defaults_line(part) for part in parts]
    models: dict[str, list] = {}
    for item in catalog or ():
        if not (isinstance(item, dict) and item.get("provider") and checks.guarded(item["provider"])):
            continue
        for entry in item.get("models") or ():
            name = _mapping(entry).get("model")
            if isinstance(name, str) and name:
                models.setdefault(name, list(entry.get("efforts") or ()))
    if models:
        lines.append("모델 — `employee_yaml` 의 `chat` 에 적는다 (지금 이 PC 에서 쓸 수 있는 것):")
        lines += [f"- `{name}` 노력치 {' · '.join(map(str, efforts))}" if efforts
                  else f"- `{name}` 노력치 없음 — `effort` 를 적지 않는다" for name, efforts in models.items()]
    lines.append("GUIDE 틀 — `guide_md` 는 그 파트의 틀을 면담 내용으로 채운다. 틀이 없는 파트는 범용 틀을 쓴다:")
    for part in parts:
        frame = part.get("guide_template").strip() if isinstance(part.get("guide_template"), str) else ""
        lines.append(f"--- `{part['id']}` {part['name']} 의 틀 ---")
        lines.append(frame or "(틀 없음 — 범용 틀)")
    return f"===== {HIRING_TITLE} =====\n" + "\n".join(lines)


def _slot_text(slot: Any) -> str:
    """`opus/high`: the model and the effort of a slot or a cap, whichever is written."""
    return "/".join(str(slot[key]) for key in CAP_KEYS if _mapping(slot).get(key))


def _defaults_line(part: dict) -> str:
    """One line of `hiring_notes`: what an employee.yaml of this part takes for the keys it does not write."""
    models = _mapping(part.get("default_models"))
    work = ", ".join(f"{stage}={_slot_text(slot)}" for stage, slot in _mapping(models.get("work")).items()
                     if _slot_text(slot))
    values = (("relay", part.get("default_relay")), ("mcp", ", ".join(_name_list(part.get("default_mcp")))),
              ("work", work), ("cap", _slot_text(models.get("cap"))))
    return f"- `{part['id']}` " + " · ".join(f"{key} {value or '없음'}" for key, value in values)


# --- the four other parts (H-1 · T3; §3.4 · §3.15) --------------------------------------------------------------
def part_defaults() -> dict[str, dict]:
    """What the rows of PART_IDS take where they have nothing, from relay_agent/studio/parts/: `guide_template`
    is `<id>.md`, `default_mcp` and `default_models` (`{chat, work, cap}`) are that part's entry of
    `defaults.yaml`. The files are the original a person reads and changes; the relay is the seed's (schema.py)."""
    data, _ = parse((PARTS_DIR / "defaults.yaml").read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for part_id in PART_IDS:
        entry = _mapping(_mapping(data).get(part_id))
        out[part_id] = {
            "guide_template": (PARTS_DIR / f"{part_id}.md").read_text(encoding="utf-8"),
            "default_mcp": _name_list(entry.get("mcp")),
            "default_models": {key: entry[key] for key in MODEL_KEYS if isinstance(entry.get(key), dict) and entry[key]},
        }
    return out


def defaults_problems(department_id: str, *, relay: Any, mcp: Any, models: Any, checks: Checks) -> list[dict]:
    """The checks of §9.4 on a part's defaults: `[{code, message}]`, [] when an employee.yaml that writes none of
    its keys would pass with them. The defaults are read as the keys of the same name of an employee.yaml of that
    part and handed to `check`. `relay` · `mcp` of None are left out (not looked at); `models` takes `chat` ·
    `work` · `cap` only — another key is `unknown_key`, and what is no mapping is `yaml_invalid`."""
    if not isinstance(models, dict):
        return [_problem("yaml_invalid", "default_models 는 {chat, work, cap} 이어야 한다")]
    extra = [str(key) for key in models if key not in MODEL_KEYS]
    body: dict[str, Any] = {"version": VERSION, "department": department_id}
    body.update({key: models[key] for key in MODEL_KEYS if key in models})
    if relay is not None:
        body["relay"] = relay
    if mcp is not None:
        body["mcp"] = mcp
    found = check(yaml.safe_dump(body, allow_unicode=True), department_id=department_id,
                  department={"id": department_id, "default_mcp": mcp if _names(mcp) else []}, checks=checks)
    if not extra:
        return found
    message = f"default_models 에 모르는 키가 있다: {', '.join(extra)}"
    known = [p for p in found if p["code"] == "unknown_key"]
    for problem in known:
        problem["message"] = f"{message} / {problem['message']}"
    found = found if known else [_problem("unknown_key", message), *found]
    return [p for code in CODES for p in found if p["code"] == code]


# --- a part proposed with a hire (T3; §5.5) ----------------------------------------------------------------
PART_GUIDE = """# 지침

- 맡은 일: {job}
- 담당 범위:
- 하지 않는 것:
- 끝나면 한 일 · 확인한 방법 · 남은 문제를 적고 `task_request_review` 로 검수를 올린다.

## 결재 기준

- 묻고 한다 (`ask_owner` 로 무엇을 · 왜 · 안 하면 어떻게 되는지를 올린다):
- 알아서 한다:
- 알아서 한 판단은 검수를 올릴 때 적는다.
"""


def proposes_part(draft: dict) -> bool:
    """A hire draft proposes a new part when its `part_name` is not empty."""
    name = draft.get("part_name")
    return isinstance(name, str) and bool(name.strip())


def proposed_part(draft: dict) -> tuple[dict | None, str]:
    """The part a hire draft proposes, as a row would carry it (`id` = the draft's `department_id`; no MCP, no
    sort_order), and "" — or (None, why) when `part_models` is not a YAML mapping of `chat` · `work` · `cap`."""
    models: Any = {}
    text = draft.get("part_models")
    if isinstance(text, str) and text.strip():
        try:
            models = yaml.safe_load(text)
        except (yaml.YAMLError, ValueError, RecursionError) as e:
            return None, f"part_models 를 YAML 로 읽지 못했다: {' '.join(str(e).split())[:200]}"
        if not isinstance(models, dict):
            return None, "part_models 는 chat · work · cap 의 묶음(매핑)이어야 한다"
    job = " ".join(str(draft.get("part_job") or "").split())
    return {"id": (draft.get("department_id") or "").strip(), "name": (draft.get("part_name") or "").strip(),
            "default_relay": (draft.get("part_relay") or "").strip() or None, "default_mcp": [],
            "default_models": models, "guide_template": PART_GUIDE.replace("{job}", job)}, ""


def part_proposal_problems(draft: dict, parts: list[dict], checks: Checks) -> list[dict]:
    """What would refuse the new part of a hire draft: `[{code, message}]`. `parts` are all the rows, archived
    ones too. The defaults go through the checks of §9.4 as an employee.yaml's keys (`defaults_problems`), so a
    relay that is not in relays/ is `relay_unknown`."""
    out: list[dict] = []
    part, why = proposed_part(draft)
    name = (draft.get("part_name") or "").strip()
    if not " ".join(str(draft.get("part_job") or "").split()):
        out.append(_problem("part_job_missing", "새 파트가 하는 일(part_job)이 비어 있다"))
    if any(row["name"] == name for row in parts):
        out.append(_problem("part_name_taken", f"같은 이름의 파트가 이미 있다 (보관된 파트 포함): {name}"))
    if part is not None and any(row["id"] == part["id"] for row in parts):
        out.append(_problem("part_id_taken", f"같은 id 의 파트가 이미 있다: {part['id']}"))
    if part is None:
        out.append(_problem("part_models_invalid", why))
    else:
        out += defaults_problems(part["id"], relay=part["default_relay"], mcp=None, models=part["default_models"],
                                 checks=checks)
    return out


HANDOVER_TITLE = "## 인계"
_APPROVAL_TITLE = "## 결재 기준"


def with_handover(guide: str, original: str) -> str:
    """H-5: `guide` with the '인계' section of `original` added, only when `original` has one and `guide` has
    a value but neither a line that starts `## 인계` nor the word `handoff` (an owner who rewrote the section
    keeps it). The section goes before the first `## 결재 기준` line, else at the end; the rest of `guide` is
    not changed. Idempotent: the result has the word `handoff`."""
    if not guide.strip() or "handoff" in guide:
        return guide
    guide_lines = guide.splitlines(keepends=True)
    if any(line.startswith(HANDOVER_TITLE) for line in guide_lines):
        return guide
    lines = original.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith(HANDOVER_TITLE)), None)
    if start is None:
        return guide
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    section = "".join(lines[start:end]).strip()
    at = next((i for i, line in enumerate(guide_lines) if line.startswith(_APPROVAL_TITLE)), None)
    if at is None:
        return guide.rstrip() + "\n\n" + section + "\n"
    return "".join(guide_lines[:at]).rstrip() + "\n\n" + section + "\n\n" + "".join(guide_lines[at:])


def fill_parts(store: StudioStore) -> list[dict]:
    """Fill `guide_template` · `default_mcp` · `default_models` of the rows of PART_IDS from `part_defaults()`,
    each cell on its own and only while it is empty ('' · [] · {}): a value that is there is never written over,
    so a DB that already runs gets the originals and keeps what its owner changed. The one exception (H-5):
    a `guide_template` that has a value gets the original's '## 인계' section added (`with_handover`) when it
    has none — nothing else of it is changed; idempotent, no event; a section deleted on purpose comes back
    unless the guide says `handoff`. `default_relay` is not
    touched — NULL says "no relay", and the seed has the four. One transaction, no event. Returns the rows
    that were written, in the order of PART_IDS; [] when nothing was empty.

    `default_models` is one cell: it is filled only while the whole of it is empty, and `chat` · `work` · `cap`
    are not merged key by key — the owner's `chat` under the original's `cap` is a pair no check has seen. A
    cell emptied on purpose is filled again at the next start: nothing remembers that it was emptied."""
    written: list[dict] = []
    defaults = part_defaults()
    with store.transaction() as tx:
        for part_id, values in defaults.items():
            part = store.get_department(part_id, tx=tx)
            if part is None:
                continue
            fields = {key: value for key, value in values.items() if value and not part.get(key)}
            if "guide_template" not in fields and part.get("guide_template"):
                added = with_handover(part["guide_template"], values.get("guide_template", ""))
                if added != part["guide_template"]:
                    fields["guide_template"] = added
            if fields:
                written.append(store.update_department(part_id, tx=tx, **fields))
    return written


def fill_hr_part(store: StudioStore) -> dict | None:
    """Fill the HR part's `guide_template` · `default_models`, each on its own and only while it is empty: a
    value that is there is never written over. Returns the row when one was written, else None (nothing was
    empty, or there is no HR part). No event."""
    with store.transaction() as tx:
        part = store.get_department(HR_DEPARTMENT, tx=tx)
        if part is None:
            return None
        fields = {key: value for key, value in hr_defaults().items() if value and not part.get(key)}
        return store.update_department(HR_DEPARTMENT, tx=tx, **fields) if fields else None


def _free_name(store: StudioStore, tx: Any, name: str) -> str:
    """`name`, or `name 2`, `name 3` … — the first no employee has. A retired employee's name is taken (§3.5)."""
    taken = {row["name"] for row in store.list_employees(include_retired=True, tx=tx)}
    pick, number = name, 1
    while pick in taken:
        number += 1
        pick = f"{name} {number}"
    return pick


def ensure_hr(store: StudioStore, studio_dir: Path | str) -> dict | None:
    """Make the HR employee — the folder with its four files, the row (not placed), the 1:1 room — in a DB
    that has no row of the HR part at all, whatever other employees it has. Retired ones count, so an HR that
    was retired does not come back, and a second call makes nothing: a name or a file that was changed stays as
    it is. The name is HR_NAME, or the next free one when an employee has it. As `seed`: a folder left by a
    start that broke off is used with the files it has, and a folder made here goes when its files cannot be
    written or the row is not made. Returns the row, or None when nothing was made."""
    if store.list_employees(department_id=HR_DEPARTMENT, include_retired=True):
        return None
    target = folder(studio_dir, HR_ID)
    fresh = not target.exists()
    employee = None
    try:
        write_files(target, hr_texts(), keep=True)
        with store.transaction() as tx:
            if not store.list_employees(department_id=HR_DEPARTMENT, include_retired=True, tx=tx):
                employee = _enroll(store, tx, HR_ID, _free_name(store, tx, HR_NAME), HR_DEPARTMENT)
    except BaseException:
        employee = None
        raise
    finally:
        if employee is None and fresh:
            shutil.rmtree(target, ignore_errors=True)
    return employee


def hr_guides_behind(store: StudioStore, studio_dir: Path | str) -> list[str]:
    """The ids of the HR employees who are not retired and whose GUIDE.md does not name HIRING_TITLE: a guide
    made before the section was, which tells HR nothing of where the ids and the models are. `ensure_hr` writes
    over no file, so a start can only say so (api.py `ensure_hr`) — the owner brings it up to hr/GUIDE.md by
    hand. A guide that is not there or cannot be read is left out: nothing in it is behind."""
    behind = []
    for row in store.list_employees(department_id=HR_DEPARTMENT):
        text = _read(folder(studio_dir, row["id"]) / "GUIDE.md")
        if text is not None and HIRING_TITLE not in text:
            behind.append(row["id"])
    return behind
