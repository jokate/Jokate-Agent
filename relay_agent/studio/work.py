"""Task runs (S-8, part 1): the values a task's run is made with, and the run itself.

Contract: design/studio/ARCHITECTURE.md §9.5 (the seven steps, the cap, `auto`), §11.2 (what T3 hands to
`RelayEngine.create`), §4.3 T3 (a run that cannot be made, a project that holds the server's code), §6.4
(`GET /studio/models`).

The seven steps, each time a task's run is made:
1. the relay: `tasks.relay` -> the `relay` of `employee.yaml` -> the part's. `auto` is picked now, once, by the
   engine's `route()` among the relays whose stages all name Claude Code; 2–7 go on with what was picked;
2. `work` is spread over that relay's stages (`spread`): the stage's own key, else `*`, else no slot;
3. the task's override (`tasks.stage_models_json`) replaces a stage's slot whole — an empty one is "the relay's";
4. a stage the relay has not: refused in the override, dropped from `employee.yaml`;
5. (가) every stage runs on Claude Code — the slot's provider, else the relay file's; (나) the model of an
   overriding slot is one `GET /studio/models` lists, its effort one that model takes;
6. the cap: a slot of the override over it is refused, a slot of `employee.yaml` is lowered to it;
7. `create`, with `auto_apply=False`, `approval="ai"`, the employee's `mcp` + `studio`, and `pin_copy` for a
   project that holds this server's code.
`settle` is 2–6 on values alone. `WorkEffects` is the `Effects` of the task machine: `create_run` does 1–7 for
T3 and raises `Refused` — the machine then leaves the task in backlog with the reason (§4.3) —, and
`check_start` is what a `start` request looks at before the task joins the line.

No FastAPI and no api.py here: server.py connects it (`api.use_effects`), tests use it with an engine of their own.

The line and its slots, `stop` and advancing the run are studio/queue.py's (S-8, part 2): `WorkEffects.kick` is
how a task that joined the line reaches it.

Part 3 is at the end of this module (§4.7 '런이 멈췄을 때' · §4.8). `stopped` is what the thread of a slot does
when its run has stopped: step 2 — the decision texts still owed go into the run (`deliver`) —, a `run` row for
each `stage_skipped` (`skipped`), and step 3, the table (`table`): T4 by the system, or the line again for a
failed run that goes on by itself. `recover` is the five steps of the server's start. A run that waits for
approval is filed by `runs.RunSink` when `advance` lets go of it; the table asks the same code
(`runs.file_stop`), which files nothing for a task that is blocked by then — so no stop is filed twice.
A task's run is not narrowed with `mcp_only`: the project's own MCP servers are part of the work. In a folder
without a project context `studio` and the registry servers the employee picked are attached all the same (§9.5).
"""

from __future__ import annotations

import dataclasses
import difflib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from .. import projctx
from ..config import ROOT
from ..pipeline import RelaySpec, cap_effort, cap_model
from . import campaigns, cutoff, employees, gate, harness_root, plugins, review, runs
from . import editor as editor_control
from . import machine as machine_info
from .importer import find_uproject
from .inbox import SYSTEM_ROOM, InboxError
from .store import StudioStore
from .tasks import PRE_EFFECT, STATE_INBOX, SYSTEM, Effects, TransitionError

CODES = ("unknown_stage", "provider_unguarded", "model_unavailable", "effort_unsupported", "model_over_cap",
         "effort_over_cap")  # §9.5: the `failed[]` of a `start` that is refused, in the order of the steps 4 · 5 · 6
ANY = "*"  # the key of `work` that stands for every stage without a key of its own
SERVER_CODE = "relay_agent"  # the folder of the server root a project must not hold to be worked on in place
# T4: a line in the goal of a program employee's task run that names the structure-document order (the order itself is
# the `## 구조 문서 결재` section of program.md — the employee's guide)
STRUCTURE_DOC_FEATURE = ("구조 문서 결재 대상 — 코드를 바꾸기 전에 지침의 「구조 문서 결재」 순서대로 한다 "
                         "(HTML 구조 문서를 작업방에 올리고 structure_choice 결재를 받는다).")
STRUCTURE_DOC_CHANGE = "구조를 바꾸는 일(리팩터링)이면 코드를 바꾸기 전에 지침의 「구조 문서 결재」 순서대로 한다."
STRUCTURE_DOC_NOTES = {"feature": STRUCTURE_DOC_FEATURE, "fix": STRUCTURE_DOC_CHANGE, "chore": STRUCTURE_DOC_CHANGE}
GOAL_SECTIONS = (("사유", "reason"), ("명세", "spec"), ("완료 기준", "done_criteria"))  # after the title, this order
RELAY_ERRORS = (OSError, ValueError, yaml.YAMLError)  # what reading a relay file raises
JOINS_LINE = ("queued", "T5", "T8")  # what `Effects.after` hears when a task has got a `queued_at` (§4.6)
GATES = {"T5": "rework", "T7": "pass", "T8": "rework", "T9": "revert", "T11": "revert"}  # rule -> what `review.gate` judges
INSTEAD_OF_CANCEL = "취소 대신 결재를 결정해(T5) 작업을 이어 간다"  # §4.3 T11: what a cancel that cannot revert is told
# the bodies of the `question` the system files when a task's run has stopped (§4.7 의 표 · §5.3)
STOPPED = "owner 가 작업을 중지했다. 다시 시도하거나 지금까지 한 것으로 검수를 요청할 수 있다."
CANCELLED = "작업 런이 취소됐다. 다시 시도하거나 지금까지 한 것으로 검수를 요청할 수 있다."
FAILED = "작업 런이 실패했다: {error}\n다시 시도하거나 지금까지 한 것으로 검수를 요청할 수 있다."
NOT_ADVANCED = "작업 런을 진행시키지 못했다: {error}\n다시 시도하거나 지금까지 한 것으로 검수를 요청할 수 있다."
NO_REVIEW = "작업 런이 검수 요청(`task_request_review`) 없이 끝났다. 답하면 남은 일을 마치고 검수를 요청하게 한다."
REPEAT = "같은 이유로 {n}번째 실패입니다 — 다시 시도보다 '새로 시작'이나 '검수 요청'을 권합니다"  # §4.7: from the 2nd, same run
NOT_VERIFIED = "검증 못 함: {why}"   # the note of the checklist when the server files the review a wrapped-up run did not
SIMILAR = 0.7                      # how alike two errors are, 0..1, for 'the same reason' (`similar`)
RESTARTED = "서버 재시작"   # how the error of a run `RelayEngine.recover_interrupted` closed begins
FOLDER_BUSY = "다른 실행("  # in the error of a run whose folder another run was editing (`pipeline.py` _advance)
AGAIN = "끊긴 런을 다시 잇는다"
RESTART_WHY = "런이 실패해 새 대화로 다시 시작한다"
RESTARTED_AGAIN = "자동으로 {n}번 다시 시작했지만 또 실패했다"
SKIPPED = "stage_skipped"   # the engine's event of an `optional` stage no AI could run (§4.7)
WITH_RUN = ("working", "blocked", "review")  # the states whose run the recovery reads (§4.8 의 1)
BROKEN = "작업 '{title}' ({id}) 이 {status} 인데 대기 중인 결재가 없다. 고치지 않고 알린다 — 작업 상태를 확인하라."
FILED, REQUEUED = "filed", "requeued"  # what `table` did: the keys of what `recover` returns
# the editor check of a run that uses the editor's MCP (M-7; §4.3 T3 · §4.5 · §4.7)
EDITOR_MCP = plugins.DEFAULT_SERVER   # the MCP server whose name makes a run one that uses the editor
EDITOR_EVENT = "editor_unresponsive"  # the reason of the question (§5.3), and `detail.event` of the `run` row
EDITOR_DOWN = ("작업 런을 진행하기 전에 에디터가 응답하지 않았다: {why}\n"
               "에디터를 확인한 뒤 다시 시도한다 — 에디터를 다시 검사하고 런을 잇는다.")
EDITOR_LOST = ("작업 런이 도는 동안 에디터가 응답하지 않아 런을 끊었다 (모달 창 · 크래시일 수 있다).\n"
               "에디터를 확인한 뒤 다시 시도한다 — 에디터를 다시 검사하고 런을 잇는다.")
EDITOR_CUT = "에디터가 응답하지 않아 런을 끊었다"
# the `run` row of `editor_lost` when the question was filed and the run had ended before it could be cut
EDITOR_ENDED = "에디터가 응답하지 않았지만 런은 끊기 전에 이미 끝나 있었다"

log = logging.getLogger(__name__)

# the '빌드·검증' section of every stage of a task run in an Unreal project (§9.1). The tool names of the editor's MCP
# are the plugin's (MNYS `Plugins/Unreal-MCP-main`, outside this repository and not read): the server does not know
# them, so the section says to find them with ToolSearch instead of naming them. Once the names are confirmed, edit
# TOOL_HINT (and nothing else) to name them.
BUILD_TITLE = "빌드·검증"
LIVE_CODING = "LiveCoding.Compile"  # the Unreal console command of a Live Coding compile
TOOL_HINT = ("MCP 도구 이름은 서버가 모른다 — ToolSearch 로 찾는다 (검색어 예: `unreal console command` 는 콘솔 명령 실행, "
             "`unreal PIE play stop` 은 PIE 시작·정지). 찾지 못하면 도구 이름을 지어내지 말고, 그 일을 하지 못했다고 "
             "`unverified` 에 적는다.")
