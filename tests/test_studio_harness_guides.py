"""T1: the harness-skill lines of the part's template added to the GUIDE.md of employees already hired
(`employees.add_harness_lines` · `api.add_harness_skills`). Every employee is a fake one in a temporary folder."""
from relay_agent.studio import api, employee_git, employees
from test_studio_importer import studio  # noqa: F401 - `studio` is its fixture

MARK = employees.HARNESS_LINE_MARK
AUTO = employees.HARNESS_AUTO_MESSAGE
GUIDE_NO_LINES = "먼저 묻는다\n\n## 인계\n넘긴다\n\n## 결재 기준\n묻고 한다\n"


def program_original():
    return employees.part_defaults()["program"]["guide_template"]


def hire(studio, guide, part="program", name="민수"):
    cfg = api._config()
    emp = studio.store.create_employee(name, part)
    target = employees.folder(cfg.studio_dir, emp["id"])
    employees.write_files(target, {"PERSONA.md": "나는 민수다\n", "GUIDE.md": guide, "MEMORY.md": "",
                                   "employee.yaml": "version: 1\ndepartment: " + part + "\n"}, keep=True)
    employee_git.commit(target, "first")
    return emp, target


def events(studio):
    return [e for e in studio.store.list_stream_events()
            if e["type"] == "employee.updated" and e["data"]["change"] == "file"]


def autos(target):
    return [h for h in employee_git.history(target, "GUIDE.md") if h["message"] == AUTO]


def read(target):
    return (target / "GUIDE.md").read_text(encoding="utf-8")


def test_with_harness_lines_adds_the_lines_before_the_approval_section_once():
    original = program_original()
    lines = [line.strip().removeprefix("- ") for line in original.splitlines() if MARK in line]
    assert len(lines) == 2
    fresh = employees.with_harness_lines(GUIDE_NO_LINES, original)
    assert fresh != GUIDE_NO_LINES
    assert fresh.index(MARK) < fresh.index("## 결재 기준")
    for line in lines:
        assert "- " + line in fresh
        fresh = fresh.replace("- " + line, "")
    assert "".join(fresh.split()) == "".join(GUIDE_NO_LINES.split())  # nothing else moved
    once = employees.with_harness_lines(GUIDE_NO_LINES, original)
    assert employees.with_harness_lines(once, original) == once
    appended = employees.with_harness_lines("먼저 묻는다\n", original)
    assert appended.startswith("먼저 묻는다\n\n- ") and appended.endswith("\n") and MARK in appended


def test_with_harness_lines_leaves_a_guide_that_has_them_or_nothing_to_add():
    original = program_original()
    mine = "먼저 묻는다\n- 하네스 스킬: 내 말로 적었다\n"
    assert employees.with_harness_lines(mine, original) == mine
    assert employees.with_harness_lines("", original) == "" and employees.with_harness_lines("  \n", original) == "  \n"
    assert employees.with_harness_lines(GUIDE_NO_LINES, "") == GUIDE_NO_LINES
    assert employees.with_harness_lines(GUIDE_NO_LINES, "줄 없음\n") == GUIDE_NO_LINES


def test_a_guide_without_the_lines_gets_them_in_a_commit_of_their_own_and_an_event(studio):
    emp, target = hire(studio, GUIDE_NO_LINES)
    assert api.add_harness_skills() == [emp["id"]]
    text = read(target)
    assert MARK in text and text.index(MARK) < text.index("## 결재 기준") and text.startswith("먼저 묻는다\n")
    assert len(autos(target)) == 1
    assert len(events(studio)) == 1
    assert events(studio)[0]["data"]["employee"]["id"] == emp["id"]


def test_a_guide_that_has_the_lines_is_left_as_it_is(studio):
    guide = GUIDE_NO_LINES + "\n- 하네스 스킬: 내가 적었다\n"
    emp, target = hire(studio, guide)
    assert api.add_harness_skills() == []
    assert read(target) == guide
    assert autos(target) == [] and events(studio) == []


