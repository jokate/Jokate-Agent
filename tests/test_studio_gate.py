"""The harness gates of S-11 ②: a program employee is placed only where the harness is laid, a feature that changed
code asks for the review only after a structure choice was decided, the package's `harness_checks`, and the files
a structure choice or a review brings (§6.6 '직원의 첨부'). Mock runs and fake tokens: no AI is called."""
import pytest

from relay_agent.studio import Actor, api, files, gate, tools
from relay_agent.studio.inbox import Inbox, InboxError
from relay_agent.studio.tasks import TransitionError
from test_studio_api_tasks import world as tasks_world  # noqa: F401 - the fixture `desk` stands on
from test_studio_employee_lifecycle import assign, other_project
from test_studio_inbox import PAYLOAD, ok, refused, world  # noqa: F401
from test_studio_review import COPY, YES, pack, reviewed
from test_studio_runs import lab  # noqa: F401
from test_studio_tools import CHECKLIST, OPTIONS, REVIEW, desk, failed  # noqa: F401
from test_studio_work import shop  # noqa: F401

CODE = "Source/jump.cpp"


def gated(shop):  # noqa: F811
    """An inbox over the shop's machine, whose effects are the server's (WorkEffects): T6 meets the gate."""
    world = shop.lab.world
    return Inbox(shop.store, shop.m, shop.lab.hooks, studio_dir=world.dir, checks=world.box.checks)


# --- the placement ---------------------------------------------------------------------------------------------
def test_a_program_employee_is_not_placed_where_the_harness_is_not_laid(world):  # noqa: F811
    bare = other_project(world, harness=False)  # Docs is there, CLAUDE.md is not
    answer = refused(assign(world, bare), 409, "precondition_failed")
    assert answer["failed"] == [gate.NO_HARNESS] and "CLAUDE.md" in answer["detail"]
    assert world.store.get_employee(world.emp)["project_id"] == world.pid  # unchanged
    nowhere = world.store.create_project("없는 폴더", "D:/no/such/folder", "none")["id"]
    assert refused(assign(world, nowhere), 409, "precondition_failed")["failed"] == [gate.NO_HARNESS]
    ready = other_project(world)
    assert ok(assign(world, ready))["project_id"] == ready


def test_other_parts_are_placed_in_a_project_without_the_harness(world):  # noqa: F811
    bare = other_project(world, harness=False)
    planner, _ = world.staff("기획자", "design_doc")
    assert ok(assign(world, bare, planner))["project_id"] == bare
    artist, _ = world.staff("미술", "art")
    assert ok(assign(world, bare, artist))["project_id"] == bare


def test_a_hire_for_the_program_part_needs_the_harness_too(world):  # noqa: F811
    bare = other_project(world, harness=False)
    ready = other_project(world)
    draft = {"department_id": "program"}
    with pytest.raises(InboxError) as e:
        world.box._check_hire(draft, "새직원", bare)
    assert e.value.code == "precondition_failed" and "하네스" in e.value.detail
    assert world.box._check_hire(draft, "새직원", ready)["id"] == "program"
    assert world.box._check_hire({"department_id": "design_doc"}, "기획", bare)["id"] == "design_doc"
    assert world.box._check_hire(draft, "대기", None)["id"] == "program"  # placed nowhere: nothing to lay


def test_a_part_with_the_code_relay_needs_the_harness_like_the_program_part(world):  # noqa: F811
    tool = world.store.create_department("툴 프로그램", default_relay="auto")
    docs = world.store.create_department("문서 파트", default_relay="doc-write")
    bare_part = world.store.create_department("릴레이 없음")
    bare, ready = other_project(world, harness=False), other_project(world)
    coder, _ = world.staff("툴러", tool["id"])
    answer = refused(assign(world, bare, coder), 409, "precondition_failed")
    assert answer["failed"] == [gate.NO_HARNESS] and "CLAUDE.md" in answer["detail"] and "툴 프로그램" in answer["detail"]
    assert world.store.get_employee(coder)["project_id"] == world.pid  # unchanged
    assert ok(assign(world, ready, coder))["project_id"] == ready
    for part in (docs, bare_part):
        who, _ = world.staff("문서" + part["id"][-3:], part["id"])
        assert ok(assign(world, bare, who))["project_id"] == bare
    assert gate.needs_harness({"id": "program"}) and gate.needs_harness({"id": "x", "default_relay": "auto"})
    assert not gate.needs_harness({"id": "x", "default_relay": "doc-write"}) and not gate.needs_harness(None)


