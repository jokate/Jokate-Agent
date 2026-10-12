"""An employee's folder and its git history (H-3, part 1): a file is written by path and committed, the history
is listed, an older version is put back; a folder that had no `.git` becomes a repository at the start with
its files as they were; the approvals of `hire` · `guide_amend` commit (§2.2, §5.5, §6.4, §9.6). Real git runs
in the temporary folder; no AI is called."""
import hashlib
import re
import threading
from pathlib import Path

import pytest

from relay_agent import journal
from relay_agent.studio import Actor, api, employee_git, employees, inbox, runs
from test_studio_inbox import DRAFT, ok, refused, world  # noqa: F401 - the fixture these tests stand on

DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
YAML = "version: 1\ndepartment: program\n"
OLD, NEW = "옛 지침\n", "새 지침\n"
FILES = {"PERSONA.md": "나는 민수다.\n", "GUIDE.md": OLD, "MEMORY.md": "", "employee.yaml": YAML}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def settle(world, files=None):  # noqa: F811
    """민수's folder with the four files, as a hire before H-3 left it: no `.git`."""
    folder = employees.folder(world.dir, world.emp)
    employees.write_files(folder, {**FILES, **(files or {})})
    return folder


def put(world, name, text, base, emp=None):  # noqa: F811
    return world.owner.put(f"/studio/employees/{emp or world.emp}/files/{name}",
                           json={"text": text, "base_sha256": base})


def history(world, file=None, emp=None):  # noqa: F811
    return ok(world.owner.get(f"/studio/employees/{emp or world.emp}/history", params={"file": file} if file else None))


def revert(world, commit, emp=None):  # noqa: F811
    return world.owner.post(f"/studio/employees/{emp or world.emp}/revert", json={"commit": commit})


def file_events(world):  # noqa: F811
    return [e["data"] for e in world.store.list_stream_events(limit=10_000)
            if e["type"] == "employee.updated" and e["data"]["change"] == "file"]


def read(folder, name):
    return (folder / name).read_bytes().decode("utf-8")


# --- the module: a repository of the folder's own ------------------------------------------------------------
def test_the_folder_commits_into_its_own_git_whatever_surrounds_it(tmp_path, monkeypatch):
    outer = tmp_path / "server"
    (outer / "runs").mkdir(parents=True)
    (outer / "README.md").write_bytes(b"server\n")
    assert employee_git.commit(outer, "outer")
    folder = outer / "runs" / "emp_1"
    folder.mkdir()
    (folder / "GUIDE.md").write_bytes(b"one\n")
    assert not employee_git.is_repo(folder) and employee_git.head(folder) is None
    assert employee_git.history(folder) == [] and employee_git.resolve(folder, "abcdef0") is None
    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))  # neither the environment nor the repository above
    monkeypatch.setenv("GIT_AUTHOR_NAME", "somebody")

    first = employee_git.commit(folder, "첫 커밋")
    assert first and (folder / ".git").is_dir() and employee_git.head(folder) == first
    assert employee_git.commit(folder, "nothing new") is None
    assert [(c["commit"], c["message"], c["files"]) for c in employee_git.history(folder)] == [
        (first, "첫 커밋", ["GUIDE.md"])]
    assert [c["message"] for c in employee_git.history(outer)] == ["outer"]
    who = employee_git._git(folder, "log", "-1", "--pretty=%an <%ae> / %cn").stdout.decode("utf-8").strip()
    assert who == "agent-katae <agent-katae@localhost> / agent-katae"  # not the global settings' author

    assert employee_git.resolve(folder, first[:8]) == first == employee_git.resolve(folder, first)
    for bad in ("HEAD", "--all", "", "zzzzzzz", "0" * 40, first[:6], "main"):
        assert employee_git.resolve(folder, bad) is None
    assert employee_git.show(folder, first, "GUIDE.md") == b"one\n"
    assert employee_git.show(folder, first, "MEMORY.md") is None
    with pytest.raises(employee_git.GitError):
        employee_git.commit(tmp_path / "nowhere", "x")


