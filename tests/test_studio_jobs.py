"""Making a new UE project as a job (M-8, the first half): `POST /studio/projects/create`, `GET /studio/jobs/{id}`,
`GET /studio/projects/create/options`, and `job.step` · `job.log` · `job.finished` (§6.4, §7.4).

The steps are the real ones with a runner that starts nothing: no engine, no game-harness and no git are run.
Everything is under the test's own folder — the jobs, the DB, the project, the settings file a repository is
written in. The settings of this repository are the same after a test."""
import asyncio
import dataclasses
import json
import os
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay_agent.config import ROOT, Config
from relay_agent.repos import RepoRegistry, save_local_repo
from relay_agent.studio import api, creator, harness, jobs, plugins, ws

DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
TOKEN = "shared-Token-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
STEP_KEYS = {"job_id", "kind", "step", "index", "total", "status", "error"}


class Runner:
    """A `run` that starts nothing: it keeps which command it was asked for, leaves what that command would
    (the rules file of `bootstrap.bat`, the `.git` of `git init`) and answers with the code given for it."""

    def __init__(self):
        self.calls, self.codes = [], {}

    def __call__(self, argv, *, cwd, log):
        argv = [str(part) for part in argv]
        key = Path(argv[0]).name
        if key == "Build.bat":
            key = "projectfiles" if "-projectfiles" in argv else "build"
        self.calls.append(key)
        log(f"ran {key}")
        log(f"in {cwd}")
        if key == harness.BOOTSTRAP:
            (Path(argv[1]) / "CLAUDE.local.md").write_text("rules", encoding="utf-8")
        if argv[:2] == ["git", "init"]:
            (Path(cwd) / ".git").mkdir()
        return self.codes.get(key, 0)


class Repos:
    """What the server keeps of its repositories, read from a settings file of the test's own."""

    def __init__(self, tmp_path):
        self.config, self.local = tmp_path / "relay.config.yaml", tmp_path / "relay.config.local.yaml"
        self.repos = RepoRegistry(Config.load(self.config).repos)

    def reload(self) -> RepoRegistry:
        self.repos = RepoRegistry(Config.load(self.config).repos)
        return self.repos


class World:
    def __init__(self, server, tmp_path, run, repos):
        self.cfg, self.run, self.repos, self.tmp = server.cfg, run, repos, tmp_path
        self.owner = TestClient(server.app, headers=AUTH)
        self.store = api.get_store()
        self.parent = tmp_path / "Projects"
        self.runner = api._job_runner()

    def create(self, **over):
        body = {"name": "Sample", "engine_version": "5.7", "parent_dir": str(self.parent), **over}
        return self.owner.post("/studio/projects/create", json=body)

    def started(self, **over) -> dict:
        r = self.create(**over)
        assert r.status_code == 202, r.text
        return r.json()["job"]

    def finished(self, job) -> dict:
        assert self.runner.wait(job["id"], 30.0)
        r = self.owner.get(f"/studio/jobs/{job['id']}")
        assert r.status_code == 200, r.text
        return r.json()

    def events(self) -> list[dict]:
        return [e for e in self.store.list_stream_events(limit=10_000)
                if e["type"].startswith("job.") or e["type"] == "project.updated"]

    def stored(self) -> list[dict]:
        return [{key: row[key] for key in ws.ENVELOPE} for row in self.store.list_stream_events(limit=10_000)]

    def job_files(self) -> list[str]:
        folder = jobs.folder(self.cfg.studio_dir)
        return sorted(file.name for file in folder.iterdir()) if folder.is_dir() else []


def wait(done, seconds=5.0):
    end = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < end, "기다리던 일이 일어나지 않았다"
        time.sleep(0.01)


def engine_folder(folder: Path, bat=True) -> Path:
    if bat:
        creator.build_bat(folder).parent.mkdir(parents=True)
        creator.build_bat(folder).write_text("@echo off\r\n", encoding="ascii")
    return folder


@pytest.fixture
def world(tmp_path, monkeypatch):
    import relay_agent.server as server

    if not creator.PATH_RE.fullmatch(str(tmp_path)):
        pytest.skip(f"임시 폴더의 경로에 `check` 가 받지 않는 글자가 있다: {tmp_path}")
    real = ROOT / "relay.config.local.yaml"
    before = real.read_bytes() if real.exists() else None
    (tmp_path / "Projects").mkdir()
    engines = {"4.27": str(engine_folder(tmp_path / "UE_4.27")), "5.6": str(tmp_path / "UE_5.6"),
               "5.7": str(engine_folder(tmp_path / "UE_5.7"))}
    for source, uplugin in (("Unreal-MCP-main", "UnrealMCP"), ("YSTagUsage", "YSTagUsage")):
        (tmp_path / "MNYS" / "Plugins" / source).mkdir(parents=True)
        (tmp_path / "MNYS" / "Plugins" / source / f"{uplugin}.uplugin").write_text('{"FileVersion": 3}', "utf-8")
    home = tmp_path / "game-harness"
    home.mkdir()
    for name in (harness.BOOTSTRAP, harness.INDEX_BUILD):
        (home / name).write_text("@echo off\r\n", encoding="ascii")
    run, repos = Runner(), Repos(tmp_path)

    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    monkeypatch.setattr(server.cfg, "studio_plugins", Config().studio_plugins)
    monkeypatch.setattr(server.cfg, "studio_plugin_root", str(tmp_path / "MNYS"))
    monkeypatch.setattr(server.cfg, "studio_harness_root", str(home))
    monkeypatch.setattr(ws, "PING_INTERVAL", 0.05)
    monkeypatch.setattr(api, "_liveness", None)
    monkeypatch.setattr(api, "_machine", dataclasses.replace(api._machine, engines=lambda: dict(engines)))
    monkeypatch.setattr(api, "_repos", lambda: repos.repos)
    monkeypatch.setattr(api, "_job_parts", api._job_parts)  # use_jobs below replaces both: the server's own
    monkeypatch.setattr(api, "_jobs", None)                 # connection is back after the test
    api.close_store()
    api.use_jobs(reload=repos.reload, local_file=repos.local, run=run, is_open=lambda port: False)
    made = World(server, tmp_path, run, repos)
    yield made
    assert made.runner.wait(timeout=30.0)  # no thread of a job outlives its store
    wait(lambda: len(ws.hub) == 0 and ws.hub._ticker is None)
    api.close_store()
    assert (real.read_bytes() if real.exists() else None) == before


