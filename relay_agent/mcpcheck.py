"""Is an MCP server up before a stage spends money on it?

A stage that needs the Unreal editor's MCP, started while the editor (or the server) is down, burns its turns
discovering that. One cheap check per run start instead:
- http / sse servers: a TCP connection to the URL's host and port (no request is sent, so no auth is needed);
- stdio servers: start the command and send the MCP `initialize` request; the server must answer in time.

Limit: a stdio bridge that answers `initialize` by itself and only reaches the editor on a tool call
(some Unreal MCP bridges work this way) passes even with the editor closed.
Standard library only.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import threading
from urllib.parse import urlparse

TIMEOUT_S = 20.0
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "katae-mcp-check", "version": "1"}}}


def probe(config: dict, timeout_s: float = TIMEOUT_S) -> str | None:
    """None when the server answers; otherwise a short reason (Korean, for the run error)."""
    kind = (config.get("type") or ("http" if config.get("url") else "stdio")).lower()
    if kind in ("http", "sse", "streamable-http"):
        return _probe_url(str(config.get("url") or ""), timeout_s)
    return _probe_stdio(config, timeout_s)


def _probe_url(url: str, timeout_s: float) -> str | None:
    parsed = urlparse(url)
    if not parsed.hostname:
        return f"URL 이 올바르지 않음: {url!r}"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=min(timeout_s, 5.0)):
            return None
    except OSError as e:
        return f"{parsed.hostname}:{port} 에 연결할 수 없음 ({e.__class__.__name__}) — 서버(에디터)가 켜져 있는지 확인"


def _probe_stdio(config: dict, timeout_s: float) -> str | None:
    command = config.get("command")
    if not command:
        return "command 가 없는 설정"
    exe = shutil.which(command) or command  # npx -> npx.cmd on Windows
    env = {**os.environ, **{k: str(v) for k, v in (config.get("env") or {}).items()}}
    try:
        proc = subprocess.Popen([exe, *[str(a) for a in config.get("args") or []]], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=config.get("cwd") or None)
    except OSError as e:
        return f"실행할 수 없음: {command} ({e.__class__.__name__}: {e})"
    answers: queue.Queue = queue.Queue()

    def read() -> None:
        for raw in proc.stdout:  # newline-delimited JSON-RPC; servers may log other lines first
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == 1:
                answers.put(msg)
                return
        answers.put(None)  # stdout closed: the process exited

    err: list[bytes] = []
    threading.Thread(target=read, daemon=True).start()
    # drain stderr too: a chatty server would otherwise block on a full pipe before it answers
    threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True).start()
    try:
        proc.stdin.write((json.dumps(INITIALIZE) + "\n").encode())
        proc.stdin.flush()
    except OSError:
        pass  # exited already: the reader reports it
    try:
        msg = answers.get(timeout=timeout_s)
    except queue.Empty:
        msg = "timeout"
    finally:
        _stop(proc)
    if isinstance(msg, dict):
        return None if "result" in msg else f"initialize 오류: {str(msg.get('error'))[:200]}"
    if msg == "timeout":
        return f"{timeout_s:.0f}초 안에 응답 없음 ({command})"
    tail = b"".join(err).decode("utf-8", "replace").strip()[-300:]
    return f"시작 직후 종료됨 ({command})" + (f": {tail}" if tail else "")


def _stop(proc: subprocess.Popen) -> None:
    try:
        proc.stdin.close()
    except OSError:
        pass
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
