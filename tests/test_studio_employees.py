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
from relay_agent.pipeline import EFFORT_ORDER, MODEL_ORDER
from relay_agent.providers import ProviderRegistry
from relay_agent.studio import Actor, Inbox, StudioStore, api, employees
from relay_agent.studio.inbox import DRAFT_KEYS
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


# --- the HR employee's own files (H-1 · H-2) ------------------------------------------------------------------
HR_STEPS = ("무엇을 맡길지", "담당 범위와 금지", "결재 기준", "일하는 방식 · 말투", "모델 · 노력치")  # H-2, this order
HIRED_PARTS = ("program", "design_doc", "design_setup")  # the parts of the default DB a hire can be of
HR_GUIDE_BUDGET = 5632 # bytes of UTF-8: this test's budget for the packaged hr/GUIDE.md, not a limit of the server
HR_NO_EFFORT = "노력치가 없다고 적힌 모델에는 `effort` 를 적지 않는다"  # haiku: a work slot would refuse the pair


def test_the_hr_files_are_package_data():
    """As the seed's: pyproject.toml lists every file of hr/, and `hr_texts` reads the three beside the module."""
    root = Path(employees.__file__).resolve().parents[2]
    tool = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    package = employees.HR_DIR.parent
    listed = {p for pattern in tool["package-data"]["relay_agent.studio"] for p in package.glob(pattern)}
    files = {p for p in employees.HR_DIR.iterdir() if p.is_file()}
    assert files and files <= listed
    assert sorted(p.name for p in files) == sorted(set(employees.FILES) - {"MEMORY.md"})
    texts = employees.hr_texts()
    assert tuple(texts) == employees.FILES and texts["MEMORY.md"] == ""
    assert all(texts[name].strip() for name in employees.FILES if name != "MEMORY.md")
    assert texts["PERSONA.md"].startswith(f"# {employees.HR_NAME}\n")  # the name and the title change together
    assert "임시" not in texts["PERSONA.md"] + texts["GUIDE.md"]  # the original: HR's files are not written over


def test_the_hr_employee_yaml_passes_the_checks(tmp_path):
    store = StudioStore(tmp_path / "studio.sqlite")
    try:
        text, part = employees.hr_texts()["employee.yaml"], store.get_department("hr")
        assert part is not None
        assert employees.check(text, department_id="hr", department=part, checks=employees.context(Config())) == []
        data = yaml.safe_load(text)
        assert set(data) <= {"version", "department", "chat"} and data["department"] == "hr"  # the rest: the part's
        assert all(store.get_department(part_id) is not None for part_id in HIRED_PARTS)
        context = employees.context(Config())
        chats = [{"model": model, "effort": effort} for model in MODEL_ORDER for effort in EFFORT_ORDER]
        for part_id in HIRED_PARTS:  # what the guide lets HR write for a hire: every pair, and haiku with no effort
            for chat in chats + [{"model": "haiku"}]:
                written = dump({"version": 1, "department": part_id, "chat": chat})
                assert employees.check(written, department_id=part_id, department=store.get_department(part_id),
                                       checks=context) == [], (part_id, chat)
    finally:
        store.close()


def test_the_hr_guide_holds_the_interview_and_the_two_tools():
    guide = employees.hr_texts()["GUIDE.md"]
    assert len(guide.encode("utf-8")) <= HR_GUIDE_BUDGET  # it is in the prompt of every turn (§9.1)
    assert HR_NO_EFFORT in guide
    assert "`employee_draft`" in guide and "`employee_submit`" in guide
    interview = guide.split("## 면담", 1)[1].split("\n## ", 1)[0]
    numbered = [line.split(". ", 1)[1] for line in interview.splitlines() if re.match(r"\d+\. ", line)]
    assert len(numbered) == len(HR_STEPS)
    assert all(line.startswith(step) for line, step in zip(numbered, HR_STEPS, strict=True))
    assert "한 번에 한 질문" in interview and "그냥 프로그래머 한 명" in interview
    assert all(f"`{key}`" in guide for key in DRAFT_KEYS + ("draft_id", "reason"))  # the arguments as they are
    # the part ids and the models are the section's — this PC's store and catalog —: the guide spells none out
    assert f"'{employees.HIRING_TITLE}' 절" in guide
    assert [word for word in (*HIRED_PARTS, *MODEL_ORDER) if word in guide] == []
    frame = guide[guide.index("```"):guide.rindex("```")]  # the empty frame of a new employee's GUIDE (H-1)
    assert "## 결재 기준" in frame and "`ask_owner`" in frame and "`task_request_review`" in frame
    assert "ask_owner" not in guide.replace(frame, "")  # HR's chat turn has no task: ask_owner would be refused


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