def refused(r, code, field, reason) -> dict:
    body = r.json()
    assert (r.status_code, body.get("code"), body.get("field"), body.get("reason")) == (409, code, field, reason), r.text
    return body


# --- the five steps -----------------------------------------------------------------------------------------
def test_a_job_goes_through_the_five_steps_and_the_project_is_there(world):
    job = world.started()
    assert jobs.JOB_ID.fullmatch(job["id"]) and (job["kind"], job["status"]) == ("project_create", "pending")
    assert [(step["step"], step["status"]) for step in job["steps"]] == [(name, "pending") for name in jobs.STEPS]
    assert jobs.STEPS == ("folder", "build", "plugins", "harness", "register")
    assert job["request"] == {"name": "Sample", "engine_version": "5.7", "parent_dir": str(world.parent),
                              "plugins": ["unreal_mcp", "ys_tag_usage"], "harness": True}  # what was left out

    done = world.finished(job)
    assert (done["status"], done["error"]) == ("done", None) and done["finished_at"]
    assert [(step["step"], step["status"], step["error"]) for step in done["steps"]] == [
        (name, "done", None) for name in jobs.STEPS]
    assert all(step["tail"] and step["started_at"] and step["ended_at"] for step in done["steps"])
    assert world.run.calls == ["projectfiles", "build", harness.BOOTSTRAP, harness.INDEX_BUILD, "git"]
    assert world.job_files() == [f"{job['id']}.json"]  # under the test's runs_dir, and no temporary file left
    assert jobs.folder(world.cfg.studio_dir) == world.tmp / "runs" / "studio" / "jobs"

    root = world.parent / "Sample"
    project = world.store.get_project(done["project_id"])
    assert (project["name"], project["repo"], project["engine_version"]) == ("Sample", "Sample", "5.7")
    assert Path(project["path"]) == root.resolve() and project["mcp_port"] == done["mcp_port"] == plugins.FIRST_PORT
    assert (done["repo"], done["path"]) == ("Sample", str(root))
    assert str(root) in done["steps"][0]["created"] and str(root / ".git") in done["steps"][4]["created"]
    assert (root / "Plugins" / "Unreal-MCP-main" / "UnrealMCP.uplugin").is_file() and (root / ".mcp.json").is_file()
    repo = world.repos.repos.repos["Sample"]  # the tmp settings file, read again by the step
    assert (repo.workspace, repo.docs, list(repo.mcp)) == ("inplace", "Docs", ["unreal"])
    assert world.runner.unfinished() == [] and world.runner.ports() == []


def test_the_events_are_those_of_the_document_in_order(world):
    done = world.finished(world.started())
    rows = world.events()
    assert [row["type"] for row in rows] == ["job.step"] * 9 + ["project.updated", "job.step", "job.finished"]
    steps = [row["data"] for row in rows if row["type"] == "job.step"]
    assert [(data["step"], data["status"]) for data in steps] == [
        (name, status) for name in jobs.STEPS for status in ("running", "done")]
    for data in steps:
        assert set(data) == STEP_KEYS and (data["job_id"], data["kind"], data["total"]) == (done["id"], jobs.KIND, 5)
        assert data["index"] == jobs.STEPS.index(data["step"]) + 1 and data["error"] is None
    assert rows[-1]["data"] == {"job_id": done["id"], "kind": "project_create", "status": "done",
                                "project_id": done["project_id"], "error": None}
    # the envelope names the project only once `register` has put its row in
    assert [row["project_id"] for row in rows] == [None] * 9 + [done["project_id"]] * 3
    assert all(row["room_id"] is None and row["task_id"] is None for row in rows)
    text = DOC.read_text(encoding="utf-8")
    events = text[text.index("### 7.4 이벤트"):text.index("### 7.5")]
    assert "| `job.step` | ● |" in events and "`{job_id, kind, step, index, total, status, error}`" in events
    assert "| `job.log` | |" in events and "`{job_id, step, lines[]}`" in events
    assert "| `job.finished` | ● |" in events and "`{job_id, kind, status, project_id, error}`" in events
    assert " → ".join(f"`{name}`" for name in jobs.STEPS) in events
    assert "`running` · `done` · `failed` · `skipped`" in events and "잠정, M-8" not in events


def test_a_step_that_is_turned_off_is_skipped_after_running(world):
    done = world.finished(world.started(plugins=[], harness=False))
    assert done["status"] == "done" and done["request"]["plugins"] == [] and done["request"]["harness"] is False
    assert [step["status"] for step in done["steps"]] == ["done", "done", "done", "skipped", "done"]
    assert world.run.calls == ["projectfiles", "build", "git"]
    pairs = [(e["data"]["step"], e["data"]["status"]) for e in world.events() if e["type"] == "job.step"]
    assert pairs[6:8] == [("harness", "running"), ("harness", "skipped")] and len(pairs) == 10
    project = world.store.get_project(done["project_id"])
    assert project["mcp_port"] is None and done["mcp_port"] is None  # no MCP plugin: no port
    assert not (world.parent / "Sample" / ".mcp.json").exists()


