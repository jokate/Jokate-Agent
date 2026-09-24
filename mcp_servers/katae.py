"""katae MCP: drive Agent 카태 from inside Claude Code (or any MCP client).

    claude mcp add katae -- <venv python> C:/Users/kkkk4017/Projects/relay-agent/mcp_servers/katae.py

Talks to the running server (default http://127.0.0.1:8020), so relays keep running and show up on
the dashboard even after the Claude Code session ends.

Typical use from Claude Code: "이 대화 이어서 카태로 넘겨" -> katae_start(goal, import_this_conversation=True)
imports this conversation's prompts (compact, no transcript) into a session and starts a relay there.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

from relay_agent import cc_import  # noqa: E402
from relay_agent.config import Config  # noqa: E402

_cfg = Config.load()
BASE = (os.environ.get("KATAE_URL") or _cfg.server_url).rstrip("/")
TOKEN = os.environ.get("KATAE_TOKEN") or _cfg.auth_token
mcp = FastMCP("katae")


def _call(method: str, path: str, **kwargs):
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    try:
        resp = httpx.request(method, BASE + path, timeout=60, headers=headers, **kwargs)
    except httpx.ConnectError:
        raise RuntimeError(f"Agent 카태 서버가 꺼져 있습니다 ({BASE}). "
                           "relay-agent 폴더에서 `uv run uvicorn relay_agent.server:app --port 8020` 로 켜세요.")
    if resp.status_code >= 400:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:300]}")
    return resp.json() if "json" in resp.headers.get("content-type", "") else resp.text


@mcp.tool()
def katae_providers() -> list[dict]:
    """AI 별 사용 가능 여부와 남은 사용량(예: Claude 5시간/7일 사용률 %, 24시간 지출)."""
    rows = _call("GET", "/providers")
    out = []
    for p in rows:
        windows = ", ".join(f"{w['window']} {round((w['utilization'] or 0) * 100)}%" for w in p.get("limits") or [])
        out.append({"provider": p["name"], "available": p["available"], "reason": p["reason"],
                    "usage_windows": windows, "spent_24h_usd": round(p["usage_24h"]["cost_usd"], 4)})
    return out


@mcp.tool()
def katae_repos() -> list[dict]:
    """등록된 저장소: 이름, 경로, 브랜치, 미커밋 파일 수, 기본 작업 공간·릴레이, 검증 명령."""
    keep = ("name", "path", "exists", "branch", "dirty", "workspace", "relay", "verify")
    return [{k: r.get(k) for k in keep if r.get(k) not in (None, [], "")} for r in _call("GET", "/repos")]


@mcp.tool()
def katae_start(goal: str, relay: str = "quick", repo: str = "", workdir: str = "", workspace: str = "",
                import_this_conversation: bool = True, session_id: str = "") -> dict:
    """릴레이 시작. repo(등록된 저장소 이름, katae_repos 로 확인)를 쓰면 경로·검증 명령·메모가 자동 적용된다.
    repo 도 workdir 도 없으면 현재 폴더(서버가 다른 머신이면 repo 를 쓸 것).
    relay: quick(기본, 단일 Sonnet) | default(설계 승인 포함 큰 작업) | quick-fable | docs-qa
    | game-cycle(게임 기획 → 구현·엔진 MCP 로 씬 구성 → 플레이 검증).
    import_this_conversation=True 면 이 폴더의 최근 Claude Code 대화에서 질문과 답 요약만 뽑아 맥락으로 보낸다.
    workspace: copy(패치로 돌려받기) | inplace | none (기본은 저장소→릴레이 설정)."""
    if not repo and not workdir:
        matched = _call("GET", "/repos/match", params={"path": os.getcwd()}).get("repo")
        repo, workdir = (matched, "") if matched else ("", os.getcwd())
    if import_this_conversation and not session_id:
        # Extract on this machine (where Claude Code's transcripts live) and send only the compact turns,
        # so this also works when the 카태 server runs on another machine.
        found = cc_import.latest_session_for_cwd(os.getcwd())
        if found:
            turns, meta = cc_import.extract_turns(*found)
            imported = _call("POST", "/claude-code/import-turns", json={
                "source_id": found[1], "turns": turns, "meta": meta, "workdir": workdir or os.getcwd()})
            session_id = imported["session_id"]
    body = {"goal": goal, "relay": relay, "workdir": workdir or None, "repo": repo or None,
            "session_id": session_id or None, "workspace": workspace or None}
    run = _call("POST", "/runs", json=body)
    return {"run_id": run["id"], "session_id": run["session_id"], "status": run["status"],
            "dashboard": f"{BASE}/", "note": "진행 상황은 katae_status 또는 대시보드에서 확인"}


@mcp.tool()
def katae_status(run_id: str) -> dict:
    """한눈에 보는 결과: 한 줄 요약, 핵심 작업, 바뀐 파일, 사용자 확인 필요, 검증 명령, MCP 사용량, 비용.
    중단됐다면 중단 지점(끝난/남은 단계, 멈춘 단계에서 한 일, 이어가는 방법)도 포함."""
    s = _call("GET", f"/runs/{run_id}/summary")
    keep = ("status", "headline", "highlights", "user_checks", "open_issues", "verification", "stop", "cost_usd")
    out = {k: s[k] for k in keep if s.get(k)}
    if s["changes"]["files"]:
        out["changes"] = {"files": [f["path"] for f in s["changes"]["files"]], "status": s["changes"]["status"]}
    if s["mcp"]:
        out["mcp"] = s["mcp"]
    return out


@mcp.tool()
def katae_handoff(run_id: str) -> str:
    """실행의 HANDOFF 전체(목표·결정·이슈·포인터·산출물)."""
    return _call("GET", f"/runs/{run_id}/handoff")


@mcp.tool()
def katae_approve(run_id: str) -> str:
    """승인 대기(설계 검토 또는 예산 도달) 중인 실행을 계속 진행."""
    return _call("POST", f"/runs/{run_id}/approve")["status"]


@mcp.tool()
def katae_patch(run_id: str) -> str:
    """copy 작업 공간의 결과 패치(diff). 적용은 대시보드나 katae_apply 로."""
    return _call("GET", f"/runs/{run_id}/patch") or "(변경 없음)"


@mcp.tool()
def katae_apply(run_id: str) -> str:
    """copy 작업 공간의 변경을 원본 폴더에 적용(충돌 시 아무것도 적용하지 않음)."""
    return _call("POST", f"/runs/{run_id}/changes/apply")["changes_status"]


@mcp.tool()
def katae_cancel(run_id: str) -> str:
    """실행 중인 릴레이 취소(나중에 재개 가능)."""
    return _call("POST", f"/runs/{run_id}/cancel")["status"]


if __name__ == "__main__":
    mcp.run()
