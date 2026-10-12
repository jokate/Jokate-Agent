"""The three other parts and a part of the owner's (H-1): the originals of relay_agent/studio/parts/, the fill of
a start (only the empty cells; §3.4 · §3.15), the checks of §9.4 on a part's defaults, what HR is told of a part
when it hires (§8.4), and the three REST paths of a part (§6.4). No run and no AI is started."""
import dataclasses
import tomllib
from pathlib import Path

import yaml

from relay_agent.studio import api, drafts, employees
from relay_agent.studio.store import now
from test_studio_api import refused, world  # noqa: F401 - `world` is its fixture

PARTS = ("design_doc", "program", "design_setup")
RELAYS = {"design_doc": "doc-write", "program": "auto", "design_setup": "editor-task"}  # CAMPAIGN.md H-1: the seed
STAGES = {"design_doc": "doc-write", "program": "studio-task", "design_setup": "editor-task"}  # whose stages `work` names
FRAME = "- 묻고 한다 (`ask_owner` 로 무엇을 · 왜 · 안 하면 어떻게 되는지를 올린다):\n- 알아서 한다:\n"


def no(r, code):
    return refused(r, api.STATUS[code], code)


def part_rooms(world, archived=0):
    items = world.owner.get(f"/studio/rooms?kind=department&archived={archived}").json()["items"]
    return {room["department_id"]: room for room in items}


# --- the originals ------------------------------------------------------------------------------------------------
def test_the_originals_are_a_guide_for_each_part_and_one_file_of_defaults():
    defaults = employees.part_defaults()
    assert tuple(defaults) == employees.PART_IDS == PARTS
    for values in defaults.values():
        guide = values["guide_template"]
        assert guide.startswith("# 지침\n") and "`task_request_review`" in guide
        assert FRAME in guide[guide.index("## 결재 기준"):]  # the frame of the approvals, empty
        assert len(guide.encode("utf-8")) <= 4096 and "모델" not in guide
        assert set(values["default_models"]) == set(employees.MODEL_KEYS)
        assert values["default_mcp"] and employees.STUDIO_MCP not in values["default_mcp"]
    program = defaults["program"]["guide_template"]  # the harness: in this order
    steps = [program.index(step) for step in
             ("1. 기획 사양", "2. 구조 조사", "3. owner 선택", "4. 완료 기준", "5. 구현", "6. 리뷰")]
    assert steps == sorted(steps) and "`ask_owner`" in program[steps[2]:steps[3]]
    setup = defaults["design_setup"]["guide_template"]
    assert all(word in setup for word in ("DataTable", "DataAsset", "GameplayTag", "프로젝트 설정", "MCP 로"))
    assert "`Docs/`" in defaults["design_doc"]["guide_template"]


def test_the_originals_are_package_data():
    root = Path(employees.__file__).resolve().parents[2]
    tool = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    listed = {p for pattern in tool["package-data"]["relay_agent.studio"]
              for p in employees.PARTS_DIR.parent.glob(pattern)}
    files = {p for p in employees.PARTS_DIR.iterdir() if p.is_file()}
    assert files <= listed and sorted(p.name for p in files) == sorted(["defaults.yaml", *(f"{p}.md" for p in PARTS)])


# --- the fill of a start ------------------------------------------------------------------------------------------
def test_a_start_fills_only_the_empty_cells_of_the_three_parts(world):
    import relay_agent.server as server

    store, defaults = world.store, employees.part_defaults()
    assert all(store.get_department(p)["guide_template"] == "" for p in PARTS)
    assert [row["id"] for row in api.fill_parts()] == list(PARTS)
    for p in PARTS:
        part = store.get_department(p)
        assert {key: part[key] for key in defaults[p]} == defaults[p] and part["default_relay"] == RELAYS[p]
    assert api.fill_parts() == []  # nothing is empty: nothing is written

    store.update_department("program", guide_template="# 고친 지침\n", default_mcp=[],
                            default_models={"chat": {"model": "haiku"}})
    assert [row["id"] for row in api.fill_parts()] == ["program"]  # each cell on its own: the one that is empty
    part = store.get_department("program")
    assert (part["guide_template"], part["default_models"]) == ("# 고친 지침\n", {"chat": {"model": "haiku"}})
    assert part["default_mcp"] == defaults["program"]["default_mcp"]
    store.update_department("design_doc", default_relay=None)
    assert api.fill_parts() == [] and store.get_department("design_doc")["default_relay"] is None  # not the relay
    assert store.get_department("hr")["guide_template"] == ""  # HR's cells are `ensure_hr`'s
    assert store.list_stream_events() == []

    starts = [f.__name__ for f in server.app.router.on_startup]  # a start of the server does it, after HR's
    assert starts.index("fill_studio_parts") == starts.index("ensure_studio_hr") + 1


