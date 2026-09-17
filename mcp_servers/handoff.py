"""handoff MCP: save/load HANDOFF.md in a fixed format, for any client (Claude Code included).

    python mcp_servers/handoff.py <runs_dir>

Use at session end (save_handoff) and session start (load_handoff) instead of
carrying a long conversation forward.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from relay_agent.baton import Baton, Decision, Pointer  # noqa: E402

runs_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "runs").resolve()
handoffs_dir = runs_dir / "handoffs"
mcp = FastMCP("handoff")


def _safe(name: str) -> str:
    if not re.fullmatch(r"[\w\-. ]{1,80}", name):
        raise ValueError("name: letters, digits, - _ . space only")
    return name


@mcp.tool()
def save_handoff(
    name: str,
    goal: str,
    state: str,
    decisions: list[Decision] | None = None,
    open_issues: list[str] | None = None,
    next_steps: list[str] | None = None,
    pointers: list[Pointer] | None = None,
) -> str:
    """Write handoffs/<name>.md (and .json). Pointers are path + heading/symbol/line range, not file contents."""
    baton = Baton(
        goal=goal, state=state, decisions=decisions or [], open_issues=open_issues or [],
        next_steps=next_steps or [], pointers=pointers or [],
    )
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    base = handoffs_dir / _safe(name)
    base.with_suffix(".json").write_text(baton.model_dump_json(indent=2), encoding="utf-8")
    base.with_suffix(".md").write_text(baton.to_markdown(), encoding="utf-8")
    return str(base.with_suffix(".md"))


@mcp.tool()
def load_handoff(name: str) -> str:
    """Read a saved handoff, or a relay run's HANDOFF.md when `name` is a run id."""
    for path in (handoffs_dir / f"{_safe(name)}.md", runs_dir / _safe(name) / "HANDOFF.md"):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return f"not found: {name}. available: {list_handoffs()}"


@mcp.tool()
def list_handoffs() -> dict[str, list[str]]:
    """Saved handoff names and relay run ids, newest last."""
    return {
        "handoffs": sorted(p.stem for p in handoffs_dir.glob("*.md")) if handoffs_dir.exists() else [],
        "runs": sorted(p.parent.name for p in runs_dir.glob("*/HANDOFF.md")),
    }


if __name__ == "__main__":
    mcp.run()
