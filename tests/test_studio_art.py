"""Art, the fifth built-in part, and a new part proposed with a hire (T3; §3.15 · §5.5): HR hires into art like into
any part; a hire that proposes a part makes the part, its room, the employee and the 1:1 room in one approval, and
a rejection makes nothing; what cannot be made is refused in the draft. On the mock runner of test_studio_hr."""
from relay_agent.studio import employees
from test_studio_hr import PERSONA, draft, office  # noqa: F401 - the fixture `office`
from test_studio_runs import lab  # noqa: F401 - the fixture `office` stands on

ART_YAML = "version: 1\ndepartment: art\nchat: {model: sonnet, effort: medium}\n"
TOKEN = "{department_id}"  # what HR writes for the part's id, which only the server knows
NEW_YAML = f"version: 1\ndepartment: {TOKEN}\nchat: {{model: sonnet, effort: medium}}\n"
PART = {"part_name": "사운드", "part_job": "효과음과 음악의 규격을 정하고 받은 소리를 정리한다", "part_relay": "quick",
        "part_models": "chat: {model: sonnet, effort: low}\n"}


def hr_turn(office, *steps):  # noqa: F811
    """One turn of HR in which each step is called as `step(tool, results_so_far)`; returns their results."""
    out = []

    def act(call):
        for step in steps:
            out.append(step(lambda tool, **args: office.tool(call, tool, **args), out))

    office.play(["확인했습니다"], act)
    office.say("뽑아 달라")
    office.turn()
    return out


def draft_and_file(office, **args):  # noqa: F811
    return hr_turn(office, lambda t, o: t("employee_draft", **args),
                   lambda t, o: t("employee_submit", draft_id=o[0]["draft_id"], reason="소리 담당이 필요하다"))


def propose(office, **over):  # noqa: F811
    """A turn that drafts and files a hire with a new part; returns (the draft's result, the item)."""
    args = draft(name="소리", employee_yaml=NEW_YAML, department_id="", **{**PART, **over})
    made, filed = draft_and_file(office, **args)
    assert made["ok"] and made["problems"] == [] and filed["ok"], (made, filed)
    return made, office.store.get_inbox_item(filed["inbox_id"])


def test_an_art_employee_is_hired_like_any_other(office):  # noqa: F811
    store = office.store
    made, filed = draft_and_file(office, **draft(name="이아트", department_id="art", employee_yaml=ART_YAML))
    assert made["ok"] and made["problems"] == [] and "department_id" not in made and filed["ok"]
    item = store.get_inbox_item(filed["inbox_id"])
    assert item["title"] == "채용: 이아트" and item["payload"]["draft"]["part_name"] == ""
    assert office.lab.decide(item, "approve").status_code == 200
    [hired] = office.named("이아트")
    assert hired["department_id"] == "art"
    assert [m["employee_id"] for m in store.list_room_members(room_id="room_dep_art")] == [hired["id"]]
    assert len(store.list_departments()) == 5


def test_a_new_part_proposed_with_a_hire_is_made_with_the_employee_in_one_approval(office):  # noqa: F811
    store = office.store
    made, item = propose(office)
    part_id = made["department_id"]
    assert part_id.startswith("dep_") and store.get_department(part_id) is None  # nothing before the decision
    assert item["title"] == "채용: 소리 — 새 파트 사운드 를 만든다"
    sent = item["payload"]["draft"]
    assert {key: sent[key] for key in PART} == PART and sent["department_id"] == part_id
    assert f"department: {part_id}" in sent["employee_yaml"] and TOKEN not in sent["employee_yaml"]

    assert office.lab.decide(item, "approve").status_code == 200
    part = store.get_department(part_id)
    assert (part["name"], part["builtin"], part["default_relay"], part["default_mcp"], part["sort_order"]) == (
        "사운드", False, "quick", [], 5)
    assert part["default_models"] == {"chat": {"model": "sonnet", "effort": "low"}}
    assert "- 맡은 일: 효과음과 음악의 규격을 정하고 받은 소리를 정리한다" in part["guide_template"]
    [hired] = office.named("소리")
    assert hired["department_id"] == part_id
    [room] = store.list_rooms(kind="department", department_id=part_id)
    assert room["title"] == "사운드"
    assert [m["employee_id"] for m in store.list_room_members(room_id=room["id"])] == [hired["id"]]
    [dm] = store.list_rooms(kind="dm", employee_id=hired["id"])
    assert store.get_room_member(dm["id"], hired["id"]) is not None
    folder = employees.folder(office.desk.cfg.studio_dir, hired["id"])
    assert f"department: {part_id}" in (folder / "employee.yaml").read_text(encoding="utf-8")
    assert [e["room_id"] for e in store.list_stream_events() if e["type"] == "room.updated"].count(room["id"]) == 1
    assert [d["id"] for d in store.list_departments()][-1] == part_id and len(store.list_departments()) == 6