def test_a_failed_step_ends_the_job_and_the_later_steps_do_not_run(world):
    world.run.codes["build"] = 7
    done = world.finished(world.started())
    assert done["status"] == "failed" and done["project_id"] is None and done["finished_at"]
    assert [step["status"] for step in done["steps"]] == ["done", "failed", "pending", "pending", "pending"]
    failed = done["steps"][1]
    assert "exit 7" in failed["error"] and failed["tail"][-1] == failed["error"]
    assert done["error"] == f"build: {failed['error']}" and world.run.calls == ["projectfiles", "build"]
    rows = world.events()
    assert [(row["type"], row["data"].get("step"), row["data"]["status"]) for row in rows] == [
        ("job.step", "folder", "running"), ("job.step", "folder", "done"), ("job.step", "build", "running"),
        ("job.step", "build", "failed"), ("job.finished", None, "failed")]
    assert rows[3]["data"]["error"] == failed["error"] and set(rows[3]["data"]) == STEP_KEYS
    assert rows[4]["data"] == {"job_id": done["id"], "kind": "project_create", "status": "failed",
                               "project_id": None, "error": done["error"]}
    assert world.store.list_projects(include_archived=True) == [] and world.runner.unfinished() == []
    assert (world.parent / "Sample").is_dir()  # nothing is taken back here: the cleanup is the next task's
    refused(world.create(), "conflict", "name", "folder_exists")


def test_a_step_that_raises_is_a_failed_step(world, monkeypatch):
    def boom(spec, **kw):
        raise RuntimeError("없는 경우")

    monkeypatch.setattr(jobs.creator, "build", boom)
    done = world.finished(world.started())
    assert done["status"] == "failed" and [step["status"] for step in done["steps"]][:3] == ["done", "failed", "pending"]
    assert "RuntimeError" in done["steps"][1]["error"] and "없는 경우" in done["error"]
    assert world.events()[-1]["type"] == "job.finished" and world.runner.unfinished() == []


def test_a_job_that_cannot_go_on_fails_the_step_it_was_in(world, monkeypatch):
    real, raised = jobs._save, []

    def save(studio_dir, job):  # the file cannot be written once: the end of `build`
        if not raised and job["steps"][1]["status"] == "done":
            raised.append(job["id"])
            raise OSError("디스크가 찼다")
        return real(studio_dir, job)

    monkeypatch.setattr(jobs, "_save", save)
    done = world.finished(world.started())
    assert raised == [done["id"]] and done["status"] == "failed" and done["finished_at"]
    assert [step["status"] for step in done["steps"]] == ["done", "failed", "pending", "pending", "pending"]
    rows = world.events()  # no step is left `running`: its `job.step` comes before `job.finished`
    assert [(row["type"], row["data"].get("step"), row["data"]["status"]) for row in rows] == [
        ("job.step", "folder", "running"), ("job.step", "folder", "done"), ("job.step", "build", "running"),
        ("job.step", "build", "failed"), ("job.finished", None, "failed")]
    assert set(rows[3]["data"]) == STEP_KEYS and rows[3]["data"]["index"] == 2
    assert rows[3]["data"]["error"] == rows[4]["data"]["error"] == done["error"] == done["steps"][1]["error"]
    assert "OSError" in done["error"] and "디스크가 찼다" in done["error"] and done["steps"][1]["ended_at"]
    assert not done["error"].startswith("build:")  # the job's own error, not a step's
    assert world.run.calls == ["projectfiles", "build"] and world.runner.unfinished() == []


def test_a_failed_job_keeps_its_port_from_the_next(world):
    world.run.codes[harness.BOOTSTRAP] = 3
    first = world.finished(world.started())
    assert first["status"] == "failed" and first["steps"][3]["status"] == "failed"
    assert first["mcp_port"] == plugins.FIRST_PORT and world.runner.ports() == [plugins.FIRST_PORT]
    world.run.codes.clear()
    second = world.finished(world.started(name="Other"))
    assert second["status"] == "done" and second["mcp_port"] == plugins.FIRST_PORT + 1
    assert world.store.get_project(second["project_id"])["mcp_port"] == plugins.FIRST_PORT + 1
    assert world.runner.ports() == [plugins.FIRST_PORT] and world.runner.ports(but=first["id"]) == []


def test_one_job_runs_at_a_time_and_its_twin_is_refused(world):
    world.runner._turn.acquire()  # as if a job were running
    try:
        first = world.started()
        refused(world.create(), "conflict", "name", "job_running")
        second = world.started(name="Other")
        assert {job["id"] for job in world.runner.unfinished()} == {first["id"], second["id"]}
        assert world.owner.get(f"/studio/jobs/{first['id']}").json()["status"] == "pending"
        assert world.events() == [] and world.run.calls == []
    finally:
        world.runner._turn.release()
    done = [world.finished(first), world.finished(second)]
    assert [job["status"] for job in done] == ["done", "done"]
    assert {job["mcp_port"] for job in done} == {plugins.FIRST_PORT, plugins.FIRST_PORT + 1}
    ids = [row["data"]["job_id"] for row in world.events() if row["type"].startswith("job.")]
    assert len(ids) == 22 and sum(1 for a, b in zip(ids, ids[1:]) if a != b) == 1  # one after the other


# --- the stream ---------------------------------------------------------------------------------------------
def test_the_stream_carries_the_steps_and_the_log_that_is_not_stored(world):
    with world.owner.websocket_connect("/studio/ws") as sock:
        assert sock.receive_json()["type"] == "hello"
        job = world.started()
        seen = []
        while not seen or seen[-1]["type"] != "job.finished":
            message = sock.receive_json()
            if message["type"] != "ping":
                seen.append(message)
        while True:  # what was waiting beside the stored rows comes in the same round, before the next ping
            message = sock.receive_json()
            if message["type"] == "ping":
                break
            seen.append(message)
    done = world.finished(job)
    assert [message for message in seen if message["id"] is not None] == world.stored()
    assert "job.log" not in {row["type"] for row in world.stored()}
    logs = [message for message in seen if message["type"] == "job.log"]
    assert logs and all(message["id"] is None and list(message) == list(ws.ENVELOPE) for message in logs)
    assert all(set(message["data"]) == {"job_id", "step", "lines"} and message["data"]["job_id"] == job["id"]
               for message in logs)
    for step in done["steps"]:  # the last one of a step is the tail the job's file keeps
        mine = [message["data"]["lines"] for message in logs if message["data"]["step"] == step["step"]]
        assert 1 <= len(mine) <= 2 and mine[-1] == step["tail"], step["step"]
    assert len(done["steps"][0]["tail"]) <= creator.TAIL_LINES and jobs.LOG_EVERY == 1.0