BUILD_TOOL = "editor_build"  # the studio MCP tool that builds the C++ (studio/editor_build.py)
BUILD_TOOL_OFF = (f"C++ 를 바꿨으면 MCP 도구 `{BUILD_TOOL}` 로 빌드한다 — 에디터를 저장하고 닫은 뒤 그 엔진의 Build.bat 으로 "
                  f"빌드하고 에디터를 다시 켠다.")
# Live Coding needs an editor that answers, so only the section of a running one names it
BUILD_TOOL_LINE = BUILD_TOOL_OFF + f" Live Coding(`{LIVE_CODING}`)은 .cpp 안의 작은 수정에만, 원하면 쓴다."
UNVERIFIED = ("빌드나 측정을 하지 못하면 검수 요청(`task_request_review`)의 checklist `unverified` 에 "
              "무엇을 왜 하지 못했는지 적는다 — 해 본 것처럼 쓰지 않는다.")


PLAY_REQUEST = ("입력 · 손맛 · 타이밍처럼 사람이 플레이해야 볼 수 있는 검증은, 빌드하고 PIE 를 띄운 뒤 `ask_owner` 로 "
                "무엇을 어떤 순서로 해 달라는지(시나리오 · 몇 번 · 끝나면 무엇을 누를지)를 올리고 턴을 끝낸다. "
                "owner 가 끝냈다고 답하면 로그를 읽어 표를 만든다.")


def build_section(project: dict, uproject: Path, state: dict, engines: list[dict]) -> str:
    """The section a stage of a task run in an Unreal project ends with: how to build and verify, from
    what the server saw when the stage started — `state` is `editor.status` ({running, healthy}), `engines` is
    `[{version, path, exists}]`. An editor that answers: Live Coding through the MCP console command, no UBT. Else
    `Build.bat` of the project's engine, and no such line when the engine's folder is not known."""
    stem = uproject.stem
    port = project.get("mcp_port")
    log_file = Path(str(project.get("path") or uproject.parent)) / "Saved" / "Logs" / f"{stem}.log"
    lines = [f"===== {BUILD_TITLE} =====", "아래는 이 단계를 시작할 때 서버가 본 값이다 — 도는 동안 에디터 상태는 바뀔 수 있다."]
    if state.get("running") and state.get("healthy") and port:
        lines += [f"- 에디터: 켜져 있고 MCP(포트 {port})가 응답한다 (이 단계 시작 시점).",
                  f"- 빌드: {BUILD_TOOL_LINE}",
                  "- PIE: MCP 도구로 시작하고 정지한다.",
                  f"- 도구: {TOOL_HINT}"]
    else:
        if not state.get("running"):
            why = "꺼져 있다"
        elif not port:
            why = "켜져 있지만 프로젝트에 MCP 포트가 정해져 있지 않다"
        else:
            why = f"켜져 있지만 MCP(포트 {port})가 응답하지 않았다"
        version = str(project.get("engine_version") or "").strip()
        engine = next((e for e in engines if version and str(e.get("version")) == version and e.get("exists")), None)
        lines.append(f"- 에디터: {why} (이 단계 시작 시점).")
        if engine is not None:
            lines.append(f"- 빌드: {BUILD_TOOL_OFF}")
        else:
            lines.append("- 빌드: 이 컴퓨터의 엔진 폴더를 몰라 빌드 명령을 줄 수 없다 — 명령을 지어내지 않는다.")
        lines.append("- PIE: 에디터가 켜져 MCP 가 응답할 때만 MCP 도구로 시작하고 정지한다. 지금은 측정하지 못할 수 있다.")
    lines += [f"- 사람이 플레이: {PLAY_REQUEST}", f"- 로그: `{log_file}`", f"- {UNVERIFIED}"]
    return "\n".join(lines)


class _Stopped(Exception):
    """`WorkEffects.editor_ready` only: the owner's `stop` came while the editor was waited for."""


def _problem(code: str, stage: str | None, message: str) -> dict:
    return {"code": code, "stage": stage, "message": message}


def _by_step(problems: Iterable[dict]) -> list[dict]:
    """In the order of CODES (a code of another list last); within a code, as they were found."""
    return sorted(problems, key=lambda p: CODES.index(p["code"]) if p["code"] in CODES else len(CODES))


class Refused(Exception):
    """The run is not made (§4.3 T3 '런을 만들지 못하면'). `problems` is `[{code, stage, message}]` for what has
    a code — §9.5's six, and the codes of an `employee.yaml` that cannot be read (§9.6) —, else []; `codes` is
    its codes, once each. The text is what the task's `run` event keeps."""

    def __init__(self, message: str = "", problems: Iterable[dict] = ()):
        self.problems = _by_step(problems)
        self.codes = list(dict.fromkeys(p["code"] for p in self.problems))
        told = " / ".join(f"{p['message']} ({p['code']})" for p in self.problems)
        super().__init__(" — ".join(part for part in (message, told) if part))


# --- the values (§9.5 의 2–6) and the models (§6.4) -----------------------------------------------------------
def models(catalog: Iterable[dict], checks: employees.Checks) -> list[dict]:
    """`GET /studio/models`: of `ProviderRegistry.catalog()` — the models usable now, the benched ones marked —
    the entries of a provider a guard hook can be put on. Each entry is given as it is."""
    return [item for item in catalog if item.get("provider") and checks.guarded(item["provider"])]


def _slot(value: Any) -> dict:
    """A slot as it is used: its three keys, only where a value is written. {} for what is no slot."""
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in employees.SLOT_KEYS if isinstance(value.get(key), str) and value[key]}


def spread(work: dict | None, names: Iterable[str]) -> dict[str, dict]:
    """Step 2: `work` over the stages of a relay — the stage's own key, else `*`, else no slot. A key that is
    there with nothing in it is that stage's all the same ("the relay's values", not `*`). Keys the relay has
    no stage for are dropped (step 4, for `employee.yaml`)."""
    work = work or {}
    out: dict[str, dict] = {}
    for name in names:
        slot = _slot(work[name] if name in work else work.get(ANY))
        if slot:
            out[name] = slot
    return out


def judge(stage: str, slot: dict, provider: str | None, relay_model: str | None, cap: dict,
          checks: employees.Checks, listed: Iterable[dict]) -> list[dict]:
    """Steps 5 (나) and 6 on one slot of the override. `provider` is what the stage would run on (None = not
    known yet: every listed provider counts), `relay_model` the model the relay file gives that stage — an
    effort written without a model is that model's. An effort is not looked at beside a model nobody lists."""
    model, effort, found = slot.get("model"), slot.get("effort"), []
    offered: dict[Any, dict] = {}
    for item in listed:
        if not provider or item.get("provider") == provider:
            for entry in item.get("models") or []:
                offered.setdefault(entry.get("model"), entry)
    if model and model not in offered:
        found.append(_problem("model_unavailable", stage,
                              f"[{stage}] 모델 {model} 은 지금 고를 수 있는 목록(GET /studio/models)에 없다"))
    elif effort:
        used = model or relay_model
        if used in offered:
            allowed = list(offered[used].get("efforts") or [])
        elif used:
            allowed = checks.efforts(provider, used)
        else:
            allowed = sorted({e for entry in offered.values() for e in entry.get("efforts") or []})
        if effort not in allowed:
            found.append(_problem("effort_unsupported", stage,
                                  f"[{stage}] {used or '고를 수 있는 모델'} 은 노력치 {effort} 를 받지 않는다 "
                                  f"(받는 값: {' · '.join(allowed) or '없음'})"))
    if model and cap_model(model, cap.get("model")) != model:
        found.append(_problem("model_over_cap", stage, f"[{stage}] 모델 {model} 이 상한 {cap.get('model')} 을 넘는다"))
    if effort and cap_effort(effort, cap.get("effort")) != effort:
        found.append(_problem("effort_over_cap", stage,
                              f"[{stage}] 노력치 {effort} 가 상한 {cap.get('effort')} 를 넘는다"))
    return found


def settle(stages: Iterable[Any], work: dict | None, override: dict | None, cap: dict,
           checks: employees.Checks, listed: Iterable[dict]) -> tuple[dict[str, dict], list[dict]]:
    """Steps 2–6: (the slots `create` takes as `stage_models`, the problems that refuse the run).

    `stages` are the relay's (`name` · `primary` · `model`), `work` the employee's (`resolve()`), `override` the
    task's `stage_models`, `cap` `{model, effort}`, `listed` what `models()` gave. The slots are in the order of
    the stages; a stage without one runs on the relay file's values. Nothing is lowered in the override — a
    value the owner picked does not change without a word —, and the slots mean nothing when there are problems."""
    stages, listed = list(stages), list(listed)
    by_name = {stage.name: stage for stage in stages}
    slots = {name: employees.lower(slot, cap) for name, slot in spread(work, by_name).items()}  # 2, and 6 for the file
    problems, own = [], {}
    for name, value in (override or {}).items():  # 3 · 4
        if name not in by_name:
            problems.append(_problem("unknown_stage", str(name),
                                     f"[{name}] 이 릴레이에 없는 단계다 (있는 단계: {' · '.join(by_name)})"))
            continue
        own[name] = _slot(value)
        slots.pop(name, None)
        if own[name]:
            slots[name] = dict(own[name])
    for stage in stages:  # 5 (가): whichever of the three named it — the override, the file, the relay
        provider = slots.get(stage.name, {}).get("provider") or stage.primary
        if not checks.guarded(provider):
            problems.append(_problem("provider_unguarded", stage.name,
                                     f"[{stage.name}] provider {provider} 는 Claude Code 가 아니다 — 가드를 걸 수 없다"))
    for name, slot in own.items():  # 5 (나) · 6: a slot of the file goes to the engine's own checks, lowered
        problems += judge(name, slot, slot.get("provider") or by_name[name].primary, by_name[name].model, cap,
                          checks, listed)
    return {stage.name: slots[stage.name] for stage in stages if stage.name in slots}, _by_step(problems)


