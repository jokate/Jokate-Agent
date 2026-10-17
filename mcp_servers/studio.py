"""studio MCP: the tools an employee's run uses to talk to Katae Studio (design/studio/ARCHITECTURE.md §8).

The engine attaches this server to the runs studio started and to no other (§8.2): it fills `env` per stage
with `STUDIO_URL` (default: `Config.server_url`) and `STUDIO_ACTOR_TOKEN` — who the caller is. No tool takes
an employee id.

One tool is one `POST /studio/mcp/<tool>`: the arguments are the body, the answer is the dict of §8.3. This
process opens no DB and checks nothing; the server does (§8.1). What it adds is the three results the server
cannot give (§8.3): 401 becomes `actor_invalid` and 422 — the request's validation error — `invalid_arguments`;
any other HTTP error and a failed connection become `server_unreachable`. The first and the last end the turn
("이제 턴을 끝내라"). `invalid_arguments` does not (provisional, S-6): the employee corrects the arguments and
calls again.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

# §8.3, word for word what relay_agent/studio/tools.py says (tests compare the two). Written again here so
# this process does not import the studio package.
END_TURN = "이제 턴을 끝내라. 도구를 더 부르지 말고, 지금까지 한 일과 기다리는 것을 한 문단으로 적고 멈춰라."
TIMEOUT_S = 60
MISTAKES, MISTAKES_CHARS = 5, 300  # how much of a 422 goes into the message


def _base() -> str:
    url = os.environ.get("STUDIO_URL")
    if not url:
        from relay_agent.config import Config

        url = Config.load().server_url
    return url.rstrip("/")


BASE = _base()
TOKEN = os.environ.get("STUDIO_ACTOR_TOKEN", "")
mcp = FastMCP("studio", instructions=(
    "카태 스튜디오의 직원 도구다. 모든 도구는 {ok, end_turn, message, …} 를 돌려준다. ok 가 false 면 code 와 "
    "message 를 읽고 고쳐 다시 부른다. end_turn 이 true 면 — message 가 '이제 턴을 끝내라'로 끝난다 — 도구를 더 "
    "부르지 말고, 지금까지 한 일과 기다리는 것을 한 문단으로 적고 멈춘다."))


def _fail(code: str, message: str, *, end_turn: bool = True) -> dict:
    """A result this process makes itself (§8.3): no added fields. The turn ends, unless `end_turn` is False —
    then the message says what to correct and the employee calls again."""
    return {"ok": False, "code": code, "message": f"{message} {END_TURN}" if end_turn else message,
            "end_turn": end_turn}


def _mistakes(resp: httpx.Response) -> str:
    """What a 422 says is wrong, short enough for a message: FastAPI's `detail` list as `<argument>: <msg>`
    (`loc` without its leading `body`), a `detail` string as it is, "HTTP 422" for a body that cannot be read."""
    try:
        detail = resp.json()["detail"]
    except (ValueError, KeyError, TypeError):
        return "HTTP 422"
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:MISTAKES_CHARS]
    parts = []
    for item in detail[:MISTAKES] if isinstance(detail, list) else []:
        if not isinstance(item, dict):
            continue
        loc = item.get("loc")
        loc = [str(part) for part in loc] if isinstance(loc, (list, tuple)) else []
        name, msg = ".".join(loc[1:] if loc[:1] == ["body"] else loc), str(item.get("msg") or "").strip()
        if name or msg:
            parts.append(f"{name}: {msg}" if name and msg else name or msg)
    return "; ".join(parts)[:MISTAKES_CHARS] or "HTTP 422"


def _call(tool: str, **args) -> dict:
    """POST the arguments to `/studio/mcp/<tool>`. A 200 is the server's result, passed on as it is."""
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    try:
        resp = httpx.post(f"{BASE}/studio/mcp/{tool}", json=args, headers=headers, timeout=TIMEOUT_S)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        return _fail("server_unreachable", f"studio 서버({BASE})에 닿지 못했다 — {type(e).__name__}.")
    if resp.status_code == 401:
        return _fail("actor_invalid", "행위자 토큰이 무효다 — 이 런이 끝났거나 서버가 다시 시작됐다.")
    if resp.status_code == 422:  # the request's validation error (§6.3): a wrong type, an argument no tool has
        return _fail("invalid_arguments", f"인자가 맞지 않는다 — {_mistakes(resp).rstrip('. ')}. 고쳐서 다시 불러라.",
                     end_turn=False)
    if resp.status_code != 200:
        return _fail("server_unreachable", f"studio 서버가 HTTP {resp.status_code} 로 답했다.")
    try:
        out = resp.json()
    except ValueError:
        out = None
    if not isinstance(out, dict) or not isinstance(out.get("ok"), bool):
        return _fail("server_unreachable", "studio 서버의 답을 읽지 못했다.")
    return out


