"""Where the harness is, and the '하네스' section of an employee's prompt (ARCHITECTURE §9.1 · §12.5).

Not `harness.locate`: that one needs `bootstrap.bat` and `index_build.bat` in the folder, raises `ValueError`
and serves the job that sets a new project up (`Config.studio_harness_root`, harness.py). This module has no
dependency on the importer · creator · repos, never raises and answers a `Path` or `None` — the prompt of a
turn is written with or without the harness, as it was before.

The root is, in this order: the setting `studio_harness_root` (when it is not empty: that folder or nothing),
else what the junction `~/.claude/skills/game-onboard` points at, two levels up (`<root>/skills/game-onboard`),
which must hold a `skills/` folder. No path is written in code except that link's name; `home` is injected.
"""
from __future__ import annotations

from pathlib import Path

import yaml

SECTION_TITLE = "하네스"  # the title of the section (employees.HARNESS_TITLE is the same text)
SECTION_LIMIT = 2400  # bytes of UTF-8: the principle and all seven skills of today's harness fit (about 2,000)
LINK = (".claude", "skills", "game-onboard")  # under home: a junction into the harness
PRINCIPLE_CHARS = 170  # the README's first paragraph, as one line
WHEN_CHARS = 90  # a skill's description: its first sentence, at most this many characters
# Measured on this PC's harness (2026-10-17): with whole description lines only 2 of 7 skills fitted in 1,500 bytes.


def default_home() -> Path:
    """The user's home. A function, so that a test can put another one in its place."""
    return Path.home()


def resolve_root(setting: str | None, home: Path | str | None = None) -> Path | None:
    """The harness folder, or None. A set `setting` counts alone: it is not looked for anywhere else."""
    try:
        if (setting or "").strip():
            root = Path(setting.strip())
            return root if root.is_dir() else None
        link = Path(home if home is not None else default_home()).joinpath(*LINK)
        if not link.exists():
            return None
        real = link.resolve()
        if real == link.parent.resolve() / link.name:
            return None  # a copy, not a link: two levels up is `~/.claude`, which has a skills/ as well
        root = real.parent.parent
        return root if (root / "skills").is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _front(text: str) -> dict:
    """The `---` block at the head of a file as a dict; {} when there is none or it does not parse."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:end]))
            except yaml.YAMLError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def _short(text: str, limit: int) -> str:
    """`text` without markdown emphasis, at most `limit` characters (cut ones end in '…')."""
    text = " ".join(text.replace("**", "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _first_sentence(text: str) -> str:
    """Up to the first '다.' or '. ' (a Korean or an English sentence end), else the whole text."""
    ends = [i + 2 for i in (text.find("다."), text.find(". ")) if i >= 0]
    return text[:min(ends)].strip() if ends else text.strip()


def _principle(root: Path) -> list[str]:
    """README.md's first paragraph of prose (no front matter, title, rule or code), joined into one line — the
    README wraps its lines — and shortened to PRINCIPLE_CHARS."""
    try:
        lines = (root / "README.md").read_text(encoding="utf-8", errors="replace").lstrip("﻿").splitlines()
    except OSError:
        return []
    if lines and lines[0].strip() == "---":
        ends = [n for n, line in enumerate(lines[1:], 1) if line.strip() == "---"]
        lines = lines[ends[0] + 1:] if ends else lines
    paragraph: list[str] = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith(("#", "---", "```", "- ", "|")):
            if paragraph:
                break
            continue
        paragraph.append(text)
    return [_short(" ".join(paragraph), PRINCIPLE_CHARS)] if paragraph else []


def _skills(root: Path) -> list[str]:
    """`- name — when` for each skills/*/SKILL.md, by folder name: `when` is the first sentence of the
    description's first line, shortened to WHEN_CHARS. A skill whose head does not parse is left out."""
    out = []
    try:
        folders = sorted(p for p in (root / "skills").iterdir() if p.is_dir())
    except OSError:
        return out
    for folder in folders:
        try:
            head = _front((folder / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not head:
            continue
        name = str(head.get("name") or folder.name).strip()
        first = next((line.strip() for line in str(head.get("description") or "").splitlines() if line.strip()), "")
        when = _short(_first_sentence(first), WHEN_CHARS)
        out.append(f"- {name} — {when}" if when else f"- {name}")
    return out


def section(root: Path | None, limit: int = SECTION_LIMIT) -> str:
    """The body of the section: the principle, then a line per skill, from the front, whole lines up to `limit`
    bytes of UTF-8 (the head matters more than the tail — the opposite of `employees.clip_memory`). A first line
    longer than `limit` is cut at a byte, between characters. "" when there is no root or nothing to say."""
    if root is None:
        return ""
    lines = _principle(root) + _skills(root)
    out, size = [], 0
    for line in lines:
        cost = len(line.encode("utf-8")) + (1 if out else 0)
        if size + cost > limit:
            if not out:
                out.append(line.encode("utf-8")[:limit].decode("utf-8", errors="ignore"))
            break
        out.append(line)
        size += cost
    return "\n".join(out)


def section_for(setting: str | None, home: Path | str | None = None) -> str:
    return section(resolve_root(setting, home))
