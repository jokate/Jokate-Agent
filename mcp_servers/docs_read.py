"""docs-read MCP: read Markdown docs by outline and section instead of whole files.

    python mcp_servers/docs_read.py <docs_root>

Tools: list_docs, outline, read_section, search. search returns pointers only;
read the body with read_section so only the needed section enters context.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
MAX_SECTION_CHARS = 12000

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
mcp = FastMCP("docs-read")


def _resolve(doc: str) -> Path:
    path = (root / doc).resolve()
    if root not in path.parents and path != root:
        raise ValueError("doc must be inside the docs root")
    if not path.is_file():
        raise ValueError(f"no such doc: {doc}")
    return path


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, title), skipping fenced code blocks."""
    out, fenced = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and (m := HEADING.match(line)):
            out.append((i, len(m.group(1)), m.group(2)))
    return out


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


@mcp.tool()
def list_docs() -> list[str]:
    """List Markdown docs under the docs root (relative paths)."""
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*.md"))


@mcp.tool()
def outline(doc: str) -> str:
    """Heading tree of one doc with line numbers and section sizes. Read this before read_section."""
    lines = _read(_resolve(doc))
    hs = _headings(lines)
    rows = []
    for n, (i, level, title) in enumerate(hs):
        end = next((j for j, lv, _ in hs[n + 1:] if lv <= level), len(lines))
        rows.append(f"{'  ' * (level - 1)}- {title}  (L{i + 1}, {end - i} lines)")
    return "\n".join(rows) or "(no headings)"


@mcp.tool()
def read_section(doc: str, heading: str, include_subsections: bool = True) -> str:
    """Body of the first section whose title contains `heading` (case-insensitive)."""
    lines = _read(_resolve(doc))
    hs = _headings(lines)
    key = heading.strip().lstrip("#").strip().lower()
    for n, (i, level, title) in enumerate(hs):
        if key in title.lower():
            rest = hs[n + 1:]
            stop = (lambda lv: lv <= level) if include_subsections else (lambda lv: True)
            end = next((j for j, lv, _ in rest if stop(lv)), len(lines))
            text = "\n".join(lines[i:end])
            if len(text) > MAX_SECTION_CHARS:
                text = text[:MAX_SECTION_CHARS] + "\n...(잘림: include_subsections=false 로 좁히거나 하위 헤딩을 지정)"
            return text
    return f"heading not found. outline:\n{outline(doc)}"


@mcp.tool()
def search(query: str, limit: int = 10) -> list[dict]:
    """Find sections mentioning all query words. Returns pointers (doc, heading, line, snippet), not bodies."""
    words = [w.lower() for w in query.split() if w]
    hits = []
    for rel in list_docs():
        lines = _read(root / rel)
        hs = _headings(lines)
        for i, line in enumerate(lines):
            low = line.lower()
            if words and all(w in low for w in words):
                owner = next((t for j, _, t in reversed(hs) if j <= i), "")
                hits.append({"doc": rel, "heading": owner, "line": i + 1, "snippet": line.strip()[:160]})
                if len(hits) >= limit:
                    return hits
    return hits


if __name__ == "__main__":
    mcp.run()
