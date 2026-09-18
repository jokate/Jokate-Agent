"""Runners execute one stage on one provider: (role prompt, baton prompt) -> StageResult + usage.

- mock:       no model call; tests and dry runs.
- claude_cli: headless Claude Code (`claude -p`), subscription login, file/bash tools.
              MCP off unless listed (measured 184K -> 7.5K), system prompt replaced (7.5K -> 0.6K).
              Reports subscription usage (5-hour / 7-day utilization) from `rate_limit_event`.
- api:        Anthropic Messages API (per-token billing).
- cli:        other agent CLIs (Codex, OpenCode, Gemini, ...) from a command template.
- openai:     any OpenAI-compatible HTTP endpoint (OpenAI, OpenRouter, Ollama, ...).

Errors carry a kind so the engine can switch providers: quota (usage/rate limit), unavailable,
cancelled, or error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Protocol

from .baton import StageResult, result_schema
from .providers import ProviderRegistry, ProviderSpec, looks_like_quota


@dataclass
class Usage:
    runner: str
    model: str
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: int = 0
    estimated: bool = False  # token counts guessed from text length (provider did not report them)

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


@dataclass
class StageCall:
    stage: str
    model: str | None
    effort: str | None
    system: str
    prompt: str
    cwd: Path
    tools: list[str] | None = None  # claude_cli only; None = no tools
    mcp_servers: list[str] = field(default_factory=list)  # claude_cli only
    allowed_tools: list[str] = field(default_factory=list)  # claude_cli only
    mcp_overrides: dict[str, dict] = field(default_factory=dict)  # per-run MCP entries (e.g. repo docs root)
    settings_path: Path | None = None  # Claude Code --settings (pre-edit backup hook for in-place runs)
    permission_mode: str = "default"
    timeout_s: int = 1800
    stall_s: int = 480  # no output from the AI process for this long -> killed and started once more
    # claude_cli token controls
    system_mode: str = "replace"  # replace: our prompt only; append: keep Claude Code's default prompt
    isolate: bool = False  # skip CLAUDE.md / skills / hooks of the target folder
    max_budget_usd: float | None = None
    fallback_model: str | None = None
    # (kind, detail) activity callback, e.g. ("tool_use", {"tool": "Read", "target": "a.py"})
    on_event: Callable[[str, dict], None] | None = None
    cancel_event: threading.Event | None = None
    live: "LiveChannel | None" = None  # user messages injected into the running session (Claude Code)
    result_mode: str = "schema"  # claude_cli: "text" = JSON object at the end of the answer, "schema" = --json-schema
    add_dirs: list[str] = field(default_factory=list)  # extra readable folders (user attachments)
    project: bool = False  # the folder's own CLAUDE.md / skills / MCP apply (never --safe-mode or skill-less)
    env: dict[str, str] = field(default_factory=dict)  # extra environment for the CLI process
    # claude_cli: keep the conversation (session_id) so a cancelled/failed stage can be continued with
    # resume_session instead of re-exploring from scratch
    session_id: str | None = None
    resume_session: str | None = None
    fresh_prompt: str | None = None  # the full prompt, used if resuming the conversation fails

    def emit(self, kind: str, detail: dict) -> None:
        if self.on_event:
            self.on_event(kind, detail)

    @property
    def cancelled(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())


class TokenWatch:
    """Where a stage's tokens go. Every model turn re-reads the whole context (billed as cache reads), so
    cost ~ turns x context size; the context grows with each tool result. Records both, reported once."""

    def __init__(self):
        self.turns: list[dict] = []  # per API call: context size and output
        self.results: list[dict] = []  # per tool result: tool, target, chars
        self._seen: set[str] = set()

    def on_event(self, event: dict, pending: dict) -> None:
        kind = event.get("type")
        msg = event.get("message") or {}
        if kind == "assistant" and msg.get("id") and msg["id"] not in self._seen and msg.get("usage"):
            self._seen.add(msg["id"])
            u = msg["usage"]
            self.turns.append({"ctx": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                               + u.get("cache_read_input_tokens", 0),
                               "new": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)})
        elif kind == "user":
            for block in msg.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                    if block.get("tool_use_id", "") not in pending:
                        continue  # the StructuredOutput acknowledgement, not a tool the model used
                    name, target = pending[block["tool_use_id"]]
                    self.results.append({"tool": name, "target": target, "chars": len(text)})

    def report(self) -> dict:
        ctx = [x["ctx"] for x in self.turns]
        top = sorted(self.results, key=lambda r: -r["chars"])[:8]
        return {"turns": len(ctx), "contexts": ctx[:80], "peak_context": max(ctx, default=0),
                "reread_tokens": sum(ctx), "tool_results": len(self.results),
                "tool_result_chars": sum(r["chars"] for r in self.results), "top_results": top}


class LiveChannel:
    """A line to the stdin of the AI session running now. send() returns False when nothing accepts input
    (another AI, not started yet, or the answer is already in) — the caller then falls back to the baton."""

    def __init__(self):
        self._lock = threading.Lock()
        self._write: Callable[[str], None] | None = None

    def attach(self, write: Callable[[str], None]) -> None:
        with self._lock:
            self._write = write

    def detach(self) -> None:
        with self._lock:
            self._write = None

    def send(self, text: str) -> bool:
        with self._lock:
            if self._write is None:
                return False
            try:
                self._write(text)
                return True
            except (OSError, ValueError):
                self._write = None
                return False


def user_message_line(text: str) -> str:
    """One stream-json input message for `claude -p --input-format stream-json`."""
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n"


class RunnerError(RuntimeError):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind  # error | quota | unavailable | cancelled


class Runner(Protocol):
    name: str

    def run(self, call: StageCall) -> tuple[StageResult, Usage]: ...


class LiveDetail:
    """What the AI is doing right now, from the stream: the thinking/text being written (tail), the tool call
    being composed, or the tool that is running and for how long. In memory only, never logged."""

    TAIL = 240

    def __init__(self):
        self.doing = "start"
        self.snippet = ""
        self.tool: str | None = None
        self.target: str | None = None
        self.tool_since: float | None = None
        self.tools = 0

    def on_stderr(self, line: str) -> dict:
        """A retry/overload notice on stderr: the CLI is waiting on the API, not working."""
        self.doing, self.snippet = "retry", line[-self.TAIL:]
        return self.pulse()

    def pulse(self) -> dict:
        return {"doing": self.doing, "snippet": self.snippet, "tool": self.tool, "target": self.target,
                "tool_since": self.tool_since, "tools": self.tools}

    def on_event(self, event: dict) -> dict:
        etype = event.get("type")
        if etype == "stream_event":
            ev = event.get("event") or {}
            kind = ev.get("type")
            if kind == "content_block_start":
                block = ev.get("content_block") or {}
                btype = block.get("type")
                self.doing = {"thinking": "thinking", "text": "writing", "tool_use": "tool_input"}.get(btype, self.doing)
                self.snippet = ""
                if btype == "tool_use":
                    self.tool, self.target = block.get("name"), None
            elif kind == "content_block_delta":
                d = ev.get("delta") or {}
                piece = d.get("thinking") or d.get("text") or d.get("partial_json") or ""
                if piece:
                    self.snippet = (self.snippet + piece)[-self.TAIL:]
        elif etype == "assistant":
            blocks = (event.get("message") or {}).get("content", []) or []
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            if uses:
                last = uses[-1]
                self.doing, self.tool, self.tools = "tool", last.get("name"), len(uses)
                self.target = describe_tool(self.tool or "", last.get("input") or {})
                self.tool_since, self.snippet = time.time(), ""
            else:
                self.doing = "thinking" if any(b.get("type") == "thinking" for b in blocks) else "writing"
        elif etype == "user":
            self.doing, self.tool, self.target, self.tool_since, self.snippet = "tool_result", None, None, None, ""
        elif etype == "system" and event.get("subtype") == "thinking_tokens":
            self.doing = "thinking"
        elif etype == "system" and "retry" in str(event.get("subtype", "")):
            self.doing = "retry"
            self.snippet = " ".join(f"{k}={v}" for k, v in event.items() if k not in ("type", "subtype", "uuid", "session_id"))[-self.TAIL:]
        return self.pulse()


def describe_tool(name: str, tool_input: dict) -> str:
    """Short human-readable target of a tool call."""
    for key in ("file_path", "command", "pattern", "path", "url", "query"):
        if key in tool_input:
            value = str(tool_input[key])
            if key == "pattern" and "path" in tool_input:
                value += f"  @ {tool_input['path']}"
            return value[:200]
    return json.dumps(tool_input, ensure_ascii=False)[:200]


RETRY_RE = re.compile(r"retry|retrying|overloaded|rate.?limit|529|503|ECONNRESET|ETIMEDOUT|fetch failed", re.I)


def run_process(args: list[str], call: StageCall, stdin_text: str | None,
                on_line: Callable[[str], None], live: LiveChannel | None = None,
                on_stderr: Callable[[str], None] | None = None,
                busy: Callable[[], bool] | None = None) -> tuple[int, str]:
    """Stream stdout lines to on_line. stderr is drained on a thread (no pipe deadlock); lines that look like
    API retries/overload go to on_stderr so the dashboard can show why nothing is happening.
    Kills the whole process tree on timeout, cancellation, a stall (no output for call.stall_s while `busy()` is
    false — a running tool such as a long build prints nothing and is not a stall) or a callback error.
    With `live`, stdin stays open for stream-json user messages until the result line arrives."""
    # npm installs CLIs as .cmd shims that CreateProcess can't find by bare name; resolve the full path.
    args = [shutil.which(args[0]) or args[0], *args[1:]]
    if os.name == "nt" and sum(len(a) + 3 for a in args) > 30000:
        raise RunnerError("명령줄이 Windows 한도(~32K)를 넘습니다 — stdin 으로 프롬프트를 받는 CLI 를 쓰거나 바통을 줄이세요")
    if call.cancelled:
        raise RunnerError("사용자가 취소함", "cancelled")
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", cwd=call.cwd,
            env={**os.environ, **call.env} if call.env else None,
        )
    except FileNotFoundError as e:
        raise RunnerError(f"{Path(args[0]).name} not installed", "unavailable") from e
    stderr_chunks: list[str] = []
    last_output = [time.monotonic()]

    def drain_stderr() -> None:
        for line in proc.stderr:
            stderr_chunks.append(line)
            if on_stderr is not None and RETRY_RE.search(line):
                last_output[0] = time.monotonic()  # the CLI is alive, waiting on the API
                try:
                    on_stderr(line.strip())
                except Exception:
                    pass

    drain = threading.Thread(target=drain_stderr, daemon=True)
    drain.start()
    stop = threading.Event()
    reason: list[str] = []

    def watch():
        deadline = time.monotonic() + call.timeout_s
        while not stop.is_set():
            now = time.monotonic()
            if busy is not None and busy():
                last_output[0] = now  # a tool is running: the silence is the command, not the AI
            stalled = call.stall_s and now - last_output[0] > call.stall_s
            if call.cancelled or now > deadline or stalled:
                reason.append("cancelled" if call.cancelled else "timeout" if now > deadline else "stalled")
                call.emit("process_stopped", {"why": reason[0], **kill_tree(proc)})
                return
            stop.wait(0.3)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    write_lock = threading.Lock()

    def write(text: str) -> None:
        with write_lock:
            proc.stdin.write(user_message_line(text))
            proc.stdin.flush()

    def close_input() -> None:
        if live is not None:
            live.detach()
        with write_lock:
            try:
                proc.stdin.close()
            except OSError:
                pass

    try:
        if stdin_text is not None:
            proc.stdin.write(stdin_text)
            proc.stdin.flush()
        if live is None:
            proc.stdin.close()
        else:
            live.attach(write)
        for line in proc.stdout:
            last_output[0] = time.monotonic()
            on_line(line)
            if live is not None and '"result"' in line and (parse_json_line(line) or {}).get("type") == "result":
                close_input()  # the answer is in: end the session (it would otherwise wait for more input)
        proc.wait()
    except BaseException:
        kill_tree(proc)  # e.g. the event callback failed: don't leave the AI editing files
        raise
    finally:
        if live is not None:
            close_input()
        stop.set()
        drain.join(timeout=5)
        if reason:
            watcher.join(timeout=20)  # its process_stopped report (kill + survivors check) comes before the error
    if reason and reason[0] == "cancelled":
        raise RunnerError("사용자가 취소함", "cancelled")
    if reason and reason[0] == "timeout":
        raise RunnerError(f"timeout after {call.timeout_s}s", "timeout")
    if reason and reason[0] == "stalled":
        tail = "".join(stderr_chunks)[-300:].strip()
        raise RunnerError(f"AI 프로세스가 {call.stall_s // 60}분간 아무 출력이 없어 중단" + (f" (stderr: {tail})" if tail else ""),
                          "stalled")
    return proc.returncode, "".join(stderr_chunks)


def pid_alive(pid: int) -> bool:
    """Is a process (e.g. a `relay run` in a terminal, or a tool the AI started) still alive?"""
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def descendants(pid: int) -> list[int]:
    """Child processes (shells, builds, tests the AI started), so a cancel can check they are gone too."""
    try:
        if os.name == "nt":
            out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                  "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        else:
            out = subprocess.run(["ps", "-e", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in found:
                found.append(child)
                todo.append(child)
    return found


def kill_tree(proc: subprocess.Popen) -> dict:
    """Kill a process and its children (claude.exe spawns shells and tools), then check they are really gone."""
    if proc.poll() is not None:
        return {"pid": proc.pid, "confirmed": True, "children": 0, "survivors": []}
    tree = descendants(proc.pid)
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    survivors = [p for p in tree if pid_alive(p)]
    for p in survivors:  # orphaned grandchildren (e.g. a build the AI started): one more try each
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p)], capture_output=True)
        else:
            try:
                os.kill(p, 9)
            except OSError:
                pass
    survivors = [p for p in survivors if pid_alive(p)]
    return {"pid": proc.pid, "confirmed": proc.poll() is not None and not survivors,
            "children": len(tree), "survivors": survivors}


class MockRunner:
    """Deterministic runner. `script` maps stage name -> list of results consumed in order."""

    name = "mock"

    def __init__(self, script: dict[str, list[dict]] | None = None, delay_s: float = 0.0):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[StageCall] = []
        self.delay_s = delay_s
        self.live_messages: list[str] = []

    def run(self, call: StageCall) -> tuple[StageResult, Usage]:
        self.calls.append(call)
        if call.live is not None:
            call.live.attach(self.live_messages.append)
        call.emit("tool_use", {"tool": "Read", "target": f"mock/{call.stage}.txt"})
        if call.cancel_event is not None:
            if call.cancel_event.wait(self.delay_s):
                raise RunnerError("사용자가 취소함", "cancelled")
        else:
            time.sleep(self.delay_s)
        queue = self.script.get(call.stage)
        scripted = bool(queue)
        data = queue.pop(0) if queue else {
            "summary": f"{call.stage} 완료(mock)",
            "state": f"{call.stage} 까지 진행",
            "open_issues": [],
            "next_steps": [],
            "output": f"[{call.stage} mock 산출물]",
        }
        if not scripted and call.stage == "build":
            # demo relay: show how key actions, user checks and a diagram look on the dashboard
            data |= {
                "highlights": ["로그인 폼에 비밀번호 보기 토글 추가", "토글 상태를 세션 동안 유지"],
                "user_checks": ["브라우저에서 토글 클릭 시 비밀번호가 보이는지 확인", "스크린리더로 라벨이 읽히는지 확인"],
                "diagram": "flowchart LR\n  U[사용자 클릭] --> T{토글 상태}\n  T -- 보기 --> V[type=text]\n  T -- 숨김 --> H[type=password]",
            }
        if isinstance(data, Exception):
            raise data
        if call.live is not None:
            call.live.detach()
        usage = Usage(self.name, call.model or "mock", input_tokens=len(call.system + call.prompt) // 4, output_tokens=50)
        return StageResult.model_validate(data), usage


# --- Claude Code (headless) ------------------------------------------------------
REPLACE_PREAMBLE = """You are one stage of an automated relay run by a programmer's agent server.
Environment: {os_name}. Working directory: {cwd} (already the current directory; use relative paths; `cd` only
as `cd <project root> && <command>` when the prompt names a project root).
Rules:
- Locate with Grep/Glob first, then Read only the needed line ranges (offset/limit). Never read whole large files.
- Use tools directly without narration. Do not repeat file contents in your answer.
- Turn budget: every turn re-reads the whole context (10K+ tokens), so aim for about 4 turns:
  1) explore ONCE: put every search/read you may need in one message (several tool calls in parallel, or one
     Bash command chaining them with `;`, e.g. `cat -n a.py; ls tests; grep -rn "X" src | head -40`);
  2) edit ONCE: all Edit/Write calls in one message;  3) verify ONCE: the single relevant command;  4) answer.
  Look a little wider in step 1 rather than coming back for one more grep. No no-op or "just checking" turns.
