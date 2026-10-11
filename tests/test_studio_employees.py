"""Employee folders and employee.yaml (S-7, 1/2): the eleven checks of §9.4 and their codes, the part's
defaults (§9.2 · §9.6), the values of a chat turn (§9.5), the order of the prompt and the 4 KB of MEMORY.md
(§9.1), the derived status (§4.9), the seed employee (§2.2 · §3.15) and the two approvals that write an
employee.yaml (§9.6). No run and no AI is started."""
import hashlib
import re
import tomllib
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from relay_agent.config import Config
from relay_agent.providers import ProviderRegistry
from relay_agent.studio import Actor, Inbox, StudioStore, api, employees
from relay_agent.studio.store import now
from test_studio_inbox import AUTH, DOC, DRAFT, TOKEN, ok, refused, world  # noqa: F401 - `world` is its fixture

BASE = {"version": 1, "department": "program"}
PART = {"id": "program", "default_relay": "auto", "default_mcp": ["unreal"],
        "default_models": {"chat": {"model": "sonnet", "effort": "low"}, "work": {"*": {"model": "sonnet"}},
                           "cap": {"effort": "high"}}}
EMP = {"id": "emp_1", "department_id": "program"}


def dump(data):
    return yaml.safe_dump(data, allow_unicode=True)


def section(start, end):
    text = DOC.read_text(encoding="utf-8")
    return text[text.index(start):text.index(end)]


@pytest.fixture
def checks(tmp_path):
    """A relay folder with one relay, three MCP names, and a Claude runner whose own models take `low` only."""
    relays = tmp_path / "relays"
    relays.mkdir()
    (relays / "quick.yaml").write_text("name: quick\n", encoding="utf-8")
    return employees.Checks(relays, frozenset({"unreal", "docs_read", "studio"}),
                            ProviderRegistry({"claude": {"efforts": ["low"]}}))


def codes(text, checks, department=PART):
    text = text if isinstance(text, (str, bytes)) else dump(text)
    out = employees.check(text, department_id="program", department=department, checks=checks)
    assert all(p["message"] for p in out)
    return [p["code"] for p in out]


def put(studio, text=None, employee_id="emp_1", **files):
    folder = employees.folder(studio, employee_id)
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in {**({"employee.yaml": text} if text is not None else {}), **files}.items():
        (folder / name.replace("_md", ".md").replace("PERSONA", "PERSONA")).write_bytes(body.encode("utf-8"))
    return folder


# --- the checks (§9.4) ----------------------------------------------------------------------------------------
CASES = [
    ("yaml_invalid", "a: [1"), ("yaml_invalid", "- a\n- b\n"), ("yaml_invalid", ""), ("yaml_invalid", b"\xff\xfe"),
    ("yaml_invalid", None), ("yaml_invalid", {**BASE, "chat": "sonnet"}), ("yaml_invalid", {**BASE, "work": ["build"]}),
    ("yaml_invalid", {**BASE, "work": {"build": "opus"}}), ("yaml_invalid", {**BASE, "chat": {"model": 5}}),
    ("unknown_key", {**BASE, "name": "민수"}), ("unknown_key", {**BASE, "chat": {"tier": "opus"}}),
    ("unknown_key", {**BASE, "work": {"*": {"budget": 1}}}), ("unknown_key", {**BASE, "cap": {"usd": 5}}),
    ("version_unsupported", {"department": "program"}), ("version_unsupported", {**BASE, "version": 2}),
    ("version_unsupported", {**BASE, "version": "1"}), ("version_unsupported", {**BASE, "version": True}),
    ("department_mismatch", {"version": 1}), ("department_mismatch", {**BASE, "department": "hr"}),
    ("relay_unknown", {**BASE, "relay": "nowhere"}), ("relay_unknown", {**BASE, "relay": "../relays/quick"}),
    ("relay_unknown", {**BASE, "relay": 3}),
    ("mcp_unknown", {**BASE, "mcp": ["nope"]}), ("mcp_unknown", {**BASE, "mcp": ["studio"]}),
    ("mcp_unknown", {**BASE, "mcp": "unreal"}),
    ("chat_mcp_not_subset", {**BASE, "mcp": ["docs_read"], "chat_mcp": ["unreal"]}),
    ("chat_mcp_not_subset", {**BASE, "chat_mcp": ["docs_read"]}),  # no `mcp`: the part's ["unreal"] is the set
    ("chat_mcp_not_subset", {**BASE, "chat_mcp": "unreal"}),
    ("provider_unguarded", {**BASE, "chat": {"provider": "codex"}}),
    ("provider_unguarded", {**BASE, "work": {"build": {"provider": "nobody"}}}),
    ("effort_unsupported", {**BASE, "chat": {"effort": "ultra"}}),
    ("effort_unsupported", {**BASE, "work": {"*": {"model": "my-model", "effort": "high"}}}),  # efforts_for: low
    ("cap_invalid", {**BASE, "cap": {"model": "gpt"}}), ("cap_invalid", {**BASE, "cap": {"effort": "ultra"}}),
    ("cap_invalid", {**BASE, "cap": "opus"}),
    ("over_cap", {**BASE, "cap": {"model": "sonnet"}, "chat": {"model": "opus"}}),
    ("over_cap", {**BASE, "cap": {"effort": "medium"}, "work": {"*": {"effort": "high"}}}),
    ("over_cap", {**BASE, "cap": {"model": "opus"}, "chat": {"model": "claude-mythos-5"}}),  # mythos counts as fable
]