def test_a_rejected_proposal_makes_nothing(office):  # noqa: F811
    store = office.store
    made, item = propose(office)
    before = (len(store.list_departments(include_archived=True)), len(store.list_rooms(include_archived=True)))
    assert office.lab.decide(item, "reject", comment="파트는 필요 없다").status_code == 200
    assert store.get_department(made["department_id"]) is None and office.named("소리") == []
    assert (len(store.list_departments(include_archived=True)), len(store.list_rooms(include_archived=True))) == before
    assert store.list_rooms(kind="department", department_id=made["department_id"]) == []


def test_a_proposal_that_cannot_be_made_is_refused_in_the_draft(office):  # noqa: F811
    cases = (({"part_name": "아트"}, "part_name_taken"), ({"part_name": "프로그램"}, "part_name_taken"),
             ({"part_job": " "}, "part_job_missing"), ({"part_relay": "no-such-relay"}, "relay_unknown"),
             ({"part_models": "[1, 2]"}, "part_models_invalid"),
             ({"part_models": "chat: {model: sonnet, effort: extreme}"}, "effort_unsupported"))
    steps = []
    for over, _ in cases:  # one turn of HR drafts and files every one of them
        args = draft(name="소리", employee_yaml=NEW_YAML, department_id="", **{**PART, **over})
        steps += [lambda t, o, args=args: t("employee_draft", **args),
                  lambda t, o: t("employee_submit", draft_id=o[-1]["draft_id"], reason="소리")]
    # a part's id is the server's: with a new part the draft names none
    steps.append(lambda t, o: t("employee_draft", **draft(name="소리", employee_yaml=NEW_YAML, **PART)))
    *out, clash = hr_turn(office, *steps)
    for (over, code), made, filed in zip(cases, out[0::2], out[1::2], strict=True):
        assert made["ok"] and code in [p["code"] for p in made["problems"]], (over, made)
        assert (filed["ok"], filed["code"]) == (False, "precondition_failed") and code in filed["failed"]
    assert office.lab.pending() == [] and len(office.store.list_departments(include_archived=True)) == 5
    assert (clash["ok"], clash["code"]) == (False, "precondition_failed")


def test_a_name_taken_after_the_filing_refuses_the_approval_and_makes_nothing(office):  # noqa: F811
    store = office.store
    made, item = propose(office)
    assert office.lab.owner.post("/studio/departments", json={"name": "사운드"}).status_code == 201  # meanwhile
    r = office.lab.decide(item, "approve")
    assert r.status_code >= 400 and r.json()["code"] == "precondition_failed", r.text
    assert office.named("소리") == [] and store.get_department(made["department_id"]) is None
    assert store.get_inbox_item(item["id"])["status"] == "pending"
    assert [d["name"] for d in store.list_departments()].count("사운드") == 1


def test_an_edit_of_the_owner_can_rename_the_proposed_part(office):  # noqa: F811
    store = office.store
    made, item = propose(office)
    r = office.lab.decide(item, "approve", data={"edits": {"draft": {"part_name": "오디오"}}})
    assert r.status_code == 200, r.text
    assert store.get_department(made["department_id"])["name"] == "오디오"
    [room] = store.list_rooms(kind="department", department_id=made["department_id"])
    assert room["title"] == "오디오"