- Edit with the Edit tool (small exact replacements). Run only the verification command you need, once.
- Bash runs without a human: only pre-approved commands work — the verification/project commands listed in the
  prompt and read-only ls, cat, head, tail, wc, grep, rg, find, git log/show/diff/status. Every part of a chained
  command is checked; one unapproved part blocks the whole line. Stay inside the working directory.
- If a command is denied or needs approval, do NOT retry it or a variant. Note it in open_issues and continue.
{finish}
"""

FINISH_SCHEMA = """- Finish with ONE structured output call: summary, state, open_issues, next_steps are required; add decisions_added,
  pointers_added, output, verdict (pass|retry|fail) when relevant. Write the text fields in Korean."""
FINISH_TEXT = """- Your FINAL message is only one JSON object (no code fence, no prose). Required: "summary" (1-2 sentences),
  "state" (overall status), "open_issues" [str], "next_steps" [str]. Optional: "decisions_added" [{"decision","reason"}],
  "pointers_added" [{"path","anchor","note"}], "output" (deliverable for the next stage), "verdict" (pass|retry|fail),
  "highlights" [<=3 short str], "user_checks" [str], "diagram" (mermaid, only if it helps),
  "needs_approval" (bool) + "approval_reason" when a human must decide before going on. Text values in Korean."""


DIGEST_RULE = """
Documents and large files: to understand several docs or any file over ~8K chars, call the digest tool
(paths/globs, e.g. ["Docs/**/*.md"]) instead of Reading them one by one. Each file is summarized in its own
fresh call and cached, so only summaries enter your context and later stages get them for free. Summaries
cite sections (§); Read only the exact section you still need. Reading many whole docs makes every later
turn re-read all of them.
"""

# a digest over dozens of files is one long tool call
os.environ.setdefault("MCP_TOOL_TIMEOUT", "900000")


def os_name() -> str:
    import platform

    return {"Windows": "Windows (bash tool runs Git Bash)", "Darwin": "macOS"}.get(platform.system(), platform.system())


# Read-only inspection commands a headless stage may always run (with Bash). Claude Code checks every part
# of a chained command; without these, `cat a; ls b` stops at "requires approval" and the turn is wasted.
READ_ONLY_BASH = ["Bash(ls:*)", "Bash(dir:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
                  "Bash(grep:*)", "Bash(rg:*)", "Bash(find:*)", "Bash(pwd)", "Bash(echo:*)", "Bash(which:*)",
                  "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)", "Bash(git status:*)", "Bash(git blame:*)"]


class ClaudeCliRunner:
    name = "claude"

    def __init__(self, exe: str | None = None, mcp_registry: dict[str, dict] | None = None, name: str = "claude"):
        self.exe = exe or shutil.which("claude") or "claude"
        self.mcp_registry = mcp_registry or {}
        self.name = name

    def build_args(self, call: StageCall, mcp_config_path: Path | None) -> list[str]:
        args = [
            self.exe, "-p",
            "--output-format", "stream-json", "--verbose",
            "--include-partial-messages",  # thinking/text as it is written -> the dashboard shows what the AI is doing
            "--input-format", "stream-json",  # keeps stdin open so a user note can join the running session
            "--permission-mode", call.permission_mode,
            "--strict-mcp-config",
        ]
        if call.result_mode != "text" or call.system_mode != "replace":
            args += ["--json-schema", json.dumps(result_schema(), ensure_ascii=False)]
        if call.resume_session:
            args += ["--resume", call.resume_session]
        else:
            args += ["--session-id", call.session_id] if call.session_id else ["--no-session-persistence"]
        if call.model:
            args += ["--model", call.model]
        if call.system_mode == "replace":
            # Replacing Claude Code's default system prompt was measured at ~7.5K -> ~0.6K input tokens.
            # It also drops the environment/tool-usage guidance, so restate the essentials.
            finish = FINISH_TEXT if call.result_mode == "text" else FINISH_SCHEMA
            args += ["--system-prompt", REPLACE_PREAMBLE.format(cwd=call.cwd, os_name=os_name(), finish=finish)
                     + (DIGEST_RULE if "digest" in call.mcp_servers else "") + call.system]
        else:
            # Moves cwd/git-status out of the system prompt so it caches across working directories.
            args += ["--append-system-prompt", call.system, "--exclude-dynamic-system-prompt-sections"]
        if call.isolate and not call.project:
            # --safe-mode also disables MCP servers and hooks, so only disable skills when either is needed.
            args += ["--disable-slash-commands"] if (call.mcp_servers or call.settings_path) else ["--safe-mode"]
        if call.settings_path:
            args += ["--settings", str(call.settings_path)]
        for extra in call.add_dirs:
            args += ["--add-dir", extra]
        if call.max_budget_usd is not None:
            args += ["--max-budget-usd", str(call.max_budget_usd)]
        if call.fallback_model and call.fallback_model != call.model:
            args += ["--fallback-model", call.fallback_model]  # CLI-level switch on overload
        if call.effort:
            args += ["--effort", call.effort]
        args += ["--tools", ",".join(call.tools) if call.tools else ""]
        # Headless runs cannot answer permission prompts: pre-approve listed MCP servers and tools.
        allowed = [f"mcp__{s}" for s in call.mcp_servers] + call.allowed_tools
        if "Bash" in (call.tools or []):
            allowed += [r for r in READ_ONLY_BASH if r not in allowed]
        if allowed:
            args += ["--allowedTools", ",".join(allowed)]
        if mcp_config_path:
            args += ["--mcp-config", str(mcp_config_path)]
        return args

    @staticmethod
    def session_file(cwd: Path, session_id: str) -> Path:
        """Where Claude Code keeps a conversation: ~/.claude/projects/<cwd with non-alphanumerics as '-'>/<id>.jsonl"""
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
        return Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"

    def run(self, call: StageCall) -> tuple[StageResult, Usage]:
        """Run on call.model; if that fails (unavailable, limit, error), retry once on fallback_model."""
        if call.resume_session:
            if not self.session_file(call.cwd, call.resume_session).exists():
                # killed before the CLI saved anything (e.g. a cancel in the first seconds): nothing to continue
                call.emit("resume_fallback", {"reason": "이전 대화가 저장되지 않았음 (시작 직후 중단)"})
                call = replace(call, resume_session=None, prompt=call.fresh_prompt or call.prompt)
            else:
                try:
                    return self._run_once(call, call.model)
                except RunnerError as e:
                    if e.kind in ("cancelled", "quota", "budget"):
                        raise
                    # the saved conversation is gone or unusable: start the stage fresh
                    call.emit("resume_fallback", {"reason": str(e)[:200]})
                    call = replace(call, resume_session=None, prompt=call.fresh_prompt or call.prompt)
        try:
            try:
                return self._run_once(call, call.model)
            except RunnerError as e:
                if e.kind != "stalled":
                    raise
                # no output for stall_s: usually the CLI stuck before/at the API. One fresh start, same model.
                call.emit("stall_restart", {"reason": str(e)[:300]})
                return self._run_once(call, call.model)
        except RunnerError as e:
            # A timeout would just burn the same time again (on a half-edited workspace); a cancel is final.
            # A usage limit goes to the engine, which benches just that model and remembers it for later stages.
            if e.kind in ("cancelled", "timeout", "quota") or not call.fallback_model or call.fallback_model == call.model:
                raise
            call.emit("model_fallback", {"from": call.model, "to": call.fallback_model, "reason": str(e)[:300]})
            return self._run_once(call, call.fallback_model)

    def _run_once(self, call: StageCall, model: str | None) -> tuple[StageResult, Usage]:
        call = replace(call, model=model)
        with tempfile.TemporaryDirectory(prefix="katae-") as tmp:  # never inside the workspace (would enter the patch)
            cfg_path = None
            registry = {**self.mcp_registry, **call.mcp_overrides}
            if call.mcp_servers:
                missing = [s for s in call.mcp_servers if s not in registry]
                if missing:
                    raise RunnerError(f"unknown MCP servers for stage {call.stage}: {missing}", "unavailable")
                cfg_path = Path(tmp) / "mcp.json"
                cfg_path.write_text(
                    json.dumps({"mcpServers": {s: registry[s] for s in call.mcp_servers}}), encoding="utf-8"
                )
            started = time.monotonic()
            box: dict = {"texts": []}
            pending: dict[str, tuple[str, str]] = {}  # tool_use_id -> (tool name, target), for result sizes
            watch = TokenWatch()

            detail = LiveDetail()
            last_pulse = [0.0]

            def on_line(line: str) -> None:
                event = parse_json_line(line)
                if event is None:
                    return
                etype = event.get("type")
                pulse = detail.on_event(event)
                if etype != "stream_event" or time.monotonic() - last_pulse[0] > 0.5:  # chunks arrive many per second
                    last_pulse[0] = time.monotonic()
                    call.emit("pulse", {**pulse, "turn": len(watch.turns)})
                if etype == "stream_event":
                    return  # partial chunks: liveness only; the full message follows as its own line
                if etype == "result":
                    box["result"] = event
                elif event.get("type") == "rate_limit_event":
                    info = event.get("rate_limit_info") or {}
                    box["limit_status"] = info.get("status")
                    box["limit_type"] = info.get("rateLimitType")
                    box["limit_resets"] = info.get("resetsAt")
                    self._emit_limits(call, info)
                else:
                    if event.get("type") == "user":
                        watch.on_event(event, pending)  # before _emit_activity pops the tool name
                    self._emit_activity(call, event, pending, box["texts"])
                    if event.get("type") == "assistant":
                        watch.on_event(event, pending)

            try:
                code, stderr = run_process(self.build_args(call, cfg_path), call, user_message_line(call.prompt),
                                           on_line, live=call.live or LiveChannel(),
                                           on_stderr=lambda ln: call.emit("pulse", {**detail.on_stderr(ln),
                                                                                   "turn": len(watch.turns)}),
                                           busy=lambda: detail.doing == "tool")
            finally:
                if watch.turns or watch.results:
                    call.emit("token_report", watch.report())
        data = box.get("result")
        rejected = box.get("limit_status") == "rejected"  # structured signal from Claude Code itself

        def error(message: str, kind: str) -> RunnerError:
            err = RunnerError(message, kind)
            # which model hit which limit, so the engine can bench just that model (e.g. Fable) and not all of Claude
            err.model, err.limit_type, err.resets_at = model, box.get("limit_type"), box.get("limit_resets")
            return err

        if data is None:
            text = stderr[-500:]
            raise error(f"claude -p ended without a result (exit {code}): {text}",
                        "quota" if rejected or looks_like_quota(text) else "error")
        subtype = data.get("subtype") or ""
        if "structured_output" not in data and not data.get("is_error") and isinstance(data.get("result"), str):
            try:
                data = {**data, "structured_output": extract_result(data["result"])}
            except RunnerError:
                pass  # not JSON: _salvage keeps the prose as the stage output
        salvage = self._salvage(call, data, subtype, box["texts"])
        if salvage is not None:
            data = {**data, "structured_output": salvage, "is_error": False}
        if data.get("is_error") or "structured_output" not in data:
            # error results often carry no "result" text: the reason is in subtype / errors / terminal_reason
            detail = data.get("result") or " · ".join(str(x) for x in (data.get("errors") or [])) or data.get("terminal_reason") or ""
            text = f"{subtype}: {detail}"[:500] if subtype and subtype != "success" else str(detail)[:500]
            cost = data.get("total_cost_usd")
            if subtype == "error_max_budget_usd" or data.get("terminal_reason") == "budget_exhausted":
                err = RunnerError(f"단계 예산 ${call.max_budget_usd} 도달 (사용 ${cost or 0:.2f}, {data.get('num_turns')}턴)", "budget")
                err.cost_usd = cost
                raise err
            if subtype == "error_max_turns":
                raise RunnerError(f"최대 턴 수 도달 ({data.get('num_turns')}턴): {detail}"[:500])
            if not text.strip() or text == "None":
                text = f"결과 없음 (subtype={subtype or '?'}, stop={data.get('stop_reason')}, turns={data.get('num_turns')})"
            # Only an error result can be a limit message; a normal answer that merely mentions "limit" is not.
            quota = rejected or data.get("api_error_status") == 429 or (data.get("is_error") and looks_like_quota(text))
            raise error(f"claude -p failed: {text}", "quota" if quota else "error")
        u = data.get("usage", {})
        served = [m for m in (data.get("modelUsage") or {}) if "haiku" not in m or "haiku" in (call.model or "")]
        usage = Usage(
            self.name,
            served[0] if served else (call.model or "claude"),  # the model that actually answered
            input_tokens=u.get("input_tokens", 0),
            cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=u.get("cache_read_input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            cost_usd=data.get("total_cost_usd"),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        out = data["structured_output"]
        return (out if isinstance(out, StageResult) else StageResult.lenient(out)), usage

    @staticmethod
    def _salvage(call: StageCall, data: dict, subtype: str, texts: list[str]):
        """The structured result is missing or malformed (small models sometimes answer in prose, or give up
        after the schema retries). Recover instead of failing the relay; limits and budget are real errors."""
        if data.get("subtype") in ("error_max_budget_usd",) or data.get("terminal_reason") == "budget_exhausted":
            return None
        if data.get("api_error_status") or looks_like_quota(str(data.get("result") or "")) and data.get("is_error"):
            return None
        out = data.get("structured_output")
        if isinstance(out, dict):
            try:
                StageResult.model_validate(out)
                return None  # fine as is
            except ValueError:
                try:
                    fixed = StageResult.lenient(out)
                    call.emit("result_repaired", {"reason": "결과 필드 형식을 자동 보정"})
                    return fixed
                except ValueError:
                    pass
        schema_trouble = "structured" in subtype or "structured" in str(data.get("errors") or "").lower()
        if data.get("is_error") and not schema_trouble:
            return None
        text = (data.get("result") if isinstance(data.get("result"), str) else "") or "\n\n".join(texts[-3:])
        if not text.strip():
            return None
        try:
            result = extract_result(text)
        except RunnerError:
            result = StageResult.from_text(text)
        call.emit("result_repaired", {"reason": f"구조화 결과 없이 끝남({subtype or 'no structured_output'}) — 본문에서 복구"})
        return result

    def _emit_limits(self, call: StageCall, info: dict) -> None:
        windows = [
            {"window": name, "utilization": w.get("utilization"), "resets_at": w.get("resetsAt")}
            for name, w in (info.get("unifiedWindows") or {}).items()
        ]
        call.emit("rate_limit", {"provider": self.name, "status": info.get("status"), "windows": windows})

    LARGE_RESULT_CHARS = 8000

    @staticmethod
    def _emit_activity(call: StageCall, event: dict, pending: dict | None = None, texts: list | None = None) -> None:
        """tool_use / mcp_call when a tool is called; mcp_result (size) for MCP results and
        tool_result_large for any result big enough to matter for tokens; tool_error on failures."""
        if event.get("type") not in ("assistant", "user"):
            return
        pending = pending if pending is not None else {}
        for block in event.get("message", {}).get("content", []) or []:
            btype = block.get("type")
            if btype == "tool_use" and block.get("name") != "StructuredOutput":
                name = block.get("name", "")
                target = describe_tool(name, block.get("input") or {})
                pending[block.get("id", "")] = (name, target)
                if name.startswith("mcp__"):
                    server, _, tool = name[len("mcp__"):].partition("__")
                    call.emit("mcp_call", {"server": server, "tool": tool, "target": target})
                else:
                    call.emit("tool_use", {"tool": name, "target": target})
            elif btype == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                name, target = pending.pop(block.get("tool_use_id", ""), ("", ""))
                if name.startswith("mcp__"):
                    server, _, tool = name[len("mcp__"):].partition("__")
                    call.emit("mcp_result", {"server": server, "tool": tool, "chars": len(text),
                                             "is_error": bool(block.get("is_error"))})
                elif len(text) >= ClaudeCliRunner.LARGE_RESULT_CHARS:
                    call.emit("tool_result_large", {"tool": name, "target": target, "chars": len(text)})
                if block.get("is_error"):
                    call.emit("tool_error", {"message": text[:300]})
            elif btype == "tool_use" and block.get("name") == "StructuredOutput":
                inp = block.get("input") or {}
                call.emit("stage_answer", {"summary": str(inp.get("summary", ""))[:500],
                                           "verdict": inp.get("verdict", "pass"),
                                           "open_issues": (inp.get("open_issues") or [])[:8]
                                           if isinstance(inp.get("open_issues"), list) else []})
            elif btype == "text" and block.get("text", "").strip():
                text = block["text"].strip()
                if texts is not None:
                    texts.append(text)
                if text.startswith("{") and '"summary"' in text:
                    try:
                        answer = extract_result(text)  # the final result JSON (text result mode)
                        call.emit("stage_answer", {"summary": answer.summary[:500], "verdict": answer.verdict,
                                                   "open_issues": answer.open_issues[:8]})
                        continue
                    except RunnerError:
                        pass
                call.emit("note", {"text": text[:4000]})
            elif btype == "thinking" and (block.get("thinking") or "").strip():
                call.emit("thinking", {"text": block["thinking"].strip()[:4000]})


def probe_claude_limits(exe: str | None = None, timeout_s: int = 90) -> dict:
    """Cheapest possible call (Haiku, no tools, one-line system prompt) just to read the
    subscription usage windows Claude Code reports. Costs a few hundred tokens."""
    call = StageCall(stage="probe", model="haiku", effort=None, system="", prompt="ok", cwd=Path.home(),
                     timeout_s=timeout_s)
    args = [exe or shutil.which("claude") or "claude", "-p", "--output-format", "stream-json", "--verbose",
            "--model", "haiku", "--system-prompt", "Reply with OK.", "--no-session-persistence",
            "--tools", "", "--strict-mcp-config"]
    found: dict = {}

    def on_line(line: str) -> None:
        event = parse_json_line(line)
        if event and event.get("type") == "rate_limit_event":
            info = event.get("rate_limit_info") or {}
            found["status"] = info.get("status")
            found["windows"] = [{"window": k, "utilization": w.get("utilization"), "resets_at": w.get("resetsAt")}
                                for k, w in (info.get("unifiedWindows") or {}).items()]
        elif event and event.get("type") == "result":
            found["cost_usd"] = event.get("total_cost_usd")

    run_process(args, call, "ok", on_line)
    return found


def parse_json_line(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


# --- other agent CLIs and OpenAI-compatible APIs -----------------------------------
JSON_INSTRUCTION = """