def unguarded_relays(relays_dir: Path | str, checks: employees.Checks) -> list[str]:
    """§9.5 `auto`: the relays a router must not pick for a studio run — those with a stage whose relay file
    names a provider that is no Claude Code runner. A file that cannot be read is no candidate anyway."""
    out = []
    for path in sorted(Path(relays_dir).glob("*.yaml")):
        try:
            spec, _ = RelaySpec.load(path)
        except RELAY_ERRORS:
            continue
        if any(not checks.guarded(stage.primary) for stage in spec.stages):
            out.append(path.stem)
    return out


# --- a project that holds the server's code (§4.3 T3) ---------------------------------------------------------
def _parts(path: Any) -> tuple[str, ...] | None:
    """A path as §10.2 의 4 compares them: quotes off, variables and `~` opened, `/c/…` · `/mnt/c/…` and `\\\\?\\`
    put right on Windows, resolved (links, short names, `..`), and without regard to case. None = no path."""
    text = str(path or "").strip().strip("\"'")
    if not text:
        return None
    text = os.path.expandvars(os.path.expanduser(text))
    if os.name == "nt":
        if text.startswith("\\\\?\\"):
            text = text[4:]
        drive = re.match(r"/(?:mnt/)?([A-Za-z])(?:/|$)", text)
        if drive:
            text = f"{drive.group(1)}:/{text[drive.end():]}"
    try:
        return tuple(part.casefold() for part in Path(text).resolve().parts)
    except (OSError, ValueError, RuntimeError):
        return None


def holds_server_code(*paths: Any, root: Path | str | None = None) -> bool:
    """Is one of the paths the server's `relay_agent/`, a folder inside it, or a folder that holds it (the
    server root and everything above)? `root` is the server root (`config.ROOT`). A run in such a project is
    made with `pin_copy`: it never writes in the folder the server runs from."""
    code = _parts(Path(root or ROOT) / SERVER_CODE)
    for path in paths:
        mine = _parts(path)
        if code and mine and (mine[:len(code)] == code or code[:len(mine)] == mine):
            return True
    return False


def uses_editor(run, repo_spec=None) -> bool:
    """Does that run work through the editor (M-7)? `EDITOR_MCP` is among the servers picked for the run
    (`run.mcp`), the repository's own (`repo_spec.mcp`) or those of a stage the relay has still to run
    (`stages[run.index:]`). A relay file that cannot be read adds nothing."""
    names = [*(getattr(run, "mcp", None) or []), *(getattr(repo_spec, "mcp", None) or [])]
    try:
        spec, _ = RelaySpec.load(Path(run.relay))
        for stage in spec.stages[int(getattr(run, "index", 0) or 0):]:
            names += list(getattr(stage, "mcp", None) or [])
    except RELAY_ERRORS:
        pass
    return EDITOR_MCP in names