def test_a_second_start_adds_nothing_more(studio):
    emp, target = hire(studio, GUIDE_NO_LINES)
    assert api.add_harness_skills() == [emp["id"]]
    text = read(target)
    assert api.add_harness_skills() == [] and api.add_harness_skills() == []
    assert read(target) == text
    assert len(autos(target)) == 1 and len(events(studio)) == 1


def test_lines_the_owner_deleted_are_not_put_back(studio):
    emp, target = hire(studio, GUIDE_NO_LINES)
    assert api.add_harness_skills() == [emp["id"]]
    (target / "GUIDE.md").write_text(GUIDE_NO_LINES + "\n내 말\n", encoding="utf-8")
    employee_git.commit(target, "owner: 하네스 줄 지움")
    assert api.add_harness_skills() == []
    assert read(target) == GUIDE_NO_LINES + "\n내 말\n"
    assert len(autos(target)) == 1 and len(events(studio)) == 1


def test_a_part_of_the_owners_own_is_not_touched(studio):
    dep = studio.store.create_department("내 파트")
    emp, target = hire(studio, GUIDE_NO_LINES, part=dep["id"])
    assert api.add_harness_skills() == []
    assert read(target) == GUIDE_NO_LINES
    assert autos(target) == []


def test_a_retired_employee_is_left_out(studio):
    gone, gone_dir = hire(studio, GUIDE_NO_LINES, name="퇴사")
    kept, kept_dir = hire(studio, GUIDE_NO_LINES, name="재직")
    studio.store.update_employee(gone["id"], retired_at="2026-01-01T00:00:00")
    assert api.add_harness_skills() == [kept["id"]]
    assert read(gone_dir) == GUIDE_NO_LINES
    assert autos(gone_dir) == [] and len(autos(kept_dir)) == 1


def test_hr_gets_its_line_from_the_hr_guide(studio):
    hr = employees.ensure_hr(studio.store, api._config().studio_dir)
    target = employees.folder(api._config().studio_dir, hr["id"])
    old = "".join(line for line in read(target).splitlines(keepends=True) if MARK not in line)
    (target / "GUIDE.md").write_text(old, encoding="utf-8", newline="")
    employee_git.commit(target, "first")
    assert api.add_harness_skills() == [hr["id"]]
    assert MARK in read(target) and len(autos(target)) == 1


def test_a_write_that_fails_for_one_employee_does_not_stop_the_others(studio, monkeypatch):
    first, first_dir = hire(studio, GUIDE_NO_LINES, name="하나")
    second, second_dir = hire(studio, GUIDE_NO_LINES, name="둘")
    real = employees.with_harness_lines
    calls = []

    def broken(guide, original):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("locked")
        return real(guide, original)

    monkeypatch.setattr(employees, "with_harness_lines", broken)
    assert len(api.add_harness_skills()) == 1
    assert sum(MARK in read(d) for d in (first_dir, second_dir)) == 1


def test_a_failing_git_or_history_does_not_stop_the_start(studio, monkeypatch):
    emp, target = hire(studio, GUIDE_NO_LINES, name="하나")
    other, other_dir = hire(studio, GUIDE_NO_LINES, name="둘")
    real = employee_git.history

    def history(folder, *args, **kw):
        if folder.name == emp["id"]:
            raise RuntimeError("git broke")
        return real(folder, *args, **kw)

    monkeypatch.setattr(employee_git, "history", history)
    assert api.add_harness_skills() == [other["id"]]
    assert MARK not in read(target) and MARK in read(other_dir)

    def commit(folder, message):
        raise RuntimeError("git down")

    monkeypatch.setattr(employee_git, "history", real)
    monkeypatch.setattr(employee_git, "commit", commit)
    assert api.add_harness_skills() == [emp["id"]]  # the file is written, the commit failed and was logged
    assert MARK in read(target)

    def whole(*a, **kw):
        raise RuntimeError("whole thing")

    monkeypatch.setattr(api, "_add_harness_skills", whole)
    assert api.add_harness_skills() == []  # the start goes on
