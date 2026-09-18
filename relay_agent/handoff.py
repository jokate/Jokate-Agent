"""HANDOFF writer: when a relay stops before finishing, a lightweight model writes the hand-over.

It is one cheap call (Haiku from a neutral folder, no tools, no thinking) over facts the engine already
has, so the next attempt starts from "what was asked, what is done, what is left" instead of a tool log.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from .runners import Usage

SYSTEM = """너는 작업 인계서(HANDOFF) 작성기다. 받은 사실만으로, 이 작업을 이어받을 AI 가 바로 이어서 일할 수 있게
한국어 마크다운으로 쓴다. 아래 세 절만, 머리말·맺음말 없이 바로 시작한다.

### 요청
- 사용자가 요청한 것 (실행 중 추가 지시 포함)
### 작업된 내역
- 실제로 끝낸 작업과 그 결과·결정. 바꾼 파일은 경로로
### 남은 일
- 멈춘 지점부터 이어서 해야 할 것, 순서대로

규칙
- 요청과 작업된 내역 기준으로 서술한다. 도구 호출·명령 실행·파일 읽기 같은 사용 이력은 쓰지 않는다.
- 사실에 없는 것은 추측하지 않는다. 끝났는지 불확실하면 '확인 필요'라고 쓴다.
- 전체 {limit}자 이내."""


class HandoffWriter:
    def __init__(self, model: str = "haiku", exe: str | None = None, timeout_s: int = 120, limit: int = 1200):
        self.model = model
        self.exe = exe or shutil.which("claude") or "claude"
        self.timeout_s = timeout_s
        self.limit = limit

    def __call__(self, facts: str) -> tuple[str, Usage]:
        # Neutral folder + --safe-mode: no CLAUDE.md, memory, skills or hooks in a one-shot summary call.
        args = [self.exe, "-p", "--model", self.model, "--output-format", "json", "--tools", "",
                "--system-prompt", SYSTEM.format(limit=self.limit), "--strict-mcp-config",
                "--no-session-persistence", "--safe-mode"]
        proc = subprocess.run(args, input=facts, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=self.timeout_s, cwd=tempfile.gettempdir(),
                              env={**os.environ, "MAX_THINKING_TOKENS": "0"})
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            raise RuntimeError((proc.stderr or proc.stdout or "no output")[-300:])
        if data.get("is_error"):
            raise RuntimeError(str(data.get("result") or data.get("subtype"))[:300])
        u = data.get("usage") or {}
        usage = Usage("claude", self.model, input_tokens=u.get("input_tokens", 0),
                      cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0),
                      cache_read_input_tokens=u.get("cache_read_input_tokens", 0),
                      output_tokens=u.get("output_tokens", 0), cost_usd=data.get("total_cost_usd"),
                      duration_ms=data.get("duration_ms", 0))
        text = str(data.get("result") or "").strip()
        if not text:
            raise RuntimeError("빈 인계서")
        return text, usage