# --- the effects of the task machine ------------------------------------------------------------------------
class WorkEffects(Effects):
    """What T3 does outside the studio DB, on one engine. `store` gives the StudioStore (`api.get_store`),
    `catalog` the models that can be picked now — the engine's `ProviderRegistry.catalog` when it has a
    registry, else the config's —, `server_root` the folder `holds_server_code` measures against.

    `kick` is the queue's look at the line (`queue.TaskQueue.kick`; server.py sets it): `after` calls it when a
    task has joined the line. None = nobody starts or resumes a run.

    `check` and `before` are the final review's (S-12; studio/review.py part 2): the conditions of T5 · T7 · T8
    · T9 · T11 on the task's run, and — T7 · T9 · T11 — the run closed, then its changes applied and committed,
    or reverted. `holds` is the queue's "does that task hold a slot" (`queue.TaskQueue.holds`; server.py sets
    it): a run whose thread has not let go of its slot has not stopped yet (§4.6). None = no queue, so nothing
    holds one. The harness gate of T6 (S-11 ②; `gate.py`) is `check`'s: a feature's code
    without a decided structure choice.

    `campaign` is the look at the studio's campaigns (S-14; `CampaignRunner.advance_studio`, server.py sets it):
    `after` calls it when a change may have made a task of a running campaign ready, or ended the campaign
    (`campaigns.LOOKS`, §4.10). None = no campaign goes on by itself.

    The editor check (M-7; §4.3 T3 · §4.7) is `editor_ready`, which the queue calls with its slot, after T3 and
    before a run is taken up again: a run that uses the editor (`uses_editor`) in a project with an `mcp_port`
    goes on only once that editor answers — one that is off is started and waited for, `cfg.studio_editor_wait`
    seconds —, else the system asks `editor_unresponsive` (T4) and the run stays as it was. The queue also gives
    it the cancel event of the slot (`stopped`): an owner's `stop` ends the wait at its next look and nothing is
    asked about the editor — the queue files `run_cancelled`. `editor_engines` ·
    `editor_processes` · `editor_healthy` · `editor_tools` are where it reads the computer and how it starts the
    editor; a test replaces them. `watch` (server.py sets it: `editor_watch.EditorWatch.begin`) is told of each
    run that passed and looks at the editor while the run is at work. None = nobody looks."""

    kick: Callable[[], Any] | None = None
    holds: Callable[[str], bool] | None = None
    campaign: Callable[..., Any] | None = None  # (campaign id or None), and `again=True` after a start · resume
    watch: Callable[[str, str], Any] | None = None  # (task id, run id): the run passed the editor check
    staffing_look: Callable[[dict, dict | None], Any] | None = None  # (task, its proposal item): §5.3 triggers

    def __init__(self, engine, cfg, store: Callable[[], StudioStore],
                 catalog: Callable[[], list[dict]] | None = None, server_root: Path | str | None = None):
        self.engine, self.cfg, self.store = engine, cfg, store
        self._catalog, self.server_root = catalog, server_root
        self._guard, self._deciding = threading.Lock(), {}  # task id -> the lock of its pass · revert · cancel
        self.editor_engines: Callable[[], list[dict]] = machine_info.engines
        self.editor_processes: Callable[[], list[dict]] = machine_info.editor_processes
        self.editor_healthy: Callable[[Any], bool] = machine_info.health_check
        self.editor_tools = editor_control.Tools()

    def staffing(self, task: dict, item: dict | None) -> None:
        """A task was filed: `staffing_look` (server.py sets it) looks at the triggers for a hire (§5.3)."""
        if self.staffing_look is not None:
            self.staffing_look(task, item)

    def folder(self, task: dict) -> Path | None:
        """The folder that task's run works in: what the queue asks `RelayEngine.folder_holder` about before it
        gives a slot (§4.6). None when it cannot be told — no project, a repo this PC has not."""
        try:
            return self._folder(self.store(), task)[2]
        except Refused:
            return None

    def catalog(self) -> list[dict]:
        """`ProviderRegistry.catalog()`, not narrowed yet: what `api.use_effects(models=…)` is given."""
        if self._catalog is not None:
            return self._catalog()
        providers = getattr(self.engine, "providers", None) or employees.context(self.cfg).providers
        return providers.catalog()

    # --- Effects ------------------------------------------------------------------------------------------
    def create_run(self, task: dict, *, restart_from: str | None = None, note: str = "") -> str:
        """T3: steps 1–7 with what the employee's file, the part, the config and the task say now, and the run
        `create` made of them — pending, not advanced. `Refused` when a step says no or the engine does."""
        store = self.store()
        employee = self._assignee(store, task)
        settings, checks = self._settings(store, employee)
        project, repo, folder = self._folder(store, task)
        session_id = self._session(store, task, repo, folder)
        goal = self._goal(store, task, employee,
                          lead=self._carry(restart_from, note) if restart_from is not None else "")
        path, spec = self._relay(task, settings, checks)
        routed = None
        if spec.router is not None:  # `auto`: picked here, once; `create` gets the relay that was picked
            try:
                routed = self.engine.route(path, goal, None if repo else folder, session_id=session_id, repo=repo,
                                           needs_writes=True, exclude=unguarded_relays(checks.relays_dir, checks))
                path = Path(routed["path"])
                spec, _ = RelaySpec.load(path)
            except RELAY_ERRORS as e:
                raise Refused(f"릴레이를 고르지 못했다: {e}") from e
        slots, problems = settle(spec.stages, settings["work"], task.get("stage_models"), settings["cap"], checks,
                                 models(self.catalog(), checks))
        if problems:
            raise Refused(f"릴레이 {path.stem} 로 런을 만들 수 없다", problems)
        try:
            run = self.engine.create(
                path, goal, None if repo else folder, session_id=session_id, repo=repo, auto_apply=False,
                stage_models=slots, approval="ai", mcp=[*settings["mcp"], employees.STUDIO_MCP],
                model_cap=settings["cap"]["model"], effort_cap=settings["cap"]["effort"], needs_writes=True,
                pin_copy=holds_server_code(project["path"], folder, root=self.server_root), routed=routed)
        except ValueError as e:
            raise Refused(f"엔진이 런을 만들지 않았다: {e}") from e
        boost = float(getattr(self.cfg, "studio_game_budget_boost", 1.0) or 1.0)
        if boost > 1.0 and projctx.game_root(folder) is not None:  # §11.2: a game's work starts with a wider budget
            run.stage_budget_boost = {stage.name: boost for stage in spec.stages}
            self.engine.save(run)
        return run.id

    def cancel_run(self, run_id: str) -> None:
        """T3's transaction failed after `create_run`: the run made for it is closed (§4.5). It was never
        advanced, so nobody holds it; one that is gone or closed already is left."""
        try:
            self.engine.cancel(run_id)
        except (OSError, ValueError):
            pass

    # --- the editor check (M-7) ---------------------------------------------------------------------------
    def _editor_project(self, store: StudioStore, task: dict, run_id: str | None) -> dict | None:
        """The project whose editor that run needs, None when the run is not checked: the project has no
        `mcp_port` — studio has nowhere to ask, and the engine's own `_check_mcp_ready` is what looks then —,
        the engine has no such run, or the run does not use the editor (`uses_editor`)."""
        project = store.get_project(task["project_id"]) if task.get("project_id") else None
        if project is None or not project.get("mcp_port"):
            return None
        run = _load(self.engine, run_id)
        if run is None:
            return None
        spec = None
        if project.get("repo"):
            try:
                spec = self.engine.repos.get(project["repo"])
            except KeyError:
                spec = None
        return project if uses_editor(run, spec) else None

    def editor_ready(self, inbox, task: dict, run_id: str, stopped: Callable[[], bool] | None = None) -> bool:
        """§4.3 T3 '에디터 검사' · §4.7 '런을 이을 때': may that run go on? True for a run that is not checked
        (`_editor_project`) and once the project's editor answers — `watch` is told then. An editor that cannot
        be started or does not answer within `cfg.studio_editor_wait` seconds: the system asks
        `editor_unresponsive` (T4, the task ends in blocked) and the answer is False — the run is not advanced
        and stays as it was. A task that left `working` meanwhile gets no question. Called on the thread of the
        task's slot, which it holds for as long as the wait.

        `stopped` (the queue: the cancel event of that slot) says the owner stopped the task. It is asked before
        the editor is started, before and after each wait of `bring_up`, and before the question or the watch:
        once it says so the answer is False with no question and no watch — an editor is not what stopped that
        run, and the queue files `run_cancelled` for it (`_settle`). The wait ends within one `poll`."""
        store = inbox.store
        project = self._editor_project(store, task, run_id)
        if project is None:
            return True
        gone = stopped or (lambda: False)
        if gone():
            return False  # no editor is started for a run that will not go on
        tools = self.editor_tools
        if stopped is not None:
            nap = tools.sleep

            def sleep(seconds: float) -> None:
                if gone():
                    raise _Stopped
                nap(seconds)
                if gone():
                    raise _Stopped

            tools = dataclasses.replace(tools, sleep=sleep)  # `bring_up` itself knows nothing of a stop
        try:
            engines = list(self.editor_engines())
        except Exception:  # noqa: BLE001 - no list of engines is an editor that cannot be started
            log.exception("studio: 설치된 엔진 목록을 읽지 못했다")
            engines = []
        wait = float(getattr(self.cfg, "studio_editor_wait", editor_control.START_TIMEOUT))
        try:
            editor_control.bring_up(project, store.list_projects(), engines, processes=self.editor_processes,
                                    healthy=self.editor_healthy, tools=tools, timeout=wait)
        except _Stopped:
            log.info("studio: 작업 %s 이 중지돼 에디터를 더 기다리지 않는다 (런 %s)", task["id"], run_id)
            return False
        except editor_control.Refused as e:
            log.info("studio: 작업 %s 의 런 %s 을 진행하지 않는다 — %s", task["id"], run_id, e.message)
            if gone():
                return False  # stopped meanwhile: the owner is not asked about the editor of a task they stopped
            if editor_quiet(run_id, e.message):
                return True  # a studio run is told, not stopped; no watch: the agent can start it with `editor_build`
            try:
                ask(inbox, task["id"], EDITOR_EVENT, EDITOR_DOWN.format(why=e.message), error=e.message)
            except TransitionError as t:
                log.info("studio: 작업 %s 에 에디터 무응답을 묻지 못했다: %s", task["id"], t.detail)
            return False
        if gone():
            return False  # it answers, and the run does not go on: nothing to watch
        if self.watch is not None:
            self.watch(task["id"], run_id)
        return True

    def editor_alive(self, store: StudioStore, task: dict) -> bool | None:
        """Does the editor of that task's project answer now? One `health_check` of the project's port; None
        when the project has no port — there is nothing to look at. The processes are not read."""
        project = store.get_project(task["project_id"]) if task.get("project_id") else None
        port = (project or {}).get("mcp_port")
        if not port:
            return None
        try:
            return bool(self.editor_healthy(port))
        except Exception:  # noqa: BLE001 - a check that fails is an editor that did not answer
            log.exception("studio: 에디터의 health_check 를 하지 못했다 (포트 %s)", port)
            return False

    def _judge(self, rule: str, task: dict) -> Any:
        """`review.gate` for that rule on the task's run as it is now: the run (None when the task has none
        that can be read and the rule needs none — a rework, or a revert · cancel of a run the engine has no
        more: `review.lost`), or `precondition_failed` with the gate's code."""
        want = GATES[rule]
        if want == "rework" and not task.get("run_id"):
            return None
        run = _load(self.engine, task.get("run_id"))
        held = self.holds is not None and bool(self.holds(task["id"]))
        stopped = run is not None and run.status != "running" and not held
        gone = run is None and review.lost(self.engine, task.get("run_id"))
        code, why = review.gate(self.store(), task, self.engine, run, stopped, want=(want,), lost=gone,
                                instead=INSTEAD_OF_CANCEL if rule == "T11" else review.REWORK)[want]
        if code:
            raise TransitionError("precondition_failed", why, from_status=task["status"], rule=rule, failed=[code])
        return run

    def check(self, rule: str, task: dict) -> None:
        """§4.3 '언제' of T7 · T9 · T11 (the run has stopped, the changes can be applied or reverted) and of T8
        · T5 (the run can still go on): nothing is done here, a refusal is `precondition_failed` with one of
        `review.CODES`. T6 (S-11 ②): a `feature` task whose run changed code needs a decided structure choice
        (`gate.judge`); the run is still at work, so what it changed is read from its edit tools' log."""
        if rule in GATES:
            self._judge(rule, task)
        elif rule == "T6":
            self._structure(task)

    def _structure(self, task: dict) -> None:
        if task.get("type") != gate.FEATURE:
            return
        run = _load(self.engine, task.get("run_id"))
        if run is None:  # a run nobody can read is not held against the request
            return
        paths = gate.edited(self.engine, run.id)
        try:
            paths += [f["path"] for f in review.changed_files(run, self.engine.patch_text(run.id))]
        except (OSError, ValueError, TypeError, KeyError):
            pass
        code, why = gate.judge(task, paths, gate.chosen(self.store(), task["id"]))
        if code:
            raise TransitionError("precondition_failed", why, from_status=task["status"], rule="T6", failed=[code])

    def before(self, rule: str, task: dict) -> str | None:
        """T7 · T9 · T11, before step 1 (§4.5): the run is closed, then its changes are applied and committed
        (`review.passed`) or reverted (`review.reverted`). One at a time for a task, and judged again on the
        run as it is by then — a pass and a revert that came together do not both act. What the engine or git
        refuses goes up as it is (ValueError -> `engine_error`); either way nothing is written.
        A revert · cancel whose run the engine has no more (§4.3 `run_gone` 의 예외) reverts nothing and returns
        `review.LOST` — the transition row says so after its reason. None otherwise."""
        if rule not in PRE_EFFECT:
            return None
        with self._guard:
            lock = self._deciding.setdefault(task["id"], threading.Lock())
        with lock:
            run = self._judge(rule, task)
            if rule == "T7":
                review.passed(self.engine, run, task)
            elif run is None:  # the gate took it: nothing is there to close or to revert from
                log.warning("studio: 작업 %s 의 런 %s 이 엔진에 없다 — 변경을 되돌리지 않고 %s 를 받는다",
                            task["id"], task.get("run_id"), rule)
                return review.LOST
            else:
                review.reverted(self.engine, run)
        return None

    def after(self, what: str, row: dict, event_ids: tuple[int, ...]) -> None:
        """A task that has joined the line (`queued` · T5 · T8): the queue looks at the line (§4.6) — it starts
        or resumes the run when there is a slot; what it raises is logged, the change stands.
        T3: the session of the run just made becomes the assignee's in the task's room — the row is there
        from T3's own transaction on —, so the task's next run is filed under it (§11.2 `session_id`).
        A change that may have made a task of a running campaign ready, or ended the campaign
        (`campaigns.target`): that campaign — or all of them — is looked at (§4.10); what that raises is
        logged as well. A campaign that has just gone to `running` (`CAMPAIGN_STARTED`) is looked at with
        `again=True`: its notices are said anew (§5.9). Every other look is called with the one argument."""
        if what in JOINS_LINE and row.get("queued_at") and self.kick is not None:
            try:
                self.kick()
            except Exception:  # noqa: BLE001 - the task is in the line; a later look takes it up
                log.exception("studio: 작업 %s 이 줄을 섰는데 큐를 보지 못했다", row.get("id"))
        scope = campaigns.target(what, row)
        if scope is not None and self.campaign is not None:
            try:
                if what == campaigns.CAMPAIGN_STARTED:
                    self.campaign(scope, again=True)
                else:
                    self.campaign(None if scope == campaigns.EVERY else scope)
            except Exception:  # noqa: BLE001 - the change stands; the next occasion looks again
                log.exception("studio: %s (%s) 뒤에 캠페인을 보지 못했다", what, row.get("id"))
        if what != "T3" or not row.get("run_id") or not row.get("assignee_id"):
            return
        store = self.store()
        member = self._member(store, row["id"], row["assignee_id"])
        try:
            run = self.engine.load(row["run_id"])
        except (OSError, ValueError):
            return
        if member is not None and member["session_id"] != run.session_id:
            # a new session is a new folder or a first run: what was resumed there is not resumed here (chat.py)
            store.update_room_member(member["room_id"], member["employee_id"], session_id=run.session_id,
                                     conversation_id=None)

    # --- the `start` request (§9.5 '두 번 본다') ------------------------------------------------------------
    def check_start(self, task: dict, stage_models: dict | None = None) -> list[dict]:
        """What a `start` request refuses: steps 3–6 on the override — `stage_models`, or the one the task
        keeps when None. `[{code, stage, message}]`, [] when nothing stands in the way; it never raises.

        An `employee.yaml` that cannot be read gives its own codes (§9.6). While the relay is `auto` — or
        cannot be told yet — nothing is picked: each slot's own provider (5 가), 5 (나) and 6 are looked at,
        and step 4 and the relay's stages wait for `create_run`. A task without an assignee or a relay is not
        this check's to refuse (§4.3 T3 '언제', §9.5 의 1)."""
        override = task.get("stage_models") if stage_models is None else stage_models
        store = self.store()
        try:
            settings, checks = self._settings(store, self._assignee(store, task))
        except Refused as e:
            return e.problems
        try:
            _, spec = self._relay(task, settings, checks)
        except Refused:
            spec = None
        listed = models(self.catalog(), checks)
        if spec is not None and spec.router is None:
            return settle(spec.stages, settings["work"], override, settings["cap"], checks, listed)[1]
        found = []
        for name, value in (override or {}).items():
            slot = _slot(value)
            if not checks.guarded(slot.get("provider")):
                found.append(_problem("provider_unguarded", str(name),
                                      f"[{name}] provider {slot['provider']} 는 Claude Code 가 아니다 — 가드를 걸 수 없다"))
            found += judge(str(name), slot, slot.get("provider"), None, settings["cap"], checks, listed)
        return _by_step(found)

    # --- what create_run is made of -----------------------------------------------------------------------
    def _assignee(self, store: StudioStore, task: dict) -> dict:
        employee = store.get_employee(task["assignee_id"]) if task.get("assignee_id") else None
        if employee is None or employee["retired_at"] is not None:
            raise Refused("담당이 없거나 재직 중이 아니다")
        return employee

    def _settings(self, store: StudioStore, employee: dict) -> tuple[dict, employees.Checks]:
        """The employee's file as it is now, over the part's defaults and the config's cap (§9.1 · §9.5 의 6)."""
        department = store.get_department(employee["department_id"])
        try:
            settings = employees.read_settings(self.cfg.studio_dir, employee, department,
                                               employees.config_cap(self.cfg))
        except employees.Unreadable as e:
            raise Refused(f"{employee['name']} 의 employee.yaml 을 읽지 못했다",
                          [_problem(p["code"], None, p["message"]) for p in e.problems]) from e
        return settings, employees.context(self.cfg)

    def _folder(self, store: StudioStore, task: dict) -> tuple[dict, str | None, Path]:
        """(the project, its repo, the folder the run works in): `projects.repo` as the engine's registry has
        it, or — a project with no repo — `projects.path` itself."""
        project = store.get_project(task["project_id"])
        if project is None:
            raise Refused("작업의 프로젝트가 없다")
        if project["repo"]:
            try:
                spec = self.engine.repos.get(project["repo"])
            except KeyError as e:
                raise Refused(str(e.args[0] if e.args else e)) from e
            if not spec.exists:
                raise Refused(f"저장소 {project['repo']} 의 경로가 이 PC 에 없다: {spec.path or '(미지정)'}")
            return project, project["repo"], spec.resolved
        if not project["path"] or not Path(project["path"]).is_dir():
            raise Refused(f"프로젝트 폴더가 이 PC 에 없다: {project['path'] or '(미지정)'}")
        return project, None, Path(project["path"]).resolve()

    def _member(self, store: StudioStore, task_id: str, employee_id: str) -> dict | None:
        room = next(iter(store.list_rooms(task_id=task_id, include_archived=True)), None)
        return store.get_room_member(room["id"], employee_id) if room else None

    def _session(self, store: StudioStore, task: dict, repo: str | None, folder: Path) -> str | None:
        """The session the assignee's runs of this task are filed under: the one its row in the task's room
        keeps, while that session is there and is of this folder. None = the engine opens one (`after` keeps it)."""
        member = self._member(store, task["id"], task["assignee_id"])
        session = self.engine.history.get_session(member["session_id"]) if member and member["session_id"] else None
        if session is not None and (session.get("repo") or None) == repo and (
                repo is not None or Path(session["workdir"]) == folder):
            return session["id"]
        return None

    def _carry(self, run_id: str, note: str) -> str:
        """T5 · restart: what the new run is told first — the closed run's hand-over paragraph (what it changed and
        why it stopped) and the owner's comment. A run that cannot be read is said so in one line."""
        run = _load(self.engine, run_id)
        lines = ["===== 지난 런 인계 (새로 시작) ====="]
        if run is None:
            lines.append(f"지난 런 `{run_id}` 을 읽지 못했다. 작업 폴더의 변경은 되돌리지 않았다. 새 대화로 시작한다.")
        else:
            baton = run.baton
            text = ((baton.stop.summary if baton.stop else "") or baton.handoff or "").strip()
            paragraph = next((p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()), "")[:700]
            lines.append(f"지난 런 `{run_id}`({run.status})는 닫았다. 작업 폴더의 변경은 되돌리지 않았다. 새 대화로 시작한다.")
            lines += [paragraph, f"멈춘 이유: {(run.error or '').strip()[:300]}" if (run.error or "").strip() else ""]
            if run.workspace_mode == "copy" and run.changes_status == "ready":
                files = int((run.changes or {}).get("files") or 0)
                lines.append(f"주의: 지난 런의 변경 {files}개 파일은 원본이 아니라 지난 런의 복사본에 있다. "
                             "새 런의 작업 폴더에는 아직 없다. owner 가 지난 런의 변경을 반영해야 들어온다.")
        if (note or "").strip():
            lines.append(f"owner 코멘트: {note.strip()}")
        return "\n".join(line for line in lines if line.strip())

    def _goal(self, store: StudioStore, task: dict, employee: dict, lead: str = "") -> str:
        """§11.2 `goal`: the task first — its title is the head, which a new session takes its name from —, then
        the hand-over of a closed run (`lead`, T5 · restart), reason, spec and done criteria, then the employee's part of the prompt (§9.1: persona, guide, memory,
        the project's policy, the harness, the recent messages of the task's room)."""
        room = next(iter(store.list_rooms(task_id=task["id"], include_archived=True)), None)
        parts = [f"작업: {(task['title'] or '').strip()}"]
        if lead:
            parts.append(lead)
        note = STRUCTURE_DOC_NOTES.get(task.get("type")) if employee.get("department_id") == "program" else None
        if note:
            parts.append(note)
        parts += [f"===== {title} =====\n{task[key].strip()}" for title, key in GOAL_SECTIONS
                  if (task[key] or "").strip()]
        parts.append(employees.prompt_for(store, self.cfg.studio_dir, employee, room["id"] if room else None,
                                          task["project_id"],
                                          harness=harness_root.section_for(self.cfg.studio_harness_root)))
        return "\n\n".join(part for part in parts if part.strip())

    def stage_section(self, store: StudioStore, project_id: str | None) -> str | None:
        """The '빌드·검증' section of a stage that is about to start (§9.1; `api.guard_stage` hands it to the engine as
        `prompt_append`): what the editor and the machine say now. None when the project is not an Unreal one — then
        neither the editor nor the engine list is read."""
        project = store.get_project(project_id) if project_id else None
        uproject = find_uproject(Path(project["path"])) if project and project.get("path") else None
        return self._build_section(store, project, uproject) if uproject is not None else None

    def _build_section(self, store: StudioStore, project: dict, uproject: Path) -> str:
        """`build_section` of what the editor and the machine say now. A look that fails is an editor that is off
        and an engine that is not known (the same as `editor_ready` takes an engine list it cannot read)."""
        try:
            state = editor_control.status(project, store.list_projects(), processes=self.editor_processes,
                                          healthy=self.editor_healthy)
        except Exception:  # noqa: BLE001
            log.exception("studio: 에디터 상태를 읽지 못했다 (프로젝트 %s)", project.get("id"))
            state = {"running": False, "healthy": False}
        try:
            engines = list(self.editor_engines())
        except Exception:  # noqa: BLE001
            log.exception("studio: 설치된 엔진 목록을 읽지 못했다")
            engines = []
        return build_section(project, uproject, state, engines)

    def _relay(self, task: dict, settings: dict, checks: employees.Checks) -> tuple[Path, RelaySpec]:
        """Step 1: (the relay file, read). `settings["relay"]` is the file's, else the part's."""
        name = task.get("relay") or settings["relay"]
        if not name:
            raise Refused("릴레이가 정해지지 않았다 — 작업 · employee.yaml · 파트 기본값 어디에도 없다")
        if not checks.relay_known(name):
            raise Refused(f"릴레이 {name} 가 relays/ 에 없다")
        path = checks.relays_dir / f"{name}.yaml"
        try:
            spec, _ = RelaySpec.load(path)
        except RELAY_ERRORS as e:
            raise Refused(f"릴레이 {name} 를 읽지 못했다: {type(e).__name__}: {e}") from e
        return path, spec