def test_a_hire_that_proposes_a_part_with_the_code_relay_needs_the_harness(world):  # noqa: F811
    bare = other_project(world, harness=False)
    base = {"department_id": "sound", "part_name": "사운드", "part_job": "효과음을 정리한다",
            "part_models": "chat: {model: sonnet, effort: low}\n"}
    with pytest.raises(InboxError) as e:
        world.box._check_hire({**base, "part_relay": "auto"}, "새직원", bare)
    assert e.value.code == "precondition_failed" and "하네스" in e.value.detail
    assert world.box._check_hire({**base, "part_relay": "doc-write"}, "새직원", bare)["id"] == "sound"


# --- the judgement ---------------------------------------------------------------------------------------------
def test_judge_asks_a_structure_choice_only_for_a_features_code():
    feature, fix = {"type": "feature"}, {"type": "bugfix"}
    code, why = gate.judge(feature, ["Docs/a.md", "Source/a.CPP", "Source/b.h", "Source/c.cs", "X/d.py"], None)
    assert code == gate.MISSING and "Source/a.CPP" in why and "ask_owner" in why and "외 1개" in why
    assert gate.judge(feature, ["Docs/a.md", "Config/x.ini", "Content/Hero.uasset"], None) == (None, "")
    assert gate.judge(fix, ["Source/a.cpp"], None) == (None, "")
    assert gate.judge(feature, ["Source/a.cpp"], {"id": "inb_1"}) == (None, "")


def test_a_revised_structure_choice_is_no_ground(shop):  # noqa: F811
    task = shop.task(relay="edit")
    row, _ = shop.start(task)
    box, who = shop.lab.world.box, Actor.employee(row["assignee_id"])
    item = box.create("structure_choice", who, title="구조", payload=PAYLOAD["structure_choice"], task_id=row["id"])
    ok(shop.lab.decide(item, "revise", comment="다시"))
    assert gate.chosen(shop.store, row["id"]) is None
    shop.store.update_task(row["id"], queued_at=None)
    again = box.create("structure_choice", who, title="구조", payload=PAYLOAD["structure_choice"], task_id=row["id"])
    ok(shop.lab.decide(again, "choose", data={"option_id": "b"}))
    found = gate.chosen(shop.store, row["id"])
    assert found["id"] == again["id"] and gate.option_of(found) == "b"


# --- T6 --------------------------------------------------------------------------------------------------------
def test_a_review_of_code_without_a_structure_choice_is_refused_and_the_task_stays(shop):  # noqa: F811
    task = shop.task(relay="edit")
    row, run = shop.start(task)
    who = Actor.employee(row["assignee_id"])
    box = gated(shop)
    shop.engine.history.add_event(run.id, "build", "tool_use", {"tool": "Read", "target": "Source/other.cpp"})
    shop.engine.history.add_event(run.id, "build", "tool_use", {"tool": "Edit", "target": "Docs/notes.md"})
    item = box.create("final_review", who, title="검수", payload=PAYLOAD["final_review"], task_id=row["id"])
    assert shop.store.get_task(row["id"])["status"] == "review"  # a document and a Read are no code change
    ok(shop.lab.decide(item, "rework", comment="다시"))
    shop.store.update_task(row["id"], queued_at=None)

    shop.engine.history.add_event(run.id, "build", "tool_use", {"tool": "Edit", "target": CODE})
    before = shop.store.get_task(row["id"])
    with pytest.raises(TransitionError) as e:
        box.create("final_review", who, title="검수", payload=PAYLOAD["final_review"], task_id=row["id"])
    assert e.value.code == "precondition_failed" and e.value.failed == [gate.MISSING] and CODE in e.value.detail
    assert shop.store.get_task(row["id"]) == before  # the state, the run and the times are as they were
    assert not shop.store.list_inbox(task_id=row["id"], status="pending")

    ask = box.create("structure_choice", who, title="구조", payload=PAYLOAD["structure_choice"], task_id=row["id"])
    ok(shop.lab.decide(ask, "choose", data={"option_id": "a"}))
    shop.store.update_task(row["id"], queued_at=None)
    item = box.create("final_review", who, title="검수", payload=PAYLOAD["final_review"], task_id=row["id"])
    assert shop.store.get_task(row["id"])["status"] == "review"
    assert item["payload"]["structure_choice_inbox_id"] == ask["id"]


