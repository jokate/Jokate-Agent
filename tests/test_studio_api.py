"""Studio REST, part 1 (S-3): authentication (§6.2), error body (§6.3), basic resources (§6.4, §6.6)."""
import base64
import hashlib
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from relay_agent.studio import StudioSchemaError, StudioStoreError, api
from relay_agent.studio.store import now
from relay_agent.studio.tasks import CODES, STATES, TaskNotFound, TransitionError

DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
TOKEN = "shared-Token-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SEEDED_ROOMS = {"room_system", "room_dep_hr", "room_dep_design_doc", "room_dep_program", "room_dep_design_setup",
                "room_dep_art"}


class World:
    """The server app on a studio DB of its own, with one project, one employee (and DM room), one task."""

    def __init__(self, server):
        self.cfg = server.cfg
        self.owner = TestClient(server.app, headers=AUTH)                    # this PC, with the shared token
        self.local = TestClient(server.app)                                  # this PC, no token
        self.remote = TestClient(server.app, client=("192.168.0.7", 50000))  # another device, no token
        self.broken = TestClient(server.app, headers=AUTH, raise_server_exceptions=False)
        self.store = s = api.get_store()
        self.project = s.create_project("게임", "D:/game", "game", engine_version="5.7", mcp_port=8101)
        self.pid = self.project["id"]
        self.emp = s.create_employee("민수", "program", project_id=self.pid)
        self.dm = s.create_room("dm", title="민수", employee_id=self.emp["id"])
        s.create_room_member(self.dm["id"], self.emp["id"])
        self.task = s.create_task(self.pid, title="2단 점프", reason="조작감", spec="공중에서 한 번 더",
                                  done_criteria="테스트 통과", type="feature", status="backlog",
                                  proposer_kind="owner", assignee_id=self.emp["id"])
        self.attachments = self.cfg.studio_dir / "attachments"

    def says(self, body, room_id=None):
        """A message of the employee, written straight into the store."""
        return self.store.create_message(room_id or self.dm["id"], "employee", author_id=self.emp["id"], body=body)

    def events(self, type):
        return [e for e in self.store.list_stream_events() if e["type"] == type]

    def upload(self, name="shot.png", data=b"\x89PNG-bytes"):
        r = self.owner.post("/studio/attachments", json={"name": name, "data_b64": base64.b64encode(data).decode()})
        assert r.status_code == 201, r.text
        return r.json()

    def post(self, room_id=None, client=None, **body):
        return (client or self.owner).post(f"/studio/rooms/{room_id or self.dm['id']}/messages", json=body)


@pytest.fixture
def world(tmp_path, monkeypatch):
    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    api.close_store()
    yield World(server)
    api.close_store()


def refused(r, status, code):
    assert (r.status_code, r.json().get("code")) == (status, code), r.text
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]
    return r.json()


def doc_section(start, end):
    text = DOC.read_text(encoding="utf-8")
    return text[text.index(start):text.index(end)]


# --- authentication (§6.2) --------------------------------------------------------------------------------
def test_remote_call_without_a_token_is_401(world):
    refused(world.remote.get("/studio/projects"), 401, "unauthorized")
    refused(world.remote.get("/studio/projects", headers={"Authorization": "Bearer wrong"}), 401, "unauthorized")
    refused(world.remote.post("/studio/attachments", json={"name": "a", "data_b64": "eA=="}), 401, "unauthorized")
    assert world.remote.get("/studio/projects", headers=AUTH).status_code == 200


def test_loopback_needs_the_token_under_studio_and_nowhere_else(world):
    refused(world.local.get("/studio/projects"), 401, "unauthorized")
    refused(world.local.post(f"/studio/rooms/{world.dm['id']}/read", json={"message_id": 1}), 401, "unauthorized")
    refused(world.local.get("/studio/projects", headers={"Authorization": "Bearer wrong"}), 401, "unauthorized")
    refused(world.local.get("/studio/projects", headers={"Authorization": TOKEN}), 401, "unauthorized")  # no scheme
    assert world.local.get("/version").status_code == 200  # outside /studio this PC is trusted as before
    assert world.local.get("/").status_code == 200         # the dashboard opens without a token
    assert world.owner.get("/studio/projects").status_code == 200


def test_without_a_shared_token_studio_answers_401_and_says_how_to_set_it(world, monkeypatch):
    monkeypatch.setattr(world.cfg, "auth_token", "")
    for client in (world.remote, world.local):
        detail = refused(client.get("/studio/projects"), 401, "unauthorized")["detail"]
        assert "auth_token" in detail and "KATAE_TOKEN" in detail
    refused(world.remote.get("/studio/projects", headers={"Authorization": "Bearer"}), 401, "unauthorized")
    refused(world.remote.get("/studio/projects", headers={"Authorization": "Bearer x"}), 401, "unauthorized")
    assert world.remote.get("/version").status_code == 403  # the old guard keeps its own answer


def test_device_token_is_the_owner_under_studio_only(world, monkeypatch):
    token = "sdt_" + "k" * 43
    bearer = {"Authorization": f"Bearer {token}"}
    refused(world.remote.get("/studio/projects", headers=bearer), 401, "unauthorized")  # not a known device
    dev = world.store.create_device("폰", hashlib.sha256(token.encode()).hexdigest())

    def seen():
        return world.store.get_device(dev["id"])["last_seen_at"]

    assert world.remote.get("/studio/projects", headers=bearer).status_code == 200
    assert seen() is not None
    recent = (datetime.now() - timedelta(seconds=30)).isoformat(timespec="seconds")
    world.store.update_device(dev["id"], last_seen_at=recent)
    assert world.remote.get("/studio/projects", headers=bearer).status_code == 200
    assert seen() == recent  # once a minute is enough
    old = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    world.store.update_device(dev["id"], last_seen_at=old)
    assert world.remote.get("/studio/projects", headers=bearer).status_code == 200
    assert seen() > old

    assert world.remote.get("/version", headers=bearer).status_code == 401  # outside /studio it is no token
    refused(world.remote.post("/studio/mcp/room_post", json={}, headers=bearer), 403, "actor_not_allowed")
    monkeypatch.setattr(world.cfg, "auth_token", "")
    assert world.remote.get("/studio/projects", headers=bearer).status_code == 200  # needs no shared token
    world.store.update_device(dev["id"], revoked_at=now())
    refused(world.remote.get("/studio/projects", headers=bearer), 401, "unauthorized")


def test_a_device_token_the_db_cannot_check_is_503_not_500(world, monkeypatch):
    token = "sdt_" + "k" * 43
    bearer = {"Authorization": f"Bearer {token}"}
    world.store.create_device("폰", hashlib.sha256(token.encode()).hexdigest())

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as mp:
        mp.setattr(world.store, "get_device_by_token_hash", locked)
        detail = refused(world.remote.get("/studio/projects", headers=bearer), 503, "unavailable")["detail"]
        assert "OperationalError" in detail and "database is locked" in detail
        refused(world.remote.post("/studio/mcp/room_post", json={}, headers=bearer), 503, "unavailable")
        assert world.owner.get("/studio/projects").status_code == 200  # the shared token needs no DB
    with monkeypatch.context() as mp:
        mp.setattr(world.store, "update_device", locked)  # found, but it cannot be marked as seen
        refused(world.remote.get("/studio/projects", headers=bearer), 503, "unavailable")
    assert world.remote.get("/studio/projects", headers=bearer).status_code == 200