# --- a task's run that has stopped (§4.7 '런이 멈췄을 때') ------------------------------------------------------
def ask(inbox, task_id: str, reason: str, body: str, **extra: Any) -> dict:
    """T4 by the system: a `question` of that reason (§5.3), and the task ends in blocked. The item."""
    return inbox.create("question", SYSTEM, task_id=task_id, payload={"reason": reason, "body": body, **extra})


def studio_run(run_id: str) -> bool:
    """Is that run one studio started (a task run or a chat turn)? The same test as the budget notice's
    (`notice.mode`): a run that is not studio's keeps the old stops."""
    from . import notice  # not at import: notice imports api

    try:
        return notice.mode(run_id) is not None
    except Exception:  # noqa: BLE001
        return False


def editor_quiet(run_id: str, why: str) -> bool:
    """The editor does not answer and the run is a studio run: the room is told (`notice.tell`, a quiet push)
    and nothing else — no question, no cut. True when it was told; False for a run outside studio, which the
    caller stops as before. Never raises."""
    if not studio_run(run_id):
        return False
    from . import notice

    log.info("studio: 런 %s 의 에디터가 응답하지 않아 안내만 한다 — %s", run_id, why)
    notice.tell(run_id, "editor", {"why": why})
    return True


def _release_editor(engine, inbox, out: dict) -> None:
    """Step 0: an `editor_unresponsive` question that is still open from before is closed — the editor no longer
    stops a studio run — and the run goes on: the owner's `retry` (the editor is checked, and a studio run is
    told, not stopped). Only the questions of a task whose run is a studio run. `out["editor_released"]` gets the
    ids closed. One failing item does not keep the others."""
    from . import notice

    released = out.setdefault("editor_released", [])
    for item in inbox.store.list_inbox(status="pending"):
        try:
            payload = item["payload"] or {}
            if item["kind"] != "question" or payload.get("reason") != EDITOR_EVENT or not item["task_id"]:
                continue
            task = inbox.store.get_task(item["task_id"])
            run_id = task["run_id"] if task else None
            if not run_id or not (notice.mode(run_id) or _saved_mode(engine, run_id)):
                continue
            inbox.decide(item["id"], "answer", comment="", data={"option_id": "retry"}, via="app")
            released.append(item["id"])
        except Exception:  # noqa: BLE001
            log.exception("studio: 재시작 복구가 에디터 무응답 질문 %s 을 닫지 못했다", item["id"])