# --- the start: a folder that had no .git --------------------------------------------------------------------
def test_a_folder_without_git_becomes_a_repository_at_the_start_as_it_is(world):  # noqa: F811
    folder = settle(world, {"GUIDE.md": "줄 하나\r\n줄 둘\r\n"})
    (folder / "notes.bin").write_bytes(b"\x00\xff")
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    assert not employee_git.is_repo(folder) and history(world) == []

    assert api.commit_folders() == [world.emp]
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.name != ".git"} == before
    log = history(world)
    assert [(c["message"], c["files"]) for c in log] == [(f"adopt: {world.emp}", sorted(before))]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", log[0]["at"])
    assert employee_git.show(folder, log[0]["commit"], "GUIDE.md") == before["GUIDE.md"]  # the bytes, CRLF and all
    who = employee_git._git(folder, "log", "-1", "--pretty=%an").stdout.decode("utf-8").strip()
    assert who == "agent-katae"

    (folder / "PERSONA.md").write_bytes("손으로 고쳤다\n".encode("utf-8"))
    assert api.commit_folders() == [] and len(history(world)) == 1  # a repository is left as it is
    ok(put(world, "GUIDE.md", NEW, hashlib.sha256(before["GUIDE.md"]).hexdigest()))
    assert history(world)[0]["files"] == ["GUIDE.md", "PERSONA.md"]  # the next write takes it along

    vcs = journal.detect_vcs(folder)  # what a chat run in this folder sees from now on: the folder's own git
    assert vcs.name == "git" and Path(vcs.root).samefile(folder)


# --- edit, history, revert (§6.4) ----------------------------------------------------------------------------
def test_a_guide_is_edited_listed_and_put_back(world):  # noqa: F811
    folder = settle(world)
    assert api.commit_folders() == [world.emp]
    first = employee_git.head(folder)

    out = ok(put(world, "GUIDE.md", NEW, sha(OLD).upper() + " "))
    assert (out["name"], out["sha256"]) == ("GUIDE.md", sha(NEW)) and read(folder, "GUIDE.md") == NEW
    assert out["commit"] == employee_git.head(folder) != first
    log = history(world, "GUIDE.md")
    assert [(c["commit"], c["message"], c["files"]) for c in log] == [
        (out["commit"], "edit: GUIDE.md", ["GUIDE.md"]), (first, f"adopt: {world.emp}", sorted(FILES))]
    assert history(world) == log and history(world, "PERSONA.md") == log[1:]
    assert file_events(world) == [{"employee": world.store.get_employee(world.emp), "change": "file"}]

    back = ok(revert(world, first[:10]))
    assert read(folder, "GUIDE.md") == OLD and back == {"commit": employee_git.head(folder)}
    assert world.owner.get(f"/studio/employees/{world.emp}").json()["files"]["guide_md"] == OLD
    log = history(world)
    assert [(c["message"], c["files"]) for c in log] == [
        (f"revert: {first[:7]}", ["GUIDE.md"]), ("edit: GUIDE.md", ["GUIDE.md"]),
        (f"adopt: {world.emp}", sorted(FILES))]  # nothing of the history is taken away
    assert len(file_events(world)) == 2

    assert ok(revert(world, first)) == back  # already as that commit has it: nothing is written
    assert len(history(world)) == 3 and len(file_events(world)) == 2
    ahead = ok(revert(world, out["commit"]))  # and forward again, to the edit
    assert read(folder, "GUIDE.md") == NEW and ahead["commit"] != back["commit"]


def test_a_first_file_of_an_employee_without_a_folder(world):  # noqa: F811
    other, _ = world.staff("지수")
    out = refused(put(world, "GUIDE.md", NEW, sha(OLD), emp=other), 409, "precondition_failed")
    assert out["failed"] == ["base_mismatch"] and not employees.folder(world.dir, other).exists()
    out = ok(put(world, "GUIDE.md", NEW, "", emp=other))  # "" = there was no file
    assert out["commit"] and [c["files"] for c in history(world, emp=other)] == [["GUIDE.md"]]
    assert history(world, emp=world.emp) == []