def test_actor_paths_answer_with_their_own_codes(world):
    for path in ("/studio/mcp/room_post", "/studio/guard/check"):
        for client in (world.local, world.remote):
            refused(client.post(path, json={}), 401, "actor_invalid")
        refused(world.local.post(path, json={}, headers={"Authorization": "Bearer wrong"}), 401, "actor_invalid")
        # actor tokens are verified by S-6; until then they establish nobody
        refused(world.local.post(path, json={}, headers={"Authorization": "Bearer sat1.c.s"}), 401, "actor_invalid")
        refused(world.owner.post(path, json={}), 403, "actor_not_allowed")
    refused(world.local.get("/studio/projects", headers={"Authorization": "Bearer sat1.c.s"}), 401, "unauthorized")


def test_only_post_pair_passes_without_a_token(world):
    # let through without a token: what is checked there is the code (M-2, tests/test_studio_devices.py)
    refused(world.remote.post("/studio/devices/pair", json={"code": "x", "name": "폰"}), 400, "pairing_invalid")
    assert world.remote.post("/studio/devices/pair", json={}).status_code == 422
    refused(world.remote.get("/studio/devices/pair"), 401, "unauthorized")
    refused(world.remote.post("/studio/devices/pairing"), 401, "unauthorized")


def test_origin_and_network_checks_still_come_first(world, monkeypatch):
    r = world.owner.post(f"/studio/rooms/{world.dm['id']}/read", json={"message_id": 0},
                         headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and "code" not in r.json()
    monkeypatch.setattr(world.cfg, "remote_networks", ["100.64.0.0/10"])
    r = world.remote.get("/studio/projects", headers=AUTH)
    assert r.status_code == 403 and "code" not in r.json()
    assert world.owner.get("/studio/projects").status_code == 200


# --- the store behind the router (§3.2) -------------------------------------------------------------------
def test_store_opens_once_under_runs_studio(world):
    assert world.cfg.studio_db.is_file() and world.cfg.studio_db.parent.name == "studio"
    assert api.open_store() is True and api.get_store() is world.store


def test_a_store_that_cannot_start_answers_503_and_is_not_retried(world, monkeypatch):
    calls = []

    def cannot_start(path):
        calls.append(path)
        raise StudioSchemaError("the DB is newer than this code")

    api.close_store()
    monkeypatch.setattr(api, "StudioStore", cannot_start)
    assert api.open_store() is False
    for _ in range(2):
        assert "newer" in refused(world.owner.get("/studio/projects"), 503, "unavailable")["detail"]
    refused(world.owner.post("/studio/attachments", json={"name": "a", "data_b64": "eA=="}), 503, "unavailable")
    refused(world.remote.get("/studio/projects", headers={"Authorization": "Bearer sdt_x"}), 503, "unavailable")
    assert len(calls) == 1
    refused(world.local.get("/studio/projects"), 401, "unauthorized")  # the shared token needs no DB
    assert world.local.get("/version").status_code == 200              # the rest of the server runs


# --- errors (§6.3) ----------------------------------------------------------------------------------------
def test_status_codes_are_the_documents_table():
    rows = re.findall(r"^\| `([a-z_]+)` \| (\d{3}) \|", doc_section("### 6.3 오류", "### 6.4 엔드포인트"), re.M)
    assert len(rows) == 15 and {code: int(status) for code, status in rows} == api.STATUS
    assert set(CODES) <= set(api.STATUS)


def test_routes_are_the_s3_rows_of_the_document():
    doc, section = set(), doc_section("**프로젝트 · 에디터 · 생성 작업**", "**캠페인 · 작업**")
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        row = re.fullmatch(r"`(GET|POST|PATCH|PUT|DELETE) (/studio/[^`]+)`", cells[0]) if len(cells) == 5 else None
        if row and re.search(r"S-3|M-6|M-8", cells[4]):
            doc.add((row.group(1), row.group(2)))
    scope = ("/studio/projects", "/studio/jobs", "/studio/departments", "/studio/employees", "/studio/rooms",
             "/studio/attachments")
    built = {(method, r.path) for r in api.router.routes for method in r.methods if r.path.startswith(scope)}
    members = {(method, "/studio/rooms/{id}/members/{employee_id}") for method in ("POST", "DELETE")}  # S-9
    assert "| `POST` · `DELETE /studio/rooms/{id}/members/{employee_id}` |" in section  # one row for the two
    assert ("POST", "/studio/projects") in doc  # M-6: bringing in a project (tests/test_studio_importer.py)
    # S-3's 14 and M-6's one, and the members of a task room (S-9); nothing else of M-* · H-* · S-7 and later
    muted = {("PATCH", "/studio/rooms/{id}")}  # N-2: a room is muted (tests/test_studio_push.py)
    assert "| `PATCH /studio/rooms/{id}` | `{muted}` |" in section
    made = {("POST", "/studio/projects/create"), ("GET", "/studio/projects/create/options"),
            ("GET", "/studio/jobs/{id}")}  # M-8: making a project as a job (tests/test_studio_jobs.py)
    assert made <= doc and "| `POST /studio/jobs/{id}/retry` · `/cleanup` |" in section  # one row for the two
    failed = {("POST", f"/studio/jobs/{{id}}/{what}") for what in ("retry", "cleanup")}  # M-8: a job that failed
    editor = {("GET", "/studio/projects/{id}/editor"), *(("POST", f"/studio/projects/{{id}}/editor/{what}")
                                                         for what in ("start", "stop", "screenshot"))}  # M-7
    assert "| `POST /studio/projects/{id}/editor/start` · `/stop` |" in section  # one row for the two
    assert "| `GET /studio/projects/{id}/editor` |" in section  # (tests/test_studio_editor.py)
    assert "| `POST /studio/projects/{id}/editor/screenshot` |" in section
    parts = {("POST", "/studio/departments"), ("PATCH", "/studio/departments/{id}"),
             ("POST", "/studio/departments/{id}/archive")}  # H-1: a part of the owner's (tests/test_studio_parts.py)
    assert all(f"| `{method} {path}` |" in section for method, path in parts)
    history = {("PUT", "/studio/employees/{id}/files/{name}"), ("GET", "/studio/employees/{id}/history"),
               ("POST", "/studio/employees/{id}/revert")}  # H-3: the files (tests/test_studio_employee_git.py)
    assert all(f"| `{method} {path}` |" in section for method, path in history)
    life = {("PATCH", "/studio/employees/{id}"), ("POST", "/studio/employees/{id}/assign"),
            ("POST", "/studio/employees/{id}/leave"),
            ("POST", "/studio/employees/{id}/retire")}  # H-3: a life (tests/test_studio_employee_lifecycle.py)
    assert all(f"| `{method} {path}` |" in section for method, path in life)
    memory = {("GET", "/studio/employees/{id}/memory"), ("PATCH", "/studio/employees/{id}/memory/{item_id}"),
              ("DELETE", "/studio/employees/{id}/memory/{item_id}")}  # H-4: the memory (tests/test_studio_memory.py)
    assert all(f"| `{method} {path}` |" in section for method, path in memory)
    group = {("POST", "/studio/rooms"), ("POST", "/studio/rooms/{id}/rename"),
             ("POST", "/studio/rooms/{id}/archive"),  # the group rooms (the tests of the group rooms below)
             ("POST", "/studio/rooms/{id}/continue")}  # and their turns (tests/test_studio_group.py)
    assert all(f"| `{method} {path}` |" in section for method, path in group)
    policy = {("GET", "/studio/projects/{id}/policy"), ("PUT", "/studio/projects/{id}/policy")}  # H-6
    assert "| `GET` · `PUT /studio/projects/{id}/policy` |" in section  # one row for the two (tests/test_studio_policy.py)
    others = members | muted | failed | editor | parts | history | life | memory | group | policy
    assert len(doc) == 18 and not doc & others and built == doc | others


def test_studio_error_carries_the_fields_of_its_code():
    assert api.StudioError("conflict", "겹칩니다", field="name").body() == {
        "detail": "겹칩니다", "code": "conflict", "field": "name"}
    assert api.StudioError("precondition_failed", "안 됩니다").body()["failed"] == []
    assert api.error_response("payload_too_large", "큽니다").status_code == 413
    with pytest.raises(ValueError):
        api.StudioError("nope", "x")


def test_transition_errors_and_missing_tasks_take_the_error_body():
    app = FastAPI()
    api.add_error_handlers(app)

    @app.get("/refuse/{code}")
    def refuse(code: str):
        raise TransitionError(code, "막혔습니다", from_status="done", to_status="working", failed=["no_run"],
                              fields=["title"])

    @app.get("/lost")
    def lost():
        raise TaskNotFound("task_0000")

    client = TestClient(app)
    want = {"illegal_transition": (409, {"from": "done", "to": "working"}), "actor_not_allowed": (403, {}),
            "precondition_failed": (409, {"failed": ["no_run"]}), "missing_fields": (422, {"fields": ["title"]}),
            "stale_state": (409, {})}
    assert set(want) == set(CODES)
    for code, (status, extra) in want.items():
        r = client.get(f"/refuse/{code}")
        assert r.status_code == status and r.json() == {"detail": "막혔습니다", "code": code, **extra}
    assert "task_0000" in refused(client.get("/lost"), 404, "not_found")["detail"]


def test_validation_errors_keep_the_default_form(world):
    for r in (world.owner.get(f"/studio/rooms/{world.dm['id']}/messages?limit=201"),
              world.owner.patch(f"/studio/projects/{world.pid}", json={"mcp_port": 70000}),
              world.owner.post(f"/studio/rooms/{world.dm['id']}/read", json={})):
        assert r.status_code == 422 and isinstance(r.json()["detail"], list) and "code" not in r.json()


# --- projects ---------------------------------------------------------------------------------------------
def test_projects_list_leaves_archived_out_unless_asked(world):
    old = world.store.create_project("옛 게임", "D:/old", "old")
    world.store.update_project(old["id"], archived_at=now())
    items = world.owner.get("/studio/projects").json()["items"]
    assert items == [world.store.get_project(world.pid)]
    assert set(items[0]) == {"id", "name", "path", "engine_version", "mcp_port", "repo", "created_at", "updated_at",
                             "archived_at"}
    both = world.owner.get("/studio/projects?archived=1").json()["items"]
    assert [p["id"] for p in both] == [world.pid, old["id"]]
    assert world.owner.get("/studio/projects?archived=2").status_code == 422


def test_project_detail_adds_employees_task_counts_and_editor(world):
    body = world.owner.get(f"/studio/projects/{world.pid}").json()
    assert body["name"] == "게임" and body["editor"] is None
    assert body["task_counts"] == {**dict.fromkeys(STATES, 0), "backlog": 1}
    assert [(e["id"], e["dm_room_id"]) for e in body["employees"]] == [(world.emp["id"], world.dm["id"])]
    refused(world.owner.get("/studio/projects/prj_none"), 404, "not_found")


def test_project_patch_changes_only_what_is_given(world):
    url = f"/studio/projects/{world.pid}"
    r = world.owner.patch(url, json={"name": "새 게임", "engine_version": "5.8"})
    row = r.json()
    assert r.status_code == 200 and (row["name"], row["engine_version"], row["mcp_port"]) == ("새 게임", "5.8", 8101)
    assert row == world.store.get_project(world.pid)
    events = world.events("project.updated")
    assert len(events) == 1 and events[0]["data"] == {"project": row} and events[0]["project_id"] == world.pid
    assert world.owner.patch(url, json={"mcp_port": None}).json()["mcp_port"] is None  # null clears the port
    assert world.owner.patch(url, json={"name": "새 게임"}).json()["name"] == "새 게임"
    assert len(world.events("project.updated")) == 2  # a patch that changes nothing says nothing
    refused(world.owner.patch("/studio/projects/prj_none", json={"name": "x"}), 404, "not_found")
    assert refused(world.owner.patch(url, json={"name": "  "}), 422, "missing_fields")["fields"] == ["name"]
    assert world.owner.patch(url, json={"repo": "other"}).status_code == 422  # not a field this path changes


def test_project_patch_stores_a_null_engine_version_as_empty(world):
    url = f"/studio/projects/{world.pid}"
    r = world.owner.patch(url, json={"engine_version": None})
    assert r.status_code == 200 and r.json()["engine_version"] == ""  # §3.3: the column is NOT NULL DEFAULT ''
    assert world.store.get_project(world.pid)["engine_version"] == ""
    assert len(world.events("project.updated")) == 1
    assert world.owner.patch(url, json={"engine_version": None}).json()["engine_version"] == ""
    assert len(world.events("project.updated")) == 1  # '' again changes nothing


def test_project_patch_refuses_a_taken_name_or_port(world):
    url = f"/studio/projects/{world.pid}"
    other = world.store.create_project("둘째", "D:/b", "b", mcp_port=8102)
    assert refused(world.owner.patch(url, json={"name": "둘째"}), 409, "conflict")["field"] == "name"
    assert refused(world.owner.patch(url, json={"mcp_port": 8102}), 409, "conflict")["field"] == "mcp_port"
    assert world.store.get_project(world.pid) == world.project and world.events("project.updated") == []
    world.store.update_project(other["id"], archived_at=now())
    assert world.owner.patch(url, json={"mcp_port": 8102}).json()["mcp_port"] == 8102  # an archived one frees its port
    assert refused(world.owner.patch(url, json={"name": "둘째"}), 409, "conflict")["field"] == "name"


def test_project_archive_waits_for_unfinished_tasks(world):
    url = f"/studio/projects/{world.pid}/archive"
    assert refused(world.owner.post(url), 409, "precondition_failed")["failed"] == []
    assert world.store.get_project(world.pid)["archived_at"] is None and world.events("project.updated") == []
    world.store._exec("UPDATE tasks SET status = 'cancelled', finished_at = ? WHERE id = ?", (now(), world.task["id"]))
    r = world.owner.post(url)
    assert r.status_code == 200 and r.json()["archived_at"] is not None
    events = world.events("project.updated")
    assert len(events) == 1 and events[0]["data"] == {"project": r.json()}
    again = world.owner.post(url)
    assert again.status_code == 200 and again.json() == r.json() and len(world.events("project.updated")) == 1
    assert world.owner.get("/studio/projects").json()["items"] == []
    refused(world.owner.post("/studio/projects/prj_none/archive"), 404, "not_found")


# --- departments · employees ------------------------------------------------------------------------------
def test_departments_are_the_seeded_five(world):
    items = world.owner.get("/studio/departments").json()["items"]
    assert {d["id"] for d in items} == {"hr", "design_doc", "program", "design_setup", "art"} and items[0]["id"] == "hr"
    assert all(d["builtin"] is True for d in items)
    assert isinstance(items[0]["default_mcp"], list) and "default_mcp_json" not in items[0]


def test_employees_list_filters_and_carries_the_dm_room(world):
    jia = world.store.create_employee("지아", "design_doc")
    gone = world.store.create_employee("현우", "program", project_id=world.pid)
    world.store.update_employee(gone["id"], retired_at=now())

    def ids(query=""):
        return [e["id"] for e in world.owner.get("/studio/employees" + query).json()["items"]]

    assert ids() == [world.emp["id"], jia["id"]]
    assert ids(f"?project_id={world.pid}") == [world.emp["id"]]
    assert ids("?department_id=design_doc") == [jia["id"]]
    assert ids(f"?project_id={world.pid}&retired=1") == [world.emp["id"], gone["id"]]
    items = {e["id"]: e for e in world.owner.get("/studio/employees").json()["items"]}
    assert items[world.emp["id"]]["dm_room_id"] == world.dm["id"] and items[jia["id"]]["dm_room_id"] is None
    assert (items[world.emp["id"]]["name"], items[world.emp["id"]]["status"]) == ("민수", "idle")


def test_a_project_lists_the_hr_employee_as_company_wide(world):
    old = world.store.create_employee("옛 인사", "hr")
    world.store.update_employee(old["id"], retired_at=now())
    hr = world.store.create_employee("인사", "hr")  # in service, placed nowhere
    idle = world.store.create_employee("대기", "program")  # not placed and not HR: of no project

    def rows(query=""):
        return world.owner.get("/studio/employees" + query).json()["items"]

    def ids(query=""):
        return [e["id"] for e in rows(query)]

    listed = rows(f"?project_id={world.pid}")
    assert [(e["id"], e["company_wide"]) for e in listed] == [(world.emp["id"], False), (hr["id"], True)]
    assert listed[1]["project_id"] is None and "dm_room_id" in listed[1]  # shown with the project, not placed in it
    assert ids(f"?project_id={world.pid}&department_id=hr") == [hr["id"]]
    assert ids(f"?project_id={world.pid}&department_id=program") == [world.emp["id"]]
    assert ids(f"?project_id={world.pid}&retired=1") == [world.emp["id"], hr["id"]]  # a retired HR is of no project
    assert world.store.get_project("prj_none") is None  # a project that is not there: nobody, HR neither
    assert ids("?project_id=prj_none") == ids("?project_id=prj_none&department_id=hr") == []
    assert ids("?project_id=prj_none&retired=1") == []
    assert set(ids()) == {world.emp["id"], hr["id"], idle["id"]}  # without a project: as it was
    assert set(ids("?retired=1")) == {world.emp["id"], old["id"], hr["id"], idle["id"]}
    assert {e["id"] for e in rows("?retired=1") if e["company_wide"]} == {old["id"], hr["id"]}
    detail = world.owner.get(f"/studio/projects/{world.pid}").json()["employees"]
    assert [(e["id"], e["company_wide"]) for e in detail] == [(world.emp["id"], False), (hr["id"], True)]
    assert world.owner.get(f"/studio/employees/{hr['id']}").json()["company_wide"] is True
    assert world.store.get_employee(hr["id"])["project_id"] is None and "company_wide" not in world.store.get_employee(hr["id"])


def test_employee_detail_adds_files_tasks_and_runs(world):
    url = f"/studio/employees/{world.emp['id']}"
    body = world.owner.get(url).json()
    assert body["name"] == "민수" and body["dm_room_id"] == world.dm["id"] and body["runs"] == []
    assert body["files"] == {"persona_md": None, "guide_md": None, "memory_md": None, "employee_yaml": None}
    assert [t["id"] for t in body["tasks"]] == [world.task["id"]]

    folder = world.cfg.studio_dir / "employees" / world.emp["id"]
    folder.mkdir(parents=True)
    (folder / "PERSONA.md").write_text("# 민수\n", encoding="utf-8")
    (folder / "employee.yaml").write_text("department: program\n", encoding="utf-8")
    assert world.owner.get(url).json()["files"] == {
        "persona_md": "# 민수\n", "guide_md": None, "memory_md": None, "employee_yaml": "department: program\n"}

    world.store._exec("UPDATE tasks SET status = 'cancelled', finished_at = ? WHERE id = ?", (now(), world.task["id"]))
    assert world.owner.get(url).json()["tasks"] == []  # finished work is read from the task list
    refused(world.owner.get("/studio/employees/emp_none"), 404, "not_found")


# --- rooms ------------------------------------------------------------------------------------------------
def test_rooms_list_counts_unread_and_shows_the_last_message(world):
    dm = world.dm["id"]
    world.says("했어요")
    world.says("확인 부탁드려요")
    mine = world.store.create_message(dm, "owner", body="볼게요")

    def rooms(query=""):
        return {r["id"]: r for r in world.owner.get("/studio/rooms" + query).json()["items"]}

    listed = rooms()
    assert set(listed) == SEEDED_ROOMS | {dm}
    assert listed[dm]["unread"] == 2 and listed[dm]["last_message"] == mine  # the owner's own message is not unread
    assert listed["room_system"]["unread"] == 0 and listed["room_system"]["last_message"] is None
    assert set(rooms("?kind=dm")) == {dm} and len(rooms("?kind=department")) == 5
    assert world.owner.get("/studio/rooms?kind=nope").status_code == 422
    world.store.update_room(dm, archived_at=now())
    assert dm not in rooms() and dm in rooms("?archived=1")


def test_room_detail_adds_members_and_unread(world):
    world.says("했어요")
    body = world.owner.get(f"/studio/rooms/{world.dm['id']}").json()
    assert body["kind"] == "dm" and body["unread"] == 1
    assert [m["employee_id"] for m in body["members"]] == [world.emp["id"]]
    system = world.owner.get("/studio/rooms/room_system").json()
    assert system["members"] == [] and system["unread"] == 0
    refused(world.owner.get("/studio/rooms/room_none"), 404, "not_found")


def test_read_mark_only_moves_forward(world):
    dm = world.dm["id"]
    first, second = world.says("1")["id"], world.says("2")["id"]
    url = f"/studio/rooms/{dm}/read"

    def unread():
        return world.owner.get(f"/studio/rooms/{dm}").json()["unread"]

    r = world.owner.post(url, json={"message_id": first})
    assert r.status_code == 200 and r.json()["owner_read_id"] == first and unread() == 1
    events = world.events("room.updated")
    assert len(events) == 1 and events[0]["room_id"] == dm and events[0]["data"]["room"] == r.json()
    assert [m["employee_id"] for m in events[0]["data"]["members"]] == [world.emp["id"]]
    assert world.owner.post(url, json={"message_id": second + 100}).json()["owner_read_id"] == second  # not past the end
    assert world.owner.post(url, json={"message_id": first}).json()["owner_read_id"] == second         # not back
    assert len(world.events("room.updated")) == 2 and unread() == 0
    assert world.owner.post(url, json={"message_id": -1}).status_code == 422
    refused(world.owner.post("/studio/rooms/room_none/read", json={"message_id": 1}), 404, "not_found")


# --- members of a task room (S-9; §6.4) -------------------------------------------------------------------
def task_room(world):
    """The room of the world's task with its assignee seated, as T3 leaves it."""
    room = world.store.create_room("task", title=world.task["title"], task_id=world.task["id"])
    world.store.create_room_member(room["id"], world.emp["id"])
    return room["id"]


def test_a_task_rooms_member_is_seated_taken_out_and_seated_again(world):
    room, me = task_room(world), world.emp["id"]
    mate = world.store.create_employee("지수", "program", project_id=world.pid)["id"]
    never = world.store.create_employee("현우", "program", project_id=world.pid)["id"]
    url = f"/studio/rooms/{room}/members/{mate}"

    def ids(r):
        assert r.status_code == 200 and set(r.json()) == {"members"}, r.text
        return {m["employee_id"] for m in r.json()["members"]}

    def detail():
        return {m["employee_id"] for m in world.owner.get(f"/studio/rooms/{room}").json()["members"]}

    def told():
        return [{m["employee_id"] for m in e["data"]["members"]} for e in world.events("room.updated")]

    assert ids(world.owner.post(url)) == {me, mate} == detail()
    [event] = world.events("room.updated")
    assert (event["room_id"], event["task_id"], event["data"]["room"]["id"]) == (room, world.task["id"], room)
    assert told() == [{me, mate}]
    assert ids(world.owner.post(url)) == {me, mate} and len(told()) == 1  # seated already: nothing is said

    assert ids(world.owner.delete(url)) == {me} == detail()
    assert world.store.get_room_member(room, mate)["left_at"] is not None  # the row stays, and what it kept
    assert told() == [{me, mate}, {me}]
    assert ids(world.owner.delete(url)) == {me} and len(told()) == 2  # gone already
    assert ids(world.owner.delete(f"/studio/rooms/{room}/members/{never}")) == {me}  # one who was never there
    assert world.store.get_room_member(room, never) is None and len(told()) == 2

    assert ids(world.owner.post(url)) == {me, mate} == detail()  # back, in the row it had
    assert world.store.get_room_member(room, mate)["left_at"] is None
    assert len(world.store.list_room_members(room_id=room, include_left=True)) == 2
    assert told() == [{me, mate}, {me}, {me, mate}]


def test_room_members_are_refused_outside_a_task_room_or_its_project_and_the_assignee_stays(world):
    room, me = task_room(world), world.emp["id"]
    mate = world.store.create_employee("지수", "program", project_id=world.pid)["id"]
    unplaced = world.store.create_employee("대기", "program")["id"]
    other = world.store.create_project("다른 게임", "D:/other", "other", engine_version="5.7", mcp_port=8102)
    away = world.store.create_employee("파견", "program", project_id=other["id"])["id"]

    def rows():
        return {m["employee_id"]: m["left_at"] for m in world.store.list_room_members(room_id=room, include_left=True)}

    for call in (world.owner.post, world.owner.delete):
        refused(call(f"/studio/rooms/{world.dm['id']}/members/{mate}"), 409, "precondition_failed")  # a 1:1 room
        refused(call("/studio/rooms/room_system/members/" + mate), 409, "precondition_failed")
        refused(call(f"/studio/rooms/room_none/members/{mate}"), 404, "not_found")
        refused(call(f"/studio/rooms/{room}/members/emp_none"), 404, "not_found")
    for stranger in (unplaced, away):  # placed nowhere, placed in another project
        assert refused(world.owner.post(f"/studio/rooms/{room}/members/{stranger}"), 409,
                       "precondition_failed")["failed"] == []
    refused(world.owner.delete(f"/studio/rooms/{room}/members/{me}"), 409, "precondition_failed")  # the assignee
    assert rows() == {me: None} and world.events("room.updated") == []  # a refusal changes and says nothing
    assert [m["employee_id"] for m in world.store.list_room_members(room_id=world.dm["id"])] == [me]

    # where the employee is placed is asked of POST alone: one who was moved away can still be taken out
    assert world.owner.post(f"/studio/rooms/{room}/members/{mate}").status_code == 200
    world.store.update_employee(mate, project_id=other["id"])
    r = world.owner.delete(f"/studio/rooms/{room}/members/{mate}")
    assert r.status_code == 200 and [m["employee_id"] for m in r.json()["members"]] == [me]
    assert rows()[mate] is not None
    refused(world.owner.post(f"/studio/rooms/{room}/members/{mate}"), 409, "precondition_failed")  # not back in
    assert rows()[mate] is not None and len(world.events("room.updated")) == 2


# --- group rooms (§3.6 · §6.4) ----------------------------------------------------------------------------
def _staff(world, *names, **where):
    """Employees of the program part, by name; placed in no project unless `project_id` says where."""
    return [world.store.create_employee(name, "program", **where)["id"] for name in names]


def _group(world, *employee_ids, **more):
    r = world.owner.post("/studio/rooms", json={"employee_ids": list(employee_ids), **more})
    assert r.status_code == 201, r.text
    return r.json()


def _notices(world, room_id):
    """`meta` of the room's system messages, oldest first."""
    return [m["meta"] for m in world.store.list_messages(room_id) if m["author_kind"] == "system"]


def _told(world, room_id):
    """Who each `room.updated` of the room said is in it."""
    return [{m["employee_id"] for m in e["data"]["members"]} for e in world.events("room.updated")
            if e["room_id"] == room_id]


def test_a_group_room_is_made_of_serving_employees_wherever_they_are_placed(world):
    me = world.emp["id"]
    [mate], [idle] = _staff(world, "지수", project_id=world.pid), _staff(world, "대기")
    hr = next((e["id"] for e in world.store.list_employees(department_id="hr")), None)
    hr = hr or world.store.create_employee("최인사", "hr")["id"]  # company-wide: in no project
    room = _group(world, mate, me, mate, idle, hr)  # one named twice is one
    names = ", ".join(world.store.get_employee(e)["name"] for e in (mate, me, idle, hr))
    assert (room["kind"], room["title"], room["exchange_count"], room["unread"]) == ("group", names, 0, 0)
    assert room["id"].startswith("room_") and room["archived_at"] is None
    assert (room["employee_id"], room["task_id"], room["department_id"]) == (None, None, None)
    assert {m["employee_id"] for m in room["members"]} == {me, mate, idle, hr}

    [notice] = world.store.list_messages(room["id"])  # the room says who was invited
    assert (notice["author_kind"], notice["author_id"]) == ("system", None) and names in notice["body"]
    assert notice["meta"] == {"event": "group_invited", "employee_ids": [mate, me, idle, hr]}
    assert room["owner_read_id"] == notice["id"]  # the owner's own doing is not unread
    [made], [said] = world.events("room.updated"), world.events("message.created")
    assert made["id"] < said["id"] and (made["room_id"], said["room_id"]) == (room["id"], room["id"])
    assert made["data"]["room"] == {k: v for k, v in room.items() if k not in ("members", "unread")}
    assert made["data"]["members"] == room["members"] and said["data"]["message"]["id"] == notice["id"]
    assert (made["project_id"], made["task_id"]) == (None, None)

    [listed] = world.owner.get("/studio/rooms?kind=group").json()["items"]
    assert (listed["id"], listed["unread"], listed["last_message"]["id"]) == (room["id"], 0, notice["id"])
    assert room["id"] in {r["id"] for r in world.owner.get("/studio/rooms").json()["items"]}
    assert world.owner.get(f"/studio/rooms/{room['id']}").json() == room
    assert world.owner.get("/studio/rooms?kind=dm").json()["items"][0]["id"] == world.dm["id"]

    named = _group(world, me, mate, title="  점프 회의 ")  # a name of the owner's; the same people again is fine
    assert named["title"] == "점프 회의" and named["id"] != room["id"]
    assert len(world.owner.get("/studio/rooms?kind=group").json()["items"]) == 2


def test_a_group_room_needs_two_who_serve_and_holds_no_more_than_the_limit(world, monkeypatch):
    me = world.emp["id"]
    a, b, c, gone = _staff(world, "지수", "현우", "다은", "퇴사")
    world.store.update_employee(gone, retired_at=now())
    assert world.cfg.studio_group_max_members == 8
    monkeypatch.setattr(world.cfg, "studio_group_max_members", 3)

    def make(*ids, **more):
        return world.owner.post("/studio/rooms", json={"employee_ids": list(ids), **more})

    refused(make(), 409, "precondition_failed")
    refused(make(me), 409, "precondition_failed")
    refused(make(me, me), 409, "precondition_failed")       # one, named twice
    refused(make(me, a, b, c), 409, "precondition_failed")  # one over the limit
    refused(make(me, "emp_none"), 404, "not_found")
    refused(make(me, a, gone), 409, "precondition_failed")  # one who retired
    refused(make(gone, "emp_none"), 404, "not_found")       # every one is looked for before anyone's retirement
    refused(make(me, gone, "emp_none"), 404, "not_found")
    assert make(me, a, nope=1).status_code == 422 and world.owner.post("/studio/rooms", json={}).status_code == 422
    assert world.store.list_rooms(kind="group", include_archived=True) == []  # a refusal makes and says nothing
    assert world.events("room.updated") == [] and world.events("message.created") == []

    r = make(me, a, b, a)  # at the limit
    assert r.status_code == 201, r.text
    room = r.json()["id"]
    url = f"/studio/rooms/{room}/members/"
    refused(world.owner.post(url + c), 409, "precondition_failed")     # no seat
    assert world.owner.post(url + a).status_code == 200                # seated already: the limit is not asked
    assert world.owner.delete(url + b).status_code == 200
    refused(world.owner.post(url + gone), 409, "precondition_failed")  # a seat, but not for one who retired
    assert world.owner.post(url + c).status_code == 200
    refused(world.owner.post(url + b), 409, "precondition_failed")     # one who comes back needs a seat too
    assert {m["employee_id"] for m in world.owner.get(f"/studio/rooms/{room}").json()["members"]} == {me, a, c}
    assert _told(world, room) == [{me, a, b}, {me, a}, {me, a, c}]


def test_a_group_room_is_made_once_per_client_room_id(world):
    me = world.emp["id"]
    mate, gone = _staff(world, "지수", "현우")
    body = {"employee_ids": [me, mate, gone], "client_room_id": "r-1"}

    def make(**more):
        return world.owner.post("/studio/rooms", json={**body, **more})

    def groups():
        return world.store.list_rooms(kind="group", include_archived=True)

    r = make()
    room = r.json()
    assert r.status_code == 201, r.text
    assert _notices(world, room["id"]) == [{"event": "group_invited", "employee_ids": [me, mate, gone],
                                            "client_room_id": "r-1"}]

    again = make()  # nothing new: the room the first one made, as GET gives it
    assert again.status_code == 200 and again.json() == room, again.text
    assert again.json() == world.owner.get(f"/studio/rooms/{room['id']}").json()
    assert len(groups()) == 1 and len(world.store.list_messages(room["id"])) == 1
    assert len(world.events("room.updated")) == 1 and len(world.events("message.created")) == 1
    r = make(employee_ids=[me])  # the key is looked at first: the rest of a retry is not
    assert (r.status_code, r.json()["id"]) == (200, room["id"])

    world.store.update_employee(gone, retired_at=now())  # a member retired since: still the room it made
    assert (make().status_code, make().json()["id"]) == (200, room["id"])
    left = world.owner.post(f"/studio/rooms/{room['id']}/archive").json()
    r = make()  # and a room the owner left is not made again
    assert (r.status_code, r.json()["id"], r.json()["archived_at"]) == (200, room["id"], left["archived_at"])
    assert len(groups()) == 1 and len(world.events("message.created")) == 1

    other = make(employee_ids=[me, mate], client_room_id="r-2")  # another key is another room
    assert other.status_code == 201 and other.json()["id"] != room["id"], other.text
    blank = [make(employee_ids=[me, mate], client_room_id="") for _ in range(2)]  # a blank key is no key
    assert [b.status_code for b in blank] == [201, 201] and blank[0].json()["id"] != blank[1].json()["id"]
    assert _notices(world, blank[0].json()["id"]) == [{"event": "group_invited", "employee_ids": [me, mate]}]
    assert len(groups()) == 4


def test_a_group_rooms_member_is_invited_taken_out_and_comes_back_to_its_conversation(world):
    me = world.emp["id"]
    mate, late, never = _staff(world, "지수", "현우", "다은")
    room = _group(world, me, mate)["id"]
    url = f"/studio/rooms/{room}/members/{late}"

    def ids(r):
        assert r.status_code == 200 and set(r.json()) == {"members"}, r.text
        return {m["employee_id"] for m in r.json()["members"]}

    def said():
        return [(m["event"], m["employee_ids"]) for m in _notices(world, room)]

    assert ids(world.owner.post(url)) == {me, mate, late}
    assert said() == [("group_invited", [me, mate]), ("group_invited", [late])]
    assert _told(world, room) == [{me, mate}, {me, mate, late}] and len(world.events("message.created")) == 2
    assert ids(world.owner.post(url)) == {me, mate, late}  # seated already: nothing is said
    assert len(said()) == 2 and len(_told(world, room)) == 2

    world.store.update_room_member(room, late, session_id="s1", conversation_id="c1", fed_message_id=2)
    assert ids(world.owner.delete(url)) == {me, mate}
    assert world.store.get_room_member(room, late)["left_at"] is not None  # the row stays, and what it kept
    assert said()[-1] == ("group_removed", [late]) and _told(world, room)[-1] == {me, mate}
    assert ids(world.owner.delete(url)) == {me, mate}  # gone already
    assert ids(world.owner.delete(f"/studio/rooms/{room}/members/{never}")) == {me, mate}  # one who was never there
    assert world.store.get_room_member(room, never) is None
    assert len(said()) == 3 and len(_told(world, room)) == 3 and len(world.events("message.created")) == 3

    assert ids(world.owner.post(url)) == {me, mate, late}  # back, in the row it had: the same conversation
    row = world.store.get_room_member(room, late)
    assert (row["left_at"], row["session_id"], row["conversation_id"], row["fed_message_id"]) == (None, "s1", "c1", 2)
    assert len(world.store.list_room_members(room_id=room, include_left=True)) == 3
    assert said()[-1] == ("group_invited", [late]) and _told(world, room)[-1] == {me, mate, late}
    assert ids(world.owner.delete(f"/studio/rooms/{room}/members/{me}")) == {mate, late}  # no assignee: anyone

    detail = world.owner.get(f"/studio/rooms/{room}").json()
    assert detail["unread"] == 0 and detail["owner_read_id"] == world.store.list_messages(room)[-1]["id"]
    for call in (world.owner.post, world.owner.delete):
        refused(call(f"/studio/rooms/{room}/members/emp_none"), 404, "not_found")
    assert len(said()) == 5


def test_a_group_room_is_renamed_and_says_so(world):
    me = world.emp["id"]
    [mate] = _staff(world, "지수")
    room = _group(world, me, mate)
    url = f"/studio/rooms/{room['id']}/rename"

    r = world.owner.post(url, json={"title": "  점프 회의 "})
    assert r.status_code == 200 and r.json()["title"] == "점프 회의", r.text
    notice = world.store.list_messages(room["id"])[-1]
    assert notice["meta"] == {"event": "group_renamed", "title": "점프 회의", "old_title": room["title"]}
    assert r.json()["owner_read_id"] == notice["id"] and "점프 회의" in notice["body"]
    assert world.events("room.updated")[-1]["data"]["room"] == r.json()
    assert world.events("message.created")[-1]["data"]["message"]["id"] == notice["id"]
    assert world.owner.get(f"/studio/rooms/{room['id']}").json()["title"] == "점프 회의"

    assert world.owner.post(url, json={"title": "점프 회의 "}).json() == r.json()  # the same name: nothing is said
    assert len(world.events("room.updated")) == 2 and len(world.events("message.created")) == 2
    assert refused(world.owner.post(url, json={"title": "  "}), 422, "missing_fields")["fields"] == ["title"]
    assert world.owner.post(url, json={}).status_code == 422
    assert world.owner.post(url, json={"title": "새 이름", "muted": True}).status_code == 422
    for other in (world.dm["id"], "room_system", "room_dep_program"):  # only a group room has a name to change
        refused(world.owner.post(f"/studio/rooms/{other}/rename", json={"title": "새 이름"}), 409, "precondition_failed")
    refused(world.owner.post("/studio/rooms/room_none/rename", json={"title": "새 이름"}), 404, "not_found")
    assert len(world.events("room.updated")) == 2 and world.store.get_room(world.dm["id"])["title"] == "민수"


def test_the_owner_leaves_a_group_room_by_archiving_it(world):
    me = world.emp["id"]
    [mate] = _staff(world, "지수")
    room = _group(world, me, mate)["id"]
    url = f"/studio/rooms/{room}/archive"

    def listed(query):
        return [r["id"] for r in world.owner.get("/studio/rooms?kind=group" + query).json()["items"]]

    r = world.owner.post(url)
    assert r.status_code == 200 and r.json()["archived_at"], r.text
    assert listed("") == [] and listed("&archived=1") == [room]
    assert world.events("room.updated")[-1]["data"]["room"] == r.json() and _told(world, room)[-1] == {me, mate}
    assert world.owner.post(url).json() == r.json() and len(world.events("room.updated")) == 2  # again: the same row
    assert len(world.store.list_room_members(room_id=room)) == 2  # the members stay, and nothing is said in the room
    assert len(world.store.list_messages(room)) == 1 and len(world.events("message.created")) == 1

    refused(world.owner.post(f"/studio/rooms/{room}/rename", json={"title": "새 이름"}), 409, "precondition_failed")
    for call in (world.owner.post, world.owner.delete):  # nobody is invited into or taken out of a room left
        refused(call(f"/studio/rooms/{room}/members/{mate}"), 409, "precondition_failed")
    refused(world.post(room, body="아직 계신가요"), 409, "precondition_failed")  # nor does the owner write there
    assert len(world.store.list_messages(room)) == 1 and len(world.events("message.created")) == 1
    for other in (world.dm["id"], "room_system", "room_dep_program"):
        refused(world.owner.post(f"/studio/rooms/{other}/archive"), 409, "precondition_failed")
    refused(world.owner.post("/studio/rooms/room_none/archive"), 404, "not_found")
    assert world.store.get_room(world.dm["id"])["archived_at"] is None and len(world.events("room.updated")) == 2


def test_a_message_saved_before_the_owner_left_a_group_room_still_answers_its_retry(world):
    me = world.emp["id"]
    [mate] = _staff(world, "지수")
    room = _group(world, me, mate)["id"]
    first = world.post(room, body="먼저 나갑니다", client_msg_id="c-1")
    assert first.status_code == 201, first.text
    assert world.owner.post(f"/studio/rooms/{room}/archive").status_code == 200

    again = world.post(room, body="먼저 나갑니다", client_msg_id="c-1")
    assert again.status_code == 200 and again.json() == first.json()  # the retry of what was saved
    refused(world.post(room, body="새 글", client_msg_id="c-2"), 409, "precondition_failed")
    assert len(world.store.list_messages(room)) == 2 and len(world.events("message.created")) == 2


def test_the_owners_message_in_a_group_room_is_kept_and_told_and_asks_its_members_turns(world, monkeypatch):
    asked, turns = [], []
    monkeypatch.setattr(api, "_ask_turn", lambda *a, **k: asked.append(a))
    monkeypatch.setattr(api, "_group_turns", lambda room_id, ids, mentioned=False: turns.append((room_id, list(ids),
                                                                                                 mentioned)))
    me = world.emp["id"]
    mate, outsider = _staff(world, "지수", "현우")
    room = _group(world, me, mate)["id"]

    r = world.post(room, body="다들 봐 주세요", mentions=[mate])
    assert r.status_code == 201 and (r.json()["author_kind"], r.json()["mentions"]) == ("owner", [mate]), r.text
    assert world.events("message.created")[-1]["data"]["message"] == r.json()
    # §6.6: who is mentioned only, through the room's line and not as the turn of a 1:1 room (tests/test_studio_group.py)
    assert asked == [] and turns == [(room, [mate], False)]
    refused(world.post(room, body="봐요", mentions=[outsider]), 409, "precondition_failed")  # not of the room
    assert len(turns) == 1  # what was refused asks nothing

    def row():
        return {g["id"]: g for g in world.owner.get("/studio/rooms?kind=group").json()["items"]}[room]

    assert row()["unread"] == 0 and row()["last_message"]["id"] == r.json()["id"]
    answer = world.says("넵", room)
    assert row()["unread"] == 1 and row()["last_message"]["id"] == answer["id"]
    # what waits to be read is not jumped over by a notice of the owner's own doing
    read = world.store.get_room(room)["owner_read_id"]
    assert world.owner.post(f"/studio/rooms/{room}/rename", json={"title": "점프 회의"}).json()["owner_read_id"] == read
    assert row()["unread"] == 2
    last = world.store.list_messages(room)[-1]["id"]
    assert world.owner.post(f"/studio/rooms/{room}/read", json={"message_id": last}).json()["owner_read_id"] == last
    assert row()["unread"] == 0 and asked == []
    assert world.post(room, body="다들 의견 주세요").status_code == 201  # no mention: every member, in the room's order
    assert turns[-1] == (room, api.group_order(world.store, room), False) and set(turns[-1][1]) == {me, mate}
    world.store.update_employee(mate, retired_at="2026-10-12T00:00:00")  # one who retired takes no turn
    assert api.group_order(world.store, room) == [me]


# --- messages ---------------------------------------------------------------------------------------------
def test_messages_page_in_both_directions(world):
    world.says("다른 방의 글", "room_dep_program")
    ids = [world.says(f"m{i}")["id"] for i in range(1, 6)]
    url = f"/studio/rooms/{world.dm['id']}/messages"

    def page(**query):
        r = world.owner.get(url, params=query)
        assert r.status_code == 200 and set(r.json()) == {"items", "has_more"}
        return [m["id"] for m in r.json()["items"]], r.json()["has_more"]

    assert page() == (ids, False)
    assert page(limit=2) == (ids[3:], True)                        # the newest two, oldest first
    assert page(before_id=ids[3], limit=2) == (ids[1:3], True)     # scrolling up
    assert page(before_id=ids[1]) == (ids[:1], False)
    assert page(after_id=ids[0], limit=2) == (ids[1:3], True)      # catching up
    assert page(after_id=ids[2]) == (ids[3:], False)
    assert page(after_id=0, limit=5) == (ids, False)
    assert page(after_id=ids[0], before_id=ids[4], limit=2) == (ids[2:4], True)
    assert page(after_id=ids[0], before_id=ids[4], limit=3) == (ids[1:4], False)
    assert page(limit=200) == (ids, False)
    for bad in ({"limit": 201}, {"limit": 0}, {"before_id": -1}):
        assert world.owner.get(url, params=bad).status_code == 422
    assert world.owner.get(url).json()["items"][0] == world.store.get_message(ids[0])
    refused(world.owner.get("/studio/rooms/room_none/messages"), 404, "not_found")


def test_owner_posts_a_message_once_per_client_msg_id(world):
    dm = world.dm["id"]
    r = world.post(body="안녕하세요", mentions=[world.emp["id"]], client_msg_id="c-1")
    msg = r.json()
    assert r.status_code == 201 and msg == world.store.get_message(msg["id"])
    assert (msg["room_id"], msg["author_kind"], msg["author_id"], msg["body"]) == (dm, "owner", None, "안녕하세요")
    assert (msg["mentions"], msg["attachments"], msg["meta"]) == ([world.emp["id"]], [], {"client_msg_id": "c-1"})
    events = world.events("message.created")
    assert len(events) == 1 and events[0]["data"] == {"message": msg}
    assert (events[0]["room_id"], events[0]["project_id"]) == (dm, world.pid)

    again = world.post(body="안녕하세요", client_msg_id="c-1")
    assert again.status_code == 200 and again.json() == msg  # nothing new: the first one is returned
    assert len(world.store.list_messages(dm)) == 1 and len(world.events("message.created")) == 1
    assert world.post(body="두 번째").json()["meta"] == {}
    assert world.owner.get(f"/studio/rooms/{dm}").json()["unread"] == 0
    assert world.post("room_dep_program", body="파트 공지").status_code == 201  # no mention needed


def test_a_device_token_answers_into_a_room_as_the_owner(world):
    token = "sdt_" + "r" * 43  # the notification's reply (N-3): the same path the app's room uses
    world.store.create_device("폰", hashlib.sha256(token.encode()).hexdigest())
    phone = TestClient(world.owner.app, client=("192.168.0.7", 50000), headers={"Authorization": f"Bearer {token}"})
    r = world.post(client=phone, body="알겠습니다", client_msg_id="n-1")
    msg = r.json()
    assert r.status_code == 201 and (msg["room_id"], msg["author_kind"], msg["body"]) == (world.dm["id"], "owner", "알겠습니다")
    assert world.post(client=phone, body="알겠습니다", client_msg_id="n-1").status_code == 200  # a retry is no second


def test_post_refusals_write_nothing(world):
    assert refused(world.post(body="  \n"), 422, "missing_fields")["fields"] == ["body"]
    assert refused(world.post("room_system", body="공지"), 409, "precondition_failed")["failed"] == []
    stranger = world.store.create_employee("지아", "design_doc")
    assert stranger["id"] in refused(world.post(body="@지아", mentions=[stranger["id"]]),
                                     409, "precondition_failed")["detail"]
    for unknown in ("att_000000000000", "../../studio.sqlite", "_staged"):
        refused(world.post(body="파일", attachments=[unknown]), 409, "precondition_failed")
    refused(world.post("room_none", body="x"), 404, "not_found")
    assert world.store.list_messages(world.dm["id"]) == [] and world.events("message.created") == []


# --- attachments (§6.6) -----------------------------------------------------------------------------------
def test_attachment_goes_from_staged_to_the_message_folder(world):
    data = b"\x89PNG\r\n-bytes"
    att = world.upload("a/b/shot?.png", data)
    assert re.fullmatch(r"att_[0-9a-f]{12}", att["id"])
    assert (att["name"], att["mime"], att["size"]) == ("shot_.png", "image/png", len(data))
    staged = world.attachments / "_staged" / att["id"]
    assert (staged / "shot_.png").read_bytes() == data

    r = world.post(attachments=[att["id"]])  # no body: the file is enough
    msg = r.json()
    ref = {"name": "shot_.png", "path": f"{msg['id']}/shot_.png", "mime": "image/png", "size": len(data)}
    assert r.status_code == 201 and msg["attachments"] == [ref] and msg["body"] == ""
    assert (world.attachments / ref["path"]).read_bytes() == data and not staged.exists()
    assert world.events("message.created")[0]["data"]["message"]["attachments"] == [ref]

    got = world.owner.get(f"/studio/attachments/{ref['path']}")
    assert got.status_code == 200 and got.content == data and got.headers["content-type"] == "image/png"
    refused(world.local.get(f"/studio/attachments/{ref['path']}"), 401, "unauthorized")
    for missing in (f"{msg['id']}/nope.png", f"_staged/{att['id']}", "999/shot_.png", "inb_00000000/x.png"):
        refused(world.owner.get("/studio/attachments/" + missing), 404, "not_found")
    refused(world.post(body="다시", attachments=[att["id"]]), 409, "precondition_failed")  # the id is used up


def test_a_staged_id_attaches_once_even_when_its_folder_cannot_be_removed(world, monkeypatch):
    att = world.upload()
    with monkeypatch.context() as mp:
        mp.setattr(api.shutil, "rmtree", lambda *args, **kwargs: None)  # the cleanup after the commit does nothing
        first = world.post(body="하나", attachments=[att["id"]])
        refused(world.post(body="둘", attachments=[att["id"]]), 409, "precondition_failed")
    assert first.status_code == 201 and len(first.json()["attachments"]) == 1
    assert [x["id"] for x in world.store.list_messages(world.dm["id"])] == [first.json()["id"]]
    # claimed inside the transaction: the folder no longer answers to the id, whether or not it was deleted
    assert [p.name for p in (world.attachments / "_staged").iterdir()] == [att["id"] + api.TAKEN]


def test_two_files_with_one_name_in_a_message(world):
    a, b = world.upload("shot.png", b"1"), world.upload("shot.png", b"22")
    msg = world.post(body="둘", attachments=[a["id"], b["id"], a["id"]]).json()
    assert [(x["name"], x["size"]) for x in msg["attachments"]] == [("shot.png", 1), ("shot-2.png", 2)]
    assert world.owner.get(f"/studio/attachments/{msg['id']}/shot-2.png").content == b"22"


def test_upload_takes_a_data_url_and_refuses_bad_or_big_data(world, monkeypatch):
    r = world.owner.post("/studio/attachments", json={"name": "..", "data_b64": "data:text/plain;base64,aGk="})
    assert r.status_code == 201 and (r.json()["name"], r.json()["size"]) == ("file", 2)
    r = world.owner.post("/studio/attachments", json={"name": "x.bin", "data_b64": "abc"})
    assert r.status_code == 422 and "code" not in r.json()
    before = sorted(p.name for p in (world.attachments / "_staged").iterdir())
    monkeypatch.setattr(api, "MAX_ATTACHMENT_MB", 0)
    refused(world.owner.post("/studio/attachments", json={"name": "x.bin", "data_b64": "eA=="}),
            413, "payload_too_large")
    assert sorted(p.name for p in (world.attachments / "_staged").iterdir()) == before


def test_a_post_that_fails_keeps_the_staged_file_and_leaves_no_copy(world, monkeypatch):
    att = world.upload()

    def fails(*args, **kwargs):
        raise RuntimeError("emit failed")

    with monkeypatch.context() as mp:
        mp.setattr(world.store, "emit", fails)
        assert world.post(client=world.broken, attachments=[att["id"]]).status_code == 500
    assert world.store.list_messages(world.dm["id"]) == []
    assert (world.attachments / "_staged" / att["id"] / att["name"]).is_file()
    assert [p.name for p in world.attachments.iterdir()] == ["_staged"]
    assert [p.name for p in (world.attachments / "_staged").iterdir()] == [att["id"]]  # the claim was given back
    assert world.post(attachments=[att["id"]]).status_code == 201  # so the same id can be tried again


# --- the store's read models ------------------------------------------------------------------------------
def test_read_models_take_a_transaction_too(world):
    dm = world.dm["id"]
    world.says("1")
    with world.store.transaction() as tx:
        assert world.store.count_unread(dm, tx=tx) == {dm: 1}
        assert world.store.last_messages(dm, tx=tx)[dm]["body"] == "1"
        assert world.store.count_tasks_by_status(world.pid, tx=tx)["backlog"] == 1
    assert world.store.count_unread() == {dm: 1} and world.store.count_unread("room_system") == {}
    assert set(world.store.last_messages()) == {dm} and world.store.last_messages("room_system") == {}
    assert world.store.count_tasks_by_status("prj_none") == dict.fromkeys(STATES, 0)


def test_set_message_attachments_belongs_to_the_creating_transaction(world):
    msg = world.says("파일")
    with pytest.raises(StudioStoreError):
        world.store.set_message_attachments(None, msg["id"], [])
    with world.store.transaction() as tx:
        row = world.store.set_message_attachments(tx, msg["id"], [{"name": "a.png"}])
    assert row["attachments"] == [{"name": "a.png"}] == world.store.get_message(msg["id"])["attachments"]


# --- the server's code version ----------------------------------------------------------------------------
def test_code_version_covers_the_studio_package(tmp_path, monkeypatch):
    import relay_agent.server as server

    (tmp_path / "studio").mkdir()
    (tmp_path / "server.py").write_text("a = 1\n", encoding="utf-8")
    inner = tmp_path / "studio" / "api.py"
    inner.write_text("b = 1\n", encoding="utf-8")
    monkeypatch.setattr(server, "__file__", str(tmp_path / "server.py"))
    before = server.code_version()
    assert server.code_version() == before
    inner.write_text("b = 2\n", encoding="utf-8")
    assert server.code_version() != before  # a change under studio/ makes the running server an old one