---
## 반환 형식 (반드시 지킬 것)
작업을 마치면 마지막 응답으로 아래 JSON 스키마를 만족하는 JSON 객체 하나만 출력한다. 코드 블록·설명 없이 JSON 만.
{schema}
"""


def compact_schema() -> str:
    return json.dumps(result_schema(), ensure_ascii=False, separators=(",", ":"))


def extract_result(text: str) -> StageResult:
    """Find the last JSON object in free text that validates as a StageResult."""
    decoder = json.JSONDecoder()
    candidates = []
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "summary" in obj:
                candidates.append(obj)
    for obj in reversed(candidates):
        try:
            return StageResult.lenient(obj)
        except ValueError:
            continue
    raise RunnerError("no valid result JSON in output")


def estimate(spec: ProviderSpec, model: str | None, prompt: str, output: str) -> Usage:
    u = Usage(spec.name, model or spec.name, input_tokens=len(prompt) // 3, output_tokens=len(output) // 3, estimated=True)
    price = spec.prices.get(model or "")
    if price:
        u.cost_usd = (u.input_tokens * price[0] + u.output_tokens * price[1]) / 1_000_000
    return u


class ExternalCliRunner:
    """Codex / OpenCode / Gemini CLI and similar, driven by the provider's command template."""

    def __init__(self, spec: ProviderSpec):
        self.spec = spec
        self.name = spec.name

    def build_args(self, call: StageCall, out_file: Path, schema_file: Path, prompt: str) -> list[str]:
        fill = {"cwd": str(call.cwd), "out_file": str(out_file), "schema_file": str(schema_file)}
        args = [part.format(**fill) for part in self.spec.command]
        if call.model and self.spec.model_args:
            model_args = [part.format(model=call.model, **fill) for part in self.spec.model_args]
            at = min(2, len(args))  # after "<binary> <subcommand>"
            args = args[:at] + model_args + args[at:]
        if self.spec.prompt_via == "arg":
            args.append(prompt)
        elif self.spec.prompt_via == "arg_p":
            args += ["-p", prompt]
        return args

    def run(self, call: StageCall) -> tuple[StageResult, Usage]:
        prompt = f"{call.system}\n\n{call.prompt}{JSON_INSTRUCTION.format(schema=compact_schema())}"
        started = time.monotonic()
        lines: list[str] = []
        with tempfile.TemporaryDirectory(prefix="katae-") as tmp:
            out_file, schema_file = Path(tmp) / "last.txt", Path(tmp) / "schema.json"
            schema_file.write_text(json.dumps(result_schema(), ensure_ascii=False), encoding="utf-8")

            def on_line(line: str) -> None:
                lines.append(line)
                event = parse_json_line(line)
                if event:
                    self._maybe_limits(call, event)

            code, stderr = run_process(self.build_args(call, out_file, schema_file, prompt), call,
                                       prompt if self.spec.prompt_via == "stdin" else None, on_line)
            stdout = "".join(lines)
            final = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else stdout
        try:
            result = extract_result(final)
        except RunnerError:
            # Judge limits from the CLI's error stream only, not from the model's own text.
            kind = "quota" if code != 0 and looks_like_quota(stderr[-1500:]) else "error"
            raise RunnerError(f"{self.name} exit {code}: {(stderr or stdout)[-400:]}", kind)
        usage = estimate(self.spec, call.model, prompt, final)
        usage.duration_ms = int((time.monotonic() - started) * 1000)
        return result, usage

    def _maybe_limits(self, call: StageCall, event: dict) -> None:
        # Codex JSON events may carry {"rate_limits": {"primary": {"used_percent", "window_minutes", ...}}}
        limits = event.get("rate_limits") or (event.get("payload") or {}).get("rate_limits")
        if not isinstance(limits, dict):
            return
        windows = []
        for key, w in limits.items():
            if isinstance(w, dict) and "used_percent" in w:
                minutes = w.get("window_minutes")
                reset = w.get("resets_at") or (time.time() + w["resets_in_seconds"] if w.get("resets_in_seconds") else None)
                windows.append({"window": f"{minutes // 60}h" if minutes else key,
                                "utilization": w["used_percent"] / 100, "resets_at": reset})
        if windows:
            call.emit("rate_limit", {"provider": self.name, "status": "allowed", "windows": windows})


