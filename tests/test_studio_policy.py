"""The policy of a project (H-6; §2.2 · §6.4 · §8.4): GET · PUT /studio/projects/{id}/policy, the policy talk that
opens in HR's 1:1 room when a project is brought in, and the section of HR's GUIDE.md that tells the order of it.
Every project is a fake one in a temporary folder."""
import hashlib

from relay_agent.studio import api, employees
from relay_agent.studio import policy as project_policy
from relay_agent.studio.inbox import Hooks
from test_studio_importer import fake_project, snapshot, studio  # noqa: F401 - `studio` is its fixture

TITLE = employees.POLICY_TALK_TITLE
FRAME = "## 새 직원 GUIDE 틀"


class Recorder(Hooks):
    def __init__(self):
        self.turns = []

    def chat_turn(self, employee_id, room_id):
        self.turns.append((employee_id, room_id))


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def brought(studio, **kw):
    folder = fake_project(studio.root, **kw)
    r = studio.bring(folder)
    assert r.status_code == 201, r.text
    return folder, r.json()["id"]


def put(studio, pid, text, base):
    return studio.owner.put("/studio/projects/" + pid + "/policy", json={"text": text, "base_sha256": base})


def get(studio, pid):
    return studio.owner.get("/studio/projects/" + pid + "/policy")


def without_section(guide):
    return guide[:guide.index(TITLE)] + guide[guide.index(FRAME):]


# --- GET · PUT ------------------------------------------------------------------------------------------------
def test_a_project_without_a_policy_reads_empty_and_put_then_get_gives_the_text(studio):
    _, pid = brought(studio)
    assert get(studio, pid).json() == {"text": "", "sha256": "", "updated_at": None}
    r = put(studio, pid, "# 정책\n기획 문서 변경은 결재\n", "")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sha256"] == sha("# 정책\n기획 문서 변경은 결재\n") and body["updated_at"]
    assert get(studio, pid).json() == body
    path = project_policy.path(api._config().studio_dir, pid)
    assert path.read_bytes() == "# 정책\n기획 문서 변경은 결재\n".encode("utf-8")
    assert put(studio, pid, "# 정책\n둘째\n", body["sha256"].upper()).status_code == 200  # the base is case-blind


def test_a_stale_base_is_refused_and_the_file_stays(studio):
    _, pid = brought(studio)
    first = put(studio, pid, "첫째\n", "").json()
    r = put(studio, pid, "둘째\n", "")  # read before the first write
    assert (r.status_code, r.json()["code"], r.json()["failed"]) == (409, "precondition_failed", ["base_mismatch"])
    assert put(studio, pid, "둘째\n", sha("something else")).status_code == 409
    path = project_policy.path(api._config().studio_dir, pid)
    assert path.read_bytes() == "첫째\n".encode("utf-8")
    assert put(studio, pid, "첫째\n", first["sha256"]).json()["sha256"] == first["sha256"]  # same text: nothing to write


def test_no_such_project_has_no_policy(studio):
    assert get(studio, "prj_none").status_code == 404
    assert put(studio, "prj_none", "x", "").status_code == 404


# --- the talk that opens when a project is brought in ---------------------------------------------------------
def test_importing_writes_the_rules_file_into_hrs_room_and_asks_for_hrs_turn(studio, monkeypatch):
    folder = fake_project(studio.root)
    (folder / "CLAUDE.md").write_text("# 규칙\n레벨 맵은 건드리지 않는다\n", encoding="utf-8")
    hr = employees.ensure_hr(studio.store, api._config().studio_dir)
    dm = studio.store.list_rooms(kind="dm", employee_id=hr["id"])[0]
    rec = Recorder()
    monkeypatch.setattr(api, "_hooks", rec)
    before = snapshot(folder)
    r = studio.bring(folder)
    assert r.status_code == 201, r.text
    assert snapshot(folder) == before  # read, never written
    [message] = studio.store.list_messages(dm["id"])
    assert message["author_kind"] == "system"
    assert message["meta"] == {"event": "policy_interview", "project_id": r.json()["id"]}
    for text in ("[정책 면담]", "장르", "누가 무엇을", "건드리면 안 되는 것", "CLAUDE.md", "레벨 맵은 건드리지 않는다"):
        assert text in message["body"]
    assert rec.turns == [(hr["id"], dm["id"])]
    assert [e for e in studio.store.list_stream_events() if e["type"] == "message.created"]