def editor_lost(engine, inbox, task_id: str, run_id: str) -> bool:
    """§4.5 '에디터 무응답을 감지': the editor stopped answering while that run was at work — the server cuts
    the run and writes a `run` row (`{run_id, event}`). True when the run was cut.

    Only while the task's run is still that run and it is `running`. A task that is working and out of the line
    is asked first (`editor_unresponsive`, T4): a cut that came first would have the thread of the slot find a
    `cancelled` run of a working task and ask `run_cancelled` besides (§4.7 의 표). In blocked · review, and
    working in the line, no question is filed — the item of the T4 or T6 is there already, or the check before
    the run is taken up again asks. A run that ended meanwhile (`OSError` · `ValueError`) was not cut: False.
    If the question was filed by then it stays — the editor did not answer, and `retry` checks it again and
    takes the run up as it is — and a `run` row (`EDITOR_ENDED`, the same detail) says the run was not cut, so
    the blocked task is not left with a question and no row."""
    task = inbox.store.get_task(task_id)
    if task is None or task["run_id"] != run_id:
        return False
    if getattr(_load(engine, run_id), "status", None) != "running":
        return False
    asked = False
    if task["status"] == "working" and task["queued_at"] is None:
        try:
            ask(inbox, task_id, EDITOR_EVENT, EDITOR_LOST)
            asked = True
        except TransitionError as e:  # it left working since that look: its own item is there
            log.info("studio: 작업 %s 에 에디터 무응답을 묻지 못했다: %s", task_id, e.detail)
    try:
        engine.cancel(run_id)
    except (OSError, ValueError):  # ended or closed already
        if asked:
            log.info("studio: 작업 %s 의 런 %s 은 끊기 전에 끝나 있었다 — 결재는 그대로 둔다", task_id, run_id)
            inbox.machine.record(task_id, SYSTEM, "run", EDITOR_ENDED,
                                 detail={"run_id": run_id, "event": EDITOR_EVENT})
        return False
    log.info("studio: 작업 %s 의 런 %s 을 에디터 무응답으로 끊었다", task_id, run_id)
    inbox.machine.record(task_id, SYSTEM, "run", EDITOR_CUT, detail={"run_id": run_id, "event": EDITOR_EVENT})
    return True


def resumes(run) -> bool:
    """§4.7 '스스로 다시 잇는다': a failed run that is not the owner's to decide about — the server stopped
    while it was at work, or another run was editing its folder. Its task joins the line again; the queue waits
    for the folder itself (`folder_holder`). A run that failed at a usage limit is not among them yet (S-14):
    the line has no 'not before', and a task put back at once would fail again."""
    error = (getattr(run, "error", "") or "").strip()
    return getattr(run, "status", None) == "failed" and (error.startswith(RESTARTED) or FOLDER_BUSY in error)


def _load(engine, run_id: str | None):
    """That run on this engine, None when it has no such run."""
    if not run_id:
        return None
    try:
        return engine.load(run_id)
    except (OSError, ValueError):
        return None


def deliver(inbox, run_id: str | None = None) -> list[str]:
    """Step 2 of §4.7, and step 2 of §4.8 when no run is named: the decided items whose text has not reached
    their run (`delivery = 'pending'`), in the order they were closed, each fed by `Inbox.deliver`. A run that
    is at work takes none — its item stays pending —; one that is `done` is opened again for it
    (`runs.EngineHooks.interject`). Returns the ids that went in now. The text of an
    item may go in twice (a server that died between the note and the mark): at least once is the contract."""
    owed = [item for item in inbox.store.list_inbox(delivery="pending", run_id=run_id) if item["run_id"]]
    went_in = []
    for item in sorted(owed, key=lambda item: (item["closed_at"] or "", item["created_at"] or "")):
        try:
            if inbox.deliver(item["id"]):
                went_in.append(item["id"])
        except Exception:  # noqa: BLE001 - one text that cannot go in does not keep the others
            log.exception("studio: 결정문 %s 를 런 %s 에 넣지 못했다", item["id"], item["run_id"])
    return went_in


def skipped(engine, inbox, task_id: str, run_id: str) -> list[dict]:
    """§4.7 '건너뛴 단계': a `run` row of the task for each `stage_skipped` event of that run — the engine's
    reason, and `{run_id, stage, event, event_id}`. `event_id` is the id of the engine's event: a row that has
    it already is not written again, however often the run is read. Returns the rows written now."""
    events = [event for event in engine.history.events(run_id) if event["kind"] == SKIPPED]
    if not events:
        return []
    seen = {(row["detail"] or {}).get("event_id") for row in inbox.store.list_task_events(task_id)
            if row["kind"] == "run" and (row["detail"] or {}).get("run_id") == run_id}
    written = []
    for event in events:
        if event["id"] in seen:
            continue
        reason = str((event["detail"] or {}).get("reason") or "").strip()
        detail = {"run_id": run_id, "stage": event["stage"], "event": SKIPPED, "event_id": event["id"]}
        written.append(inbox.machine.record(task_id, SYSTEM, "run", reason or f"단계 {event['stage']} 를 건너뛰었다",
                                            detail=detail).event)
    return written