def test_the_tool_call_of_the_review_meets_the_gate_and_answers_the_refusal(shop):  # noqa: F811
    task = shop.task(relay="edit")
    row, run = shop.start(task)
    box = gated(shop)
    caller = tools.Caller(employee=shop.store.get_employee(row["assignee_id"]), run_id=run.id, room_id=None,
                          task_id=row["id"], mode="work")
    shop.engine.history.add_event(run.id, "build", "tool_use", {"tool": "Edit", "target": CODE})
    before = shop.store.get_task(row["id"])
    out = failed(tools.call("task_request_review", caller, REVIEW, machine=shop.m, inbox=box), "precondition_failed")
    assert out["failed"] == [gate.MISSING] and CODE in out["message"]
    assert shop.store.get_task(row["id"]) == before and not shop.store.list_inbox(task_id=row["id"], status="pending")

    ask = box.create("structure_choice", Actor.employee(row["assignee_id"]), title="구조",
                     payload=PAYLOAD["structure_choice"], task_id=row["id"])
    ok(shop.lab.decide(ask, "choose", data={"option_id": "a"}))
    shop.store.update_task(row["id"], queued_at=None)
    out = tools.call("task_request_review", caller, REVIEW, machine=shop.m, inbox=box)
    assert out["ok"] is True and out["end_turn"] is True and out["inbox_id"]
    assert shop.store.get_task(row["id"])["status"] == "review"


def test_a_feature_that_changed_only_a_blueprint_is_a_warning_not_a_pass():
    payload = {"checklist": []}
    files = [{"path": "Content/Hero.uasset"}, {"path": "Content\\Map.umap"}, {"path": "Docs/a.md"}]
    got = gate.checks({"type": "feature"}, payload, files, None, None)[0]
    assert got["status"] == "warning" and got["data"]["asset_files"] == ["Content/Hero.uasset", "Content/Map.umap"]
    assert got["data"]["code_files"] == [] and got["data"]["inbox_id"] is None
    assert gate.checks({"type": "bugfix"}, payload, files, None, None)[0]["status"] == "passed"
    assert gate.checks({"type": "feature"}, payload, files, {"id": "inb_1"}, None)[0]["status"] == "passed"
    assert gate.checks({"type": "feature"}, payload, files + [{"path": CODE}], None, None)[0]["status"] == "failed"


def test_the_package_tells_what_the_gate_judged(shop):  # noqa: F811
    def decided(row, run):
        box, who = shop.lab.world.box, Actor.employee(row["assignee_id"])
        ask = box.create("structure_choice", who, title="구조", payload=PAYLOAD["structure_choice"], task_id=row["id"])
        ok(shop.lab.decide(ask, "choose", data={"option_id": "b"}))
        shop.store.update_task(row["id"], queued_at=None)
        decided.id = ask["id"]

    task, item, run = reviewed(shop, "copy", COPY, before=decided)
    checks = {c["id"]: c for c in pack(shop, item)["harness_checks"]}
    assert list(checks) == ["structure_choice", "checklist", "skills", "mcp", "guard_warnings"]
    assert all(c["status"] in gate.STATUSES and isinstance(c["note"], str) for c in checks.values())
    assert checks["structure_choice"]["status"] == "passed"
    assert checks["structure_choice"]["data"] == {"inbox_id": decided.id, "option_id": "b",
                                                  "code_files": ["Source/jump.cpp", "Source/old.cpp"]}
    assert checks["checklist"]["data"] == {"verified_auto": 1, "verified_manual": 0, "unverified": 0}
    assert shop.store.get_inbox_item(item["id"])["payload"]["structure_choice_inbox_id"] == decided.id


def test_checks_are_filled_from_the_record_and_say_when_there_is_none():
    task = {"type": "feature"}
    payload = {"checklist": [{"criterion": "a", "status": "unverified"}, {"criterion": "b", "status": "verified_auto"}]}
    record = {"skills": [{"name": "game-review"}], "guards": [{"tool": "Bash", "verdict": "block"}],
              "counts": {"mcp_calls": 3, "mcp_errors": 1, "guard_judged": 5}}
    got = {c["id"]: c for c in gate.checks(task, payload, [{"path": "Docs/a.md"}], None, record)}
    assert got["structure_choice"]["status"] == "passed" and "코드가 바뀌지 않았다" in got["structure_choice"]["note"]
    assert (got["checklist"]["status"], got["skills"]["data"]) == ("warning", {"names": ["game-review"]})
    assert (got["mcp"]["status"], got["guard_warnings"]["status"]) == ("warning", "warning")
    blind = {c["id"]: c["status"] for c in gate.checks(task, payload, [], None, None)}
    assert (blind["skills"], blind["mcp"], blind["guard_warnings"]) == ("info", "info", "info")
    other = gate.checks({"type": "bugfix"}, payload, [{"path": CODE}], None, record)[0]
    assert other["status"] == "passed"