# --- the HR employee every studio has (H-2; §3.5 · §3.15) -----------------------------------------------------
def test_the_hr_employee_is_made_once(tmp_path):
    studio = tmp_path / "studio"
    store = StudioStore(studio / "studio.sqlite")
    made = employees.ensure_hr(store, studio)
    assert (made["id"], made["name"], made["department_id"], made["project_id"], made["status"]) == (
        employees.HR_ID, employees.HR_NAME, "hr", None, "idle")
    folder, texts = studio / "employees" / employees.HR_ID, employees.hr_texts()
    assert sorted(p.name for p in folder.iterdir()) == sorted(employees.FILES)
    assert all((folder / name).read_bytes() == texts[name].encode("utf-8") for name in employees.FILES)
    assert (folder / "MEMORY.md").read_bytes() == b"" and b"\r" not in (folder / "employee.yaml").read_bytes()
    chat = employees.read_settings(studio, made, store.get_department("hr"))["chat"]
    assert chat["model"] == yaml.safe_load(texts["employee.yaml"])["chat"]["model"]  # readable
    assert "PERSONA.md" in employees.prompt_for(store, studio, made)
    dm, = store.list_rooms(kind="dm")
    assert (dm["employee_id"], dm["title"]) == (employees.HR_ID, employees.HR_NAME)
    assert [m["employee_id"] for m in store.list_room_members(room_id=dm["id"])] == [employees.HR_ID]
    assert [m["employee_id"] for m in store.list_room_members(room_id="room_dep_hr")] == [employees.HR_ID]
    assert [e["type"] for e in store.list_stream_events()] == ["employee.updated", "room.updated", "room.updated"]

    (folder / "PERSONA.md").write_bytes("# 고친 정체성\n".encode("utf-8"))
    store.update_employee(employees.HR_ID, name="김인사")
    assert employees.ensure_hr(store, studio) is None  # a second call
    store.close()
    store = StudioStore(studio / "studio.sqlite")  # the server starts again
    assert employees.ensure_hr(store, studio) is None
    store.update_employee(employees.HR_ID, retired_at=now())  # retired: no second HR comes in its place
    assert employees.ensure_hr(store, studio) is None
    assert [(e["id"], e["name"]) for e in store.list_employees(include_retired=True)] == [(employees.HR_ID, "김인사")]
    assert len(store.list_rooms(kind="dm", include_archived=True)) == 1
    assert (folder / "PERSONA.md").read_bytes() == "# 고친 정체성\n".encode("utf-8")  # and nothing is written over
    store.close()


def test_an_hr_guide_older_than_the_section_of_the_values_is_named_and_left_as_it_is(tmp_path):
    studio = tmp_path / "studio"
    store = StudioStore(studio / "studio.sqlite")
    try:
        assert employees.hr_guides_behind(store, studio) == []  # no HR yet
        employees.ensure_hr(store, studio)
        assert employees.hr_guides_behind(store, studio) == []  # the original names the section
        guide = studio / "employees" / employees.HR_ID / "GUIDE.md"
        old = "# 지침\n\n- 결정은 이 방으로 온다. 반려되면 고쳐서 다시 올린다.\n".encode("utf-8")
        guide.write_bytes(old)
        assert employees.hr_guides_behind(store, studio) == [employees.HR_ID]
        assert employees.ensure_hr(store, studio) is None and guide.read_bytes() == old  # named, not written over
        guide.unlink()
        assert employees.hr_guides_behind(store, studio) == []  # no file: nothing in it is behind
        guide.write_bytes(old)
        store.update_employee(employees.HR_ID, retired_at=now())
        assert employees.hr_guides_behind(store, studio) == []  # a retired HR has no turn that reads it
    finally:
        store.close()