@pytest.mark.parametrize("code, text", CASES)
def test_each_check_has_its_code(checks, code, text):
    assert codes(text, checks) == [code]


def test_the_codes_are_the_eleven_of_9_4_in_the_order_of_the_table(checks):
    table = re.findall(r"^\| \d+ \| .* \| `(\w+)` \|$", section("### 9.4 검사", "### 9.5 값이 정해지는 순서"), re.M)
    assert table == list(employees.CODES) and len(table) == 11 and {code for code, _ in CASES} == set(table)
    every = {"version": 2, "department": "hr", "relay": "nowhere", "mcp": ["nope"], "chat_mcp": ["docs_read"],
             "chat": {"provider": "codex", "model": "opus", "effort": "ultra"}, "work": {"*": {"provider": "codex"}},
             "cap": {"model": "sonnet", "effort": "zzz"}, "salary": 1}
    assert codes(every, checks) == table[1:]  # each once, by its number; check 1 stands alone
    assert codes({**BASE, "chat": 3, "relay": "nowhere"}, checks) == ["yaml_invalid", "relay_unknown"]


def test_what_may_be_written_passes(checks):
    example = re.search(r"```yaml\n(.*?)```", section("### 9.3 예시", "### 9.4 검사"), re.S).group(1)
    assert codes(example, checks) == []
    for good in (BASE, {**BASE, "relay": "quick"}, {**BASE, "relay": "auto"}, {**BASE, "chat_mcp": ["unreal"]},
                 {**BASE, "mcp": [], "chat_mcp": []}, {**BASE, "mcp": ["docs_read", "unreal"], "chat_mcp": ["unreal"]},
                 {**BASE, "chat": {"provider": "claude", "model": "opus", "effort": "max"}},
                 {**BASE, "chat": {"provider": "", "model": "haiku", "effort": "high"}},  # haiku: no list, 9 passes
                 {**BASE, "work": {"*": {"model": "my-model", "effort": "low"}, "build": None}},
                 {**BASE, "cap": {"model": "opus", "effort": "xhigh"}, "chat": {"model": "opus", "effort": "xhigh"}},
                 {**BASE, "relay": None, "mcp": None, "chat_mcp": None, "chat": None, "work": None, "cap": None}):
        assert codes(good, checks) == [], good
    assert codes({**BASE, "chat_mcp": ["unreal"]}, checks, department=None) == ["chat_mcp_not_subset"]  # no part
    assert [p["code"] for p in employees.memory_problems("가" * 1366)] == ["memory_too_large"]  # 4098 bytes
    assert employees.memory_problems("가" * 1365) == []


def test_the_context_of_a_server_config():
    made = employees.context(Config())
    assert made.relay_known("default") and made.relay_known("auto") and not made.relay_known("nowhere")
    assert {"studio", "docs_read", "handoff"} <= made.mcp_names
    assert [made.guarded(p) for p in (None, "", "claude", "codex", "anthropic_api", "nobody", 3)] == [
        True, True, True, False, False, False, False]