class OpenAICompatRunner:
    """Text-only stages on any OpenAI-compatible chat completions endpoint."""

    def __init__(self, spec: ProviderSpec, client=None):
        import httpx

        self.spec = spec
        self.name = spec.name
        key = os.environ.get(spec.api_key_env, "") if spec.api_key_env else ""
        self.client = client or httpx.Client(
            base_url=spec.base_url.rstrip("/"), timeout=600,
            headers={"Authorization": f"Bearer {key}"} if key else {},
        )

    def run(self, call: StageCall) -> tuple[StageResult, Usage]:
        if not call.model:
            raise RunnerError(f"{self.name}: no model mapped for this stage (set model_map)", "unavailable")
        body = {
            "model": call.model,
            "messages": [{"role": "system", "content": call.system}, {"role": "user", "content": call.prompt}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "stage_result", "schema": result_schema(all_required=True), "strict": True}},
        }
        started = time.monotonic()
        if call.cancelled:
            raise RunnerError("사용자가 취소함", "cancelled")
        resp = self.client.post("/chat/completions", json=body, timeout=call.timeout_s)
        if resp.status_code == 400:  # endpoint without json_schema support
            body["response_format"] = {"type": "json_object"}
            body["messages"][1]["content"] += JSON_INSTRUCTION.format(schema=compact_schema())
            resp = self.client.post("/chat/completions", json=body, timeout=call.timeout_s)
        if call.cancelled:
            raise RunnerError("사용자가 취소함", "cancelled")
        if resp.status_code in (402, 429) or (resp.status_code >= 400 and looks_like_quota(resp.text)):
            raise RunnerError(f"{self.name} {resp.status_code}: {resp.text[:300]}", "quota")
        if resp.status_code in (401, 403, 404):
            raise RunnerError(f"{self.name} {resp.status_code}: {resp.text[:300]}", "unavailable")
        if resp.status_code >= 400:
            raise RunnerError(f"{self.name} {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = data["choices"][0]["message"].get("content") or ""
        result = extract_result(text)
        u = data.get("usage") or {}
        usage = Usage(self.name, data.get("model") or call.model,
                      input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0),
                      duration_ms=int((time.monotonic() - started) * 1000))
        price = self.spec.prices.get(call.model)
        if price:
            usage.cost_usd = (usage.input_tokens * price[0] + usage.output_tokens * price[1]) / 1_000_000
        return result, usage


