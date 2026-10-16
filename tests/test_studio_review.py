"""Studio final review, part 1 (S-12): the package `GET /studio/inbox/{id}` computes for a final review (§5.4),
the `run` of a task (§6.4), the aliases of a run's patch and tokens, and the runs the cleanup keeps (§4.3 T9).
Task runs of the engine on the mock runner, whose build stage writes files: no AI is called."""
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay_agent.repos import RepoRegistry
from relay_agent.runners import MockRunner
from relay_agent.studio import Actor, api, review, tools
from relay_agent.studio.tasks import OWNER, SYSTEM
from test_studio_inbox import PAYLOAD, TEXT, ok, refused
from test_studio_runs import lab  # noqa: F401 - the fixture `shop` stands on
from test_studio_work import shop  # noqa: F401

BLOB = bytes(range(4)) * 8
COPY = {"Source/jump.cpp": b"int jump = 2;\nint air = 1;\n", "Source/old.cpp": None,
        "Docs/점프 메모.txt": "두 번 뛴다\n".encode("utf-8"), "Data/curve.blob": BLOB}
INPLACE = {"Source/double_jump.cpp": b"int double_jump = 2;\n", "Content/Hero.uasset": b"\x00\x01uasset\x00"}
CLOSED = {"ok": False, "why": "이미 처리된 결재다"}
YES = {"ok": True, "why": ""}


class Editor(MockRunner):
    """The build stage writes `files` into the folder its run works in (None = it removes that file)."""

    def __init__(self, files):
        super().__init__()
        self.files = files

    def run(self, call):
        if call.stage == "build":
            for rel, data in self.files.items():
                path = Path(call.cwd) / rel
                if data is None:
                    path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
        return super().run(call)


def reviewed(shop, mode, files, before=None):  # noqa: F811
    """A task whose run — `mode` is its workspace — wrote `files`: T6 came while the run was at work, then the
    run did its remaining stages and ended. `before(row, run)` is called between T3 and T6.
    (the task, the item, the run)."""
    project = shop.lab.tmp / "project"
    (project / "Source").mkdir(exist_ok=True)
    (project / "Source" / "jump.cpp").write_bytes(b"int jump = 1;\n")
    (project / "Source" / "old.cpp").write_bytes(b"int old = 0;\n")
    shop.engine.repos = RepoRegistry({"game": {"path": str(project), "workspace": mode}})
    shop.lab.runner = Editor(files)
    row, run = shop.start(shop.task(relay="edit"))
    assert run.workspace_mode == mode
    if before is not None:
        before(row, run)
    item = shop.lab.world.box.create("final_review", Actor.employee(row["assignee_id"]), title="검수",
                                     payload=PAYLOAD["final_review"], task_id=row["id"])
    run = shop.engine.advance(run.id)
    assert (run.status, run.changes_status) == ("done", "ready")
    return shop.store.get_task(row["id"]), item, run


def pack(shop, item):  # noqa: F811
    view = ok(shop.owner.get(f"/studio/inbox/{item['id']}"))
    assert tuple(view["package"]) == review.PACKAGE_KEYS
    return view["package"]


def quoted(path):
    """The path as git writes it with `core.quotePath` on."""
    return '"' + "".join(chr(b) if 32 <= b < 127 and chr(b) not in '"\\' else f"\\{b:03o}"
                         for b in path.encode("utf-8")) + '"'


def rows(files):
    return {f["path"]: (f["status"], f["insertions"], f["deletions"], f["binary"]) for f in files}


# --- the parts, without a run ---------------------------------------------------------------------------------------
def test_a_patch_is_read_block_by_block_and_a_quoted_path_is_unquoted():
    memo = "Docs/점프 메모.txt"
    patch = (
        f"diff --git {quoted('a/' + memo)} {quoted('b/' + memo)}\n"
        "new file mode 100644\nindex 0000000..e69de29\n"  # an empty new file: the header is all there is
        "diff --git a/My Notes/a b.txt b/My Notes/a b.txt\nindex 1111111..2222222 100644\n"
        "--- a/My Notes/a b.txt\t\n+++ b/My Notes/a b.txt\t\n@@ -1,2 +1,3 @@\n-old\n+new\n+++ not a header\n same\n"
        "diff --git a/Art/hero.png b/Art/hero.png\nindex 1111111..2222222 100644\n"
        "Binary files a/Art/hero.png and b/Art/hero.png differ\n"
        f"diff --git {quoted('a/' + memo + '2')} {quoted('b/' + memo + '2')}\nnew file mode 100644\n"
        f"index 0000000..3333333\n--- /dev/null\n+++ {quoted('b/' + memo + '2')}\n@@ -0,0 +1 @@\n+뛴다\r\n"
        "diff --git a/old.txt b/old.txt\ndeleted file mode 100644\nindex 1111111..0000000\n"
        "--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n")
    assert [(f["path"], *rows([f])[f["path"]]) for f in review.patch_files(patch)] == [
        (memo, "added", 0, 0, False), ("My Notes/a b.txt", "modified", 2, 1, False),
        ("Art/hero.png", "modified", 0, 0, True), (memo + "2", "added", 1, 0, False),
        ("old.txt", "deleted", 0, 1, False)]
    assert review.patch_files("") == [] and review.FOLLOWUP == tools.LISTED