def test_a_write_is_refused_before_anything_is_written(world):  # noqa: F811
    folder = settle(world)
    api.commit_folders()
    first = employee_git.head(folder)

    out = refused(put(world, "GUIDE.md", NEW, sha("something else")), 409, "precondition_failed")
    assert out["failed"] == ["base_mismatch"]
    out = refused(put(world, "employee.yaml", "version: 2\ndepartment: program\n", sha(YAML)), 409,
                  "precondition_failed")
    assert out["failed"] == ["version_unsupported"]
    out = refused(put(world, "employee.yaml", "version: 1\ndepartment: hr\n", sha(YAML)), 409, "precondition_failed")
    assert out["failed"] == ["department_mismatch"]
    out = refused(put(world, "employee.yaml", "version: [1\n", sha(YAML)), 409, "precondition_failed")
    assert out["failed"] == ["yaml_invalid"]
    out = refused(put(world, "MEMORY.md", "가" * 1366, sha("")), 409, "precondition_failed")  # 4098 bytes
    assert out["failed"] == ["memory_too_large"]
    assert put(world, "notes.txt", "x", "").status_code == 422  # the four names only
    assert world.owner.put(f"/studio/employees/{world.emp}/files/GUIDE.md", json={"text": NEW}).status_code == 422
    refused(put(world, "GUIDE.md", NEW, "", emp="emp_none"), 404, "not_found")
    refused(world.owner.get("/studio/employees/emp_none/history"), 404, "not_found")
    refused(revert(world, first, emp="emp_none"), 404, "not_found")
    assert world.owner.get(f"/studio/employees/{world.emp}/history", params={"file": "notes.txt"}).status_code == 422
    for unknown in ("0" * 40, "HEAD", "main", "--all", ""):
        out = refused(revert(world, unknown), 409, "precondition_failed")
        assert out["failed"] == ["commit_unknown"]

    assert {name: read(folder, name) for name in FILES} == FILES
    assert employee_git.head(folder) == first and file_events(world) == []
    ok(put(world, "employee.yaml", YAML + "# 손본 줄\n", sha(YAML)))  # and one that passes the checks is written
    ok(put(world, "MEMORY.md", "가" * 1365, sha("")))  # 4095 bytes
    assert len(history(world)) == 3
    doc = DOC.read_text(encoding="utf-8")
    assert "`base_mismatch`" in doc and "`commit_unknown`" in doc  # §9.6 names the two codes


def test_a_revert_checks_the_employee_yaml_it_would_bring_back(world):  # noqa: F811
    bad = "version: 2\ndepartment: program\n"
    folder = settle(world, {"employee.yaml": bad})  # written by hand, before any check
    api.commit_folders()
    first = employee_git.head(folder)
    ok(put(world, "employee.yaml", YAML, sha(bad)))
    ok(put(world, "GUIDE.md", NEW, sha(OLD)))

    out = refused(revert(world, first), 409, "precondition_failed")
    assert out["failed"] == ["version_unsupported"]
    assert (read(folder, "employee.yaml"), read(folder, "GUIDE.md")) == (YAML, NEW)  # none of the files went back
    assert len(history(world)) == 3 and len(file_events(world)) == 2


def test_a_write_that_changes_nothing_and_one_that_cannot_be_committed(world, monkeypatch):  # noqa: F811
    folder = settle(world)
    api.commit_folders()
    first = employee_git.head(folder)
    out = ok(put(world, "GUIDE.md", OLD, sha(OLD)))
    assert out == {"name": "GUIDE.md", "sha256": sha(OLD), "commit": first} and file_events(world) == []

    def broken(*args):
        raise employee_git.GitError("git 이 없다")

    with monkeypatch.context() as patch:
        patch.setattr(employee_git, "commit", broken)
        out = ok(put(world, "GUIDE.md", NEW, sha(OLD)))  # the file is written all the same, and said so
    assert out["commit"] is None and read(folder, "GUIDE.md") == NEW and len(file_events(world)) == 1
    assert employee_git.head(folder) == first
    ok(put(world, "PERSONA.md", "나는 민수다. 짧게 답한다.\n", sha(FILES["PERSONA.md"])))
    assert history(world)[0]["files"] == ["GUIDE.md", "PERSONA.md"]  # the next commit holds what that one missed