# --- reading (§9.2 · §9.5 · §9.6) -----------------------------------------------------------------------------
def test_a_missing_key_takes_the_parts_value_and_nothing_is_written_back(tmp_path):
    folder = put(tmp_path, "version: 1\ndepartment: hr\n")  # the part by hand: the DB's stands (§9.6)
    got = employees.read_settings(tmp_path, EMP, PART, {"model": "opus", "effort": "low"})
    assert got == {"department": "program", "relay": "auto", "mcp": ["unreal"], "chat_mcp": [],
                   "chat": {"model": "sonnet", "effort": "low"}, "work": {"*": {"model": "sonnet"}},
                   "cap": {"model": "opus", "effort": "high"}}  # cap: the part's effort, the config's model
    assert (folder / "employee.yaml").read_bytes() == b"version: 1\ndepartment: hr\n"

    put(tmp_path, dump({**BASE, "relay": "quick", "mcp": ["docs_read", "unreal"], "chat_mcp": ["unreal"],
                        "chat": {"model": "opus"}, "work": {"build": {"model": "opus", "effort": "high"}},
                        "cap": {"model": "sonnet"}}))
    assert employees.read_settings(tmp_path, EMP, PART) == {
        "department": "program", "relay": "quick", "mcp": ["docs_read", "unreal"], "chat_mcp": ["unreal"],
        "chat": {"model": "opus"}, "work": {"build": {"model": "opus", "effort": "high"}},
        "cap": {"model": "sonnet", "effort": "high"}}
    put(tmp_path, dump(BASE))
    assert employees.read_settings(tmp_path, EMP, None) == {
        "department": "program", "relay": None, "mcp": [], "chat_mcp": [], "chat": {}, "work": {},
        "cap": {"model": None, "effort": None}}
    put(tmp_path, dump({**BASE, "mcp": ["docs_read"], "chat_mcp": ["unreal"], "relay": "nowhere"}))  # by hand
    got = employees.read_settings(tmp_path, EMP, PART)
    assert (got["relay"], got["mcp"], got["chat_mcp"]) == ("nowhere", ["docs_read"], [])  # 4–11 do not stop a read


def test_a_file_that_fails_checks_1_to_3_cannot_be_read(tmp_path):
    with pytest.raises(employees.Unreadable) as e:  # no file at all
        employees.read_settings(tmp_path, EMP, PART)
    assert e.value.codes == ["yaml_invalid"] and str(e.value)
    for text, failed in (("a: [1", ["yaml_invalid"]), ("version: 2\ndepartment: program\n", ["version_unsupported"]),
                         ("version: 1\ndepartment: program\nname: x\n", ["unknown_key"]),
                         ("version: 1\ndepartment: program\nchat: sonnet\n", ["yaml_invalid"]),
                         ("version: 3\nname: x\n", ["unknown_key", "version_unsupported"])):
        put(tmp_path, text)
        with pytest.raises(employees.Unreadable) as e:
            employees.read_settings(tmp_path, EMP, PART)
        assert e.value.codes == failed, text
    (employees.folder(tmp_path, "emp_1") / "employee.yaml").write_bytes(b"\xff\xfeversion: 1")
    with pytest.raises(employees.Unreadable):
        employees.read_settings(tmp_path, EMP, PART)


def test_a_chat_turn_takes_the_chat_slot_under_the_cap_and_chat_mcp_plus_studio(checks):
    settings = {"chat": {"model": "opus", "effort": "xhigh"}, "chat_mcp": ["unreal"],
                "cap": {"model": "sonnet", "effort": "medium"}}
    assert employees.chat_values(settings, checks) == {
        "slot": {"model": "sonnet", "effort": "medium"}, "mcp": ["unreal", "studio"], "model_cap": "sonnet",
        "effort_cap": "medium", "failed": []}
    assert settings["chat"] == {"model": "opus", "effort": "xhigh"}  # the settings are not changed
    low = employees.chat_values({**settings, "cap": {"model": "haiku", "effort": None}}, checks)
    assert low["slot"] == {"model": "haiku"} and low["effort_cap"] is None  # lowered to haiku: no effort
    free = employees.chat_values({"chat": {}, "chat_mcp": [], "cap": {"model": None, "effort": None}}, checks)
    assert free == {"slot": {}, "mcp": ["studio"], "model_cap": None, "effort_cap": None, "failed": []}
    other = employees.chat_values({**settings, "chat": {"provider": "codex", "model": "gpt-5"}}, checks)
    assert other["failed"] == ["provider_unguarded"] and other["slot"]["model"] == "gpt-5"  # not a Claude tier