def test_out_of_scope_is_what_fits_none_of_the_parts_globs_and_assets_are_named_by_their_extension():
    paths = ["Source/Jump/a.cpp", "source/b.h", "Content/Hero.uasset", "README.md", "Docs/x.txt", "Source\\win.cpp"]
    assert review.out_of_scope(paths, ["Source/", "*.MD"]) == [{"path": "Content/Hero.uasset"}, {"path": "Docs/x.txt"}]
    assert review.out_of_scope(paths, []) == [] and review.out_of_scope(paths, None) == []  # no list: not judged
    assert review.out_of_scope(["Docs/x.txt", "Docs/x.txt"], ["./Source/"]) == [{"path": "Docs/x.txt"}]
    files = [{"path": "Art/hero.PNG", "binary": True}, {"path": "Source/a.cpp", "binary": False},
             {"path": "Maps/Level.umap", "binary": False}, {"path": "Data/table.bin", "binary": True}]
    assert [(a["path"], a["asset"], a["source"]) for a in review.assets(SimpleNamespace(changes={"files": 4}), files)] == [
        ("Art/hero.PNG", True, "snapshot"), ("Maps/Level.umap", True, "snapshot"), ("Data/table.bin", False, "snapshot")]


# --- the package of a run that worked in a copy -----------------------------------------------------------------
def test_the_package_of_a_copy_run_is_computed_from_the_run_as_it_is(shop, monkeypatch):  # noqa: F811
    monkeypatch.setattr(shop.cfg, "studio_scopes", {"program": ["Source/", "*.md"]}, raising=False)

    def before(row, run):
        shop.m.record(row["id"], SYSTEM, "limit", "T6 앞의 한도")  # before T6 and no skipped stage: not carried
        shop.m.record(row["id"], SYSTEM, "run", "plan 을 건너뛰었다",
                      detail={"run_id": run.id, "stage": "plan", "event": "stage_skipped", "event_id": 1})

    task, item, run = reviewed(shop, "copy", COPY, before)
    shop.m.record(task["id"], SYSTEM, "guard", "명령을 막았다", detail={"rule_id": "rm_rf"})
    child = shop.m.create_task(OWNER, "backlog", shop.pid, parent_id=task["id"], **TEXT).task
    out = pack(shop, item)
    assert out["run"] == {"status": "done", "error": None, "stop": None} and out["run_stopped"] is True
    changes = out["changes"]
    assert (changes["status"], changes["workspace_mode"]) == ("ready", "copy")
    assert changes["patch_url"] == f"/studio/runs/{run.id}/patch"
    assert rows(changes["files"]) == {
        "Data/curve.blob": ("added", 0, 0, True), "Docs/점프 메모.txt": ("added", 1, 0, False),
        "Source/jump.cpp": ("modified", 2, 1, False), "Source/old.cpp": ("deleted", 0, 1, False)}
    assert out["assets"] == [{"path": "Data/curve.blob", "asset": False, "before_bytes": None, "after_bytes": None,
                              "restorable": True, "source": "snapshot"}]
    assert sorted(p["path"] for p in out["out_of_scope"]) == ["Data/curve.blob", "Docs/점프 메모.txt"]  # 담당 밖
    assert (out["screenshots"], out["cost"]) == ([], {"usd": 0})
    assert [c["id"] for c in out["harness_checks"]] == ["structure_choice", "checklist", "skills", "mcp", "guard_warnings"]
    assert out["harness_checks"][0]["status"] == "failed"  # a feature changed code with no structure choice decided
    assert out["harness_checks"][0]["data"]["code_files"] == ["Source/jump.cpp", "Source/old.cpp"]
    assert [(i["kind"], i["reason"]) for i in out["incidents"]] == [("run", "plan 을 건너뛰었다"), ("guard", "명령을 막았다")]
    assert out["incidents"][1]["detail"] == {"rule_id": "rm_rf"} and set(out["incidents"][0]) == {
        "id", "at", "kind", "reason", "detail"}
    assert [f["id"] for f in out["followups"]] == [child["id"]] and tuple(out["followups"][0]) == tools.LISTED
    assert out["can"] == {"pass": YES, "rework": YES, "revert": YES}

    with monkeypatch.context() as m:  # the run's thread has not let go of its slot yet (§4.6)
        m.setattr(api, "_queue", SimpleNamespace(holds=lambda task_id: task_id == task["id"]))
        held = pack(shop, item)
    assert held["run_stopped"] is False and held["can"]["rework"] == YES
    assert not held["can"]["pass"]["ok"] and not held["can"]["revert"]["ok"] and "돌고 있다" in held["can"]["pass"]["why"]

    monkeypatch.setattr(shop.cfg, "studio_scopes", {"art": ["Content/"]}, raising=False)  # no list of this part
    assert pack(shop, item)["out_of_scope"] == []
    ok(shop.lab.decide(item, "pass"))
    closed = ok(shop.owner.get(f"/studio/inbox/{item['id']}"))
    assert closed["allowed_decisions"] == [] and closed["package"]["can"] == {
        "pass": CLOSED, "rework": CLOSED, "revert": CLOSED}
    assert rows(closed["package"]["changes"]["files"]) == rows(changes["files"])  # still read from the run