# --- Anthropic API -----------------------------------------------------------------
# Per-MTok list prices: (input, output). Cache write 5m = 1.25x input, read = 0.1x input.
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
}
MODEL_ALIASES = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-5", "opus": "claude-opus-5", "fable": "claude-fable-5-1"}


class AnthropicApiRunner:
    name = "anthropic_api"

    def __init__(self, client=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client

    def run(self, call: StageCall) -> tuple[StageResult, Usage]:
        """Run on call.model; on unavailability/overload/refusal, retry once on fallback_model."""
        import anthropic

        def classify(e: Exception) -> RunnerError:
            if isinstance(e, RunnerError):
                return e
            if isinstance(e, anthropic.RateLimitError):
                return RunnerError(str(e), "quota")
            if isinstance(e, (anthropic.NotFoundError, anthropic.PermissionDeniedError)):
                return RunnerError(str(e), "unavailable")
            return RunnerError(str(e))

        retryable = (
            anthropic.NotFoundError, anthropic.PermissionDeniedError, anthropic.RateLimitError,
            anthropic.InternalServerError, anthropic.APIConnectionError, RunnerError,
        )
        try:
            return self._run_once(call, call.model)
        except retryable as e:
            if isinstance(e, RunnerError) and e.kind in ("cancelled", "timeout"):
                raise
            if not call.fallback_model or call.fallback_model == call.model:
                raise classify(e) from e
            call.emit("model_fallback", {"from": call.model, "to": call.fallback_model, "reason": str(e)[:300]})
            try:
                return self._run_once(call, call.fallback_model)
            except retryable as e2:
                raise classify(e2) from e2

    def _run_once(self, call: StageCall, model: str | None) -> tuple[StageResult, Usage]:
        model = MODEL_ALIASES.get(model or "opus", model)
        kwargs: dict = {
            "model": model,
            # A backstop, not a tuning knob: hitting it wastes the whole attempt. Streaming avoids HTTP timeouts.
            "max_tokens": 64000,
            # Role prompt is identical across runs of this stage -> cached prefix.
            "system": [{"type": "text", "text": call.system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": call.prompt}],
            "output_config": {"format": {"type": "json_schema", "schema": result_schema()}},
        }
        betas: list[str] = []
        if model != "claude-haiku-4-5":
            kwargs["thinking"] = {"type": "adaptive"}
            if call.effort:
                kwargs["output_config"]["effort"] = call.effort
        # Server-side refusal fallback inside the same call: Fable -> Opus 5, Opus 5 -> Opus 4.8.
        refusal_fallback = {"claude-fable-5-1": "claude-opus-5", "claude-opus-5": "claude-opus-4-8"}.get(model)
        if refusal_fallback:
            betas.append("server-side-fallback-2026-06-01")
            kwargs["fallbacks"] = [{"model": refusal_fallback}]

        started = time.monotonic()
        client = self.client.with_options(timeout=call.timeout_s) if hasattr(self.client, "with_options") else self.client
        opener = (lambda: client.beta.messages.stream(betas=betas, **kwargs)) if betas else (
            lambda: client.messages.stream(**kwargs))
        with opener() as stream:
            for _ in stream:  # consume events so a cancel can stop generation mid-way
                if call.cancelled:
                    raise RunnerError("사용자가 취소함", "cancelled")
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            raise RunnerError(f"stage {call.stage} refused: {getattr(response, 'stop_details', None)}")
        if response.stop_reason == "max_tokens":
            raise RunnerError(f"stage {call.stage} hit max_tokens")
        text = next(b.text for b in response.content if b.type == "text")
        u = response.usage
        usage = Usage(
            self.name,
            getattr(response, "model", None) or model,  # reflects a server-side fallback
            input_tokens=u.input_tokens,
            cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
            cache_read_input_tokens=u.cache_read_input_tokens or 0,
            output_tokens=u.output_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        usage.cost_usd = estimate_cost(usage)
        try:
            return StageResult.lenient(json.loads(text)), usage
        except ValueError:
            return extract_result(text), usage


def estimate_cost(u: Usage) -> float | None:
    price = PRICES.get(MODEL_ALIASES.get(u.model, u.model))
    if not price:
        return None
    pin, pout = price
    return (
        u.input_tokens * pin
        + u.cache_creation_input_tokens * pin * 1.25
        + u.cache_read_input_tokens * pin * 0.1
        + u.output_tokens * pout
    ) / 1_000_000


def make_runner(provider: str, registry: ProviderRegistry | None = None,
                mcp_registry: dict[str, dict] | None = None) -> Runner:
    """Runner for a provider name. Old stage `runner:` values (claude_cli/api/mock) are accepted."""
    registry = registry or ProviderRegistry()
    from .providers import LEGACY_RUNNER

    spec = registry.get(LEGACY_RUNNER.get(provider, provider))
    if spec.kind == "mock":
        return MockRunner(delay_s=float(os.environ.get("RELAY_MOCK_DELAY", "1.5")))
    ok, reason = spec.availability()
    if not ok:
        raise RunnerError(f"{spec.name}: {reason}", "unavailable")
    if spec.kind == "claude_cli":
        return ClaudeCliRunner(mcp_registry=mcp_registry, name=spec.name)
    if spec.kind == "api":
        return AnthropicApiRunner()
    if spec.kind == "cli":
        return ExternalCliRunner(spec)
    if spec.kind == "openai":
        return OpenAICompatRunner(spec)
    raise RunnerError(f"unknown provider kind: {spec.kind}", "unavailable")
