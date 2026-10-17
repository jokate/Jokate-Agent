"""The limits the owner may change (V-4 — design/studio/ARCHITECTURE.md §4.6 · §6.4 `GET`·`PATCH /studio/machine/limits`).

Four settings of `Config` — no new ones: the task runs at work at once, the turns employees may put in for each
other in one task, the turns the members of a group room may put in after one message, and the most rounds of a
discussion in a group room. A change is written into
`relay.config.local.yaml` (the file `Config.load` reads, this machine only) and then into the running `Config`, which
the queue and the exchange checks read at every use — so it holds at once and after a restart. A write that fails
changes nothing. The comments of the file stay when each changed key is a one-line top-level key (`_patch_text`);
in any other shape the file is written anew and its comments go."""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import yaml

from ..config import ROOT

LOCAL_CONFIG = ROOT / "relay.config.local.yaml"  # a test gives its own (monkeypatch)

# the name in the answer -> the Config field, and the range each value must lie in (both ends included)
FIELDS = {"max_runs": "studio_max_runs", "max_exchanges": "studio_max_exchanges",
          "group_max_exchanges": "studio_group_max_exchanges", "group_rounds": "studio_group_rounds"}
RANGES = {"max_runs": (1, 20), "max_exchanges": (1, 200), "group_max_exchanges": (1, 100), "group_rounds": (1, 10)}

_lock = threading.Lock()


def read(cfg: Any) -> dict:
    """`{max_runs, max_exchanges, group_max_exchanges, group_rounds}` as the running config holds them."""
    return {name: getattr(cfg, field) for name, field in FIELDS.items()}


def check(changes: dict) -> dict:
    """The changes that are whole numbers in their range; ValueError for anything else (a bool is no number)."""
    clean = {}
    for name, value in changes.items():
        if name not in FIELDS:
            raise ValueError(f"바꿀 수 없는 한도다: {name}")
        low, high = RANGES[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{name} 는 {low}-{high} 의 정수다: {value!r}")
        clean[name] = value
    if not clean:
        raise ValueError("바꿀 한도가 없다")
    return clean


def _patch_text(text: str, updates: dict) -> str:
    """`text` with the line of each key in `updates` (Config field -> int) replaced by `key: value` — a trailing `# …`
    comment of that line stays — and a key that is not there added at the end. ValueError when a key stands on more
    than one line."""
    for key, value in updates.items():
        line = re.compile(rf"^{re.escape(key)}[ \t]*:[^\n#]*(#[^\n]*)?$", re.MULTILINE)
        found = line.findall(text)
        if len(found) > 1:
            raise ValueError(f"{key} 가 여러 줄에 있다")
        if found:
            text = line.sub(lambda m: f"{key}: {value}" + (f"  {m.group(1)}" if m.group(1) else ""), text, count=1)
        else:
            text += ("" if not text or text.endswith("\n") else "\n") + f"{key}: {value}\n"
    return text


def save(cfg: Any, changes: dict, path: Path | None = None) -> dict:
    """Write the changes into the local settings file and then into `cfg`. The other keys stay, and so do the comments
    when each changed key is a one-line top-level key; in any other shape the file is written anew (no comments). The
    limits as they are afterwards. ValueError for a value `check` refuses; OSError when the file cannot be written,
    with `cfg` as it was."""
    clean = check(changes)
    path = path or LOCAL_CONFIG
    header = "# This machine only (git-ignored).\n"
    with _lock:
        text = path.read_text(encoding="utf-8") if path.exists() else None
        data = (yaml.safe_load(text) if text is not None else None) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path.name} 의 모양이 틀려 한도를 적지 않았다")
        updates = {FIELDS[name]: value for name, value in clean.items()}
        expected = {**data, **updates}
        new = None
        if text is not None and text.strip():
            try:
                new = _patch_text(text, updates)
                if yaml.safe_load(new) != expected:
                    new = None
            except (ValueError, yaml.YAMLError):
                new = None
        if new is None:
            new = header + yaml.safe_dump(expected, allow_unicode=True, sort_keys=False)
        path.write_text(new, encoding="utf-8")
        for name, value in clean.items():
            setattr(cfg, FIELDS[name], value)
    return read(cfg)
