"""The format of a handoff between parts (H-5; design/studio/ARCHITECTURE.md §8.4 `room_post`, §7.6, §8.3).

A `handoff` of `room_post` is `{format, <table>, source?}`. Two formats exist: `spec_table` (planning -> program:
a table of specifications) and `data_items` (program -> planning (setting): the data items that are exposed).
What goes to review is not here — that is the package of `task_request_review` (S-12).

`problems(value)` says what is wrong, `[]` if nothing; `hint(name)` is the one-line form and its required
columns, which the refusal and the part templates quote. Pure: no store, no tool. The server does not look at the
sender's part or the kind of the room — the direction is in the definition and in the templates only. Nothing is
corrected: a handoff that passes is stored as it came (`meta.handoff`).

The columns of `spec_table` follow the specification table of game-harness's game-design-doc as far as it could be
read: the original was not compared, so the columns are all kept here, in FORMATS, and nowhere else.
"""

from __future__ import annotations

from typing import Any

FORMATS: dict[str, dict[str, Any]] = {
    "spec_table": {
        "direction": "기획(문서) → 프로그램",
        "table": "rows",
        "required": ("item", "value", "done"),
        "optional": ("condition", "exception", "note"),
        "meaning": "사양 표 — 행마다 항목(item) · 값(value) · 완료 기준(done), 선택으로 조건 · 예외 · 비고",
    },
    "data_items": {
        "direction": "프로그램 → 기획(세팅)",
        "table": "items",
        "required": ("asset", "field", "type"),
        "optional": ("default", "note"),
        "meaning": "노출된 데이터 항목 목록 — 행마다 애셋(asset) · 칸(field) · 타입(type), 선택으로 기본값 · 비고",
    },
}
TOP_OPTIONAL = ("source",)  # the Docs/ path the table came from
Problem = tuple[str, str]   # (field, reason); field is what `fields[]` of the refusal carries


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def hint(name: str | None = None) -> str:
    """`spec_table` = {format, rows[{item, value, done}], source?} · `data_items` = … — one line."""
    names = [name] if isinstance(name, str) and name in FORMATS else list(FORMATS)  # a list or dict is no name
    parts = []
    for n in names:
        spec = FORMATS[n]
        parts.append(f"{n}({spec['direction']}) = {{format: \"{n}\", {spec['table']}: [{{{', '.join(spec['required'])}}}"
                     f"+선택 {', '.join(spec['optional'])}], source?}}")
    return "; ".join(parts)


def problems(value: Any) -> list[Problem]:
    """Every reason `value` is not a handoff of one of the formats; [] when it is."""
    if not isinstance(value, dict):
        return [("handoff", "handoff 는 {format, 표} 객체여야 한다")]
    name = value.get("format")
    if not isinstance(name, str) or name not in FORMATS:
        shown = f"모르는 형식 이름 {name!r}" if isinstance(name, str) else "format 이 없거나 문자열이 아니다"
        return [("format", f"{shown} — 형식은 {', '.join(FORMATS)} 중 하나다")]
    spec = FORMATS[name]
    table, required, optional = spec["table"], spec["required"], spec["optional"]
    found: list[Problem] = []
    unknown = sorted(str(k) for k in value if k not in ("format", table, *TOP_OPTIONAL))
    if unknown:
        found.append(("handoff", f"모르는 칸: {', '.join(unknown)} — {name} 의 최상위 칸은 format · {table} · source 다"))
    if "source" in value and _blank(value["source"]):
        found.append(("source", "source 는 비어 있지 않은 글(Docs/ 경로)이어야 한다"))
    rows = value.get(table)
    if table not in value:
        found.append((table, f"{name} 에는 표 {table} 가 있어야 한다"))
        return found
    if not isinstance(rows, list):
        found.append((table, f"{table} 는 행의 목록이어야 한다"))
        return found
    if not rows:
        found.append((table, f"{table} 가 비어 있다 — 행이 하나 이상 있어야 한다"))
        return found
    for i, row in enumerate(rows):
        at = f"{table}[{i}]"
        if not isinstance(row, dict):
            found.append((at, f"{at} 는 {{{', '.join(required)}}} 객체여야 한다"))
            continue
        for key in required:
            if key not in row:
                found.append((f"{at}.{key}", f"{at} 에 필수 칸 {key} 가 없다"))
            elif _blank(row[key]):
                found.append((f"{at}.{key}", f"{at}.{key} 가 비어 있다 — 비어 있지 않은 글이어야 한다"))
        for key in optional:
            if key in row and not isinstance(row[key], str):
                found.append((f"{at}.{key}", f"{at}.{key} 는 글이어야 한다"))
        extra = sorted(str(k) for k in row if k not in required and k not in optional)
        if extra:
            found.append((at, f"{at} 에 모르는 칸: {', '.join(extra)} — 칸은 {', '.join(required + optional)} 뿐이다"))
    return found
