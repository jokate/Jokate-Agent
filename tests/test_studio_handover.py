"""The format of a handoff between parts (H-5): the pure check of studio/handover.py, and that the places that
tell the employees about it — the tool's description, the part templates, ARCHITECTURE §8.4 — say the same."""
from pathlib import Path

import pytest

from relay_agent.studio import handover
from relay_agent.studio.employees import part_defaults

ROOT = Path(__file__).resolve().parent.parent
ROW = {"item": "a", "value": "1", "done": "x"}
GOOD = [
    {"format": "spec_table", "rows": [ROW]},
    {"format": "spec_table", "source": "Docs/a.md", "rows": [{**ROW, "condition": "", "exception": "e", "note": "n"}]},
    {"format": "data_items", "items": [{"asset": "a", "field": "b", "type": "c", "default": "0", "note": ""}]},
]
BAD = [  # (value, the fields it names)
    (None, ["handoff"]), ([], ["handoff"]), ("spec_table", ["handoff"]),
    ({}, ["format"]), ({"format": 1}, ["format"]), ({"format": "nope", "rows": [ROW]}, ["format"]),
    ({"format": "spec_table"}, ["rows"]), ({"format": "spec_table", "rows": {}}, ["rows"]),
    ({"format": "spec_table", "rows": []}, ["rows"]),
    ({"format": "spec_table", "rows": ["x"]}, ["rows[0]"]),
    ({"format": "spec_table", "rows": [{}]}, ["rows[0].item", "rows[0].value", "rows[0].done"]),
    ({"format": "spec_table", "rows": [{**ROW, "done": ""}]}, ["rows[0].done"]),
    ({"format": "spec_table", "rows": [{**ROW, "value": 10}]}, ["rows[0].value"]),
    ({"format": "spec_table", "rows": [{**ROW, "note": 1}]}, ["rows[0].note"]),
    ({"format": "spec_table", "rows": [ROW, {**ROW, "item": " "}]}, ["rows[1].item"]),
    ({"format": "spec_table", "rows": [ROW], "extra": 1}, ["handoff"]),
    ({"format": "spec_table", "rows": [ROW], "source": ""}, ["source"]),
    ({"format": "data_items", "rows": [ROW]}, ["handoff", "items"]),
    ({"format": "data_items", "items": [{"asset": "a", "field": "b"}]}, ["items[0].type"]),
]


@pytest.mark.parametrize("value", GOOD)
def test_a_handoff_of_a_format_has_no_problem(value):
    assert handover.problems(value) == []


@pytest.mark.parametrize("value, fields", BAD)
def test_a_handoff_that_is_no_format_says_what_is_wrong(value, fields):
    found = handover.problems(value)
    assert [field for field, _ in found] == fields and all(why for _, why in found)


def test_the_hint_names_the_format_and_its_required_columns():
    assert all(name in handover.hint() for name in handover.FORMATS)
    one = handover.hint("data_items")
    assert "data_items" in one and "spec_table" not in one and all(c in one for c in ("asset", "field", "type"))


@pytest.mark.parametrize("name", [["spec_table"], {}, None, 1, "nope"])
def test_the_hint_of_what_is_no_format_name_gives_both_formats_without_failing(name):
    text = handover.hint(name)
    assert all(known in text for known in handover.FORMATS)


def names(*formats):
    return [word for name in formats for word in (name, handover.FORMATS[name]["table"],
                                                  *handover.FORMATS[name]["required"])]


def test_the_tool_description_and_the_templates_say_the_formats():
    text = (ROOT / "mcp_servers" / "studio.py").read_text(encoding="utf-8")
    room_post = text[text.index("def room_post"):text.index("def employee_list")]
    for word in names(*handover.FORMATS) + ["invalid_format", "task_request_review"]:
        assert word in room_post, word
    guides = {part: values["guide_template"] for part, values in part_defaults().items()}
    for word in names("spec_table"):
        assert word in guides["design_doc"] and word in guides["program"], word
    for word in names("data_items"):
        assert word in guides["design_setup"] and word in guides["program"], word
    assert all("## 인계" in guides[part] for part in ("design_doc", "program", "design_setup"))


def test_architecture_names_the_formats_and_no_longer_calls_them_provisional():
    text = (ROOT / "design" / "studio" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert all(name in text[text.index("### 8.4"):] for name in handover.FORMATS)
    assert "형식 이름과 검사는 H-5 가 정하고" not in text
