"""Keep the server up: run `relay serve`, and run it again whenever it exits (a crash, or a restart request).

    python -m relay_agent.supervise [port]      # what `relay autostart on` starts at Windows logon

Standard library only, on purpose: this process lives as long as the machine is logged in, and must not hold
package files open, so `uv sync` can replace them. Each start goes through `uv run`, which syncs packages
first — a restart after `git pull` therefore runs the new code with its new dependencies.
Stop: close its window (Ctrl+C), or create runs/supervise.stop and restart the server.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STOP_FILE = ROOT / "runs" / "supervise.stop"
QUICK_EXIT_S = 30  # a server that dies this soon after starting counts as failing (backoff grows)
MAX_BACKOFF_S = 60


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def serve_command(port: int) -> list[str]:
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "relay", "serve", "--port", str(port)]
    return [sys.executable, "-m", "relay_agent.cli", "serve", "--port", str(port)]


def backoff(failures: int) -> int:
    return min(MAX_BACKOFF_S, 2 ** failures) if failures else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    port = int(argv[0]) if argv else 8020
    if port_busy(port):
        print(f"[supervise] port {port} is already serving — nothing to do")
        return 0
    STOP_FILE.unlink(missing_ok=True)
    env = {**os.environ, "KATAE_SUPERVISED": "1"}
    failures = 0
    try:
        while True:
            started = time.monotonic()
            print(f"[supervise] starting server on port {port}", flush=True)
            code = subprocess.call(serve_command(port), cwd=ROOT, env=env)
            if STOP_FILE.exists():
                STOP_FILE.unlink(missing_ok=True)
                print("[supervise] stop requested", flush=True)
                return 0
            failures = failures + 1 if time.monotonic() - started < QUICK_EXIT_S else 0
            wait = backoff(failures)
            print(f"[supervise] server exited ({code}); starting again in {wait}s", flush=True)
            time.sleep(wait)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