def test_the_hr_employee_joins_a_studio_that_has_employees_and_takes_a_free_name(tmp_path):
    store = StudioStore(tmp_path / "studio.sqlite")
    store.create_employee("민수", "program")
    gone = store.create_employee(employees.HR_NAME, "program")
    store.update_employee(gone["id"], retired_at=now())  # a retired employee's name is not free either
    store.create_employee(f"{employees.HR_NAME} 2", "design_doc")
    made = employees.ensure_hr(store, tmp_path)
    assert (made["id"], made["name"], made["department_id"], made["project_id"]) == (
        employees.HR_ID, f"{employees.HR_NAME} 3", "hr", None)
    dm, = store.list_rooms(kind="dm")
    assert (dm["employee_id"], dm["title"]) == (employees.HR_ID, made["name"])
    assert len(store.list_employees(include_retired=True)) == 4
    assert employees.ensure_hr(store, tmp_path) is None and employees.seed(store, tmp_path) is None
    assert len(store.list_employees(department_id="hr", include_retired=True)) == 1
    store.close()


def test_an_hr_employee_that_cannot_be_made_leaves_no_folder(tmp_path, monkeypatch):
    store = StudioStore(tmp_path / "studio.sqlite")

    def broken(*args, **fields):
        raise RuntimeError("no room")

    monkeypatch.setattr(store, "create_room", broken)
    with pytest.raises(RuntimeError):
        employees.ensure_hr(store, tmp_path)
    assert not (tmp_path / "employees" / employees.HR_ID).exists() and store.list_employees() == []
    left = put(tmp_path, employee_id=employees.HR_ID, GUIDE_md="# 남은 지침\n")  # a folder without its row
    with pytest.raises(RuntimeError):
        employees.ensure_hr(store, tmp_path)
    assert left.is_dir() and store.list_employees() == []  # not made here: it is not taken away
    monkeypatch.undo()
    assert employees.ensure_hr(store, tmp_path)["id"] == employees.HR_ID  # the next start makes it
    assert (left / "GUIDE.md").read_bytes() == "# 남은 지침\n".encode("utf-8")  # the file it had is kept
    assert (left / "PERSONA.md").read_bytes() == employees.hr_texts()["PERSONA.md"].encode("utf-8")
    store.close()


@pytest.mark.parametrize("maker, employee_id", [("seed", employees.SEED_ID), ("ensure_hr", employees.HR_ID)])
def test_files_that_cannot_be_written_leave_no_folder(tmp_path, monkeypatch, maker, employee_id):
    store = StudioStore(tmp_path / "studio.sqlite")
    make, target, first = getattr(employees, maker), tmp_path / "employees" / employee_id, employees.FILES[0]

    def broken(path, texts, *, keep=False):  # the folder and its first file, then the disk gives out
        path.mkdir(parents=True, exist_ok=keep)
        if not (path / first).exists():
            (path / first).write_bytes(b"half")
        raise OSError("disk full")

    monkeypatch.setattr(employees, "write_files", broken)
    with pytest.raises(OSError):
        make(store, tmp_path)
    assert not target.exists() and store.list_employees(include_retired=True) == []
    assert store.list_rooms(kind="dm") == [] and store.list_stream_events() == []
    target.mkdir(parents=True)  # a folder without its row, from an earlier start
    (target / first).write_bytes("# 남은 파일\n".encode("utf-8"))
    with pytest.raises(OSError):
        make(store, tmp_path)
    assert target.is_dir() and store.list_employees(include_retired=True) == []  # not made here: it stays
    monkeypatch.undo()
    assert make(store, tmp_path)["id"] == employee_id  # the next start makes it
    assert (target / first).read_bytes() == "# 남은 파일\n".encode("utf-8")  # the file it had is kept
    assert sorted(p.name for p in target.iterdir()) == sorted(employees.FILES)
    store.close()


def test_the_hr_part_takes_its_defaults_only_where_it_has_none(tmp_path):
    store = StudioStore(tmp_path / "studio.sqlite")
    try:
        texts, before = employees.hr_texts(), store.get_department("hr")
        assert (before["guide_template"], before["default_models"]) == ("", {})
        chat = yaml.safe_load(texts["employee.yaml"])["chat"]
        assert employees.hr_defaults() == {"guide_template": texts["GUIDE.md"], "default_models": {"chat": chat}}
        employees.fill_hr_part(store)
        part = store.get_department("hr")
        assert (part["guide_template"], part["default_models"]) == (texts["GUIDE.md"], {"chat": chat})
        assert employees.check(texts["employee.yaml"], department_id="hr", department=part,
                               checks=employees.context(Config())) == []
        assert employees.fill_hr_part(store) is None  # nothing is empty: nothing is written
        store.update_department("hr", guide_template="# 고친 지침\n", default_models={})
        employees.fill_hr_part(store)  # each on its own: the one that is empty
        part = store.get_department("hr")
        assert (part["guide_template"], part["default_models"]) == ("# 고친 지침\n", {"chat": chat})
        store.update_department("hr", default_models={"chat": {"model": "opus"}})
        assert employees.fill_hr_part(store) is None
        part = store.get_department("hr")
        assert (part["guide_template"], part["default_models"]) == ("# 고친 지침\n", {"chat": {"model": "opus"}})
        assert all(store.get_department(other)["guide_template"] == "" for other in HIRED_PARTS)  # HR's only
        assert store.list_stream_events() == []
    finally:
        store.close()