def test_the_defaults_pass_the_checks_of_an_employee_yaml(world):
    checks, defaults = employees.context(world.cfg), employees.part_defaults()
    assert api.fill_parts()
    # a registry without `unreal`, whatever the relay.config of this PC names: `unreal` is a project's own server
    # (.mcp.json), and the names of a part are not held against the registry (§3.4) — the loop below passes so
    checks = dataclasses.replace(checks, mcp_names=checks.mcp_names - {"unreal"})
    assert "unreal" in defaults["program"]["default_mcp"] and "unreal" in defaults["design_setup"]["default_mcp"]
    named = yaml.safe_dump({"version": 1, "department": "program", "mcp": ["unreal"]})  # written in the yaml: §9.4 의 6
    assert [problem["code"] for problem in employees.check(
        named, department_id="program", department=world.store.get_department("program"), checks=checks)] == [
        "mcp_unknown"]
    for p in PARTS:
        part = world.store.get_department(p)
        assert part["default_relay"] == RELAYS[p] and checks.relay_known(part["default_relay"])
        assert employees.defaults_problems(p, relay=part["default_relay"], mcp=None, models=part["default_models"],
                                           checks=checks) == []
        bare = {"version": 1, "department": p}  # an employee.yaml that writes none of the keys
        assert employees.check(yaml.safe_dump(bare), department_id=p, department=part, checks=checks) == []
        relay = yaml.safe_load((Path(world.cfg.relays_dir) / f"{STAGES[p]}.yaml").read_text(encoding="utf-8"))
        assert set(part["default_models"]["work"]) <= {stage["name"] for stage in relay["stages"]}
        got = employees.resolve(bare, {"id": "emp_x", "department_id": p}, part)
        models = defaults[p]["default_models"]
        assert (got["relay"], got["mcp"], got["chat"], got["cap"]) == (
            RELAYS[p], defaults[p]["default_mcp"], models["chat"], models["cap"])
        assert got["work"] == models["work"] and "*" not in got["work"]
        # a cell of the part cannot stay empty (the next start fills it): one employee's yaml empties its own
        assert employees.resolve({**bare, "mcp": []}, {"id": "emp_x", "department_id": p}, part)["mcp"] == []

    def codes(**given):
        given = {"relay": None, "mcp": None, "models": {}, **given}
        return [problem["code"] for problem in employees.defaults_problems("program", checks=checks, **given)]

    assert codes() == []
    assert codes(relay="no-such-relay") == ["relay_unknown"]
    assert codes(mcp=["studio"]) == ["mcp_unknown"]
    assert codes(mcp=["no_such_server"]) == ["mcp_unknown"]  # held against the registry only when `mcp` is given
    assert codes(models={"tier": {}}) == ["unknown_key"]
    assert codes(models={"tier": {}, "cap": {"usd": 5}}) == ["unknown_key"]
    assert codes(models=["chat"]) == ["yaml_invalid"]
    assert codes(models={"chat": {"model": "sonnet", "effort": "extreme"}}) == ["effort_unsupported"]
    assert codes(models={"chat": {"model": "opus"}, "cap": {"model": "sonnet"}}) == ["over_cap"]