def test_the_log_waits_apart_from_run_live():
    async def said():
        hub, conn = ws.Hub(), ws.Conn(None, None)
        hub._conns.append(conn)  # not `attach`: that starts the ticker
        live = {"id": None, "type": "run.live", "at": "t", "project_id": None, "room_id": None, "task_id": None,
                "data": {"alive": True}}
        conn.live.append(live)
        for n in range(ws.SAID_BACKLOG + 5):
            hub.say("job.log", {"n": n})
        return hub, conn, live

    hub, conn, live = asyncio.run(said())
    assert list(conn.live) == [live] and len(hub) == 1  # a burst of `job.log` drops no `run.live`
    assert len(conn.said) == ws.SAID_BACKLOG == conn.said.maxlen  # the oldest of its own are dropped
    assert (conn.said[0]["data"], conn.said[-1]["data"]) == ({"n": 5}, {"n": ws.SAID_BACKLOG + 4})
    assert all(event["id"] is None and event["type"] == "job.log" and list(event) == list(ws.ENVELOPE)
               for event in conn.said)


# --- what is refused ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("over, field, reason", [
    ({"name": "1bad"}, "name", "name_invalid"),
    ({"name": "프로젝트"}, "name", "name_invalid"),
    ({"name": "Engine"}, "name", "name_reserved"),
    ({"parent_dir": "Projects/Here"}, "parent_dir", "parent_not_absolute"),
    ({"parent_dir": "<missing>"}, "parent_dir", "parent_missing"),
    ({"engine_version": "5.6"}, "engine_version", "engine_missing"),
    ({"engine_version": "4.27"}, "engine_version", "engine_unsupported"),
    ({"engine_version": "9.9"}, "engine_version", "engine_unknown"),
    ({"plugins": ["nope"]}, "plugins", "plugin_unknown"),
])
def test_what_cannot_be_made_is_a_failed_precondition(world, over, field, reason):
    if over.get("parent_dir") == "<missing>":
        over = {"parent_dir": str(world.parent / "Nowhere")}
    body = refused(world.create(**over), "precondition_failed", field, reason)
    assert body["failed"] == [] and body["detail"]
    assert world.job_files() == [] and world.run.calls == [] and list(world.parent.iterdir()) == []


def test_a_name_a_folder_or_a_repository_that_is_taken_is_a_conflict(world):
    root = world.parent / "Sample"
    root.mkdir()
    refused(world.create(), "conflict", "name", "folder_exists")
    root.rmdir()

    def project(*row):
        made = world.store.create_project(*row)
        return lambda: world.store._exec("DELETE FROM projects WHERE id = ?", (made["id"],))

    for row, field in ((("Other", str(root.resolve()), "other"), "path"),
                       (("Sample", str(world.tmp / "Elsewhere"), "other"), "name"),
                       (("Other", str(world.tmp / "Elsewhere"), "Sample"), "repo")):
        remove = project(*row)
        refused(world.create(), "conflict", field, f"{field}_taken")
        remove()

    for name, path, field in (("Sample", world.tmp / "Elsewhere", "repo"), ("Elsewhere", root, "path")):
        save_local_repo(world.repos.local, name, {"path": path.as_posix()})
        world.repos.reload()
        refused(world.create(), "conflict", field, f"{field}_taken")
        save_local_repo(world.repos.local, name, None, remove=True)
        world.repos.reload()

    assert world.job_files() == [] and world.run.calls == [] and not root.exists()
    assert world.finished(world.started())["status"] == "done"  # with all of that gone, it is made


def test_a_request_of_another_shape_is_422_and_an_unknown_job_404(world):
    assert world.create(extra=1).status_code == 422
    assert world.owner.post("/studio/projects/create", json={"name": "Sample"}).status_code == 422
    for job_id in ("job_00000000", "nope", "job_zzzzzzzz"):
        r = world.owner.get(f"/studio/jobs/{job_id}")
        assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert jobs.load(world.cfg.studio_dir, "../studio") is None and jobs.load(world.cfg.studio_dir, 7) is None
    with pytest.raises(ValueError):
        jobs._save(world.cfg.studio_dir, {"id": "../escape"})
    assert TestClient(world.owner.app, client=("192.168.0.7", 50000)).post(
        "/studio/projects/create", json={}).status_code == 401  # the owner's path, like the rest of /studio


# --- what the making screen is given --------------------------------------------------------------------------
def test_the_options_say_which_engines_plugins_and_harness_may_be_chosen(world, monkeypatch):
    body = world.owner.get("/studio/projects/create/options").json()
    assert set(body) == {"engines", "engine_version", "plugins", "harness"}
    assert [(e["version"], e["exists"], e["usable"], bool(e["warning"])) for e in body["engines"]] == [
        ("4.27", True, False, True), ("5.6", False, False, True), ("5.7", True, True, False)]
    assert set(body["engines"][0]) == {"version", "path", "exists", "usable", "warning"}
    assert body["engine_version"] == "5.7"  # the highest one a project can be made with
    assert [(p["id"], p["checked"], p["available"], p["warning"]) for p in body["plugins"]] == [
        ("unreal_mcp", True, True, ""), ("ys_tag_usage", True, True, "")]
    assert body["harness"] == {"available": True, "checked": True, "warning": ""}

    older = world.owner.get("/studio/projects/create/options", params={"engine_version": "5.6"}).json()
    assert older["engine_version"] == "5.6" and older["engines"] == body["engines"]
    assert [(p["id"], p["checked"]) for p in older["plugins"]] == [("unreal_mcp", True), ("ys_tag_usage", False)]
    assert older["plugins"][1]["warning"]

    monkeypatch.setattr(world.cfg, "studio_harness_root", "")
    off = world.owner.get("/studio/projects/create/options").json()["harness"]
    assert (off["available"], off["checked"]) == (False, False) and "studio_harness_root" in off["warning"]
    refused(world.create(harness=True), "precondition_failed", "harness", "harness_unavailable")
    done = world.finished(world.started())  # left out: off, because it cannot be installed
    assert done["request"]["harness"] is False and done["steps"][3]["status"] == "skipped"