def table(engine, inbox, task: dict | None, run, *, owner_stop: bool = False,
          error: str | None = None, advanced: bool = False) -> tuple[str, dict] | None:
    """Step 3 of §4.7: the row of the table for that run and the task as it is now.

    Only a task that is working and out of the line has anything done: in blocked · review the T4 or T6 has been
    done already, and a working task in the line was decided about before its run stopped. `owner_stop`: the
    owner's `stop` ended the run — `run_cancelled` whatever the run says. `awaiting_approval` -> the item of the
    stop (`runs.file_stop`); `done` -> `no_review_request`; `cancelled` -> `run_cancelled`; `failed` -> the line
    again when it `resumes`, else `run_failed` with the run's error. `pending` · `running`: nothing.
    `error`: the thread of the slot came back with an exception, not from `advance` — the run was not advanced
    and is as it was. A `failed` run is then not put back in the line even when it `resumes` (it would come
    back the same way, for ever), and a `pending` one is not left as it is, working and out of the line with
    nobody to take it up: both are `run_failed` with that text. `advanced`: the engine recorded during that
    `advance` that the run failed, before the exception came — a `failed` run is then told of with its own
    error (still not put back in the line). A `running` run with an `error` is at work somewhere else: nothing
    here — the queue waits until it has stopped and asks again, with no error (`queue._stopped_elsewhere`).
    Returns (`filed`, the item) or (`requeued`, the task), None when nothing was done."""
    if task is None or task["status"] != "working" or task["queued_at"] is not None:
        return None
    if owner_stop:
        return FILED, ask(inbox, task["id"], "run_cancelled", STOPPED)
    status = getattr(run, "status", None)
    if status == runs.WAITING:
        item = runs.file_stop(engine, inbox, run)
        return (FILED, item) if item is not None else None
    if status == "done":
        owners = wrapped_up(inbox, task["id"])
        if owners or (getattr(engine, "studio_auto_review", True) and studio_run(run.id)):  # a studio run's review is filed for it, not asked about
            item = wrap_review(engine, inbox, task, run, "런이 검수 요청 없이 끝났다")
            if item is not None:
                if not owners:
                    _tell(run.id, "review", {})
                return FILED, item
        return FILED, ask(inbox, task["id"], "no_review_request", NO_REVIEW)
    if status == "cancelled":
        done = _auto_restart(engine, inbox, task, run, "런이 취소됐다")
        if done is not None:
            return done
        return FILED, ask(inbox, task["id"], "run_cancelled", CANCELLED)
    if status == "failed" or (error is not None and status == "pending"):
        if error is None and resumes(run):
            return REQUEUED, inbox.machine.requeue(task["id"], SYSTEM, reason=AGAIN).task
        own = (getattr(run, "error", "") or "").strip()
        ended = error is None or (advanced and status == "failed" and bool(own))  # the run's error is the one
        told = (own if ended else error.strip()) or "오류 내용이 남지 않았다"
        if wrapped_up(inbox, task["id"]):
            item = wrap_review(engine, inbox, task, run, told)
            if item is not None:
                return FILED, item
        done = _auto_restart(engine, inbox, task, run, told)
        if done is not None:
            return done
        body = (FAILED if ended else NOT_ADVANCED).format(error=told[:500])
        count = failures(inbox, task, told)
        tried = auto_restarts(inbox, task["id"])
        if tried:  # the server started it over by itself and it failed again: the owner is asked, restart advised
            more = "\n" + REPEAT.format(n=count) if count >= 2 else ""
            return FILED, ask(inbox, task["id"], "run_failed", body + "\n" + RESTARTED_AGAIN.format(n=tried) + more,
                              error=told, recommended="restart", failures=count, auto_restarts=tried)
        if count >= 2:
            return FILED, ask(inbox, task["id"], "run_failed", body + "\n" + REPEAT.format(n=count),
                              error=told, recommended="restart", failures=count)
        return FILED, ask(inbox, task["id"], "run_failed", body, error=told)
    return None


def auto_restarts(inbox, task_id: str) -> int:
    """How many times the server started this task over by itself (`TaskMachine.restart`) since the owner last
    did anything to it: the system's `queued` rows with `detail.restart`, after the last row of the owner."""
    count = 0
    for event in inbox.store.list_task_events(task_id):
        if event["actor_kind"] == "owner":
            count = 0
        elif event["kind"] == "queued" and (event["detail"] or {}).get("restart"):
            count += 1
    return count


def _tell(run_id: str, kind: str, detail: dict) -> None:
    from . import notice  # not at import: notice imports api

    notice.tell(run_id, kind, detail)


def _auto_restart(engine, inbox, task: dict, run, why: str) -> tuple[str, dict] | None:
    """A studio run failed (or was cancelled with nobody's stop): start the task over by itself — the changes
    stay, a new conversation with the last summary — and only tell the room. None when it does not (the run is
    not studio's, `studio_auto_restarts` is used up or 0, or the new run cannot be made): the caller asks."""
    limit = int(getattr(engine, "studio_auto_restarts", 1) or 0)
    if limit <= 0 or run is None or not studio_run(run.id) or auto_restarts(inbox, task["id"]) >= limit:
        return None
    try:
        outcome = inbox.machine.restart(task["id"], SYSTEM, reason=f"{RESTART_WHY} — {why[:200]}")
    except (TransitionError, InboxError, OSError, ValueError) as e:
        log.info("studio: 작업 %s 를 스스로 새로 시작하지 못했다: %s", task["id"], e)
        return None
    _tell(run.id, "restart", {"why": why})
    return REQUEUED, outcome.task


def _plain(text: str) -> str:
    """An error with what changes from run to run taken out: long hex ids, numbers, spacing, case."""
    text = re.sub(r"[0-9a-f]{8,}", "#", (text or "").lower())
    return " ".join(re.sub(r"\d+", "#", text).split())


def similar(a: str, b: str) -> bool:
    """The same reason for a failure: the two errors are alike (SequenceMatcher ratio >= SIMILAR) once their ids,
    numbers and spacing are taken out. Two empty ones are not alike."""
    a, b = _plain(a), _plain(b)
    return bool(a and b) and difflib.SequenceMatcher(None, a, b).ratio() >= SIMILAR


def failures(inbox, task: dict, error: str) -> int:
    """§4.7: how many times this task's run has failed for that reason, this one included. Only the `run_failed`
    questions of the same run count; a run made by `restart` begins again at 1."""
    before = [i for i in inbox.store.list_inbox(task_id=task["id"], kind="question")
              if i.get("run_id") == task["run_id"] and (i["payload"] or {}).get("reason") == "run_failed"
              and similar((i["payload"] or {}).get("error") or "", error)]
    return len(before) + 1


def wrapped_up(inbox, task_id: str) -> bool:
    """The owner's last answer on this task was `wrap_up`: the run was told to file a review, and no item has
    been decided since (a review it filed would be pending or decided later, a risky command in between would
    be the later one)."""
    last = inbox.store.list_inbox(task_id=task_id, closed_first=True, limit=1)
    return (bool(last) and last[0]["kind"] == "question" and last[0]["status"] == "decided"
            and (last[0]["decision_data"] or {}).get("option_id") == "wrap_up")


def wrap_review(engine, inbox, task: dict, run, why: str) -> dict | None:
    """§4.7: the owner chose `wrap_up` and the run ended without `task_request_review` (it failed again or
    finished): the server files the final review (T6, actor system) with the last summary, the changed files and
    `검증 못 함: <why>`. None when it cannot (the log says why) — the caller asks the usual question then."""
    baton = getattr(run, "baton", None)
    text = ((baton.stop.summary if baton is not None and baton.stop else "")
            or (baton.handoff if baton is not None else "") or "").strip()
    try:
        names = [f["path"] for f in review.changed_files(run, engine.patch_text(run.id))]
        changed = ", ".join(names[:20]) + (f" 외 {len(names) - 20}개" if len(names) > 20 else "") if names else "없음"
    except (OSError, ValueError, TypeError, KeyError):
        changed = "목록을 읽지 못했다"
    summary = (text or "직원이 남긴 요약이 없다") + "\n\n바뀐 파일: " + changed
    note = NOT_VERIFIED.format(why=(why or "").strip()[:500] or "이유가 남지 않았다")
    payload = {"summary": summary, "checklist": [{"criterion": "완료 기준 전체", "status": "unverified", "note": note}]}
    try:
        return inbox.create("final_review", SYSTEM, payload=payload, task_id=task["id"])
    except (TransitionError, InboxError, OSError, ValueError) as e:
        log.info("studio: 작업 %s 의 검수를 서버가 대신 올리지 못했다: %s", task["id"], e)
        return None


def stopped(engine, inbox, task_id: str, *, owner_stop: bool = False,
            error: str | None = None, advanced: bool = False) -> tuple[str, dict] | None:
    """What the thread of a slot does once its slot is let go (queue.py `_work`): step 2 for the task's run,
    the skipped stages, and step 3 with the task read anew. Steps 2 and the rows are done whatever state the
    task is in; a failure of either is logged and the table comes all the same. `error` is what the thread
    came back with instead of a run that stopped, `advanced` whether the engine recorded meanwhile that the
    run failed (`table`). What `table` returns."""
    task = inbox.store.get_task(task_id)
    if task is None or not task["run_id"]:
        return None
    run_id = task["run_id"]
    try:
        deliver(inbox, run_id)
    except Exception:  # noqa: BLE001
        log.exception("studio: 작업 %s 의 런 %s 에 미전달 결정문을 넣지 못했다", task_id, run_id)
    try:
        skipped(engine, inbox, task_id, run_id)
    except Exception:  # noqa: BLE001
        log.exception("studio: 작업 %s 의 런 %s 이 건너뛴 단계를 적지 못했다", task_id, run_id)
    return table(engine, inbox, inbox.store.get_task(task_id), _load(engine, run_id), owner_stop=owner_stop,
                 error=error, advanced=advanced)


