"""T4: the structure-document order of the program part — the goal of a program employee's task run names it (no other
part's does), the `structure_choice` item carries the document as `attachments` `{id, name, size}`, and the program
employees already hired get the section in their GUIDE.md once. No run and no AI is started."""
import pytest

from relay_agent.studio import api, employee_git, employees, work
from relay_agent.studio.tasks import OWNER
from test_studio_harness_guides import GUIDE_NO_LINES, autos, hire, read
from test_studio_harness_guides import studio  # noqa: F401 - `studio` is its fixture
from test_studio_inbox import TEXT
from test_studio_tools import OPTIONS, desk, failed  # noqa: F401 - `desk` is that file's fixture
from test_studio_work import shop  # noqa: F401 - the fixture of the task-run tests
from test_studio_runs import lab  # noqa: F401 - `shop` stands on it
from test_studio_api_tasks import world  # noqa: F401 - `desk` stands on it

MARK = employees.STRUCTURE_DOC_MARK
AUTO = employees.STRUCTURE_DOC_AUTO_MESSAGE
HEAD = employees.STRUCTURE_DOC_HEAD


def program_original():
    return employees.part_defaults()["program"]["guide_template"]


def goal_of(shop, emp, type_):  # noqa: A002
    row = shop.m.create_task(OWNER, "backlog", shop.pid, assignee_id=emp, **{**TEXT, "type": type_}).task
    shop.m.request_start(row["id"], OWNER)
    return shop.start(shop.store.get_task(row["id"]))[1].baton.goal


# --- (a) · (b) the goal of a task run ---------------------------------------------------------------------------
def test_a_program_features_goal_names_the_structure_document_order_and_other_tasks_do_not(shop):
    emp = shop.hire()
    goal = goal_of(shop, emp, "feature")
    assert work.STRUCTURE_DOC_FEATURE in goal and "「구조 문서 결재」" in goal
    assert goal.index(work.STRUCTURE_DOC_FEATURE) < goal.index("===== 명세")  # before the spec: the first thing read
    assert work.STRUCTURE_DOC_CHANGE in goal_of(shop, emp, "fix") and work.STRUCTURE_DOC_CHANGE in goal_of(shop, emp, "chore")
    for type_ in ("doc", "data"):
        assert "구조 문서 결재" not in goal_of(shop, emp, type_)


def test_the_goal_of_another_part_has_no_structure_document_order(shop):
    emp = shop.hire()
    row = shop.m.create_task(OWNER, "backlog", shop.pid, assignee_id=emp, **TEXT).task
    program = shop.store.get_employee(emp)
    assert work.STRUCTURE_DOC_FEATURE in shop.fx._goal(shop.store, row, program)
    for part in ("design_setup", "design_doc", "art", "hr"):
        for type_ in ("feature", "fix", "chore"):
            goal = shop.fx._goal(shop.store, {**row, "type": type_}, {**program, "department_id": part})
            assert "구조 문서 결재" not in goal, (part, type_)


# --- (e) the template ---------------------------------------------------------------------------------------------
def test_the_program_template_has_the_section_and_no_other_part_does():
    original = program_original()
    block = employees.structure_doc_block(original)
    assert block.startswith(HEAD) and "SVG" in block and "대안" in block and "structure_choice" not in block.split("\n")[0]
    assert "attachments" in block and "choose" in block and "revise" in block
    assert original.index(HEAD) < original.index("## 인계") < original.index("## 결재 기준")
    for part, values in employees.part_defaults().items():
        if part != "program":
            assert MARK not in values["guide_template"], part
    assert employees.structure_doc_block("## 다른 절\n") == ""


# --- (c) the item carries the document -----------------------------------------------------------------------------
def post_doc(desk, token, tmp_path, monkeypatch, name="structure.html"):
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    (work_dir / name).write_text("<html><body>구조</body></html>", encoding="utf-8")
    monkeypatch.setattr(api, "_workdir", lambda run_id: work_dir)
    out = desk.call("room_post", token, text="구조 문서", attachments=[{"path": name}])
    assert out["ok"]
    return out


def test_ask_owner_files_the_structure_document_as_attachments_id_name_size(desk, tmp_path, monkeypatch):
    task = desk.working()
    token = desk.token(task)
    posted = post_doc(desk, token, tmp_path, monkeypatch)
    size = len("<html><body>구조</body></html>".encode("utf-8"))
    ref = {"id": str(posted["message_id"]), "name": "structure.html", "size": size}
    assert posted["attachments"] == [ref]  # room_post hands back the values to put in the item
    out = desk.call("ask_owner", token, kind="choice", title="구조 승인", body="이 구조로 할까?", options=OPTIONS[:1],
                    attachments=[{"id": ref["id"], "name": ref["name"]}])  # one option = an approval
    assert out["ok"] and out["end_turn"]
    item = desk.store.get_inbox_item(out["inbox_id"])
    assert item["kind"] == "structure_choice" and item["payload"]["attachments"] == [ref]
    assert desk.w.owner.get(f"/studio/attachments/{ref['id']}/{ref['name']}").status_code == 200