def test_settings_that_are_wrong_are_told_as_a_failed_precondition(world, monkeypatch):
    monkeypatch.setattr(world.cfg, "studio_plugins", [{"id": "broken"}])
    refused(world.owner.get("/studio/projects/create/options"), "precondition_failed", "plugins",
            "plugins_misconfigured")
    body = refused(world.create(), "precondition_failed", "plugins", "plugins_misconfigured")
    assert "broken" in body["detail"] and world.job_files() == []


# --- the connection and the documents -------------------------------------------------------------------------
def test_the_server_hands_over_its_own_settings_file_and_reload():
    import relay_agent.server as server

    assert api._job_parts == {"reload": server._reloaded_repos, "local_file": ROOT / "relay.config.local.yaml"}
    assert ws.hub.say in api._live_listeners
    built = {(method, route.path) for route in api.router.routes for method in route.methods}
    assert {("POST", "/studio/projects/create"), ("GET", "/studio/projects/create/options"),
            ("GET", "/studio/jobs/{id}")} <= built


def test_the_server_reads_its_repositories_again_one_at_a_time(monkeypatch):
    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "repos", server.cfg.repos)  # put back after the test
    monkeypatch.setattr(server.engine, "repos", server.engine.repos)
    held = []

    class Settings:
        @staticmethod
        def load(*args, **kw):
            held.append(server._repos_lock.locked())
            return Config()

    monkeypatch.setattr(server, "Config", Settings)
    assert server._reloaded_repos() is server.engine.repos
    assert held == [True] and not server._repos_lock.locked()
    assert server.cfg.repos == Config().repos


def test_the_steps_keep_their_shared_checks():
    assert creator.engine_problem({"version": "5.7", "path": "Z:/none", "exists": False})[0] == "engine_missing"
    assert creator.engine_problem({"version": "4.27", "path": "Z:/none", "exists": True})[0] == "engine_unsupported"
    assert creator.engine_problem({"version": "5.7", "path": "Z:/none", "exists": True})[0] == "engine_incomplete"
    assert set(jobs.REASONS).isdisjoint(creator.REASONS) and set(jobs.REASONS).isdisjoint(plugins.REASONS)


def test_the_documents_say_what_the_code_does():
    text = DOC.read_text(encoding="utf-8")
    for reason in jobs.REASONS:
        assert f"`{reason}`" in text, reason
    for word in ("`relay_agent/studio/jobs.py`", "`GET /studio/projects/create/options`", "`jobs.LOG_EVERY`",
                 "`api.use_jobs(reload=, local_file=)`", "`{version, path, exists, usable, warning}`",
                 "`{available, checked, warning}`", "`runs/studio/jobs/<job_id>.json`", "`server._repos_lock`",
                 "생성 작업이 중단됐다"):
        assert word in text, word
    assert "잠정, M-8" not in text
    paths = api.openapi_schema()["paths"]
    assert "post" in paths["/studio/projects/create"] and "get" in paths["/studio/projects/create/options"]
    assert "get" in paths["/studio/jobs/{id}"]


# --- a job that failed: the retry, the cleanup, the restart -----------------------------------------------------
def post(world, job, what):
    return world.owner.post(f"/studio/jobs/{job['id']}/{what}")


def steps_told(rows) -> list[tuple]:
    return [(row["type"], row["data"].get("step"), row["data"]["status"]) for row in rows
            if row["type"].startswith("job.")]


def cut_off(world, monkeypatch, index) -> dict:
    """A job whose file could not be written at the end of the step `index`: the step did all it does, and the
    file still has it as it was before — running, its `created` not written."""
    real, raised = jobs._save, []

    def save(studio_dir, job):
        if not raised and job["steps"][index]["status"] == "done":
            raised.append(job["id"])
            raise OSError("디스크가 찼다")
        return real(studio_dir, job)

    monkeypatch.setattr(jobs, "_save", save)
    failed = world.finished(world.started())
    assert raised == [failed["id"]] and failed["status"] == "failed"
    assert failed["steps"][index]["status"] == "failed" and failed["steps"][index]["error"] == failed["error"]
    return failed


def cut_off_in_register(world, monkeypatch) -> dict:
    """A job whose file could not be written at the end of `register`: the row is in, the file does not say so."""
    failed = cut_off(world, monkeypatch, 4)
    assert failed["project_id"] is None and failed["repo_new"] is True
    return failed


def test_a_retry_goes_on_from_the_step_that_failed(world):
    world.run.codes["build"] = 7
    failed = world.finished(world.started())
    assert failed["status"] == "failed" and failed["repo_new"] is None
    seen = len(world.events())
    world.run.codes.clear()
    world.run.calls.clear()

    r = post(world, failed, "retry")
    assert r.status_code == 202, r.text
    again = r.json()["job"]
    assert (again["id"], again["status"], again["error"], again["finished_at"]) == (failed["id"], "pending", None, None)
    done = world.finished(again)
    assert (done["status"], done["error"]) == ("done", None) and done["finished_at"] and done["project_id"]
    assert [(step["status"], step["error"]) for step in done["steps"]] == [("done", None)] * 5
    assert done["steps"][0] == failed["steps"][0]  # the folder was not made again
    assert world.run.calls == ["projectfiles", "build", harness.BOOTSTRAP, harness.INDEX_BUILD, "git"]
    told = steps_told(world.events()[seen:])  # nothing tells of the step that was over
    assert told[0] == ("job.step", "build", "running") and told[-1] == ("job.finished", None, "done")
    assert [step for kind, step, status in told if status == "done" and step] == list(jobs.STEPS[1:])
    assert world.store.get_project(done["project_id"])["name"] == "Sample" and world.runner.unfinished() == []
    assert world.job_files() == [f"{done['id']}.json"]

    refused(post(world, done, "retry"), "precondition_failed", "status", "job_not_failed")
    refused(post(world, done, "cleanup"), "precondition_failed", "status", "job_not_failed")
    assert world.owner.post("/studio/jobs/job_00000000/retry").status_code == 404
    assert world.owner.post("/studio/jobs/job_00000000/cleanup").status_code == 404