# --- every employee ---------------------------------------------------------------------------------------
@mcp.tool()
def task_propose(title: str, reason: str, spec: str, done_criteria: str, type: str,
                 suggested_assignee_id: str = "", depends_on: list[str] | None = None, campaign_id: str = "",
                 project_id: str = "") -> dict:
    """새 작업을 제안한다 (owner 가 승인하면 backlog 에 들어간다). title · reason(왜) · spec(무엇을) ·
    done_criteria(완료 기준)는 모두 적고 type 은 feature · fix · chore · doc · data 중 하나다. 프로젝트는 지금
    작업의 것, 없으면 내가 배치된 것이다 (인사는 project_id 필수). 돌려주는 것: {task_id, inbox_id}."""
    return _call("task_propose", title=title, reason=reason, spec=spec, done_criteria=done_criteria, type=type,
                 suggested_assignee_id=suggested_assignee_id, depends_on=depends_on or [],
                 campaign_id=campaign_id, project_id=project_id)


@mcp.tool()
def followup_propose(title: str, reason: str, spec: str, done_criteria: str, type: str,
                     parent_task_id: str = "", suggested_assignee_id: str = "",
                     depends_on: list[str] | None = None, campaign_id: str = "", project_id: str = "") -> dict:
    """후속 작업을 제안한다 — task_propose 와 같고 부모 작업이 연결된다. parent_task_id 를 비우면 지금 작업이
    부모다. 범위가 늘어나는 일만 이것으로 올린다. 돌려주는 것: {task_id, inbox_id, parent_id}."""
    return _call("followup_propose", title=title, reason=reason, spec=spec, done_criteria=done_criteria,
                 type=type, parent_task_id=parent_task_id, suggested_assignee_id=suggested_assignee_id,
                 depends_on=depends_on or [], campaign_id=campaign_id, project_id=project_id)


@mcp.tool()
def task_get(task_id: str = "") -> dict:
    """작업 하나를 읽는다. task_id 를 비우면 지금 작업이다. 돌려주는 것: {task, depends_on[], dependents[],
    pending_inbox, recent_events[]}."""
    return _call("task_get", task_id=task_id)


@mcp.tool()
def task_list(status: str = "", assignee: str = "", campaign_id: str = "", limit: int = 50) -> dict:
    """내가 배치된 프로젝트의 작업 목록 (인사는 전체). status 는 작업 상태, assignee 는 me 또는 직원 id 다.
    돌려주는 것: {items[]} — 각 {id, title, status, type, assignee_id, is_milestone}."""
    return _call("task_list", status=status, assignee=assignee, campaign_id=campaign_id, limit=limit)


@mcp.tool()
def task_progress(note: str, percent: float | None = None) -> dict:
    """지금 작업에 진행 상황을 남긴다 (담당만, 자기 작업 런에서). note 는 한 일, percent 는 0–100 (생략 가능)이다.
    돌려주는 것: {event_id}."""
    return _call("task_progress", note=note, percent=percent)


@mcp.tool()
def task_request_review(summary: str, checklist: list[dict], evidence: list[dict] | None = None,
                        judgment_calls: list[dict] | None = None) -> dict:
    """지금 작업의 검수를 요청한다 (담당만). checklist 는 완료 기준마다 {criterion, status, note} — status 는
    verified_auto · verified_manual · unverified. evidence 는 각 {kind, ref, caption}, judgment_calls 는 묻지
    않고 정한 것 각 {text, why} 다. 성공하면 {inbox_id} 와 함께 이제 턴을 끝내라고 답한다 — 도구를 더 부르지
    말고, 지금까지 한 일과 기다리는 것을 한 문단으로 적고 멈춘다."""
    return _call("task_request_review", summary=summary, checklist=checklist, evidence=evidence or [],
                 judgment_calls=judgment_calls or [])


@mcp.tool()
def ask_owner(kind: str, title: str, body: str = "", options: list[dict] | None = None, recommended: str = "",
              fix_points: str = "", what: str = "", why: str = "", if_not: str = "", policy_ref: str = "") -> dict:
    """owner 의 결정이 있어야 나아갈 수 있을 때 결재를 올린다 (그 작업방의 구성원만). kind 는 셋이다.
    question: body(필수), options 각 {id, label}. choice: body, options 2개 이상 각 {id, label, detail, figure} —
    figure 는 "" 또는 {mermaid} —, recommended(options 의 id), fix_points. approval: what · why · if_not 모두
    필수, policy_ref. 성공하면 {inbox_id} 와 함께 이제 턴을 끝내라고 답한다 — 도구를 더 부르지 말고, 지금까지 한
    일과 기다리는 것을 한 문단으로 적고 멈춘다. 결정은 이 작업방에 온다."""
    return _call("ask_owner", kind=kind, title=title, body=body, options=options or [], recommended=recommended,
                 fix_points=fix_points, what=what, why=why, if_not=if_not, policy_ref=policy_ref)