def test_ask_owner_refuses_an_attachment_the_room_does_not_have_and_a_wrong_shape(desk, tmp_path, monkeypatch):
    task = desk.working()
    token = desk.token(task)
    posted = post_doc(desk, token, tmp_path, monkeypatch)
    mid = str(posted["message_id"])
    for refs in ([{"id": mid, "name": "other.html"}], [{"id": "99999", "name": "structure.html"}],
                 [{"id": "abc", "name": "structure.html"}]):
        out = failed(desk.call("ask_owner", token, kind="choice", title="?", body="?", options=OPTIONS, attachments=refs),
                     "precondition_failed")
        assert out["failed"] == ["attachment_not_found"]
    for refs in ([{"id": mid}], [{"name": "structure.html"}], [{"id": "", "name": "structure.html"}]):
        assert failed(desk.call("ask_owner", token, kind="choice", title="?", body="?", options=OPTIONS,
                                attachments=refs), "missing_fields")["fields"] == ["attachments"]
    assert desk.untouched(task)
    other = desk.working()  # a message of another task's room is no attachment of this one
    out = failed(desk.call("ask_owner", desk.token(other), kind="choice", title="?", body="?", options=OPTIONS,
                           attachments=[{"id": mid, "name": "structure.html"}]), "precondition_failed")
    assert out["failed"] == ["attachment_not_found"]
    assert desk.untouched(other)


def test_a_choice_without_attachments_still_files_and_zero_options_are_refused(desk):
    task = desk.working()
    token = desk.token(task)
    assert failed(desk.call("ask_owner", token, kind="choice", title="?", body="?", options=[]),
                  "missing_fields")["fields"] == ["options"]
    out = desk.call("ask_owner", token, kind="choice", title="?", body="?", options=OPTIONS[:1])
    assert out["ok"] and "attachments" not in desk.store.get_inbox_item(out["inbox_id"])["payload"]


# --- (d) the employees already hired ------------------------------------------------------------------------------
def test_a_program_employee_gets_the_section_once_in_a_commit_of_its_own(studio):  # noqa: F811
    emp, target = hire(studio, GUIDE_NO_LINES)
    assert api.add_structure_doc() == [emp["id"]]
    text = read(target)
    assert text.count(HEAD) == 1 and text.index(HEAD) < text.index("## 결재 기준") and text.startswith("먼저 묻는다\n")
    assert text.index("## 인계") < text.index(HEAD)  # the guide's own order stays; the section sits before 결재 기준
    assert employees.structure_doc_block(program_original()) in text
    assert len([h for h in employee_git.history(target, "GUIDE.md") if h["message"] == AUTO]) == 1
    assert api.add_structure_doc() == [] and read(target) == text  # a second start: nothing
    assert read(target).count(HEAD) == 1 and autos(target) == []  # (the harness commit is another one)


def test_the_section_is_not_given_to_other_parts_nor_back_to_one_who_deleted_it(studio):  # noqa: F811
    other, other_target = hire(studio, GUIDE_NO_LINES, part="art", name="아트")
    emp, target = hire(studio, GUIDE_NO_LINES)
    assert api.add_structure_doc() == [emp["id"]]
    assert read(other_target) == GUIDE_NO_LINES
    (target / "GUIDE.md").write_text(GUIDE_NO_LINES, encoding="utf-8", newline="")  # the owner deleted it
    employee_git.commit(target, "owner: 절 삭제")
    assert api.add_structure_doc() == [] and read(target) == GUIDE_NO_LINES


def test_a_guide_that_has_the_words_or_nothing_to_add_is_left_as_it_is():
    original = program_original()
    mine = "먼저 묻는다\n\n## 구조 문서 결재\n내 말로 적었다\n"
    assert employees.with_structure_doc(mine, original) == mine
    assert employees.with_structure_doc("", original) == "" and employees.with_structure_doc(GUIDE_NO_LINES, "") == GUIDE_NO_LINES
    once = employees.with_structure_doc(GUIDE_NO_LINES, original)
    assert employees.with_structure_doc(once, original) == once
    appended = employees.with_structure_doc("먼저 묻는다\n", original)
    assert appended.startswith("먼저 묻는다\n\n" + HEAD) and appended.endswith("\n")
