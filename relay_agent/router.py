"""Auto routing: pick the relay that fits a request, so the flow adapts to the task instead of the user choosing.

One cheap call (the handoff writer's one-shot: Haiku, neutral folder, no tools) reads the request, the target's
harness (what its CLAUDE.md / skills / MCP / registered repo say) and the candidate relays' descriptions, and
answers with a relay name and a reason. New relays join the candidates by dropping a YAML in relays/.
Anything unusable (no AI, error, unknown name) falls back to the router's `fallback` relay, with the reason.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from .projctx import ProjectContext
from .runners import Usage

SYSTEM = """너는 릴레이 라우터다. 요청과 작업 대상 요약을 보고, 후보 릴레이 중 이 요청을 끝까지 해낼 가장 알맞은 하나를 고른다.

기준
- 질문·조사·설명만 필요하고 파일을 바꿀 필요가 없으면: 파일을 바꾸지 않는(읽기 전용) 릴레이.
- 작고 범위가 분명한 수정: 단계가 적은 릴레이.
- 범위가 크거나, 여러 파일·구조에 걸치거나, 되돌리기 어렵거나, 설계 판단이 필요하면: 설계·검토 단계가 있는 릴레이.
- 후보 설명이 요청의 종류(예: 게임 제작)와 맞으면 그 릴레이를 우선한다.
- 작업 대상의 지침·도구는 판단 근거로만 쓴다(릴레이가 알아서 붙인다).
- 둘 사이에서 애매하면 단계가 적은 쪽.

답: 다른 말 없이 JSON 한 줄. {"relay": "<후보 이름>", "reason": "<한국어 한 문장, 80자 이내>"}"""

GOAL_CHARS = 3000
INSTRUCTION_CHARS = 1500


def candidates(relays_dir: Path, names: list[str], exclude: list[str], load: Callable) -> list[dict]:
    """name, description and stage outline of every relay the router may pick (never another router)."""
    out = []
    for path in sorted(relays_dir.glob("*.yaml")):
        if (names and path.stem not in names) or path.stem in exclude:
            continue
        try:
            spec, _ = load(path)
        except (OSError, ValueError):
            continue
        if spec.router is not None or not spec.stages:
            continue
        stages = " → ".join(f"{s.name}({s.model or '-'}{', 파일 수정' if s.writes else ''})" for s in spec.stages)
        out.append({"name": path.stem, "description": spec.description, "stages": stages, "path": path})
    return out


def harness_summary(workdir: Path, ctx: ProjectContext | None, repo: dict | None) -> str:
    """What the target itself defines — the same things the relay will attach to the run."""
    lines = [f"- 작업 폴더: {Path(workdir).name}"]
    if repo:
        if repo.get("name"):
            lines.append(f"- 등록된 저장소: {repo['name']}" + (f" (메모: {repo['notes']})" if repo.get("notes") else ""))
        if repo.get("verify"):
            lines.append(f"- 검증 명령: {', '.join(repo['verify'])}")
    if ctx is not None:
        if ctx.skills:
            lines.append(f"- 스킬: {', '.join(ctx.skills)}")
        if ctx.mcp:
            lines.append(f"- 연결되는 MCP: {', '.join(ctx.mcp)}")
        for f in ctx.instructions[:2]:
            try:
                head = f.read_text(encoding="utf-8", errors="replace")[:INSTRUCTION_CHARS].strip()
            except OSError:
                continue
            lines.append(f"- 지침 {f.name} 앞부분:\n{head}")
    return "\n".join(lines)


def prompt(goal: str, harness: str, options: list[dict], session_context: list[str] = (),
           attachments: list[str] = ()) -> str:
    parts = ["## 요청", goal[:GOAL_CHARS]]
    if attachments:
        parts += ["## 첨부", ", ".join(Path(a).name for a in attachments)]
    if session_context:
        parts += ["## 같은 세션의 이전 질문"] + [f"- {c}" for c in list(session_context)[-5:]]
    parts += ["## 작업 대상", harness, "## 후보 릴레이"]
    parts += [f"- {o['name']}: {o['description']} [{o['stages']}]" for o in options]
    return "\n".join(parts)


def parse(text: str, names: list[str]) -> tuple[str, str] | None:
    """The chosen relay and reason from the answer, or None if it names no candidate."""
    for m in re.finditer(r"\{[^{}]*\}", text or ""):
        try:
            data = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("relay") in names:
            return data["relay"], str(data.get("reason") or "")[:200]
    # a bare name is accepted too (small models sometimes drop the JSON)
    bare = (text or "").strip().strip("`\"' .")
    return (bare, "") if bare in names else None


def route(ask: Callable[[str, str], tuple[str, Usage]] | None, model: str, text: str,
          options: list[dict], fallback: str) -> dict:
    """{"relay", "reason", "by", "usage"}. Never raises: an unusable answer falls back with the reason why."""
    names = [o["name"] for o in options]
    if fallback not in names and names:
        fallback = names[0]
    if len(names) == 1:
        return {"relay": names[0], "reason": "후보가 하나뿐", "by": "", "usage": None}
    if ask is None:
        return {"relay": fallback, "reason": "라우터 AI 를 쓸 수 없어 기본 릴레이", "by": "", "usage": None}
    try:
        answer, usage = ask(text, model)
    except Exception as e:  # noqa: BLE001 - routing must never stop a run from starting
        return {"relay": fallback, "reason": f"라우터 호출 실패로 기본 릴레이: {str(e)[:120]}", "by": "", "usage": None}
    picked = parse(answer, names)
    if picked is None:
        return {"relay": fallback, "reason": f"라우터 답에서 릴레이를 찾지 못해 기본 릴레이: {answer[:80]!r}",
                "by": usage.model, "usage": usage}
    return {"relay": picked[0], "reason": picked[1], "by": usage.model, "usage": usage}