# --- the server's start (§4.8) --------------------------------------------------------------------------------
def _stopped_runs(engine, inbox, out: dict) -> list[str]:
    """Step 1: every task with a run, in working · blocked · review. A working task out of the line whose run
    never began (`pending`) or `resumes` is given back — it joins the line after step 2 —; for the others the
    skipped stages and the table.

    A blocked task whose run is still `running` has it cut first (`cutoff.cut`, its id goes to `out["cut"]`):
    the wait that follows a T4 (§8.3) did not outlive the server, and no time left is worked out. The engine's
    own recovery has closed the runs this server was advancing, so only a run another live process holds is
    found so — `cancel` then writes `cancelled` on disk and does not reach that process. A review task's run
    is left at work (§4.3 T6)."""
    again = []
    for status in WITH_RUN:
        for task in inbox.store.list_tasks(status=status):
            try:
                run = _load(engine, task["run_id"])
                if run is None:
                    continue
                if status == "blocked" and run.status == "running" and cutoff.cut(
                        engine, inbox.store, task["id"], run_id=run.id):
                    out["cut"].append(task["id"])
                    run = _load(engine, run.id) or run
                if status == "working" and task["queued_at"] is None and (run.status == "pending" or resumes(run)):
                    again.append(task["id"])
                    continue
                skipped(engine, inbox, task["id"], run.id)
                done = table(engine, inbox, task, run)
                if done is not None:
                    out[done[0]].append(done[1]["id"])
            except Exception:  # noqa: BLE001 - one task does not keep the others
                log.exception("studio: 재시작 복구가 작업 %s 의 런 %s 을 처리하지 못했다", task["id"], task["run_id"])
    return again


BUDGET_STOPS = ("budget", "stage_budget")  # StopNote.kind of the stops a studio run no longer makes


def _saved_mode(engine, run_id: str):
    """The `studio_mode` stored in the run (None when the run is gone or never was studio's)."""
    try:
        return getattr(_load(engine, run_id), "studio_mode", None)
    except Exception:  # noqa: BLE001
        return None


def _release_budget(engine, inbox, out: dict) -> None:
    """Step 0: a studio run is not stopped by a budget any more (studio/notice.py), so a budget stop that is
    still open from before is closed and its run goes on. The pending `budget` items of a task or of a run
    studio started are approved (the decision path of the inbox does the rest: the task's block, the run, the
    record; a run studio took before a restart is known by its stored `studio_mode`); the V-4 hold (`usage_hold`) is not a run's stop and stays. A working task whose run waits at a
    budget stop with no item is approved at the engine — step 1 then finds the run `pending` and queues it.
    `out["released"]` gets the ids of the items closed. One failing item does not keep the others."""
    from . import notice  # not at import: notice imports api

    released = out.setdefault("released", [])
    for item in inbox.store.list_inbox(status="pending"):
        try:
            payload = item["payload"] or {}
            if item["kind"] != "budget" or payload.get("stop_reason") == "usage_hold":
                continue
            if not item["task_id"] and not (item["run_id"] and (notice.mode(item["run_id"])
                                                                or _saved_mode(engine, item["run_id"]))):
                continue
            inbox.decide(item["id"], "approve", comment="예산 안내 모드: 자동 승인", via="app")
            released.append(item["id"])
        except Exception:  # noqa: BLE001
            log.exception("studio: 재시작 복구가 예산 결재 %s 을 닫지 못했다", item["id"])
    for task in inbox.store.list_tasks(status="working"):
        try:
            run = _load(engine, task["run_id"]) if task["run_id"] else None
            stop = run.baton.stop if run is not None else None
            if run is not None and run.status == "awaiting_approval" and stop is not None and stop.kind in BUDGET_STOPS:
                engine.approve(run.id)
        except Exception:  # noqa: BLE001
            log.exception("studio: 재시작 복구가 작업 %s 의 예산 정지를 풀지 못했다", task["id"])


def _statuses(inbox) -> list[str]:
    """Step 4: §4.9 for everyone employed. The ids of the employees whose status changed."""
    changed = []
    for employee in inbox.store.list_employees():
        try:
            with inbox.store.transaction() as tx:
                if inbox.machine.recompute_employee(tx, employee["id"]) is not None:
                    changed.append(employee["id"])
        except Exception:  # noqa: BLE001
            log.exception("studio: 재시작 복구가 직원 %s 의 상태를 다시 계산하지 못했다", employee["id"])
    return changed


def _told_before(store, room: dict | None, page: int = 500) -> dict[str, str]:
    """task id -> when the newest `invariant_broken` line about it was written in that room. The whole room is
    read, a page at a time: such a line may be far older than the newest messages. Empty when there is no room
    or it cannot be read — a task is then told of again rather than not at all."""
    last: dict[str, str] = {}
    if room is None:
        return last
    try:
        after = 0
        while True:
            rows = store.list_messages(room["id"], after_id=after, limit=page)
            for row in rows:
                meta = row["meta"] or {}
                if meta.get("event") == "invariant_broken" and meta.get("task_id"):
                    last[meta["task_id"]] = row["created_at"] or ""
            if len(rows) < page:
                return last
            after = rows[-1]["id"]
    except Exception:  # noqa: BLE001
        log.exception("studio: 재시작 복구가 시스템 방의 지난 알림을 읽지 못했다")
        return {}


def _broken(inbox) -> list[str]:
    """Step 5: the tasks in proposed · blocked · review without the pending item their state stands on (§4.1).
    Nothing is mended: a line in the system room (`meta.event` = `invariant_broken`, §5.9 · §7.6) for each.
    Not at every start, though: a task that has such a line and has not changed since (`updated_at` is not
    later than the line) is passed over — one that was mended and broke again is told of anew. The room is
    read for that only when a task is broken. Returns the ids of the tasks told of now."""
    store, told = inbox.store, []
    room = store.get_room(SYSTEM_ROOM)
    before: dict[str, str] | None = None
    for status, kinds in STATE_INBOX.items():
        for task in store.list_tasks(status=status):
            try:
                if any(item["kind"] in kinds for item in store.list_inbox(status="pending", task_id=task["id"])):
                    continue
                if before is None:
                    before = _told_before(store, room)
                at = before.get(task["id"])
                if at is not None and at >= (task["updated_at"] or ""):
                    continue  # told of at an earlier start, and the task is as it was then
                if inbox._say(room, BROKEN.format(title=task["title"], id=task["id"], status=status),
                              {"event": "invariant_broken", "task_id": task["id"]}) is None:
                    log.warning("studio: 시스템 방이 없어 작업 %s 의 깨진 불변식을 알리지 못했다", task["id"])
                    continue  # not told: not counted as told
                told.append(task["id"])
            except Exception:  # noqa: BLE001
                log.exception("studio: 재시작 복구가 작업 %s 의 깨진 불변식을 알리지 못했다", task["id"])
    return told


def recover(engine, inbox_factory: Callable[[], Any], kick: Callable[[], Any] | None = None) -> dict:
    """§4.8: the studio recovery at the server's start, after the engine's own (`recover_interrupted` has
    closed the runs that were at work as failed, '서버 재시작…').

    1. the runs of the tasks are read (`_stopped_runs`) — and the run of a blocked task that is still at work
    is cut: the wait after its T4 went with the server (studio/cutoff.py); 2. the decision texts still owed go into their runs,
    and then — not before: a task in the line is taken up at once, and a run at work takes no text — the tasks
    of step 1 whose run was cut off join the line; 3. `kick()`: the line is looked at once, with an empty book
    of slots; 4. the employees' statuses (§4.9); 5. the tasks without their item are told of in the system room
    — once, not at every start while they stay as they are (`_broken`).

    `inbox_factory` is `api.run_inbox`; while the store is not open only step 3 is done. Never raises — the
    server must start —: a step or a task that fails is logged and the rest goes on. Returns the ids of what
    was done: `{requeued, filed, cut, delivered, employees, broken}` (tasks · items · tasks · items · employees
    · tasks; `cut` is the blocked tasks whose run was cut, `broken` the tasks told of now, not every task that
    is broken)."""
    out: dict[str, list[str]] = {REQUEUED: [], FILED: [], "cut": [], "delivered": [], "employees": [], "broken": []}

    def step(name: str, do: Callable[[], Any]) -> None:
        try:
            do()
        except Exception:  # noqa: BLE001
            log.exception("studio: 재시작 복구의 걸음 %s 을 마치지 못했다", name)

    inbox = None
    try:
        inbox = inbox_factory()
    except Exception:  # noqa: BLE001
        log.exception("studio: 재시작 복구가 결재함을 얻지 못했다")
    again: list[str] = []
    if inbox is not None:
        step("0", lambda: _release_budget(engine, inbox, out))
        step("0e", lambda: _release_editor(engine, inbox, out))
        step("1", lambda: again.extend(_stopped_runs(engine, inbox, out)))
        step("2", lambda: out["delivered"].extend(deliver(inbox)))
        for task_id in again:
            try:
                inbox.machine.requeue(task_id, SYSTEM, reason=AGAIN)
                out[REQUEUED].append(task_id)
            except Exception:  # noqa: BLE001
                log.exception("studio: 재시작 복구가 작업 %s 을 다시 줄에 세우지 못했다", task_id)
    if kick is not None:
        step("3", kick)
    if inbox is not None:
        step("4", lambda: out["employees"].extend(_statuses(inbox)))
        step("5", lambda: out["broken"].extend(_broken(inbox)))
    return out