# --- the prompt (§9.1) ----------------------------------------------------------------------------------------
def test_memory_over_4kb_is_cut_from_the_back_by_whole_lines():
    assert employees.clip_memory("") == "" and employees.clip_memory("a" * 4096) == "a" * 4096
    lines = [f"- {n:04d} 배운 점이다\n" for n in range(400)]  # 24 bytes a line
    text = "".join(lines)
    cut = employees.clip_memory(text)
    kept = cut.count("\n")
    assert cut == "".join(lines[-kept:]) and len(cut.encode("utf-8")) <= 4096 < len(text.encode("utf-8"))
    assert len("".join(lines[-kept - 1:]).encode("utf-8")) > 4096  # one more line would not fit
    long = "가" * 3000 + "끝"  # one line of 9003 bytes
    tail = employees.clip_memory("첫 줄\n" + long)
    assert long.endswith(tail) and 4096 - 3 < len(tail.encode("utf-8")) <= 4096


def test_the_prompt_is_persona_guide_memory_policy_and_the_room_in_that_order(tmp_path):
    store = StudioStore(tmp_path / "studio.sqlite")
    emp = store.create_employee("민수", "program")
    room = store.create_room("dm", title="민수", employee_id=emp["id"])["id"]
    memory = "".join(f"- {n:04d} 배운 점이다\n" for n in range(400))
    put(tmp_path, "version: 1\ndepartment: program\ncap: {model: opus}\n", emp["id"],
        **{"PERSONA.md": "나는 민수다\n", "GUIDE.md": "먼저 묻는다\n", "MEMORY.md": memory})
    (tmp_path / "projects" / "prj_1").mkdir(parents=True)
    (tmp_path / "projects" / "prj_1" / "POLICY.md").write_bytes("구조는 결재를 받는다\n".encode("utf-8"))
    for n in range(25):
        who = {"author_id": emp["id"]} if n % 2 else {}
        store.create_message(room, "employee" if n % 2 else "owner", body=f"글{n}\n" + "가" * 300, **who)
    store.create_message(room, "system", body="   ")  # nothing to say: left out

    text = employees.prompt_for(store, tmp_path, emp, room, "prj_1")
    marks = ["PERSONA.md", "나는 민수다", "GUIDE.md", "먼저 묻는다", "MEMORY.md", "- 0399 배운 점이다", "POLICY.md",
             "구조는 결재를 받는다", "방의 최근 글", "글6 ", "글24 "]
    assert [text.index(mark) for mark in marks] == sorted(text.index(mark) for mark in marks)
    assert "- 0000 배운 점이다" not in text and "cap:" not in text  # MEMORY.md is clipped; employee.yaml is not there
    said = text[text.index("방의 최근 글"):].splitlines()[1:]
    assert len(said) == 19 and "글5 " not in text  # the newest 20, less the empty one
    assert said[0].startswith("owner: 글6 가") and said[1].startswith("민수: 글7 가")
    assert all(len(line.split(": ", 1)[1]) == 201 and line.endswith("…") for line in said)  # 200 characters + …

    assert employees.prompt_for(store, tmp_path, emp) == employees.assemble("나는 민수다\n", "먼저 묻는다\n", memory)
    only = employees.assemble("P", "", None, "  \n", "")
    assert "P" in only and "GUIDE.md" not in only and "POLICY.md" not in only and "방의 최근 글" not in only
    assert employees.assemble(None, None, None) == ""
    store.close()


# --- the derived status (§4.9) --------------------------------------------------------------------------------
def test_the_status_is_the_first_rule_that_fits():
    cases = (("on_leave", {"working"}), ("idle", {"working", "blocked"}), ("working", {"blocked"}), ("blocked", set()))
    assert [employees.derive_status(current, held) for current, held in cases] == [
        "on_leave", "working", "blocked", "idle"]