# --- the package of a run that worked in the folder itself ---------------------------------------------------------
def test_the_package_of_an_inplace_run_lists_what_its_journal_listed(shop, monkeypatch):  # noqa: F811
    monkeypatch.setattr(shop.cfg, "studio_scopes", {"program": ["Source/"]}, raising=False)
    task, item, run = reviewed(shop, "inplace", INPLACE)
    out = pack(shop, item)
    assert (out["changes"]["status"], out["changes"]["workspace_mode"], out["run_stopped"]) == ("ready", "inplace", True)
    assert rows(out["changes"]["files"]) == {
        "Source/double_jump.cpp": ("added", 1, 0, False),  # in result.patch
        "Content/Hero.uasset": ("added", 0, 0, True)}      # listed by the journal alone
    assert out["assets"] == [{"path": "Content/Hero.uasset", "asset": True, "before_bytes": None, "after_bytes": 9,
                              "restorable": True, "source": "absent"}]
    assert out["assets"] == shop.engine.load(run.id).changes["binary"]
    assert out["out_of_scope"] == [{"path": "Content/Hero.uasset"}]  # 담당 밖: once, though it is in both lists
    assert out["can"] == {"pass": YES, "rework": YES, "revert": YES}
    shop.start(shop.task(relay="demo"))  # T9 의 조건 2: another task of the project has gone into working since
    after = pack(shop, item)["can"]
    assert (after["pass"], after["rework"]) == (YES, YES)
    assert not after["revert"]["ok"] and "다른 작업" in after["revert"]["why"]


# --- `run` of a task and the aliases --------------------------------------------------------------------------------
def test_a_task_shows_the_run_as_the_engine_has_it(shop):  # noqa: F811
    assert api._run_view is not None  # server.py: use_engine
    row, run = shop.start(shop.task(relay="demo"))

    def shown():
        return ok(shop.owner.get(f"/studio/tasks/{row['id']}"))["run"]

    assert shown() == {"id": run.id, "status": "pending", "changes_status": "none"}
    shop.engine.advance(run.id)
    assert shown() == {"id": run.id, "status": "done", "changes_status": "none"}
    assert shown() == review.run_view(shop.engine, run.id) and review.run_view(shop.engine, "run_none") is None
    assert review.run_view(None, run.id) is None and review.run_view(shop.engine, None) is None