# --- the marks of the checklist (tools.py looks at them already) -----------------------------------------------
def test_a_checklist_row_without_its_mark_is_refused(desk):  # noqa: F811
    task = desk.working()
    token = desk.token(task)
    bare = [{"criterion": "테스트 통과"}]
    assert failed(desk.call("task_request_review", token, summary="끝", checklist=bare),
                  "missing_fields")["fields"] == ["checklist"]
    assert desk.untouched(task)


# --- the files of a structure choice and a review --------------------------------------------------------------
@pytest.fixture
def folders(desk, tmp_path, monkeypatch):  # noqa: F811
    work, elsewhere = tmp_path / "work", tmp_path / "elsewhere"
    work.mkdir()
    elsewhere.mkdir()
    (work / "flow.png").write_bytes(b"png")
    (work / "run.log").write_bytes(b"log line")
    (elsewhere / "secret.png").write_bytes(b"secret")
    monkeypatch.setattr(api, "_workdir", lambda run_id: work)
    return work, elsewhere, tmp_path / "runs" / "studio" / "attachments"


def test_a_structure_choice_keeps_the_figure_file_under_its_item(desk, folders):  # noqa: F811
    work, elsewhere, attached = folders
    task = desk.working()
    token = desk.token(task)
    outside = [OPTIONS[0], {**OPTIONS[1], "figure": {"path": str(elsewhere / "secret.png")}}]
    out = failed(desk.call("ask_owner", token, kind="choice", title="?", body="?", options=outside),
                 "precondition_failed")
    assert out["failed"] == [files.OUTSIDE] and not attached.exists() and desk.untouched(task)

    options = [OPTIONS[0], {**OPTIONS[1], "figure": {"path": "flow.png", "mermaid": "graph TD; A-->B"}}]
    out = desk.call("ask_owner", token, kind="choice", title="구조", body="어디에?", options=options)
    item = desk.store.get_inbox_item(out["inbox_id"])
    figure = item["payload"]["options"][1]["figure"]
    assert figure == {"mermaid": "graph TD; A-->B", "name": "flow.png", "path": f"{item['id']}/flow.png",
                      "mime": files.mime("flow.png"), "size": 3}
    assert (attached / item["id"] / "flow.png").read_bytes() == b"png" and (work / "flow.png").exists()
    assert item["payload"]["options"][0]["figure"] is None


def test_a_review_keeps_the_evidence_files_and_a_failed_filing_leaves_none(desk, folders, monkeypatch):  # noqa: F811
    work, elsewhere, attached = folders
    task = desk.working()
    token = desk.token(task)
    evidence = [{"kind": "screenshot", "ref": "flow.png", "caption": "흐름"},
                {"kind": "url", "ref": "https://example.com/ci/41"}, {"kind": "log", "ref": "run.log"}]
    real = Inbox.create

    def refuse(self, *args, **kwargs):  # the write is refused after the files were copied
        assert (attached / kwargs["item_id"] / "flow.png").is_file()
        raise TransitionError("precondition_failed", "막혔다", failed=["blocked"])

    monkeypatch.setattr(Inbox, "create", refuse)
    assert failed(desk.call("task_request_review", token, **REVIEW, evidence=evidence),
                  "precondition_failed")["failed"] == ["blocked"]
    assert not list(attached.iterdir()) and desk.untouched(task)  # the folder went with the failure
    monkeypatch.setattr(Inbox, "create", real)

    out = desk.call("task_request_review", token, **REVIEW, evidence=evidence)
    item = desk.store.get_inbox_item(out["inbox_id"])
    shot, link, log = item["payload"]["evidence"]
    assert shot["ref"] == {"name": "flow.png", "path": f"{item['id']}/flow.png", "mime": files.mime("flow.png"), "size": 3}
    assert link["ref"] == "https://example.com/ci/41" and log["ref"]["path"] == f"{item['id']}/run.log"
    assert sorted(p.name for p in (attached / item["id"]).iterdir()) == ["flow.png", "run.log"]
    assert evidence[0]["ref"] == "flow.png"  # the caller's arguments are not rewritten