def test_the_machine_writes_the_status_these_rules_give(world):  # noqa: F811
    who = world.task("working")["assignee_id"]  # tasks.py has the rules written out: the two must agree

    def agrees():
        row = world.store.get_employee(who)
        held = {task["status"] for task in world.store.list_tasks(assignee_id=who)}
        with world.store.transaction() as tx:
            assert world.m.recompute_employee(tx, who) is None  # already what the machine would write
        return row["status"] == employees.derive_status(row["status"], held) and row["status"]

    assert agrees() == "working"
    world.store.update_employee(who, status="on_leave")
    assert agrees() == "on_leave"
    world.store._exec("UPDATE tasks SET status = 'blocked', queued_at = NULL WHERE assignee_id = ?", (who,))
    world.store.update_employee(who, status="idle")
    with world.store.transaction() as tx:
        assert world.m.recompute_employee(tx, who) == "blocked" == employees.derive_status("idle", {"blocked"})
    world.store._exec("UPDATE tasks SET status = 'done', finished_at = ? WHERE assignee_id = ?", (now(), who))
    with world.store.transaction() as tx:
        assert world.m.recompute_employee(tx, who) == "idle" == employees.derive_status("blocked", {"done"})


# --- the seed employee (§2.2 · §3.15) -------------------------------------------------------------------------
def test_the_seed_employee_is_made_once(tmp_path):
    studio = tmp_path / "studio"
    store = StudioStore(studio / "studio.sqlite")
    made = employees.seed(store, studio)
    assert (made["id"], made["name"], made["department_id"], made["project_id"], made["status"]) == (
        employees.SEED_ID, employees.SEED_NAME, "program", None, "idle")
    folder = studio / "employees" / employees.SEED_ID
    assert sorted(p.name for p in folder.iterdir()) == sorted(employees.FILES)
    assert (folder / "MEMORY.md").read_bytes() == b"" and b"\r" not in (folder / "employee.yaml").read_bytes()
    part = store.get_department("program")
    assert employees.check((folder / "employee.yaml").read_bytes(), department_id="program", department=part,
                           checks=employees.context(Config())) == []
    assert employees.read_settings(studio, made, part)["relay"] == "auto"  # readable; the part's relay
    assert "PERSONA.md" in employees.prompt_for(store, studio, made)
    dm, = store.list_rooms(kind="dm")
    assert dm["employee_id"] == employees.SEED_ID
    assert [m["employee_id"] for m in store.list_room_members(room_id=dm["id"])] == [employees.SEED_ID]
    assert [m["employee_id"] for m in store.list_room_members(room_id="room_dep_program")] == [employees.SEED_ID]
    assert [e["type"] for e in store.list_stream_events()] == ["employee.updated", "room.updated", "room.updated"]

    (folder / "PERSONA.md").write_bytes("# 고친 정체성\n".encode("utf-8"))
    assert employees.seed(store, studio) is None  # a second call
    store.close()
    store = StudioStore(studio / "studio.sqlite")  # the server starts again
    assert employees.seed(store, studio) is None
    store.update_employee(employees.SEED_ID, retired_at=now())  # retired: it does not come back
    assert employees.seed(store, studio) is None
    assert [e["id"] for e in store.list_employees(include_retired=True)] == [employees.SEED_ID]
    assert len(store.list_rooms(kind="dm", include_archived=True)) == 1
    assert (folder / "PERSONA.md").read_bytes() == "# 고친 정체성\n".encode("utf-8")  # and nothing is written over
    store.close()


def test_no_seed_where_there_is_an_employee(tmp_path):
    store = StudioStore(tmp_path / "studio.sqlite")
    store.create_employee("민수", "program")
    assert employees.seed(store, tmp_path) is None
    assert not (tmp_path / "employees").exists() and len(store.list_employees()) == 1
    store.close()


def test_the_seed_files_are_package_data():
    """An installed server reads the seed beside the module as well: pyproject.toml lists every file of seed/."""
    root = Path(employees.__file__).resolve().parents[2]
    tool = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    assert "relay_agent.studio" in tool["packages"]
    package = employees.SEED_DIR.parent
    listed = {p for pattern in tool["package-data"]["relay_agent.studio"] for p in package.glob(pattern)}
    files = {p for p in employees.SEED_DIR.iterdir() if p.is_file()}
    assert files and files <= listed
    assert sorted(p.name for p in files) == sorted(set(employees.FILES) - {"MEMORY.md"})  # MEMORY.md starts empty


def test_a_seed_that_cannot_be_made_leaves_no_folder(tmp_path, monkeypatch):
    store = StudioStore(tmp_path / "studio.sqlite")

    def broken(*args, **fields):
        raise RuntimeError("no room")

    monkeypatch.setattr(store, "create_room", broken)
    with pytest.raises(RuntimeError):
        employees.seed(store, tmp_path)
    assert not (tmp_path / "employees" / employees.SEED_ID).exists() and store.list_employees() == []
    monkeypatch.undo()
    assert employees.seed(store, tmp_path)["id"] == employees.SEED_ID  # the next start makes it
    store.close()