@mcp.tool()
def room_post(text: str, mentions: list[str] | None = None, room: str = "", attachments: list[dict] | None = None,
              handoff: dict | None = None) -> dict:
    """방에 글을 쓴다. room 을 비우면 지금 방이다. mentions 는 그 방의 구성원인 직원 id (자기 자신은 안 된다).
    작업방에서 멘션하면 그 직원의 턴이 들어간다 — 담당이면 그 작업 런에 글이 끼어들고, 다른 구성원이면 읽기 전용
    턴이다. queued[] 가 턴이 들어간 직원이다. 직원끼리의 멘션은 작업마다 한도가 있어, 닿으면 글을 쓰지 않고
    exchange_limit 이 온다 (owner 의 결정을 기다린다).
    attachments 는 각 {path, name=""} — 런의 작업 폴더나 프로젝트 폴더 안의 25MB 이하 파일이고, 경로만 넘기면
    서버가 검사해 복사한다. 돌려주는 것: {message_id, queued[]}. end_turn 이 true 로 오면 턴을 끝낸다.
    handoff 는 파트 간 인계일 때만 넣는다 (그 밖의 글에는 넣지 않는다). 형식은 둘이다 — spec_table (기획→프로그램):
    {format: "spec_table", rows: [{item, value, done, condition?, exception?, note?}], source?} 와 data_items
    (프로그램→기획(세팅), 노출된 데이터 항목 목록): {format: "data_items", items: [{asset, field, type, default?,
    note?}], source?}. 필수 칸은 비어 있지 않은 글이고 표는 행이 하나 이상 있어야 한다. 맞지 않으면 글을 쓰지 않고
    invalid_format 과 이유, fields[] 가 온다 (고쳐서 다시 부른다). 검수로 넘길 때는 handoff 가 아니라
    task_request_review 를 쓴다."""
    return _call("room_post", text=text, mentions=mentions or [], room=room, attachments=attachments or [],
                 handoff=handoff)


@mcp.tool()
def plan_propose(title: str, goal: str, steps: list[dict], mode: str = "milestone", project_id: str = "") -> dict:
    """여러 작업으로 이루어진 계획을 owner 에게 올려 결재를 받는다 (승인하기 전에는 아무것도 만들어지지 않는다).
    title 은 캠페인 이름, goal 은 목표, mode 는 진행 방식(nonstop · milestone · each, 기본 milestone)이다. steps 는
    1~12개이고 각 단계는 {title, reason(왜), spec(무엇을), done_criteria(완료 기준), type(feature · fix · chore ·
    doc · data), assignee_id(제안 담당, 선택), depends_on(먼저 끝나야 하는 앞선 단계의 번호 — 0부터 센다, 선택)}
    다. 12개를 넘기면 거부된다. owner 가 승인하면 서버가 캠페인과 작업을 만들고 의존을 이어 시작한다. 수정 요청이면
    이 방으로 코멘트가 오니 고쳐서 다시 올린다. 프로젝트는 내가 배치된 것이다 (인사는 project_id 필수). 돌려주는 것:
    {inbox_id}."""
    return _call("plan_propose", title=title, goal=goal, steps=steps, mode=mode, project_id=project_id)


@mcp.tool()
def employee_list(project_id: str = "", department_id: str = "") -> dict:
    """직원 목록 — 내가 배치된 프로젝트의 직원 (인사는 전체), 퇴사한 직원은 빠진다. 돌려주는 것: {items[]} —
    각 {id, name, department_id, project_id, status}."""
    return _call("employee_list", project_id=project_id, department_id=department_id)


@mcp.tool()
def staff_request(reason: str, role: str = "", task_id: str = "", department_id: str = "",
                  project_id: str = "") -> dict:
    """사람이 더 필요할 때 인사에게 판단을 요청한다. reason 은 왜 필요한지, role 은 필요한 역할, task_id 는 관련
    작업(비우면 지금 작업), department_id · project_id 는 파트와 프로젝트(비우면 작업·내 소속에서 정한다).
    인사가 판단한다 — 채용 권장은 보장이 아니고, 같은 파트·프로젝트로는 한동안 다시 올릴 수 없다. 돌려주는 것:
    {asked, why} — why 는 asked · open · skipped · recently · unheard 다."""
    return _call("staff_request", reason=reason, role=role, task_id=task_id, department_id=department_id,
                 project_id=project_id)