# --- what HR is told when it hires --------------------------------------------------------------------------------
def test_a_hire_is_told_the_defaults_and_the_frame_of_each_part(world):
    store, checks, defaults = world.store, employees.context(world.cfg), employees.part_defaults()
    bare = employees.hiring_notes(store, checks)
    assert bare.count("(틀 없음 — 범용 틀)") == 3
    assert "- `program` relay auto · mcp 없음 · work 없음 · cap 없음" in bare.splitlines()
    api.fill_parts()
    text = employees.hiring_notes(store, checks)
    assert "틀 없음" not in text and all(defaults[p]["guide_template"].strip() in text for p in PARTS)
    line = next(line for line in text.splitlines() if line.startswith("- `program` relay "))
    assert line.startswith("- `program` relay auto · mcp unreal · work ") and line.endswith(" · cap opus/high")
    assert "plan=opus/high" in line and "scout=sonnet/low" in line
    assert "- `design_doc` relay doc-write · mcp docs_read · work " in text
    assert "`hr` " not in text  # HR hires no HR: neither its defaults nor its guide


# --- a part of the owner's (§6.4) ---------------------------------------------------------------------------------
def test_a_part_of_the_owners_comes_with_its_room_and_is_listed(world):
    body = {"name": " 사운드 ", "default_relay": "quick", "default_mcp": ["unreal", "unreal"],
            "default_models": {"chat": {"model": "sonnet", "effort": "low"}}, "guide_template": "# 지침\n"}
    r = world.owner.post("/studio/departments", json=body)
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["id"].startswith("dep_") and (row["name"], row["builtin"], row["archived_at"]) == ("사운드", False, None)
    assert (row["default_relay"], row["default_mcp"], row["sort_order"]) == ("quick", ["unreal"], 4)
    assert (row["default_models"], row["guide_template"]) == (body["default_models"], "# 지침\n")
    items = world.owner.get("/studio/departments").json()["items"]
    assert len(items) == 5 and items[-1] == row
    room = part_rooms(world)[row["id"]]
    assert room["title"] == "사운드" and room["id"].startswith("room_") and len(part_rooms(world)) == 5
    assert len(world.events("room.updated")) == 1
    notes = employees.hiring_notes(world.store, employees.context(world.cfg)).splitlines()
    assert f"- `{row['id']}` 사운드 (chat 기본값: sonnet · low)" in notes  # a hire can be of it

    bare = world.owner.post("/studio/departments", json={"name": "QA"})
    assert bare.status_code == 201, bare.text
    bare = bare.json()
    assert (bare["default_relay"], bare["default_mcp"], bare["default_models"], bare["guide_template"]) == (
        None, [], {}, "")
    assert bare["sort_order"] == 5 and len(world.events("room.updated")) == 2


def test_a_part_is_refused_for_its_name_and_its_defaults(world):
    def post(**body):
        return world.owner.post("/studio/departments", json=body)

    assert no(post(name="프로그램"), "conflict")["field"] == "name"
    assert no(post(name="  "), "missing_fields")["fields"] == ["name"]
    assert no(post(name="사운드", default_relay="no-such-relay"), "precondition_failed")["failed"] == ["relay_unknown"]
    bad = post(name="사운드", default_models={"chat": {"effort": "extreme"}})
    assert no(bad, "precondition_failed")["failed"] == ["effort_unsupported"]
    assert no(post(name="사운드", default_models={"tier": {}}), "precondition_failed")["failed"] == ["unknown_key"]
    assert no(post(name="사운드", default_mcp=["studio"]), "precondition_failed")["failed"] == ["mcp_unknown"]
    assert len(world.owner.get("/studio/departments").json()["items"]) == 4 and len(part_rooms(world)) == 4
    assert world.store.list_stream_events() == []