def test_the_patch_and_the_tokens_of_a_studio_run_are_read_under_studio(shop):  # noqa: F811
    task, item, run = reviewed(shop, "copy", {"Source/jump.cpp": b"int jump = 3;\n"})
    r = shop.owner.get(pack(shop, item)["changes"]["patch_url"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.text == shop.engine.patch_text(run.id) and "diff --git a/Source/jump.cpp b/Source/jump.cpp" in r.text
    assert ok(shop.owner.get(f"/studio/runs/{run.id}/tokens")) == shop.lab.server.get_tokens(run.id)
    outside = shop.lab.create()  # a run of the engine that is no studio run
    for run_id in (outside.id, "run_none"):
        for what in ("patch", "tokens"):
            refused(shop.owner.get(f"/studio/runs/{run_id}/{what}"), 404, "not_found")


# --- the cleanup keeps the run of a task under review ---------------------------------------------------------------
def aged(engine, run_id):
    """run.json was last written before the retention period began: its age alone lets the cleanup take the run."""
    old = time.time() - 8 * 86400
    os.utime(engine.runs_dir / run_id / "run.json", (old, old))


def test_the_workspace_of_a_run_under_review_outlives_the_retention(shop):  # noqa: F811
    task, item, run = reviewed(shop, "copy", {"Source/jump.cpp": b"int jump = 3;\n"})
    engine, copy = shop.engine, shop.engine.runs_dir / run.id / "workspace"
    assert review.held_runs(shop.store) == review.held_runs_ro(shop.cfg.studio_db) == api.held_runs() == {run.id}
    assert review.held_runs_ro(shop.lab.tmp / "none.sqlite") == set()
    engine.held_runs = api.held_runs  # as server.py sets it
    aged(engine, run.id)
    assert engine.cleanup_workspaces(3)["cleaned"] == [] and copy.exists()
    assert pack(shop, item)["can"]["revert"] == YES  # the copy is there: it can still be reverted
    engine.held_runs = lambda: 1 / 0  # nobody can say which runs are held: nothing expires in that round
    assert engine.cleanup_workspaces(3)["cleaned"] == [] and copy.exists()
    engine.held_runs = api.held_runs
    ok(shop.lab.decide(item, "pass"))  # the task is done: its run is held no more, and its changes are decided —
    assert api.held_runs() == set()    # applied to the original (part 2), so the copy went with the decision
    assert not copy.exists() and engine.load(run.id).workspace_cleaned and engine.cleanup_workspaces(3)["cleaned"] == []
    assert (engine.runs_dir / run.id / "result.patch").exists()


def test_relay_cleanup_keeps_the_runs_under_review_while_the_server_is_down(shop, monkeypatch, capsys):  # noqa: F811
    """`relay cleanup` with no server running: `cli.main()` itself — only the config it loads and the engine it
    builds are this test's — reads the held runs from a studio DB that no process has open."""
    from relay_agent import cli

    task, item, run = reviewed(shop, "copy", {"Source/jump.cpp": b"int jump = 3;\n"})
    engine, copy, db = shop.engine, shop.engine.runs_dir / run.id / "workspace", shop.cfg.studio_db
    api.close_store()  # the server is down
    assert review.held_runs_ro(db) == api.held_runs() == {run.id}  # api: without a store it reads the file too
    monkeypatch.setattr(cli, "Config", SimpleNamespace(load=lambda: shop.cfg))
    monkeypatch.setattr(cli, "build_engine", lambda cfg: engine)
    monkeypatch.setattr(sys, "argv", ["relay", "cleanup", "--days", "3"])

    def cleanup():  # what `relay cleanup --days 3` prints once the run is older than that
        aged(engine, run.id)
        capsys.readouterr()
        cli.main()
        return capsys.readouterr().out

    assert "정리 0개" in cleanup() and copy.exists() and not engine.load(run.id).workspace_cleaned
    assert api._store is None  # the CLI read the file: the store is not its to open

    junk = shop.lab.tmp / "junk.sqlite"  # a DB nobody can read (an empty file would be an empty DB: no run held)
    junk.write_bytes(b"not a database " * 70)
    with pytest.raises(sqlite3.DatabaseError):
        review.held_runs_ro(junk)
    engine.held_runs = lambda: review.held_runs_ro(junk)  # "cannot say" is not "none": nothing expires
    aged(engine, run.id)
    assert engine.cleanup_workspaces(3)["cleaned"] == [] and copy.exists()

    conn = sqlite3.connect(db)  # the control: the task is under review no more, so its run is held no more
    try:
        done = conn.execute("UPDATE tasks SET status = 'done', finished_at = updated_at, queued_at = NULL "
                            "WHERE id = ? AND status = 'review'", (task["id"],))
        assert done.rowcount == 1
        conn.commit()
    finally:
        conn.close()  # Windows: an open connection keeps the file
    assert review.held_runs_ro(db) == set()
    assert "정리 1개" in cleanup() and not copy.exists() and engine.load(run.id).workspace_cleaned
    assert (engine.runs_dir / run.id / "result.patch").exists()
