"""The baton: the only context a stage receives from the stages before it.

Keeping this small and structured is the main token-saving mechanism. A stage
never sees the previous stage's transcript, only what was written here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MAX_OUTPUT_CHARS = 3000
MAX_LOG = 10
MAX_DECISIONS = 8
MAX_POINTERS = 15
MAX_DIAGRAM_CHARS = 1500


class Decision(BaseModel):
    decision: str
    reason: str


class Pointer(BaseModel):
    path: str
    anchor: str = Field("", description="heading, symbol, or line range")
    note: str = ""


class LogEntry(BaseModel):
    stage: str
    summary: str


class StopNote(BaseModel):
    """Written by the engine (no AI call) whenever a relay stops before finishing."""

    kind: str  # cancelled | failed | awaiting_approval | budget
    stage: str
    reason: str
    at: str
    done_stages: list[str] = []
    remaining_stages: list[str] = []
    partial_actions: list[str] = []  # what the interrupted stage had already done (from the activity log)
    resume_hint: str = ""


class Baton(BaseModel):
    goal: str
    session_context: list[str] = Field(default_factory=list, description="같은 세션의 이전 요청 한 줄 요약")
    state: str = "시작 전"
    decisions: list[Decision] = []
    open_issues: list[str] = []
    next_steps: list[str] = []
    pointers: list[Pointer] = []
    outputs: dict[str, str] = Field(default_factory=dict, description="stage name -> deliverable")
    log: list[LogEntry] = []
    highlights: dict[str, list[str]] = Field(default_factory=dict, description="stage -> key actions (<=3)")
    user_checks: list[str] = Field(default_factory=list, description="things a human must verify")
    diagrams: dict[str, str] = Field(default_factory=dict, description="stage -> mermaid source")
    stop: StopNote | None = None

    def to_markdown(
        self,
        include_outputs: list[str] | None = None,
        max_output_chars: int | None = MAX_OUTPUT_CHARS,
        output_ref: str | None = None,
    ) -> str:
        """Render as HANDOFF.md.

        Prompt copies are capped: only the named stage outputs, each cut at max_output_chars
        (with a path to the full text), and only the last MAX_LOG relay log lines.
        """
        full = max_output_chars is None  # the saved HANDOFF.md / dashboard, not a stage prompt
        lines = [f"# HANDOFF\n\n## 목표\n{self.goal}\n"]
        if self.stop:
            st = self.stop
            label = {"cancelled": "취소로 중단", "failed": "실패로 중단", "awaiting_approval": "승인 대기",
                     "budget": "예산 도달로 대기"}.get(st.kind, st.kind)
            lines.append(f"## 중단 지점 — {label} ({st.at})")
            lines.append(f"- 멈춘 단계: `{st.stage}` · 사유: {st.reason}")
            if st.done_stages:
                lines.append("- 끝난 단계: " + " → ".join(st.done_stages))
            if st.remaining_stages:
                lines.append("- 남은 단계: " + " → ".join(st.remaining_stages))
            if st.partial_actions:
                lines.append("- 멈춘 단계에서 이미 한 일:")
                lines += [f"  - {a}" for a in st.partial_actions]
            if st.resume_hint:
                lines.append(f"- 이어가기: {st.resume_hint}")
            lines.append("")
        if self.session_context:
            lines.append("## 이 세션의 이전 요청 (자세한 내용은 handoff MCP 의 load_handoff(run id))")
            lines += [f"- {c}" for c in self.session_context]
            lines.append("")
        lines.append(f"## 현재 상태\n{self.state}\n")
        if self.user_checks:
            lines.append("## 사용자 확인 필요")
            lines += [f"- [ ] {c}" for c in self.user_checks]
            lines.append("")
        if full and self.highlights:
            lines.append("## 핵심 작업")
            for stage, items in self.highlights.items():
                lines += [f"- [{stage}] {i}" for i in items]
            lines.append("")
        if self.decisions:
            capped = max_output_chars is not None and len(self.decisions) > MAX_DECISIONS
            lines.append("## 내린 결정과 근거" + (f" (최근 {MAX_DECISIONS}개)" if capped else ""))
            lines += [f"- {d.decision} — {d.reason}" for d in (self.decisions[-MAX_DECISIONS:] if capped else self.decisions)]
            lines.append("")
        for title, items in (("미해결 이슈", self.open_issues), ("다음 단계", self.next_steps)):
            if items:
                lines.append(f"## {title}")
                lines += [f"- {i}" for i in items]
                lines.append("")
        if self.pointers:
            capped = max_output_chars is not None and len(self.pointers) > MAX_POINTERS
            lines.append("## 관련 파일 포인터" + (f" (최근 {MAX_POINTERS}개)" if capped else ""))
            lines += [f"- `{p.path}` {p.anchor} {('— ' + p.note) if p.note else ''}".rstrip()
                      for p in (self.pointers[-MAX_POINTERS:] if capped else self.pointers)]
            lines.append("")
        if self.log:
            capped = max_output_chars is not None and len(self.log) > MAX_LOG
            lines.append("## 릴레이 기록" + (f" (최근 {MAX_LOG}개)" if capped else ""))
            lines += [f"- [{e.stage}] {e.summary}" for e in (self.log[-MAX_LOG:] if capped else self.log)]
            lines.append("")
        names = self.outputs.keys() if include_outputs is None else include_outputs
        for name in names:
            if name not in self.outputs:
                continue
            text = self.outputs[name]
            if max_output_chars is not None and len(text) > max_output_chars:
                ref = f" 전체: `{output_ref.format(stage=name)}`" if output_ref else ""
                text = text[:max_output_chars] + f"\n…(잘림 {len(text) - max_output_chars}자.{ref})"
            lines.append(f"## 산출물: {name}\n{text}\n")
        if full:  # diagrams are for people; stage prompts don't pay for them
            for stage, src in self.diagrams.items():
                lines.append(f"## 그림: {stage}\n```mermaid\n{src}\n```\n")
        return "\n".join(lines)


class StageResult(BaseModel):
    """What every stage must return. Merged into the baton by the engine."""

    summary: str = Field(description="이 단계에서 한 일을 한두 문장으로")
    state: str = Field(description="작업 전체의 현재 상태")
    decisions_added: list[Decision] = []
    open_issues: list[str] = Field(description="남은 이슈 전체 목록(교체됨)")
    next_steps: list[str] = Field(description="다음 단계 전체 목록(교체됨)")
    pointers_added: list[Pointer] = []
    output: str = Field("", description="이 단계의 산출물. 다음 단계가 읽을 내용만")
    verdict: Literal["pass", "retry", "fail"] = Field(
        "pass", description="검토 단계용. retry 는 지정된 이전 단계로 되돌림"
    )
    highlights: list[str] = Field(default_factory=list, description="핵심 작업 최대 3줄, 각 60자 이내")
    user_checks: list[str] = Field(
        default_factory=list, description="AI 가 직접 확인할 수 없어 사람이 확인해야 할 것(구체적 행동). 없으면 []"
    )
    diagram: str = Field(
        "", description="흐름·구조가 바뀌어 그림이 이해를 돕는 경우에만 Mermaid 소스(노드 12개 이하). 아니면 빈 문자열"
    )

    def apply(self, baton: Baton, stage: str) -> Baton:
        b = baton.model_copy(deep=True)
        b.state = self.state
        b.decisions += self.decisions_added
        b.open_issues = self.open_issues
        b.next_steps = self.next_steps
        known = {(p.path, p.anchor) for p in b.pointers}
        for p in self.pointers_added:
            if (p.path, p.anchor) not in known:
                known.add((p.path, p.anchor))
                b.pointers.append(p)
        if self.output:
            b.outputs[stage] = self.output
        b.log.append(LogEntry(stage=stage, summary=f"{self.summary} ({self.verdict})"))
        if self.highlights:
            b.highlights[stage] = [h.strip()[:120] for h in self.highlights[:3] if h.strip()]
        for check in self.user_checks:
            if check.strip() and check.strip() not in b.user_checks:
                b.user_checks.append(check.strip()[:200])
        if self.diagram.strip():
            b.diagrams[stage] = self.diagram.strip().removeprefix("```mermaid").removesuffix("```").strip()[:MAX_DIAGRAM_CHARS]
        b.stop = None  # a stage finished, so any earlier stop is resolved
        return b


def result_schema(all_required: bool = False) -> dict:
    """JSON schema for structured output, no extra properties.

    By default only fields without a default are required (summary, state, open_issues, next_steps):
    measured, models often drop an optional-looking field like `verdict`, and every schema rejection
    costs a full extra turn. OpenAI strict mode needs all_required=True.
    """
    schema = StageResult.model_json_schema()

    def strict(node: dict) -> None:
        node.pop("default", None)
        node.pop("title", None)
        if node.get("type") == "object" and "properties" in node:
            node["required"] = list(node["properties"].keys()) if all_required else node.get("required", [])
            node["additionalProperties"] = False
        for value in node.values():
            if isinstance(value, dict):
                strict(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        strict(item)

    strict(schema)
    for d in schema.get("$defs", {}).values():
        strict(d)
    return schema
