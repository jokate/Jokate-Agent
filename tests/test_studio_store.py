"""Studio store (S-1): versioned schema, one-time migration and seed, CRUD per table, transactions."""
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from relay_agent.config import Config
from relay_agent.studio import StudioSchemaError, StudioStore, StudioStoreError
from relay_agent.studio.schema import MIGRATIONS
from relay_agent.studio.store import now

DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
TABLES = {"projects", "departments", "employees", "rooms", "room_members", "messages", "campaigns", "tasks",
          "task_deps", "task_events", "inbox", "devices", "stream_events", "hire_signals"}
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")


@pytest.fixture
def store(tmp_path):
    s = StudioStore(tmp_path / "studio" / "studio.sqlite")
    yield s
    s.close()


def raw(path, sql):
    """A statement on a connection of its own (what another process would see)."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def make_project(store, name="게임", **over):
    return store.create_project(name, f"D:/{name}", name.lower(), **over)


def make_task(store, project_id, **over):
    fields = dict(title="2단 점프", reason="조작감", spec="공중에서 한 번 더", done_criteria="테스트 통과",
                  type="feature", status="backlog", proposer_kind="owner")
    return store.create_task(project_id, **{**fields, **over})


# --- schema and migration -----------------------------------------------------------------------------
def test_empty_db_is_created_and_migrated(tmp_path):
    path = tmp_path / "runs" / "studio" / "studio.sqlite"
    store = StudioStore(path)
    try:
        assert path.exists() and store.migrated == [1, 2, 3, 4, 5, 6]
        assert store._exec("PRAGMA user_version").fetchone()[0] == MIGRATIONS[-1][0] == 6
        assert store._exec("PRAGMA foreign_keys").fetchone()[0] == 1
        master = store._rows("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        assert {r["name"] for r in master if r["type"] == "table"} == TABLES
        assert len([r for r in master if r["type"] == "index"]) == 21
    finally:
        store.close()


def _norm(sql):
    return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", sql)).strip()


def _doc_statements(text, start, end):
    """SQL statements of the ```sql blocks between two headings, comments and layout removed."""
    a = re.search(rf"^### {re.escape(start)} ", text, re.M).start()
    b = re.search(rf"^### {re.escape(end)} ", text, re.M).start()
    blocks = re.findall(r"```sql\n(.*?)```", text[a:b], re.S)
    return [s for block in blocks for s in map(_norm, re.sub(r"--[^\n]*", "", block).split(";")) if s]


def test_schema_is_the_documents_ddl():
    if not DOC.exists():
        pytest.skip("design/studio/ARCHITECTURE.md is not in this checkout")
    text = DOC.read_text(encoding="utf-8")
    (v1, first), (v2, second), (v3, third), (v4, fourth), (v5, fifth), (v6, sixth) = MIGRATIONS
    assert (v1, v2, v3, v4, v5, v6) == (1, 2, 3, 4, 5, 6) and len(first) == 42 and len(second) == 1
    assert len(third) == 7 and len(fourth) == 2 and len(fifth) == 12 and len(sixth) == 11
    in_3_2 = _doc_statements(text, "3.2", "3.3")
    assert [_norm(s) for s in third] == in_3_2[:7]  # rooms made again (the group rooms)
    assert [_norm(s) for s in fifth] == in_3_2[7:19]  # inbox made again (the hiring recommendation) + hire_signals
    assert [_norm(s) for s in sixth] == in_3_2[19:]  # inbox made again (the plan proposal)
    ddl = _doc_statements(text, "3.3", "3.15")  # 12 tables + 21 indexes
    assert len(ddl) == 33
    assert [_norm(s) for s in first[:33]] == ddl
    assert [_norm(first[33])] == _doc_statements(text, "3.15", "3.16")  # the seed example (hr)
    assert [_norm(s) for s in second] == _doc_statements(text, "7.2", "7.3")


def test_seed_departments_and_rooms(store):
    deps = store.list_departments()
    assert [(d["id"], d["name"], d["default_relay"], d["sort_order"]) for d in deps] == [
        ("hr", "인사", None, 0), ("design_doc", "기획(문서)", "doc-write", 1),
        ("program", "프로그램", "auto", 2), ("design_setup", "기획(세팅)", "editor-task", 3),
        ("art", "아트", "editor-task", 4)]
    assert all(d["builtin"] is True and d["default_mcp"] == [] and d["default_models"] == {} for d in deps)
    assert all(STAMP.fullmatch(d["created_at"]) and d["created_at"] == d["updated_at"] for d in deps)
    rooms = store.list_rooms()
    assert [(r["id"], r["kind"], r["title"], r["department_id"]) for r in rooms] == [
        ("room_system", "system", "시스템 공지", None), ("room_dep_hr", "department", "인사", "hr"),
        ("room_dep_design_doc", "department", "기획(문서)", "design_doc"),
        ("room_dep_program", "department", "프로그램", "program"),
        ("room_dep_design_setup", "department", "기획(세팅)", "design_setup"),
        ("room_dep_art", "department", "아트", "art")]
    assert all(r["muted"] is False and r["owner_read_id"] == 0 and STAMP.fullmatch(r["created_at"]) for r in rooms)
    assert store.list_employees() == []


def test_opening_twice_migrates_and_seeds_once(tmp_path):
    path = tmp_path / "studio.sqlite"
    first, second = StudioStore(path), StudioStore(path)
    try:
        assert first.migrated == [1, 2, 3, 4, 5, 6] and second.migrated == []
        assert len(second.list_departments()) == 5 and len(second.list_rooms()) == 6
        assert list(tmp_path.glob("*.bak-v*")) == []
        # the re-check under the write lock: a step that is already in is skipped, not run again
        assert second._apply_step(1, MIGRATIONS[0][1]) is False
        assert len(first.list_departments()) == 5
    finally:
        first.close()
        second.close()
    third = StudioStore(path)
    assert third.migrated == [] and len(third.list_rooms()) == 6
    third.close()


def test_two_servers_starting_together_migrate_once(tmp_path):
    path = tmp_path / "studio.sqlite"
    barrier, stores, errors = threading.Barrier(2), [], []

    def open_store():
        try:
            barrier.wait(5)
            stores.append(StudioStore(path))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=open_store) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    try:
        assert not errors and len(stores) == 2
        assert sorted(v for s in stores for v in s.migrated) == [1, 2, 3, 4, 5, 6]
        assert len(stores[0].list_departments()) == 5 and len(stores[1].list_rooms()) == 6
    finally:
        for s in stores:
            s.close()


def test_newer_db_is_refused(tmp_path):
    path = tmp_path / "studio.sqlite"
    StudioStore(path).close()
    raw(path, "PRAGMA user_version = 99")
    with pytest.raises(StudioSchemaError):
        StudioStore(path)
    assert raw(path, "PRAGMA user_version")[0][0] == 99
    assert list(tmp_path.glob("*.bak-v*")) == []


def test_upgrade_backs_up_and_a_failed_step_leaves_the_version(tmp_path):
    path = tmp_path / "studio.sqlite"
    old = StudioStore(path, migrations=MIGRATIONS[:1])
    assert old.migrated == [1]
    old.close()

    broken = MIGRATIONS[:1] + ((2, ("CREATE TABLE extra (a TEXT)", "INSERT INTO missing VALUES (1)")),)
    with pytest.raises(sqlite3.OperationalError):
        StudioStore(path, migrations=broken)
    assert raw(path, "PRAGMA user_version")[0][0] == 1  # §3.2 규칙: the step went back as a whole
    assert raw(path, "SELECT name FROM sqlite_master WHERE name = 'extra'") == []
    backup = tmp_path / "studio.sqlite.bak-v1"
    assert backup.exists() and raw(backup, "PRAGMA user_version")[0][0] == 1

    store = StudioStore(path)
    assert store.migrated == [2, 3, 4, 5, 6] and len(store.list_departments()) == 5
    store.close()
    # what the design relies on: user_version set inside a transaction goes back with it
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("PRAGMA user_version = 7")
    conn.execute("ROLLBACK")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    conn.close()


def test_a_late_server_does_not_overwrite_the_backup(tmp_path, monkeypatch):
    path = tmp_path / "studio.sqlite"
    StudioStore(path, migrations=MIGRATIONS[:1]).close()
    StudioStore(path).close()  # 1 -> 2: the copy is taken under the write lock, named after the version re-read there
    backup = tmp_path / "studio.sqlite.bak-v1"
    assert raw(backup, "PRAGMA user_version")[0][0] == 1

    real, calls = StudioStore._user_version, []

    def stale_first(self):  # a server that read the version before the other one finished upgrading
        calls.append(1)
        return 1 if len(calls) == 1 else real(self)

    monkeypatch.setattr(StudioStore, "_user_version", stale_first)
    late = StudioStore(path)
    try:
        assert late.migrated == [] and len(calls) == 6  # read once to plan, then once under the lock of each step
    finally:
        late.close()
    assert raw(backup, "PRAGMA user_version")[0][0] == 1  # not replaced by a copy of the upgraded file
    assert sorted(p.name for p in tmp_path.glob("*.bak-v*")) == ["studio.sqlite.bak-v1"]


# --- version 3: rooms made again for the group rooms (§3.2) -------------------------------------------
OLD_ROOM_COLUMNS = ("id, kind, title, employee_id, task_id, department_id, owner_read_id, muted,"
                    " created_at, updated_at, archived_at")


def _version_2_db(path):
    """A version 2 DB with what points at rooms and what rooms point at: employees, a task, rooms the seed did
    not make (one muted and read, one archived), members (one who left) and messages. (a 1:1 room, its employee)."""
    old = StudioStore(path, migrations=MIGRATIONS[:2])
    project = make_project(old, engine_version="5.7", mcp_port=8101)
    one = old.create_employee("김기획", "design_doc", project_id=project["id"])
    two = old.create_employee("이코드", "program")
    dm = old.create_room("dm", title="김기획", employee_id=one["id"])
    gone = old.create_room("dm", title="이코드", employee_id=two["id"])
    work = old.create_room("task", title="2단 점프", task_id=make_task(old, project["id"])["id"])
    for room, employee in ((dm, one), (gone, two), (work, one), (work, two)):
        old.create_room_member(room["id"], employee["id"], session_id="s1", conversation_id="c1")
    old.update_room_member(work["id"], two["id"], left_at=now())
    for room_id in (dm["id"], work["id"], "room_system"):
        old.create_message(room_id, "employee", author_id=one["id"], body="했어요")
        old.create_message(room_id, "owner", body="볼게요")
    old.update_room(dm["id"], muted=True, owner_read_id=2)
    old.update_room(gone["id"], archived_at=now())
    assert old.migrated == [1, 2]
    old.close()
    return dm["id"], one["id"]


def _dump(path):
    """The rows in the order they are kept, and everything of the schema but the text of the rooms table."""
    tables = {"rooms": OLD_ROOM_COLUMNS, "room_members": "*", "messages": "*", "employees": "*", "tasks": "*"}
    out = {name: raw(path, f"SELECT {columns} FROM {name} ORDER BY rowid") for name, columns in tables.items()}
    out["schema"] = raw(path, "SELECT type, name, tbl_name, sql FROM sqlite_master"
                              " WHERE NOT (type = 'table' AND name = 'rooms') ORDER BY type, name")
    return out


def test_version_3_makes_rooms_again_and_keeps_every_row_index_and_reference(tmp_path):
    path = tmp_path / "studio.sqlite"
    dm, employee = _version_2_db(path)
    before = _dump(path)
    assert [len(before[name]) for name in ("rooms", "room_members", "messages", "employees")] == [8, 4, 6, 2]
    assert "'group'" not in raw(path, "SELECT sql FROM sqlite_master WHERE name = 'rooms'")[0][0]

    store = StudioStore(path, migrations=MIGRATIONS[:3])  # the step of version 3 on its own
    try:
        assert store.migrated == [3] and _dump(path) == before
        assert raw(tmp_path / "studio.sqlite.bak-v2", "PRAGMA user_version")[0][0] == 2
        assert raw(path, "SELECT name FROM sqlite_master WHERE name = 'rooms_hold'") == []
        indexes = [r for r in before["schema"] if r[0] == "index" and not r[1].startswith("sqlite_")]
        assert len(indexes) == 21 and ("index", "rooms_one_system", "rooms") in [r[:3] for r in indexes]
        # the references: none broken, the two tables still point at rooms, and they are checked again
        assert store._exec("PRAGMA foreign_key_check").fetchall() == []
        assert store._exec("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store._exec("PRAGMA defer_foreign_keys").fetchone()[0] == 0  # it was for the step's transaction
        for child in ("messages", "room_members"):
            assert "rooms" in [r[2] for r in store._exec(f"PRAGMA foreign_key_list({child})").fetchall()]
        with pytest.raises(sqlite3.IntegrityError):
            store.create_message("room_none", "owner", body="방이 없다")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_room_member("room_none", employee)
        # the rows as the store reads them, with the new column
        rooms = store.list_rooms(include_archived=True)
        assert len(rooms) == 8 and all(r["exchange_count"] == 0 for r in rooms)
        assert (store.get_room(dm)["muted"], store.get_room(dm)["owner_read_id"]) == (True, 2)
        assert store.count_unread() == {"room_system": 1, [r["id"] for r in rooms if r["kind"] == "task"][0]: 1}
        assert len(store.list_room_members(room_id=dm)) == 1 and len(store.list_messages(dm)) == 2
        # the CHECKs: a group room now, and what was refused before still is
        first, second = store.create_room("group", title="셋"), store.create_room("group", title="셋")
        assert first["kind"] == "group" and first["id"] != second["id"] and first["exchange_count"] == 0
        assert store.update_room(first["id"], exchange_count=3)["exchange_count"] == 3
        assert {r["id"] for r in store.list_rooms(kind="group")} == {first["id"], second["id"]}
        fresh = store.create_employee("박신입", "program")["id"]
        for kind, fields in (("group", {"employee_id": fresh}), ("dm", {}), ("party", {}), ("system", {})):
            with pytest.raises(sqlite3.IntegrityError):
                store.create_room(kind, **fields)
        assert store.create_room("dm", employee_id=fresh)["employee_id"] == fresh
    finally:
        store.close()


def test_a_rooms_step_that_loses_a_room_goes_back_as_a_whole(tmp_path):
    """Why the step is safe with foreign keys on: what still points at a room that did not come back is found
    at COMMIT, and the step's transaction goes back — the table, its rows and the version."""
    path = tmp_path / "studio.sqlite"
    _version_2_db(path)
    before = _dump(path)
    lossy = tuple(s.replace("FROM rooms_hold", "FROM rooms_hold WHERE kind <> 'dm'") for s in MIGRATIONS[2][1])
    assert lossy != MIGRATIONS[2][1]
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        StudioStore(path, migrations=MIGRATIONS[:2] + ((3, lossy),))
    assert raw(path, "PRAGMA user_version")[0][0] == 2 and _dump(path) == before
    assert raw(path, "SELECT name FROM sqlite_master WHERE name = 'rooms_hold'") == []
    assert "'group'" not in raw(path, "SELECT sql FROM sqlite_master WHERE name = 'rooms'")[0][0]

    store = StudioStore(path, migrations=MIGRATIONS[:3])  # the step as it is written goes in
    assert store.migrated == [3] and _dump(path) == before
    store.close()


def test_config_derives_studio_paths_from_runs_dir(tmp_path):
    cfg = Config(runs_dir=tmp_path / "r")
    assert cfg.studio_dir == tmp_path / "r" / "studio"
    assert cfg.studio_db == tmp_path / "r" / "studio" / "studio.sqlite"


# --- CRUD per table -----------------------------------------------------------------------------------
def test_projects_crud(store):
    prj = make_project(store, engine_version="5.7", mcp_port=8090)
    assert re.fullmatch(r"prj_[0-9a-f]{8}", prj["id"]) and prj["archived_at"] is None
    assert STAMP.fullmatch(prj["created_at"]) and prj["updated_at"] == prj["created_at"]
    assert store.get_project(prj["id"]) == prj and store.get_project("prj_none") is None
    assert store.update_project(prj["id"], name="게임2", mcp_port=None)["name"] == "게임2"
    assert store.get_project(prj["id"])["mcp_port"] is None
    assert store.update_project("prj_none", name="x") is None
    with pytest.raises(ValueError):
        store.update_project(prj["id"], id="prj_other")
    with pytest.raises(ValueError):
        store.create_project("b", "D:/b", "b", nope=1)
    with pytest.raises(sqlite3.IntegrityError):  # UNIQUE name
        store.create_project("게임2", "D:/c", "c")
    with pytest.raises(sqlite3.IntegrityError):  # CHECK mcp_port
        store.create_project("d", "D:/d", "d", mcp_port=0)
    other = make_project(store, "Other", mcp_port=8091)
    with pytest.raises(sqlite3.IntegrityError):  # a port is used by one live project
        store.update_project(prj["id"], mcp_port=8091)
    store.update_project(other["id"], archived_at=now())
    assert store.update_project(prj["id"], mcp_port=8091)["mcp_port"] == 8091
    assert [p["id"] for p in store.list_projects()] == [prj["id"]]
    assert [p["id"] for p in store.list_projects(include_archived=True)] == [prj["id"], other["id"]]


def test_departments_crud(store):
    dep = store.create_department("사운드", default_relay="auto", default_mcp=["unreal"],
                                  default_models={"chat": {"model": "하이쿠"}}, sort_order=9)
    assert dep["id"].startswith("dep_") and dep["builtin"] is False
    assert dep["default_mcp"] == ["unreal"] and dep["default_models"] == {"chat": {"model": "하이쿠"}}
    assert "default_mcp_json" not in dep
    stored = store._exec("SELECT default_models_json FROM departments WHERE id = ?", (dep["id"],)).fetchone()[0]
    assert "하이쿠" in stored  # ensure_ascii=False
    assert store.update_department(dep["id"], default_mcp=[], guide_template="# 안내")["default_mcp"] == []
    assert store.get_department("hr")["builtin"] is True
    with pytest.raises(ValueError):  # builtin is fixed at creation
        store.update_department(dep["id"], builtin=True)
    store.update_department(dep["id"], archived_at=now())
    assert len(store.list_departments()) == 5
    assert store.list_departments(include_archived=True)[-1]["id"] == dep["id"]


def test_employees_crud(store):
    prj = make_project(store)
    emp = store.create_employee("김기획", "design_doc", project_id=prj["id"])
    assert emp["id"].startswith("emp_") and emp["status"] == "idle" and emp["retired_at"] is None
    assert store.get_employee(emp["id"]) == emp
    assert store.update_employee(emp["id"], status="on_leave", project_id=None)["status"] == "on_leave"
    with pytest.raises(sqlite3.IntegrityError):  # foreign keys are on
        store.create_employee("유령", "no_such_department")
    with pytest.raises(sqlite3.IntegrityError):  # CHECK status
        store.update_employee(emp["id"], status="retired")
    hr = store.create_employee("박인사", "hr")
    with pytest.raises(sqlite3.IntegrityError):  # one HR employee in the company
        store.create_employee("최인사", "hr")
    assert [e["id"] for e in store.list_employees(department_id="hr")] == [hr["id"]]
    store.update_employee(hr["id"], retired_at=now())
    assert [e["id"] for e in store.list_employees()] == [emp["id"]]
    assert len(store.list_employees(include_retired=True)) == 2
    assert store.create_employee("최인사", "hr")["department_id"] == "hr"  # the retired one frees the seat


def test_rooms_and_members_crud(store):
    emp = store.create_employee("김기획", "design_doc")
    room = store.create_room("dm", employee_id=emp["id"], title="김기획")
    assert room["id"].startswith("room_") and room["muted"] is False
    assert store.list_rooms(employee_id=emp["id"]) == [room]
    assert store.update_room(room["id"], muted=True, owner_read_id=3)["muted"] is True
    with pytest.raises(sqlite3.IntegrityError):  # a dm room names its employee
        store.create_room("dm")
    with pytest.raises(sqlite3.IntegrityError):  # one system room
        store.create_room("system")
    with pytest.raises(ValueError):
        store.update_room(room["id"], kind="task")
    store.update_room(room["id"], archived_at=now())
    assert len(store.list_rooms()) == 6 and len(store.list_rooms(include_archived=True)) == 7

    member = store.create_room_member(room["id"], emp["id"], session_id="s1", conversation_id="c1")
    assert member["fed_message_id"] == 0 and STAMP.fullmatch(member["joined_at"]) and member["left_at"] is None
    assert store.get_room_member(room["id"], emp["id"]) == member
    changed = store.update_room_member(room["id"], emp["id"], conversation_id=None, fed_message_id=7)
    assert changed["conversation_id"] is None and changed["fed_message_id"] == 7
    assert store.update_room_member(room["id"], "emp_none", fed_message_id=1) is None
    with pytest.raises(sqlite3.IntegrityError):  # one row per employee and room
        store.create_room_member(room["id"], emp["id"])
    assert len(store.list_room_members(employee_id=emp["id"])) == 1
    store.update_room_member(room["id"], emp["id"], left_at=now())
    assert store.list_room_members(room_id=room["id"]) == []
    assert len(store.list_room_members(room_id=room["id"], include_left=True)) == 1


def test_messages_crud(store):
    emp = store.create_employee("김기획", "design_doc")
    first = store.create_message("room_system", "owner", body="안녕", mentions=[emp["id"]],
                                 attachments=[{"name": "a.png", "size": 3}], meta={"event": "한글"})
    assert first["id"] == 1 and first["mentions"] == [emp["id"]] and first["meta"] == {"event": "한글"}
    assert first["attachments"] == [{"name": "a.png", "size": 3}] and first["author_id"] is None
    assert store.get_message(1) == first and store.get_message(99) is None
    assert "한글" in store._exec("SELECT meta_json FROM messages WHERE id = 1").fetchone()[0]
    for n in range(2, 6):
        store.create_message("room_system", "employee", author_id=emp["id"], body=f"글 {n}")
    store.create_message("room_dep_hr", "system", body="다른 방")
    with pytest.raises(sqlite3.IntegrityError):  # an employee's message names the employee
        store.create_message("room_system", "employee", body="누구")
    ids = lambda rows: [m["id"] for m in rows]  # noqa: E731
    assert ids(store.list_messages("room_system")) == [1, 2, 3, 4, 5]
    assert ids(store.list_messages("room_system", after_id=3)) == [4, 5]
    assert ids(store.list_messages("room_system", limit=2)) == [4, 5]  # §6.4: neither bound = the most recent
    assert ids(store.list_messages("room_system", after_id=0, limit=2)) == [1, 2]
    assert ids(store.list_messages("room_system", before_id=5, limit=2)) == [3, 4]
    assert ids(store.list_messages("room_system", after_id=1, before_id=5, limit=2)) == [3, 4]
    assert ids(store.list_messages("room_dep_hr")) == [6]


def test_campaigns_crud(store):
    prj = make_project(store)
    cmp_ = store.create_campaign(prj["id"], "1차", "점프를 완성한다")
    assert cmp_["id"].startswith("cmp_") and cmp_["mode"] == "milestone" and cmp_["status"] == "draft"
    assert store.get_campaign(cmp_["id"]) == cmp_
    running = store.update_campaign(cmp_["id"], status="running", mode="each", started_at=now())
    assert running["status"] == "running" and running["mode"] == "each" and running["started_at"]
    with pytest.raises(sqlite3.IntegrityError):  # blank title
        store.create_campaign(prj["id"], "  ", "목표")
    with pytest.raises(sqlite3.IntegrityError):  # CHECK mode
        store.update_campaign(cmp_["id"], mode="fast")
    assert store.list_campaigns(project_id=prj["id"], status="running") == [running]
    assert store.list_campaigns(status="draft") == []


def test_tasks_deps_and_events_crud(store):
    prj = make_project(store)
    emp = store.create_employee("이개발", "program", project_id=prj["id"])
    cmp_ = store.create_campaign(prj["id"], "1차", "점프")
    task = make_task(store, prj["id"], campaign_id=cmp_["id"], is_milestone=True, sort_order=2,
                     stage_models={"build": {"model": "opus"}})
    assert task["id"].startswith("task_") and task["status"] == "backlog" and task["is_milestone"] is True
    assert task["stage_models"] == {"build": {"model": "opus"}} and task["upstream_rejected"] is False
    assert task["exchange_count"] == 0 and task["run_id"] is None and task["finished_at"] is None
    other = make_task(store, prj["id"], title="대시", status="proposed", proposer_kind="employee",
                      proposer_id=emp["id"], sort_order=1)
    assert store.get_task(task["id"]) == task
    assert [t["id"] for t in store.list_tasks(project_id=prj["id"])] == [other["id"], task["id"]]
    assert store.list_tasks(status="proposed") == [other] and store.list_tasks(campaign_id=cmp_["id"]) == [task]
    changed = store.update_task(task["id"], assignee_id=emp["id"], upstream_rejected=True, exchange_count=2)
    assert changed["assignee_id"] == emp["id"] and changed["upstream_rejected"] is True
    with pytest.raises(ValueError):  # only tasks.py changes the state (§4.5)
        store.update_task(task["id"], status="working")
    assert store.get_task(task["id"])["status"] == "backlog"
    for bad in ({"reason": " "}, {"spec": ""}, {"done_criteria": "   "}, {"status": "paused"},
                {"type": "bug"}, {"proposer_kind": "employee"}):
        with pytest.raises(sqlite3.IntegrityError):
            make_task(store, prj["id"], **bad)
    with store.transaction() as tx:  # the state CHECKs, the way tasks.py will write them
        with pytest.raises(sqlite3.IntegrityError):  # a closed state needs finished_at
            tx.exec("UPDATE tasks SET status = 'cancelled' WHERE id = ?", (other["id"],))
        with pytest.raises(sqlite3.IntegrityError):  # working needs a run
            tx.exec("UPDATE tasks SET status = 'working' WHERE id = ?", (task["id"],))
        tx.exec("UPDATE tasks SET status = 'cancelled', finished_at = ? WHERE id = ?", (now(), other["id"]))
        tx.exec("UPDATE tasks SET status = 'working', run_id = 'r1' WHERE id = ?", (task["id"],))
    assert store.get_task(other["id"])["status"] == "cancelled" and store.get_task(task["id"])["run_id"] == "r1"

    third = make_task(store, prj["id"], title="벽 타기")
    dep = store.create_task_dep(third["id"], task["id"])
    assert dep["task_id"] == third["id"] and dep["depends_on_id"] == task["id"] and STAMP.fullmatch(dep["created_at"])
    with pytest.raises(sqlite3.IntegrityError):  # not on itself
        store.create_task_dep(third["id"], third["id"])
    with pytest.raises(sqlite3.IntegrityError):  # once
        store.create_task_dep(third["id"], task["id"])
    assert store.list_task_deps(task_id=third["id"]) == [dep] == store.list_task_deps(depends_on_id=task["id"])
    assert store.delete_task_dep(third["id"], task["id"]) is True
    assert store.delete_task_dep(third["id"], task["id"]) is False and store.list_task_deps() == []

    created = store.create_task_event(task["id"], "owner", "created", rule="C2", to_status="backlog",
                                      detail={"run_id": "r1"})
    assert created["id"] == 1 and created["detail"] == {"run_id": "r1"} and created["from_status"] is None
    assert created["reason"] == "" and STAMP.fullmatch(created["at"])
    progress = store.create_task_event(task["id"], "employee", "progress", actor_id=emp["id"], reason="절반")
    assert progress["rule"] is None and progress["detail"] == {}
    with pytest.raises(sqlite3.IntegrityError):  # a rule comes with its target state
        store.create_task_event(task["id"], "system", "transition", rule="T2")
    assert store.list_task_events(task["id"]) == [created, progress]
    assert store.list_task_events(task["id"], after_id=1) == [progress]
    assert store.list_task_events(third["id"]) == []


def test_inbox_crud(store):
    prj = make_project(store)
    task = make_task(store, prj["id"])
    item = store.create_inbox_item("question", "어느 쪽으로 할까요", "system", project_id=prj["id"],
                                   task_id=task["id"], run_id="r1", payload={"options": ["가", "나"]})
    assert item["id"].startswith("inb_") and item["status"] == "pending" and item["delivery"] == "none"
    assert item["payload"] == {"options": ["가", "나"]} and item["decision"] is None
    assert item["decision_data"] == {} and "decision_json" not in item and item["closed_at"] is None
    assert store.get_inbox_item(item["id"]) == item
    with pytest.raises(sqlite3.IntegrityError):  # one pending item per task
        store.create_inbox_item("budget", "예산", "system", task_id=task["id"])
    with pytest.raises(sqlite3.IntegrityError):  # decided needs closed_at
        store.update_inbox_item(item["id"], status="decided", decision="approve")
    decided = store.update_inbox_item(item["id"], status="decided", decision="approve", decision_comment="가로",
                                      decision_data={"choice": "가"}, closed_at=now(), delivery="pending")
    assert decided["decision"] == "approve" and decided["decision_data"] == {"choice": "가"}
    assert decided["delivery"] == "pending" and decided["closed_at"]
    hire = store.create_inbox_item("hire", "프로그래머 채용", "system")
    assert hire["task_id"] is None
    assert store.list_inbox(status="pending") == [hire] and store.list_inbox(task_id=task["id"]) == [decided]
    assert [i["id"] for i in store.list_inbox()] == [item["id"], hire["id"]]
    assert store.update_inbox_item("inb_none", reminded_at=now()) is None


def test_devices_crud(store):
    dev = store.create_device("폰", "a" * 64, fcm_token="f1", settings={"dnd": [22, 7]})
    assert dev["id"].startswith("dev_") and dev["platform"] == "android" and dev["settings"] == {"dnd": [22, 7]}
    assert store.get_device(dev["id"]) == dev == store.get_device_by_token_hash("a" * 64)
    assert store.get_device_by_token_hash("b" * 64) is None
    with pytest.raises(sqlite3.IntegrityError):  # UNIQUE token_hash
        store.create_device("태블릿", "a" * 64)
    with pytest.raises(ValueError):  # a token is not swapped in place
        store.update_device(dev["id"], token_hash="c" * 64)
    seen = store.update_device(dev["id"], last_seen_at=now(), settings={})
    assert seen["last_seen_at"] and seen["settings"] == {}
    store.update_device(dev["id"], revoked_at=now())
    assert store.list_devices() == [] and len(store.list_devices(include_revoked=True)) == 1
    assert store.create_device("새 폰", "d" * 64, fcm_token="f1")["fcm_token"] == "f1"  # a revoked device frees it


# --- transactions -------------------------------------------------------------------------------------
def test_transaction_commits_and_rolls_back_as_one(store):
    with store.transaction() as tx:
        prj = store.create_project("A", "D:/A", "a", tx=tx)
        assert store.get_project(prj["id"], tx=tx) == prj and store.list_projects(tx=tx) == [prj]
        event_id = store.emit(tx, "project.created", {"이름": "A"}, project_id=prj["id"])
        assert tx.emitted == [event_id] == [1]
        assert tx.row("SELECT COUNT(*) AS n FROM stream_events") == {"n": 1}
    assert store.get_project(prj["id"]) == prj
    events = store.list_stream_events()
    assert [(e["id"], e["type"], e["data"], e["project_id"]) for e in events] == [
        (1, "project.created", {"이름": "A"}, prj["id"])]

    with pytest.raises(RuntimeError):
        with store.transaction() as tx:
            lost = store.create_project("B", "D:/B", "b", tx=tx)
            store.emit(tx, "project.created", project_id=lost["id"])
            raise RuntimeError("stop")
    assert store.get_project(lost["id"]) is None and len(store.list_stream_events()) == 1
    assert store.list_stream_events(after_id=1) == []
    with pytest.raises(StudioStoreError):  # an event is written with the change, never on its own
        store.emit(None, "project.created")


def test_forgetting_tx_inside_a_transaction_raises_instead_of_hanging(store):
    with store.transaction() as tx:
        with pytest.raises(StudioStoreError):
            store.list_projects()
        with pytest.raises(StudioStoreError):
            store.create_project("A", "D:/A", "a")
        with pytest.raises(StudioStoreError):
            with store.transaction():
                pass
        store.create_project("B", "D:/B", "b", tx=tx)  # the transaction is still usable
    assert [p["name"] for p in store.list_projects()] == ["B"]
    with pytest.raises(StudioStoreError):  # a finished transaction's handle is dead
        tx.exec("SELECT 1")
    with pytest.raises(StudioStoreError):
        store.create_project("C", "D:/C", "c", tx=tx)


def test_other_threads_wait_for_the_transaction(store):
    done = []
    with store.transaction() as tx:
        worker = threading.Thread(target=lambda: done.append(store.create_project("B", "D:/B", "b")))
        worker.start()
        worker.join(0.3)
        assert worker.is_alive() and not done
        store.create_project("A", "D:/A", "a", tx=tx)
    worker.join(10)
    assert len(done) == 1 and [p["name"] for p in store.list_projects()] == ["A", "B"]


# --- version 4: the built-in art part and its room (T3; §3.4 · §3.15) ----------------------------------
def _art(store):
    return store.get_department("art"), store.list_rooms(kind="department", department_id="art")


def test_version_4_makes_art_on_an_empty_db_and_on_a_running_one(tmp_path):
    fresh = StudioStore(tmp_path / "a.sqlite")
    try:
        part, rooms = _art(fresh)
        assert (part["name"], part["builtin"], part["default_relay"], part["sort_order"]) == ("아트", True, "editor-task", 4)
        assert [(r["id"], r["title"]) for r in rooms] == [("room_dep_art", "아트")]
    finally:
        fresh.close()
    path = tmp_path / "b.sqlite"  # a DB that already runs (version 3), which the owner has changed
    old = StudioStore(path, migrations=MIGRATIONS[:3])
    old.update_department("design_setup", name="세팅", default_relay="quick")
    old.update_department("hr", guide_template="고친 지침")
    old.close()
    store = StudioStore(path, migrations=MIGRATIONS[:4])
    try:
        assert store.migrated == [4] and _art(store)[0]["sort_order"] == 4
        assert store.get_department("design_setup")["name"] == "세팅"  # nothing of the owner's is written over
        assert (store.get_department("design_setup")["default_relay"], store.get_department("hr")["guide_template"]) == (
            "quick", "고친 지침")
    finally:
        store.close()
    again = StudioStore(path, migrations=MIGRATIONS[:4])  # the step ran once: nothing is made twice
    try:
        assert again.migrated == [] and len(again.list_departments()) == 5 and len(again.list_rooms()) == 6
    finally:
        again.close()


def test_version_4_goes_after_the_owners_parts_and_leaves_a_part_of_the_same_name_alone(tmp_path):
    path = tmp_path / "c.sqlite"
    old = StudioStore(path, migrations=MIGRATIONS[:3])
    mine = old.create_department("사운드", sort_order=4)
    old.create_room("department", title="사운드", department_id=mine["id"])
    old.close()
    store = StudioStore(path, migrations=MIGRATIONS[:4])
    try:
        assert _art(store)[0]["sort_order"] == 5  # MAX + 1: the owner's part keeps its place
        assert [d["id"] for d in store.list_departments()][-2:] == [mine["id"], "art"]
    finally:
        store.close()

    path = tmp_path / "d.sqlite"  # the owner's part is called 아트 already (an archived one counts too)
    old = StudioStore(path, migrations=MIGRATIONS[:3])
    taken = old.create_department("아트", sort_order=4)
    old.update_department(taken["id"], archived_at=now())
    old.close()
    store = StudioStore(path, migrations=MIGRATIONS[:4])
    try:
        assert store.migrated == [4]  # the version is taken, nothing fails
        assert _art(store) == (None, []) and store.get_department(taken["id"])["name"] == "아트"
        assert len(store.list_departments(include_archived=True)) == 5 and len(store.list_rooms()) == 5
    finally:
        store.close()