def test_a_step_that_runs_again_first_takes_away_what_its_earlier_try_left(world):
    world.run.codes["git"] = 1  # `git init` leaves its `.git` and fails
    failed = world.finished(world.started())
    root = world.parent / "Sample"
    assert failed["steps"][4]["status"] == "failed" and failed["steps"][4]["created"] == [str(root.resolve() / ".git")]
    assert failed["repo_new"] is True and "Sample" in world.repos.repos.repos
    world.run.codes.clear()
    world.run.calls.clear()
    done = world.finished(post(world, failed, "retry").json()["job"])
    assert done["status"] == "done" and world.run.calls == ["git"]  # run again: the `.git` was not in its way
    assert (root / ".git").is_dir() and done["repo_new"] is True
    assert len(world.store.list_projects(include_archived=True)) == 1


def test_a_cleanup_takes_away_what_the_job_made_and_nothing_else(world):
    other = world.finished(world.started(name="Other"))  # not this job's: a project, its folder, its repository
    world.run.codes["git"] = 1
    failed = world.finished(world.started())
    root, beside = world.parent / "Sample", world.parent / "notes.txt"
    beside.write_text("mine", encoding="utf-8")
    (root / "added.txt").write_text("put in after the failure", encoding="utf-8")
    assert failed["status"] == "failed" and root.is_dir() and set(world.repos.reload().repos) == {"Other", "Sample"}
    assert world.runner.ports() == [plugins.FIRST_PORT + 1]
    seen = len(world.events())

    r = post(world, failed, "cleanup")
    assert r.status_code == 202, r.text
    kept = r.json()["job"]  # what no step listed is not the job's: it stays, and the folder with it
    assert kept["status"] == "failed" and "added.txt" in kept["error"] and "남아 있어" in kept["error"]
    assert [item.name for item in root.iterdir()] == ["added.txt"] and world.events()[seen:] == []
    assert (root / "added.txt").read_text(encoding="utf-8") == "put in after the failure"
    assert [step["created"] for step in kept["steps"]] == [[str(root)], [], [], [], []]
    assert [step["status"] for step in kept["steps"]] == ["pending"] * 5
    assert set(world.repos.reload().repos) == {"Other"} and world.runner.ports() == [plugins.FIRST_PORT + 1]
    (root / "added.txt").unlink()

    r = post(world, kept, "cleanup")
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert (job["status"], job["error"], job["project_id"]) == ("cleaned", None, None) and job["finished_at"]
    assert all(step["created"] == [] for step in job["steps"])
    assert job == world.owner.get(f"/studio/jobs/{job['id']}").json()
    assert not root.exists() and set(world.repos.reload().repos) == {"Other"}
    assert (world.parent / "Other" / ".git").is_dir() and beside.read_text(encoding="utf-8") == "mine"
    assert [row["id"] for row in world.store.list_projects(include_archived=True)] == [other["project_id"]]
    rows = world.events()[seen:]
    assert [row["type"] for row in rows] == ["job.finished"] and rows[0]["project_id"] is None
    assert rows[0]["data"] == {"job_id": job["id"], "kind": "project_create", "status": "cleaned",
                               "project_id": None, "error": None}
    assert world.runner.ports() == [] and world.runner.unfinished() == []

    refused(post(world, job, "cleanup"), "precondition_failed", "status", "job_not_failed")
    refused(post(world, job, "retry"), "precondition_failed", "status", "job_not_failed")
    world.run.codes.clear()
    assert world.finished(world.started())["status"] == "done"  # the name and the folder are free again


def test_a_cleanup_leaves_a_repository_that_was_there_before_the_job(world):
    root = world.parent / "Sample"
    save_local_repo(world.repos.local, "Sample", {"path": root.as_posix()})  # registered by hand, before the job
    world.repos.reload()
    world.run.codes["git"] = 1
    failed = world.finished(world.started())
    assert failed["status"] == "failed" and failed["repo_new"] is False
    assert post(world, failed, "cleanup").json()["job"]["status"] == "cleaned"
    assert not root.exists() and "Sample" in world.repos.reload().repos


def test_a_cleanup_touches_nothing_outside_the_jobs_folder(world):
    world.run.codes["build"] = 7
    failed = world.finished(world.started())
    outside, root = world.tmp / "elsewhere", world.parent / "Sample"
    outside.mkdir()
    (outside / "kept.txt").write_text("kept", encoding="utf-8")
    failed["steps"][1]["created"] = [str(outside), str(root / ".." / "elsewhere")]  # a list that went wrong
    jobs._save(world.cfg.studio_dir, failed)
    seen = len(world.events())
    r = post(world, failed, "cleanup")
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert job["status"] == "failed" and "폴더 밖" in job["error"] and world.events()[seen:] == []
    assert job["steps"][1]["created"] == failed["steps"][1]["created"] and job["steps"][0]["created"] == []
    assert [step["status"] for step in job["steps"]] == ["pending"] * 5  # a retry would make all of it anew
    assert (outside / "kept.txt").read_text(encoding="utf-8") == "kept" and not root.exists()
    assert world.runner.ports() == [] and world.runner.unfinished() == []

    assert jobs._remove([str(world.parent / "Sample2")], root) == (
        [str(world.parent / "Sample2")], [f"생성 작업의 폴더 밖이라 지우지 않는다: {world.parent / 'Sample2'}"])
    assert jobs._remove([str(root / "none")], root) == ([], [])  # what is not there is gone already