def test_the_server_makes_the_hr_employee_after_the_seed_and_only_once(tmp_path, monkeypatch):
    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    api.close_store()
    try:
        assert api.ensure_hr() is None and not server.cfg.studio_dir.exists()  # the store is not open yet
        startup = server.app.router.on_startup
        assert (startup.index(server.recover) < startup.index(server.seed_studio_employee)
                < startup.index(server.ensure_studio_hr))
        assert api.open_store()
        for _ in range(2):  # an empty DB: the seed, then HR — and a second round makes nothing
            server.seed_studio_employee()
            server.ensure_studio_hr()
        owner = TestClient(server.app, headers=AUTH)
        items = owner.get("/studio/employees").json()["items"]
        assert [(e["id"], e["department_id"], e["project_id"], e["company_wide"]) for e in items] == [
            (employees.SEED_ID, "program", None, False), (employees.HR_ID, "hr", None, True)]
        assert all(e["dm_room_id"] for e in items) and len({e["dm_room_id"] for e in items}) == 2
        hr = owner.get(f"/studio/employees/{employees.HR_ID}").json()
        assert hr["name"] == employees.HR_NAME and hr["company_wide"] is True
        assert hr["files"]["guide_md"] == employees.hr_texts()["GUIDE.md"] and hr["files"]["memory_md"] == ""
        store = api.get_store()
        part = store.get_department("hr")
        assert part["guide_template"] == hr["files"]["guide_md"] and part["default_models"]["chat"]

        # what PATCH · PUT of H-3 will write: the row's name and the file. A new start keeps both.
        store.update_employee(employees.HR_ID, name="김인사")
        guide = employees.folder(server.cfg.studio_dir, employees.HR_ID) / "GUIDE.md"
        guide.write_bytes("# 고친 지침\n".encode("utf-8"))
        api.close_store()  # the server stops and starts again
        assert api.open_store()
        server.seed_studio_employee()
        server.ensure_studio_hr()
        store = api.get_store()
        assert [(e["id"], e["name"]) for e in store.list_employees(include_retired=True)] == [
            (employees.SEED_ID, employees.SEED_NAME), (employees.HR_ID, "김인사")]
        assert len(store.list_employees(department_id="hr")) == 1 and len(store.list_rooms(kind="dm")) == 2
        again = TestClient(server.app, headers=AUTH).get(f"/studio/employees/{employees.HR_ID}").json()
        assert (again["name"], again["files"]["guide_md"]) == ("김인사", "# 고친 지침\n")
        assert guide.read_bytes() == "# 고친 지침\n".encode("utf-8")
    finally:
        api.close_store()


def test_the_server_adds_the_hr_employee_to_a_studio_that_has_employees(tmp_path, monkeypatch):
    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    api.close_store()
    try:
        assert api.open_store()
        there = api.get_store().create_employee("민수", "program")
        for _ in range(2):
            server.seed_studio_employee()
            server.ensure_studio_hr()
        store = api.get_store()
        assert [e["id"] for e in store.list_employees(include_retired=True)] == [there["id"], employees.HR_ID]
        assert not employees.folder(server.cfg.studio_dir, employees.SEED_ID).exists()  # no seed: one was there
        items = {e["id"]: e for e in TestClient(server.app, headers=AUTH).get("/studio/employees").json()["items"]}
        dm, = store.list_rooms(kind="dm")
        assert items[employees.HR_ID]["dm_room_id"] == dm["id"] and dm["employee_id"] == employees.HR_ID
        assert (items[employees.HR_ID]["company_wide"], items[there["id"]]["company_wide"]) == (True, False)
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