# --- the global git settings stay out, and an approval takes the lock (§9.6) --------------------------------
def test_global_hooks_attributes_and_line_endings_stay_out_of_a_commit(tmp_path, monkeypatch):
    marker, hooks, attrs = tmp_path / "hook-ran", tmp_path / "hooks", tmp_path / "attrs"
    hooks.mkdir()
    for name in ("post-commit", "prepare-commit-msg"):  # the hooks `--no-verify` does not skip
        (hooks / name).write_bytes(f'#!/bin/sh\necho ran >> "{marker.as_posix()}"\n'.encode("utf-8"))
        (hooks / name).chmod(0o755)
    attrs.write_bytes(b"* text\n")
    (tmp_path / "gitconfig").write_bytes(
        f'[core]\n\thooksPath = "{hooks.as_posix()}"\n\tattributesFile = "{attrs.as_posix()}"\n'
        "\tautocrlf = true\n".encode("utf-8"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    raw = b"one\r\ntwo\n"

    def fresh(name):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "GUIDE.md").write_bytes(raw)
        return folder

    with monkeypatch.context() as patch:  # the control: without the settings that global file does reach in
        patch.setattr(employee_git, "SETTINGS", employee_git.SETTINGS[:-len(employee_git.GLOBAL_OUT)])
        loose = fresh("loose")
        first = employee_git.commit(loose, "control")
        assert marker.exists() and employee_git.show(loose, first, "GUIDE.md") == b"one\ntwo\n"
    marker.unlink()

    folder = fresh("emp_1")
    first = employee_git.commit(folder, "첫 커밋")
    assert first and not marker.exists()
    assert employee_git.show(folder, first, "GUIDE.md") == raw == (folder / "GUIDE.md").read_bytes()
    assert employee_git.commit(folder, "nothing new") is None and not marker.exists()


def test_an_approval_waits_while_the_folder_is_being_written(world):  # noqa: F811
    folder = settle(world)
    payload = {"action": "guide_amend", "draft_id": "drf_g1", "reason": "검수 표시", "file": "GUIDE.md",
               "target_employee_id": world.emp, "new_text": NEW, "diff": "-옛\n+새", "base_sha256": sha(OLD)}
    item = world.box.create("hire", Actor.employee(world.hr()[0]), title="지침 수정안", payload=payload)
    got = []

    def approve():
        try:
            got.append(world.box.decide(item["id"], "approve"))
        except Exception as e:  # noqa: BLE001 - what the approval was told is what the test reads
            got.append(e)

    worker = threading.Thread(target=approve, daemon=True)
    with employee_git.lock:  # a REST write of the folder is between its base check and its commit
        worker.start()
        worker.join(0.5)
        assert worker.is_alive() and got == [] and read(folder, "GUIDE.md") == OLD
        (folder / "GUIDE.md").write_bytes("먼저 쓴 지침\n".encode("utf-8"))
    worker.join(30)
    assert not worker.is_alive() and isinstance(got[0], inbox.InboxError), got
    assert read(folder, "GUIDE.md") == "먼저 쓴 지침\n" and world.item(item)["status"] == "pending"


# --- the approvals (§5.5)------------------------------------------------------------------------------------
def test_hire_and_guide_amend_approvals_commit_the_folder(world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(api, "_hooks", runs.EngineHooks(None))  # the server's hooks: `commit` is the real one
    ok(world.decide(world.hire(), "approve"))
    employee, = [e for e in world.store.list_employees() if e["name"] == "서연"]
    folder = employees.folder(world.dir, employee["id"])
    first, = employee_git.history(folder)
    assert (first["message"], first["files"]) == (f"hire: 서연 ({employee['id']})", sorted(FILES))

    payload = {"action": "guide_amend", "draft_id": "drf_g1", "reason": "검수 표시", "file": "GUIDE.md",
               "target_employee_id": employee["id"], "new_text": NEW, "diff": "-옛\n+새",
               "base_sha256": sha(DRAFT["guide_md"])}
    item = world.box.create("hire", Actor.employee(world.hr()[0]), title="지침 수정안", payload=payload)
    ok(world.decide(item, "approve"))
    log = history(world, "GUIDE.md", emp=employee["id"])
    assert [(c["message"], c["files"]) for c in log] == [
        (f"guide_amend: GUIDE.md ({item['id']})", ["GUIDE.md"]), (first["message"], first["files"])]
    assert employee_git.show(folder, log[1]["commit"], "GUIDE.md") == DRAFT["guide_md"].encode("utf-8")
    assert ok(revert(world, log[1]["commit"], emp=employee["id"]))["commit"] and read(folder, "GUIDE.md") == DRAFT[
        "guide_md"]