def test_a_job_cut_off_after_its_row_went_in_is_retried_without_a_second_row(world, monkeypatch):
    failed = cut_off_in_register(world, monkeypatch)
    row = world.store.list_projects(include_archived=True)[0]
    world.run.calls.clear()
    done = world.finished(post(world, failed, "retry").json()["job"])
    assert (done["status"], done["project_id"], done["repo"]) == ("done", row["id"], "Sample")
    assert world.run.calls == [] and "앞선 시도가 넣은 행" in done["steps"][4]["tail"][-1]
    assert [p["id"] for p in world.store.list_projects(include_archived=True)] == [row["id"]]
    assert world.events()[-1]["data"]["project_id"] == row["id"]
    # what the try that was cut off made is the step's, and is still there: the row says it ran to its end
    root = world.parent / "Sample"
    assert str(root / ".git") in failed["steps"][4]["created"] and (root / ".git").is_dir()
    assert done["steps"][4]["created"] == failed["steps"][4]["created"]
    assert all(Path(path).exists() for path in done["steps"][4]["created"])


def test_a_retry_that_comes_while_the_job_is_cleaned_writes_nothing(world, monkeypatch):
    failed = cut_off_in_register(world, monkeypatch)  # a job `_adopt` has a row to write in the file for
    real, inner, saved, seen = jobs.Jobs._adopt, jobs._save, [], []
    monkeypatch.setattr(jobs, "_save", lambda studio_dir, job: saved.append(job["id"]) or inner(studio_dir, job))

    def adopt(self, store, job):
        if not seen:  # the cleanup's: the job is its own by now
            seen.append(len(saved))
            with pytest.raises(creator.Refused) as refusal:
                self.retry(store, job["id"], entries=world.cfg.studio_plugins,
                           plugin_root=world.cfg.studio_plugin_root)
            seen.append((refusal.value.code, refusal.value.reason, len(saved)))
        return real(self, store, job)

    monkeypatch.setattr(jobs.Jobs, "_adopt", adopt)
    job = post(world, failed, "cleanup").json()["job"]
    assert seen == [0, ("conflict", "job_running", 0)]  # the retry wrote no file
    assert job["status"] == "cleaned" and not (world.parent / "Sample").exists()
    assert world.owner.get(f"/studio/jobs/{job['id']}").json()["status"] == "cleaned"
    assert world.store.list_projects(include_archived=True) == [] and world.runner.unfinished() == []


def test_a_folder_step_that_was_cut_off_is_made_again_by_a_retry(world, monkeypatch):
    failed, root = cut_off(world, monkeypatch, 0), world.parent / "Sample"
    made = failed["steps"][0]["created"]  # the folder, written before the step, then what was found in it
    assert made[0] == str(root) and len(made) > 1 and set(made[1:]) == {str(item) for item in root.iterdir()}
    assert world.run.calls == []
    done = world.finished(post(world, failed, "retry").json()["job"])
    assert done["status"] == "done" and (root / "Sample.uproject").is_file()
    assert done["steps"][0]["created"][0] == str(root) and str(root / "Sample.uproject") in done["steps"][0]["created"]


def test_a_folder_step_that_was_cut_off_is_cleaned(world, monkeypatch):
    failed, root = cut_off(world, monkeypatch, 0), world.parent / "Sample"
    assert root.is_dir()
    job = post(world, failed, "cleanup").json()["job"]
    assert job["status"] == "cleaned" and not root.exists()
    assert all(step["created"] == [] for step in job["steps"])


def test_a_plugins_step_that_was_cut_off_is_made_again_by_a_retry(world, monkeypatch):
    failed, root = cut_off(world, monkeypatch, 2), world.parent / "Sample"
    made = failed["steps"][2]["created"]
    assert str(root / "Plugins") in made and str(root / ".mcp.json") in made and failed["mcp_port"] is None
    assert not set(made) & set(failed["steps"][0]["created"])  # what `folder` listed is `folder`'s
    done = world.finished(post(world, failed, "retry").json()["job"])
    assert done["status"] == "done" and done["mcp_port"] and (root / "Plugins").is_dir()
    assert str(root / "Plugins") in done["steps"][2]["created"]


def test_a_folder_that_was_there_before_the_step_is_not_the_jobs(world):
    root = world.parent / "Sample"
    world.runner._turn.acquire()  # the job waits: the folder appears between the check and the step
    try:
        job = world.started()
        root.mkdir()
        (root / "keep.txt").write_text("mine", encoding="utf-8")
    finally:
        world.runner._turn.release()
    failed = world.finished(job)
    assert failed["status"] == "failed" and "이미 있는 폴더다" in failed["error"]
    assert all(step["created"] == [] for step in failed["steps"])
    cleaned = post(world, failed, "cleanup").json()["job"]
    assert cleaned["status"] == "cleaned" and [item.name for item in root.iterdir()] == ["keep.txt"]
    assert (root / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_a_link_is_unlinked_and_a_read_only_file_is_deleted(tmp_path):
    root, outside = tmp_path / "Sample", tmp_path / "outside"
    (root / "Source" / "Empty").mkdir(parents=True)
    outside.mkdir()
    (outside / "kept.txt").write_text("kept", encoding="utf-8")
    link = root / "link"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))  # a junction: no right is needed for one
    else:
        os.symlink(outside, link, target_is_directory=True)
    assert jobs._is_link(link) and not jobs._is_link(root / "Source") and not jobs._is_link(root / "none")

    left, errors = jobs._remove([str(root)], root)  # the folder itself: not with what nobody listed in it
    assert left == [str(root)] and len(errors) == 1 and "남은 것: link" in errors[0]
    assert not (root / "Source").exists() and (link / "kept.txt").is_file()  # the empty folders went, no more
    assert jobs._remove([str(link)], root) == ([], []) and not os.path.lexists(link)
    assert (outside / "kept.txt").read_text(encoding="utf-8") == "kept"  # unlinked, never gone into

    locked, single = root / "Plugins" / "One" / "locked.txt", root / "single.txt"
    locked.parent.mkdir(parents=True)
    for file in (locked, single):
        file.write_text("read only", encoding="utf-8")
        os.chmod(file, stat.S_IREAD)
    for number in range(7):
        (root / f"mine{number}.txt").write_text("a person's", encoding="utf-8")
    left, errors = jobs._remove([str(root), str(single), str(root / "Plugins")], root)
    assert left == [str(root)] and not single.exists() and not (root / "Plugins").exists()
    assert errors[0].endswith("(남은 것: mine0.txt, mine1.txt, mine2.txt, mine3.txt, mine4.txt 외 2개)")
    for number in range(7):
        (root / f"mine{number}.txt").unlink()
    assert jobs._remove([str(root)], root) == ([], []) and not root.exists()