def test_a_part_is_patched_and_its_room_follows_the_name(world):
    row = world.owner.post("/studio/departments", json={"name": "사운드"}).json()
    url = f"/studio/departments/{row['id']}"

    def patch(path=url, **body):
        return world.owner.patch(path, json=body)

    r = patch(name="오디오", default_relay="quick", guide_template="# 지침\n")
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["default_relay"], r.json()["guide_template"]) == ("오디오", "quick", "# 지침\n")
    assert part_rooms(world)[row["id"]]["title"] == "오디오" and len(world.events("room.updated")) == 2
    again = patch(name=" 오디오 ", default_relay="quick")
    assert again.json() == r.json() and len(world.events("room.updated")) == 2  # nothing changes: no write
    assert patch(default_relay=None).json()["default_relay"] is None and len(world.events("room.updated")) == 2

    assert no(patch(name="프로그램"), "conflict")["field"] == "name"
    assert no(patch(name=""), "missing_fields")["fields"] == ["name"]
    assert no(patch(default_models={"cap": {"model": "gpt"}}), "precondition_failed")["failed"] == ["cap_invalid"]
    no(patch("/studio/departments/dep_none", name="없는 파트"), "not_found")
    assert world.store.get_department(row["id"])["name"] == "오디오"

    # `default_mcp` is not held against the MCP registry (§3.4): a part belongs to no project, and a server of a
    # project's .mcp.json (`unreal`) is in no registry. A name nobody has shows in the run, as `mcp_missing`
    loose = patch(default_mcp=["unreal", "no_such_server"])
    assert loose.status_code == 200 and loose.json()["default_mcp"] == ["unreal", "no_such_server"], loose.text
    assert "no_such_server" not in world.cfg.mcp_registry
    assert world.store.get_department(row["id"])["default_mcp"] == ["unreal", "no_such_server"]

    # a builtin part is patched as well, and what the owner wrote stays over a start
    r = patch("/studio/departments/program", default_mcp=["docs_read"], default_models={"chat": {"model": "opus"}})
    assert r.status_code == 200 and r.json()["builtin"] is True
    assert [part["id"] for part in api.fill_parts()] == list(PARTS)
    part = world.store.get_department("program")
    assert (part["default_mcp"], part["default_models"]) == (["docs_read"], {"chat": {"model": "opus"}})
    assert part["guide_template"] == employees.part_defaults()["program"]["guide_template"]


def test_a_part_is_archived_only_when_it_is_the_owners_and_nobody_is_in_it(world):
    for builtin in ("hr", "program"):
        no(world.owner.post(f"/studio/departments/{builtin}/archive"), "precondition_failed")
    row = world.owner.post("/studio/departments", json={"name": "사운드"}).json()
    url = f"/studio/departments/{row['id']}/archive"
    staff = world.store.create_employee("소리", row["id"])
    no(world.owner.post(url), "precondition_failed")  # an employee in service
    assert world.store.get_department(row["id"])["archived_at"] is None and row["id"] in part_rooms(world)

    world.store.update_employee(staff["id"], retired_at=now())
    r = world.owner.post(url)
    assert r.status_code == 200 and r.json()["archived_at"], r.text
    assert row["id"] not in {part["id"] for part in world.owner.get("/studio/departments").json()["items"]}
    assert row["id"] not in part_rooms(world) and part_rooms(world, archived=1)[row["id"]]["archived_at"]
    assert len(world.events("room.updated")) == 2  # made, archived
    again = world.owner.post(url)
    assert again.json() == r.json() and len(world.events("room.updated")) == 2  # the same row, no second event
    assert row["id"] not in employees.hiring_notes(world.store, employees.context(world.cfg))
    assert no(world.owner.post("/studio/departments", json={"name": "사운드"}), "conflict")["field"] == "name"
    no(world.owner.post("/studio/departments/dep_none/archive"), "not_found")


def test_an_archived_part_takes_no_hire_draft(world):
    checks = employees.context(world.cfg)
    row = world.owner.post("/studio/departments", json={"name": "사운드", "default_relay": "quick"}).json()
    body = {"name": "다온", "department_id": row["id"], "persona_md": "# 다온\n", "guide_md": "# 지침\n",
            "employee_yaml": f"version: 1\ndepartment: {row['id']}\n"}
    assert drafts.hire_problems(world.store, body, checks) == []  # while the part is there it takes the draft
    r = world.owner.post(f"/studio/departments/{row['id']}/archive")
    assert r.status_code == 200 and r.json()["archived_at"], r.text
    assert [problem["code"] for problem in drafts.hire_problems(world.store, body, checks)] == [
        "department_not_found"]  # the same body, refused once the part is archived (§5.5)
