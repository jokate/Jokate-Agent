"""digest MCP: summarize many documents/large files without pulling them into the stage's context.

    python mcp_servers/digest.py <cache_dir>

Why: a stage that Reads 25 documents one by one keeps every document in its context, and every later
turn re-reads all of it (measured: 14 turns, context 8K -> 75K, 620K tokens re-read for one scout).
Here each file is summarized by its own small, tool-less Haiku call (fresh context per file), the
summaries are cached by content hash (a later stage or run gets them for free), and only the
summaries enter the stage's context.

Env (set per run by the engine): KATAE_WORKDIR, KATAE_RUN_ID, KATAE_STAGE, KATAE_USAGE_DB.
"""

from __future__ import annotations

import glob as globmod
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp.server.fastmcp import FastMCP

PROMPT_VERSION = "v1"  # bump only when the summary prompt changes (keeps the cache valid)
MODEL = os.environ.get("KATAE_DIGEST_MODEL", "haiku")
MAX_FILES = 60
CHUNK_CHARS = 60_000  # one summarizer call per chunk; a very long file becomes several summaries
SUMMARY = """너는 문서 요약기다. 받은 문서 하나를, 다른 AI 가 원문 없이도 작업할 수 있게 한국어로 압축한다.
- 남길 것: 목적/범위, 확정된 결정과 수치, 현재 상태(완료·진행·보류·미결), 다른 문서와의 관계, 모순·미결 사항
- 각 항목 끝에 원문 위치를 (§절번호 또는 제목) 로 붙인다. 추측하지 않는다.
- 마크다운 불릿만, 최대 {limit}자. 머리말·맺음말 없이 바로 시작한다.
{focus}"""

cache_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".digest-cache").resolve()
workdir = Path(os.environ.get("KATAE_WORKDIR") or os.getcwd()).resolve()
extra_dirs = [Path(p).resolve() for p in os.environ.get("KATAE_EXTRA_DIRS", "").split(os.pathsep) if p]
mcp = FastMCP("digest")


def _files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    roots = [workdir, *extra_dirs]
    for p in paths:
        base = Path(p) if Path(p).is_absolute() else workdir / p
        hits = globmod.glob(str(base), recursive=True) if any(c in p for c in "*?[") else [str(base)]
        for h in sorted(hits):
            path = Path(h).resolve()
            if path.is_file() and any(r in path.parents for r in roots) and path not in out:
                out.append(path)
    return out[:MAX_FILES]


def _record(usage: dict, cost: float | None) -> None:
    db, run_id = os.environ.get("KATAE_USAGE_DB"), os.environ.get("KATAE_RUN_ID")
    if not db or not run_id:
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from relay_agent.usage import Usage, UsageStore

        UsageStore(Path(db)).record(run_id, f"{os.environ.get('KATAE_STAGE', '?')}:digest", Usage(
            "claude", MODEL, input_tokens=usage.get("input_tokens", 0),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0), cost_usd=cost))
    except Exception:  # accounting must never break the tool
        pass


def _summarize(text: str, focus: str, limit: int) -> str:
    system = SUMMARY.format(limit=limit, focus=f"- 특히 이 관점을 중심으로: {focus}" if focus else "")
    exe = shutil.which("claude") or "claude"
    # Measured per call: in the project folder with defaults 6.4K in / 1.1K out ($0.018); from a neutral folder
    # (no CLAUDE.md/memory), --safe-mode and no thinking 3.5K in / 0.65K out ($0.0067) for the same summary.
    args = [exe, "-p", "--model", MODEL, "--output-format", "json", "--system-prompt", system, "--tools", "",
            "--strict-mcp-config", "--no-session-persistence", "--safe-mode"]
    proc = subprocess.run(args, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300, cwd=tempfile.gettempdir(), env={**os.environ, "MAX_THINKING_TOKENS": "0"})
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(f"요약 실패: {(proc.stderr or proc.stdout)[-300:]}")
    _record(data.get("usage") or {}, data.get("total_cost_usd"))
    if data.get("is_error"):
        raise RuntimeError(f"요약 실패: {data.get('result') or data.get('subtype')}")
    return str(data.get("result") or "").strip()


def _rel(path: Path) -> str:
    return path.relative_to(workdir).as_posix() if workdir in path.parents else path.as_posix()


def _digest_file(path: Path, focus: str, limit: int) -> str:
    rel = _rel(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    key = hashlib.sha256(f"{PROMPT_VERSION}|{MODEL}|{limit}|{focus}|".encode() + text.encode()).hexdigest()[:32]
    hit = cache_dir / f"{key}.md"
    if hit.exists():
        return f"### {rel} (원문 {len(text):,}자 · 캐시)\n{hit.read_text(encoding='utf-8')}"
    chunks = [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)] or [""]
    parts = [_summarize(f"문서: {rel}" + (f" (조각 {n + 1}/{len(chunks)})" if len(chunks) > 1 else "") + "\n\n" + c,
                        focus, limit) for n, c in enumerate(chunks)]
    body = "\n".join(parts)
    cache_dir.mkdir(parents=True, exist_ok=True)
    hit.write_text(body, encoding="utf-8")
    return f"### {rel} (원문 {len(text):,}자)\n{body}"


@mcp.tool()
def digest(paths: list[str], focus: str = "", max_chars_each: int = 1200) -> str:
    """Summaries of many documents/large files, one fresh cheap call per file, cached by content.
    Use this instead of Reading several docs or big files one by one: only the summaries enter your
    context. `paths` are paths or globs relative to the working directory (e.g. ["Docs/**/*.md"]).
    `focus` narrows what to keep. Then Read only the exact section you still need (summaries cite §)."""
    files = _files(paths)
    if not files:
        return "일치하는 파일이 없습니다: " + ", ".join(paths)
    limit = max(300, min(int(max_chars_each), 3000))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda f: _safe(f, focus, limit), files))
    total = sum(f.stat().st_size for f in files)
    head = f"요약 {len(files)}개 파일 (원문 약 {total:,}바이트 → 요약 {sum(len(r) for r in results):,}자)"
    return head + "\n\n" + "\n\n".join(results)


def _safe(path: Path, focus: str, limit: int) -> str:
    try:
        return _digest_file(path, focus, limit)
    except Exception as e:  # one bad file must not lose the others
        return f"### {_rel(path)}\n(요약 실패: {str(e)[:200]} — 필요하면 직접 Read)"


if __name__ == "__main__":
    mcp.run()