def test_no_hr_means_no_talk_and_a_talk_without_rules_does_not_mention_them(studio, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(api, "_hooks", rec)
    _, pid = brought(studio, claude=False)  # no HR employee in this DB
    assert rec.turns == [] and not studio.store.list_rooms(kind="dm")
    hr = employees.ensure_hr(studio.store, api._config().studio_dir)
    dm = studio.store.list_rooms(kind="dm", employee_id=hr["id"])[0]
    started = project_policy.begin_interview(studio.store, api._config().studio_dir, studio.store.get_project(pid))
    assert started == (hr["id"], dm["id"])
    body = studio.store.list_messages(dm["id"])[0]["body"]
    assert "[정책 면담]" in body and "규칙 파일" not in body


def test_long_rules_are_cut(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("가" * 20000, encoding="utf-8")
    text = project_policy.interview_text({"id": "prj_1", "name": "G"}, tmp_path)
    assert "잘림" in text and len(text) < project_policy.RULES_LIMIT + 1500


# --- HR's GUIDE.md ----------------------------------------------------------------------------------------------
def test_hr_guide_holds_the_order_of_the_policy_talk():
    guide = employees.hr_texts()["GUIDE.md"]
    section = guide[guide.index(TITLE):]
    section = section[:section.index(FRAME)]
    for text in ("[정책 면담]", "장르", "누가 무엇을", "건드리면 안 되는 것", "policy_draft", "submit=True", "결재"):
        assert text in section


def test_the_policy_section_is_added_once_and_what_the_owner_wrote_stays():
    original = employees.hr_texts()["GUIDE.md"]
    old = without_section(original) + "\n내가 고친 줄\n"
    fresh = employees.with_policy_talk(old, original)
    assert fresh != old and TITLE in fresh and "policy_draft" in fresh and "내가 고친 줄" in fresh
    assert fresh.index(TITLE) < fresh.index(FRAME)
    assert fresh.replace(fresh[fresh.index(TITLE):fresh.index(FRAME)], "") == old  # nothing else moved
    assert employees.with_policy_talk(fresh, original) == fresh  # twice = once
    mine = old + "\n## 프로젝트 정책\n내 말로 고쳤다\n"
    assert employees.with_policy_talk(mine, original) == mine
    assert employees.with_policy_talk("", original) == "" and employees.with_policy_talk(old, "") == old


def test_add_policy_talk_touches_only_the_guide_of_an_hr_employee_that_lacks_it(studio):
    cfg = api._config()
    hr = employees.ensure_hr(studio.store, cfg.studio_dir)
    guide = employees.folder(cfg.studio_dir, hr["id"]) / "GUIDE.md"
    assert employees.add_policy_talk(studio.store, cfg.studio_dir) == []  # a new HR has the section already
    guide.write_text(without_section(guide.read_text(encoding="utf-8")) + "\n내가 덧붙인 줄\n", encoding="utf-8")
    assert employees.add_policy_talk(studio.store, cfg.studio_dir) == [hr["id"]]
    text = guide.read_text(encoding="utf-8")
    assert "내가 덧붙인 줄" in text and "policy_draft" in text
    assert employees.add_policy_talk(studio.store, cfg.studio_dir) == []
    assert guide.read_text(encoding="utf-8") == text


# --- the approval rule in a prompt --------------------------------------------------------------------------------
def test_the_prompt_of_a_placed_employee_carries_the_policy_and_the_rule(studio):
    cfg = api._config()
    emp = studio.store.create_employee("민수", "program")
    employees.write_files(employees.folder(cfg.studio_dir, emp["id"]),
                          {"PERSONA.md": "나는 민수다\n", "GUIDE.md": "먼저 묻는다\n", "MEMORY.md": "",
                           "employee.yaml": "version: 1\ndepartment: program\n"}, keep=True)
    path = project_policy.path(cfg.studio_dir, "prj_1")
    path.parent.mkdir(parents=True)
    path.write_text("기획 문서 변경은 결재\n", encoding="utf-8")
    placed = employees.prompt_for(studio.store, cfg.studio_dir, emp, None, "prj_1")
    assert "기획 문서 변경은 결재" in placed and "ask_owner" in placed and "kind=approval" in placed
    assert "무엇을 · 왜 · 안 하면 어떻게 되는지" in placed
    assert placed.index("기획 문서 변경은 결재") < placed.index("===== " + employees.APPROVAL_RULE_TITLE + " =====")
    assert employees.APPROVAL_RULE_TITLE not in employees.prompt_for(studio.store, cfg.studio_dir, emp)  # unplaced
    nopolicy = employees.prompt_for(studio.store, cfg.studio_dir, emp, None, "prj_2")  # no POLICY.md: the rule stays
    assert employees.APPROVAL_RULE_TITLE in nopolicy and "기획 문서 변경은 결재" not in nopolicy


# --- one place for the path; the room HR's talk opens in ------------------------------------------------------
def test_the_policy_path_is_put_together_in_one_place():
    from pathlib import Path
    assert project_policy.path("/x", "prj_1") == employees.policy_path("/x", "prj_1")
    for name in ("policy.py", "inbox.py", "tools.py"):
        source = (Path(api.__file__).parent / name).read_text(encoding="utf-8")
        assert '/ "POLICY.md"' not in source, name


def hr_and_project(studio):
    _, pid = brought(studio, claude=False)  # no HR in this DB yet
    hr = employees.ensure_hr(studio.store, api._config().studio_dir)
    dm = studio.store.list_rooms(kind="dm", employee_id=hr["id"])[0]
    return hr, dm, studio.store.get_project(pid)


def test_the_only_hr_room_archived_means_no_talk(studio):
    hr, dm, project = hr_and_project(studio)
    studio.store.update_room(dm["id"], archived_at="2026-01-01T00:00:00")
    assert project_policy.begin_interview(studio.store, api._config().studio_dir, project) is None
    assert studio.store.list_messages(dm["id"]) == []


def test_an_hr_employee_on_leave_alone_is_asked(studio):
    hr, dm, project = hr_and_project(studio)
    studio.store.update_employee(hr["id"], status="on_leave")
    assert project_policy.begin_interview(studio.store, api._config().studio_dir, project) == (hr["id"], dm["id"])
    assert len(studio.store.list_messages(dm["id"])) == 1