# --- HR only ----------------------------------------------------------------------------------------------
@mcp.tool()
def hire_recommend(reason: str, role: str, model: str, effort: str, department_id: str = "", part_name: str = "",
                   part_job: str = "", part_relay: str = "", part_models: str = "", project_id: str = "",
                   task_ids: list[str] | None = None, signal_key: str = "") -> dict:
    """인사 전용. 채용을 권장하는 결재를 owner 에게 올린다 (초안이 아니다 — owner 가 면담을 열면 그때 이 방에서
    채용 면담을 잇는다). reason 은 왜, role 은 역할, model · effort 는 권장하는 모델과 노력치다. 맞는 파트가 있으면
    department_id, 없으면 part_name · part_job(하는 일)으로 새 파트를 제안한다 — 둘 중 하나만. part_relay ·
    part_models 는 정했을 때만. project_id 는 배치할 프로젝트, task_ids 는 근거가 되는 작업. 판단 요청을 받았으면
    그 key 를 signal_key 에 적는다. 같은 파트·프로젝트의 권장이 이미 결재를 기다리거나 최근 넘겨졌으면 거부된다.
    돌려주는 것: {inbox_id}."""
    return _call("hire_recommend", reason=reason, role=role, model=model, effort=effort,
                 department_id=department_id, part_name=part_name, part_job=part_job, part_relay=part_relay,
                 part_models=part_models, project_id=project_id, task_ids=task_ids or [], signal_key=signal_key)



@mcp.tool()
def employee_draft(name: str = "", department_id: str = "", persona_md: str = "", guide_md: str = "",
                   employee_yaml: str = "", draft_id: str = "", project_id: str = "", first_message: str = "",
                   target_employee_id: str = "", file: str = "", part_name: str = "", part_job: str = "",
                   part_relay: str = "", part_models: str = "") -> dict:
    """인사 전용. 채용 초안을 만들거나 고친다: name · department_id · persona_md · guide_md · employee_yaml 이
    필수이고, draft_id 를 주면 그 초안에서 적은 인자만 바꾼다. target_employee_id 를 주면 기존 직원의 수정안이다 —
    file(PERSONA.md · GUIDE.md · employee.yaml)과 그 파일의 본문 인자만 쓴다. 맞는 파트가 없으면 새 파트를 함께
    제안한다: part_name · part_job(하는 일)을 적고 part_relay(기본 릴레이) · part_models(chat · work · cap 의
    YAML)는 정했을 때만 적는다. 이때 department_id 는 적지 않고(서버가 새 id 를 돌려준다) employee_yaml 의
    department 에는 {department_id} 를 적는다. 돌려주는 것: {draft_id, action, problems[]} (새 파트면
    department_id 도) — problems 가 비어야 employee_submit 으로 올릴 수 있다."""
    return _call("employee_draft", draft_id=draft_id, name=name, department_id=department_id,
                 project_id=project_id, persona_md=persona_md, guide_md=guide_md, employee_yaml=employee_yaml,
                 first_message=first_message, target_employee_id=target_employee_id, file=file,
                 part_name=part_name, part_job=part_job, part_relay=part_relay, part_models=part_models)


@mcp.tool()
def employee_submit(draft_id: str, reason: str, source_inbox_id: str = "") -> dict:
    """인사 전용. 초안으로 채용 · 지침 수정 결재를 올린다. 같은 초안의 대기 중인 결재는 이것으로 바뀐다.
    source_inbox_id 는 이 수정안을 부른 검수 결재다. 돌려주는 것: {inbox_id}."""
    return _call("employee_submit", draft_id=draft_id, reason=reason, source_inbox_id=source_inbox_id)


@mcp.tool()
def policy_draft(project_id: str, policy_md: str, reason: str, submit: bool = False,
                 source_inbox_id: str = "") -> dict:
    """인사 전용. 프로젝트 정책(POLICY.md)의 초안을 저장하고 지금 파일과의 차이를 돌려준다. submit=True 면
    결재도 올린다. 돌려주는 것: {draft_id, diff, inbox_id} — 올리지 않았으면 inbox_id 는 null 이다."""
    return _call("policy_draft", project_id=project_id, policy_md=policy_md, reason=reason, submit=submit,
                 source_inbox_id=source_inbox_id)


if __name__ == "__main__":
    mcp.run()
