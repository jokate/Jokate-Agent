"""H-6 (2): the '결재 기준' section of a hire's GUIDE.md. `employees.approval_problems` is what
`drafts.hire_problems` ends with, so `employee_draft` tells HR and `employee_submit` refuses a hire whose section
is missing or is the empty frame of a part template. No AI is called."""
from pathlib import Path

import pytest

from relay_agent.studio import employees
from test_studio_mcp import DRAFT, YAML, desk, failed, hr_turn  # noqa: F401 - `desk` is a fixture
from test_studio_api_tasks import world  # noqa: F401 - the fixture `desk` stands on

PARTS = Path(employees.__file__).parent / "parts"
HEAD = "# 지침\n\n- 맡은 일: 코드\n\n"
ASK = "- 묻고 한다 (`ask_owner` 로 올린다):"
FIXED = "- 알아서 한 판단은 검수를 올릴 때 적는다.\n"


def codes(guide):
    return [problem["code"] for problem in employees.approval_problems(guide)]


def test_a_guide_without_the_section_is_refused_with_its_own_code():
    assert codes("# 지침\n\n- 맡은 일: 코드\n") == ["approval_section_missing"]
    assert codes("") == ["approval_section_missing"]
    assert codes("# 지침\n\n결재 기준은 따로 없다\n") == ["approval_section_missing"]  # not a heading


@pytest.mark.parametrize("name", ["program.md", "art.md", "design_doc.md", "design_setup.md"])
def test_the_empty_frame_of_each_part_template_is_refused(name):
    text = (PARTS / name).read_text(encoding="utf-8")
    assert "## 결재 기준" in text, name
    assert codes(text) == ["approval_section_empty"], name
    [one] = employees.approval_problems(text)
    assert "묻고 한다" in one["message"] and "알아서 한다" in one["message"]


def test_one_label_left_empty_is_named():
    guide = f"{HEAD}## 결재 기준\n\n{ASK} 구조 변경\n- 알아서 한다:\n{FIXED}"
    [one] = employees.approval_problems(guide)
    assert one["code"] == "approval_section_empty" and "알아서 한다" in one["message"] and "묻고 한다" not in one["message"]
    guide = f"{HEAD}## 결재 기준\n\n{ASK}\n- 알아서 한다: 이름\n"
    [one] = employees.approval_problems(guide)
    assert "묻고 한다" in one["message"] and "알아서 한다" not in one["message"]


def test_a_filled_section_passes_in_each_form():
    on_the_line = f"{HEAD}## 결재 기준\n\n{ASK} 구조 변경\n- 알아서 한다: 없음\n{FIXED}"
    below = f"{HEAD}## 결재 기준\n\n{ASK}\n  - 구조를 바꾸는 선택\n  - 예산을 넘는 일\n- 알아서 한다:\n  - 이름\n"
    free = f"{HEAD}## 결재 기준\n\n구조를 바꾸기 전에는 묻는다. 이름과 주석은 알아서 한다.\n"
    colon_in_parens = f"{HEAD}## 결재 기준\n\n- 묻고 한다 (ask_owner: 무엇을): 구조 변경\n- 알아서 한다: 없음\n"
    for guide in (on_the_line, below, free, colon_in_parens):
        assert employees.approval_problems(guide) == [], guide


def test_only_the_section_counts_and_it_ends_at_the_next_heading():
    guide = f"{HEAD}## 결재 기준\n\n{ASK}\n- 알아서 한다:\n\n## 인계\n\n- 내용은 여기에 있다\n"
    assert codes(guide) == ["approval_section_empty"]  # what is under the next heading is not this section's
    only_fixed = f"{HEAD}## 결재 기준\n\n{FIXED}"
    assert codes(only_fixed) == ["approval_section_empty"]


def test_employee_draft_tells_and_employee_submit_refuses_an_empty_section(desk):  # noqa: F811
    hr, token = hr_turn(desk)
    empty = (PARTS / "program.md").read_text(encoding="utf-8")
    out = desk.call("employee_draft", token, **{**DRAFT, "guide_md": empty})
    assert [p["code"] for p in out["problems"]] == ["approval_section_empty"]
    assert failed(desk.call("employee_submit", token, draft_id=out["draft_id"], reason="사람이 모자라다"),
                  "precondition_failed")["failed"] == ["approval_section_empty"]
    assert not desk.store.list_inbox(kind="hire")

    out = desk.call("employee_draft", token, **{**DRAFT, "guide_md": "# 지침\n"})
    assert [p["code"] for p in out["problems"]] == ["approval_section_missing"]
    assert failed(desk.call("employee_submit", token, draft_id=out["draft_id"], reason="r"),
                  "precondition_failed")["failed"] == ["approval_section_missing"]

    fixed = desk.call("employee_draft", token, draft_id=out["draft_id"], guide_md=DRAFT["guide_md"])
    assert fixed["problems"] == []  # the draft kept the rest; only the guide was replaced
    sent = desk.call("employee_submit", token, draft_id=out["draft_id"], reason="사람이 모자라다")
    assert desk.store.get_inbox_item(sent["inbox_id"])["kind"] == "hire" and YAML


# --- an amendment of a GUIDE.md (H-6 follow-up): the same section, except for HR's own guide ---------------------
def amend(desk, token, target, **files):  # noqa: F811
    return desk.call("employee_draft", token, target_employee_id=target, **files)


def test_a_guide_amendment_that_drops_or_empties_the_section_is_refused(desk):  # noqa: F811
    hr, token = hr_turn(desk)
    target = desk.w.emp
    gone = amend(desk, token, target, file="GUIDE.md", guide_md="# 지침\n\n- 맡은 일: 코드\n")
    assert gone["action"] == "guide_amend" and [p["code"] for p in gone["problems"]] == ["approval_section_missing"]
    assert failed(desk.call("employee_submit", token, draft_id=gone["draft_id"], reason="r"),
                  "precondition_failed")["failed"] == ["approval_section_missing"]
    empty = amend(desk, token, target, file="GUIDE.md", guide_md=(PARTS / "program.md").read_text(encoding="utf-8"))
    assert [p["code"] for p in empty["problems"]] == ["approval_section_empty"]
    assert failed(desk.call("employee_submit", token, draft_id=empty["draft_id"], reason="r"),
                  "precondition_failed")["failed"] == ["approval_section_empty"]
    assert not desk.store.list_inbox(kind="hire")

    fixed = amend(desk, token, target, draft_id=gone["draft_id"], file="GUIDE.md", guide_md=DRAFT["guide_md"])
    assert fixed["problems"] == []
    sent = desk.call("employee_submit", token, draft_id=gone["draft_id"], reason="묻고 해야 한다")
    assert desk.store.get_inbox_item(sent["inbox_id"])["payload"]["action"] == "guide_amend"


def test_the_guide_of_hr_itself_and_other_files_are_not_checked(desk):  # noqa: F811
    hr, token = hr_turn(desk)
    own = amend(desk, token, hr, file="GUIDE.md", guide_md="# 지침\n\n- 맡은 일: 채용\n")
    assert own["problems"] == []  # HR's guide holds the empty frame for new employees, not a section of its own
    persona = amend(desk, token, desk.w.emp, file="PERSONA.md", persona_md="# 다른 사람\n")
    assert persona["problems"] == []