def test_a_test_never_fails_the_jobs_of_this_pc(monkeypatch):
    """tests/conftest.py: without a studio folder of the test's own, `recover_jobs` looks at nothing."""
    asked = []
    monkeypatch.setattr(api, "_job_runner", lambda: asked.append("asked"))
    assert api.recover_jobs() == [] and asked == []


def test_a_job_cut_off_after_its_row_went_in_is_cleaned_with_the_row(world, monkeypatch):
    failed = cut_off_in_register(world, monkeypatch)
    row = world.store.list_projects(include_archived=True)[0]
    root = world.parent / "Sample"
    employee = world.store.create_employee("민수", "program", project_id=row["id"])
    refused(post(world, failed, "cleanup"), "precondition_failed", "project_id", "project_in_use")
    assert root.is_dir() and world.store.get_project(row["id"]) and "Sample" in world.repos.reload().repos
    assert world.owner.get(f"/studio/jobs/{failed['id']}").json()["status"] == "failed"

    world.store.update_employee(employee["id"], project_id=None)
    job = post(world, failed, "cleanup").json()["job"]
    assert (job["status"], job["project_id"]) == ("cleaned", row["id"])
    assert world.store.list_projects(include_archived=True) == [] and not root.exists()
    assert "Sample" not in world.repos.reload().repos
    last = world.events()[-1]
    assert (last["type"], last["data"]["status"], last["data"]["project_id"]) == ("job.finished", "cleaned", row["id"])


def test_nothing_is_cleaned_while_a_job_runs(world):
    world.run.codes["build"] = 7
    failed = world.finished(world.started())
    world.run.codes.clear()
    world.runner._turn.acquire()  # as if a job were running
    try:
        refused(post(world, failed, "cleanup"), "conflict", "status", "job_running")
        again = post(world, failed, "retry").json()["job"]
        assert again["status"] == "pending"
        refused(post(world, failed, "retry"), "precondition_failed", "status", "job_not_failed")
        refused(post(world, failed, "cleanup"), "precondition_failed", "status", "job_not_failed")
        assert api.recover_jobs() == []  # a job of this server is not one a stopped server left
    finally:
        world.runner._turn.release()
    assert world.finished(again)["status"] == "done" and (world.parent / "Sample").is_dir()


def test_a_job_left_open_by_a_server_that_stopped_is_failed_and_can_be_retried(world):
    import relay_agent.server as server

    world.run.codes["build"] = 7
    job = world.finished(world.started())
    waiting = world.finished(world.started(name="Other"))
    world.run.codes.clear()
    # as a server that stopped in the build of one, with the other waiting, would leave the two files
    job.update(status="running", error=None, finished_at=None)
    job["steps"][1].update(status="running", error=None, ended_at=None)
    waiting.update(status="pending", error=None, finished_at=None)
    for step in waiting["steps"]:
        step.update(status="pending", error=None, started_at=None, ended_at=None)
    for left in (job, waiting):
        jobs._save(world.cfg.studio_dir, left)
    seen = len(world.events())

    assert server.recover_studio_jobs in server.app.router.on_startup
    server.recover_studio_jobs()
    back = [world.owner.get(f"/studio/jobs/{left['id']}").json() for left in (job, waiting)]
    assert [(b["status"], b["error"]) for b in back] == [("failed", jobs.RESTARTED)] * 2
    assert all(b["finished_at"] for b in back) and "서버가 다시 시작됐다" in jobs.RESTARTED
    assert back[0]["steps"][1]["status"] == "failed" and back[0]["steps"][1]["error"] == jobs.RESTARTED
    assert [step["status"] for step in back[1]["steps"]] == ["pending"] * 5
    told = sorted(steps_told(world.events()[seen:]), key=lambda row: row[0], reverse=True)
    assert told == [("job.step", "build", "failed"), ("job.finished", None, "failed"),
                    ("job.finished", None, "failed")]
    assert api.recover_jobs() == [] and len(world.events()) == seen + 3  # once

    world.run.calls.clear()
    done = world.finished(post(world, back[0], "retry").json()["job"])
    assert done["status"] == "done" and world.run.calls[:2] == ["projectfiles", "build"]


def test_the_retry_the_cleanup_and_the_restart_are_in_the_documents(world):
    text = DOC.read_text(encoding="utf-8")
    for word in ("`POST /studio/jobs/{id}/retry` · `/cleanup`", "`Jobs.retry`", "`Jobs.cleanup`", "`Jobs.recover`",
                 "`api.recover_jobs()`", "`server.recover_studio_jobs`", "`jobs.readmit`", "`repo_new`",
                 jobs.RESTARTED):
        assert word in text, word
    assert "재시도 · 정리는 잠정" not in text and "M-8 의 뒤 절반" not in text
    assert "함께 사라진다" not in text and "중단된 단계의 `created` 는 비어 있다" not in text
    assert "비어 있을 때만 지운다" in text and "아직 없는 것: 기본 실행기의 제한 시간" not in text
    built = {(method, route.path) for route in api.router.routes for method in route.methods}
    assert {("POST", "/studio/jobs/{id}/retry"), ("POST", "/studio/jobs/{id}/cleanup")} <= built
    made, saved = api.openapi_schema()["paths"], json.loads(api.OPENAPI_PATH.read_text(encoding="utf-8"))["paths"]
    for path in ("/studio/jobs/{id}/retry", "/studio/jobs/{id}/cleanup"):
        assert saved[path] == made[path]
    assert api.openapi_is_current()
    job = world.started()
    assert set(job) == {"id", "kind", "status", "steps", "project_id", "error", "request", "engine_dir", "path",
                        "repo", "repo_new", "mcp_port", "created_at", "updated_at", "finished_at"}
    assert world.finished(job)["repo_new"] is True