def test_the_server_seeds_an_empty_studio_at_its_start_and_only_once(tmp_path, monkeypatch):
    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    api.close_store()
    try:
        assert api.seed_employee() is None and not server.cfg.studio_dir.exists()  # the store is not open yet
        startup = server.app.router.on_startup
        assert startup.index(server.recover) < startup.index(server.seed_studio_employee)  # after open_store()
        assert api.open_store()
        server.seed_studio_employee()
        server.seed_studio_employee()
        owner = TestClient(server.app, headers=AUTH)
        seed, = owner.get("/studio/employees").json()["items"]
        assert (seed["id"], seed["department_id"], seed["project_id"]) == (employees.SEED_ID, "program", None)
        assert seed["dm_room_id"]
        files = owner.get(f"/studio/employees/{seed['id']}").json()["files"]
        assert files["memory_md"] == "" and all(files[key] for key in ("persona_md", "guide_md", "employee_yaml"))
        api.close_store()  # the server stops and starts again
        assert api.open_store()
        server.seed_studio_employee()
        store = api.get_store()
        assert len(store.list_employees(include_retired=True)) == 1 and len(store.list_rooms(kind="dm")) == 1
    finally:
        api.close_store()


# --- the two approvals that write an employee.yaml (§9.6) -----------------------------------------------------
def test_a_hire_whose_yaml_fails_the_checks_stays_pending(world):  # noqa: F811
    item = world.hire(draft={**DRAFT, "employee_yaml": "version: 2\ndepartment: hr\nrelay: nowhere\n"})
    body = refused(world.decide(item, "approve"), 409, "precondition_failed")
    assert body["failed"] == ["version_unsupported", "department_mismatch", "relay_unknown"]
    assert world.item(item)["status"] == "pending" and not (world.dir / "employees").exists()
    assert world.hk.calls == [] and [e["name"] for e in world.store.list_employees()] == ["민수", "인사팀장"]
    edits = {"draft": {"employee_yaml": "version: 1\ndepartment: program\nchat: {model: sonnet}\n"}}
    ok(world.decide(item, "approve", data={"edits": edits}))  # fixed while approving
    employee, = [e for e in world.store.list_employees() if e["name"] == DRAFT["name"]]
    part = world.store.get_department("program")
    assert employees.read_settings(world.dir, employee, part)["chat"] == {"model": "sonnet"}


def test_an_amendment_of_employee_yaml_is_checked_at_the_approval(world):  # noqa: F811
    hr = Actor.employee(world.hr()[0])
    folder = world.dir / "employees" / world.emp
    folder.mkdir(parents=True)
    old = b"version: 1\ndepartment: program\n"
    (folder / "employee.yaml").write_bytes(old)

    def amend(n, text):
        payload = {"action": "guide_amend", "draft_id": f"drf_y{n}", "reason": "상한을 둔다", "file": "employee.yaml",
                   "target_employee_id": world.emp, "new_text": text, "diff": "-a\n+b",
                   "base_sha256": hashlib.sha256(old).hexdigest()}
        return world.box.create("hire", hr, title="설정 수정안", payload=payload)

    bad = amend(1, "version: 1\ndepartment: hr\ncap: {model: gpt}\n")
    assert refused(world.decide(bad, "approve"), 409, "precondition_failed")["failed"] == [
        "department_mismatch", "cap_invalid"]
    assert (folder / "employee.yaml").read_bytes() == old and world.item(bad)["status"] == "pending"
    good = amend(2, "version: 1\ndepartment: program\ncap: {model: opus}\n")
    ok(world.decide(good, "approve"))
    assert (folder / "employee.yaml").read_bytes() == b"version: 1\ndepartment: program\ncap: {model: opus}\n"


def test_an_inbox_without_checks_does_not_skip_them(world):  # noqa: F811
    bare = Inbox(world.store, world.m, world.hk, studio_dir=world.dir)
    item = world.hire()
    with pytest.raises(RuntimeError):
        bare.decide(item["id"], "approve")
    assert world.item(item)["status"] == "pending" and not (world.dir / "employees").exists()
