"""Studio final review, part 2 (S-12): what the owner's decision does to the task's run — a pass applies and
commits (one commit, no push), a rework opens the finished run again in the conversation it ended in, a revert
and the cancel of a blocked task put the folder back — and what is refused, by which code (§4.3 T7–T9 · T11,
§4.5). Task runs of the engine on the mock runner, whose build stage writes files. The project is a git
repository under pytest's tmp folder with a bare one beside it as its remote: no AI is called, and nothing is
committed anywhere else."""
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay_agent.pipeline import RelaySpec
from relay_agent.repos import RepoRegistry
from relay_agent.studio import Actor, review
from test_studio_inbox import DOC, PAYLOAD, ok, refused
from test_studio_review import COPY, YES, Editor, pack, reviewed
from test_studio_runs import lab  # noqa: F401 - the fixture `shop` stands on
from test_studio_work import shop  # noqa: F401

JUMP, AIR = "Source/jump.cpp", "Source/air.cpp"
WAS = b"int jump = 1;\n"  # what the folder holds there before the run
EDIT = {JUMP: b"int jump = 3;\n", AIR: b"int air = 1;\n"}  # one file changed, one added
IDENTITY = (("user.name", "owner"), ("user.email", "owner@example.invalid"), ("commit.gpgsign", "false"),
            ("core.autocrlf", "false"))


def git(folder, *args):
    done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return done.stdout.decode("utf-8", "replace").strip()


def commits(folder):
    return int(git(folder, "rev-list", "--count", "--all"))


def init(folder):
    git(folder, "init", "-q")
    for key, value in IDENTITY:
        git(folder, "config", key, value)


def repo(shop):  # noqa: F811
    """`before` of `reviewed` — the files are in the project folder by then, the run has not begun —: the folder
    becomes a git repository with one commit, pushed to a bare repository beside it."""
    def make(row=None, run=None):
        project, remote = shop.lab.tmp / "project", shop.lab.tmp / "remote.git"
        git(shop.lab.tmp, "init", "-q", "--bare", str(remote))
        init(project)
        git(project, "add", "-A")
        git(project, "commit", "-q", "-m", "처음")
        git(project, "remote", "add", "origin", str(remote))
        git(project, "push", "-q", "origin", "HEAD:refs/heads/main")
        assert commits(project) == commits(remote) == 1
    return make


def places(shop):  # noqa: F811
    return shop.lab.tmp / "project", shop.lab.tmp / "remote.git"


def events(shop, run, kind):  # noqa: F811
    return [event for event in shop.engine.history.events(run.id) if event["kind"] == kind]


def moved(shop, task_id):  # noqa: F811
    """The last `transition` row of that task."""
    return [row for row in shop.store.list_task_events(task_id) if row["kind"] == "transition"][-1]


def lose(shop, run):  # noqa: F811
    """The engine has that run no more: its folder is taken out of the runs folder."""
    shop.engine._dir(run.id).rename(shop.lab.tmp / f"lost-{run.id}")
    with pytest.raises(FileNotFoundError):
        shop.engine.load(run.id)


def blocked(shop):  # noqa: F811
    """A task that waits for an answer while its run, in place, went on to its end: `EDIT` is in the project
    folder — a git repository by then —, not committed. (the task, the question, the run)."""
    project, _ = places(shop)
    (project / "Source").mkdir()
    (project / JUMP).write_bytes(WAS)
    repo(shop)()
    shop.engine.repos = RepoRegistry({"game": {"path": str(project), "workspace": "inplace"}})
    shop.lab.runner = Editor(EDIT)
    row, run = shop.start(shop.task(relay="edit"))
    item = shop.lab.world.box.create("question", Actor.employee(row["assignee_id"]), title="질문",
                                     payload=PAYLOAD["question"], task_id=row["id"])
    run = shop.engine.advance(run.id)  # the run goes on to its end while the task waits for the answer
    assert shop.store.get_task(row["id"])["status"] == "blocked" and (run.status, run.changes_status) == ("done", "ready")
    return row, item, run


# --- the judge and the commit, without a run ----------------------------------------------------------------------
def test_the_codes_are_the_ones_the_contract_lists():
    text = DOC.read_text(encoding="utf-8")
    block = text.split("**T5 · T7 · T8 · T9 · T11 의 `failed[]`", 1)[1].split("### 4.4", 1)[0]
    assert tuple(re.findall(r"^\| `([a-z_]+)` \|", block, re.M)) == review.CODES


def test_the_gate_names_what_is_in_the_way_by_code():
    busy = []
    engine = SimpleNamespace(folder_holder=lambda folder, exclude=None: busy[0] if busy else None,
                             workspace=lambda run: SimpleNamespace(prepared=not run.workspace_cleaned))

    def run(status, mode="copy", cleaned=False):
        return SimpleNamespace(id="run_1", workdir="D:/game", workspace_mode=mode, changes_status=status,
                               workspace_cleaned=cleaned)

    def codes(of, stopped=True, **more):
        return {name: code for name, (code, _) in review.gate(None, None, engine, of, stopped, **more).items()}

    free = {"pass": "", "rework": "", "revert": ""}
    assert codes(None) == {**free, "pass": "run_gone", "revert": "run_gone"}  # a rework's text can wait
    assert codes(run("ready"), stopped=False) == {**free, "pass": "run_running", "revert": "run_running"}
    assert codes(run("ready")) == codes(run("none")) == codes(run("none", cleaned=True)) == free
    assert codes(run("applied", cleaned=True)) == {"pass": "", "rework": "changes_applied", "revert": "changes_applied"}
    for status in review.REVERTED:  # the same decision again is taken, another one is refused (§4.5)
        assert codes(run(status, cleaned=True)) == {"pass": "changes_reverted", "rework": "changes_reverted", "revert": ""}
    assert codes(run("ready", cleaned=True)) == {**free, "revert": "snapshot_gone"}
    busy.append("run_2")  # another run writes in that folder in place
    for mode in ("copy", "inplace"):
        assert codes(run("ready", mode)) == {**free, "pass": "folder_busy", "revert": "folder_busy"}
    # the pass decided again of what is applied commits that folder: not under another run either
    assert codes(run("applied", cleaned=True)) == {"pass": "folder_busy", "rework": "changes_applied", "revert": "changes_applied"}
    assert codes(run("none")) == free and codes(run("ready"), want=("rework",)) == {"rework": ""}
    code, why = review.gate(None, None, engine, run("ready", cleaned=True), True, instead="결재를 결정한다")["revert"]
    assert code in review.CODES and why.endswith("결재를 결정한다")
    # a run the engine has no more: nothing is there to pass, and a revert is taken without one
    assert codes(None, lost=True) == {"pass": "run_gone", "rework": "", "revert": ""}
    assert review.gate(None, None, engine, None, True, lost=True)["pass"][1].endswith("되돌리기 · 취소로 닫는다")


def test_a_run_is_lost_only_when_the_engine_has_no_run_json_under_its_id():
    def engine(error):
        def load(run_id):
            if error is not None:
                raise error
            return SimpleNamespace(id=run_id)
        return SimpleNamespace(load=load)

    assert review.lost(engine(FileNotFoundError("run.json")), "run_1") is True
    for other in (PermissionError("잠겼다"), ValueError("RunState 가 아니다"), None):  # there: locked, broken, read
        assert review.lost(engine(other), "run_1") is False
    assert review.lost(None, "run_1") is False and review.lost(engine(FileNotFoundError("run.json")), None) is False


def test_commit_is_one_commit_of_the_folder_and_none_where_there_is_nothing_to_commit(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("a\n", encoding="utf-8")
    assert review.commit(plain, "저장소가 아니다") is None and not (plain / ".git").exists()
    assert review.commit(tmp_path / "none", "없는 폴더") is None
    folder = tmp_path / "repo"
    (folder / "sub").mkdir(parents=True)
    init(folder)
    (folder / "sub" / "새 파일.txt").write_text("하나\n", encoding="utf-8")
    sha = review.commit(folder, "첫 커밋\n\n본문")  # also the first commit of a repository
    assert sha == git(folder, "rev-parse", "HEAD") and commits(folder) == 1
    assert git(folder, "log", "-1", "--format=%s") == "첫 커밋"
    assert review.commit(folder, "한 번 더") is None and commits(folder) == 1  # nothing differs from HEAD
    (folder / "sub" / "새 파일.txt").unlink()
    (folder / "b.txt").write_text("b\n", encoding="utf-8")
    assert review.commit(folder, "지우고 더한다") and commits(folder) == 2 and git(folder, "status", "--porcelain") == ""
    git(folder, "config", "user.name", "")  # git refuses a commit without a name
    (folder / "b.txt").write_text("c\n", encoding="utf-8")
    with pytest.raises(review.CommitError):
        review.commit(folder, "이름이 없다")
    assert commits(folder) == 2 and issubclass(review.CommitError, ValueError)
    # what `add` staged for the commit that failed is taken back; the files stay as they are
    assert git(folder, "diff", "--cached", "--name-only") == "" and git(folder, "diff", "--name-only") == "b.txt"
    (folder / "own.txt").write_text("owner\n", encoding="utf-8")
    git(folder, "add", "own.txt")  # what the owner had staged before the pass stays staged — and nothing else
    with pytest.raises(review.CommitError):
        review.commit(folder, "이름이 없다")
    assert git(folder, "diff", "--cached", "--name-only") == "own.txt" and git(folder, "diff", "--name-only") == "b.txt"
    review._unstage(folder, None)  # no tree to read back (an index git could not write as one): the paths are unstaged
    assert git(folder, "diff", "--cached", "--name-only") == "" and (folder / "own.txt").read_text(encoding="utf-8") == "owner\n"


# --- 통과 (T7) -------------------------------------------------------------------------------------------------------
def test_a_pass_applies_the_copy_and_commits_it_once_without_a_push(shop):  # noqa: F811
    task, item, run = reviewed(shop, "copy", COPY, repo(shop))
    project, remote = places(shop)
    assert (project / JUMP).read_bytes() == WAS and commits(project) == 1  # the work is in the copy alone
    out = ok(shop.lab.decide(item, "pass"))
    assert (out["task"]["status"], out["inbox"]["status"], out["inbox"]["delivery"]) == ("done", "decided", "none")
    assert (project / JUMP).read_bytes() == COPY[JUMP] and not (project / "Source/old.cpp").exists()
    assert commits(project) == 2 and git(project, "status", "--porcelain") == ""  # one commit, with all of it
    assert commits(remote) == 1  # and no push
    message = git(project, "log", "-1", "--format=%B")
    assert message.splitlines()[0] == task["title"] and task["id"] in message and run.id in message
    after = shop.engine.load(run.id)
    assert (after.status, after.changes_status, after.workspace_cleaned) == ("done", "applied", True)
    assert [e["detail"]["sha"] for e in events(shop, run, "changes_committed")] == [git(project, "rev-parse", "HEAD")]
    assert review.held_runs(shop.store) == set()


def test_a_pass_of_a_run_in_place_commits_the_folder_and_marks_the_changes_kept(shop):  # noqa: F811
    task, item, run = reviewed(shop, "inplace", EDIT, repo(shop))
    project, remote = places(shop)
    assert (project / JUMP).read_bytes() == EDIT[JUMP] and commits(project) == 1  # in the folder, not committed
    assert ok(shop.lab.decide(item, "pass"))["task"]["status"] == "done"
    assert commits(project) == 2 and commits(remote) == 1 and git(project, "status", "--porcelain") == ""
    assert {JUMP, AIR} <= set(git(project, "show", "--name-only", "--format=", "HEAD").split())
    after = shop.engine.load(run.id)
    assert (after.changes_status, after.workspace_cleaned) == ("applied", True)  # no `ready` run is left (§11.2)
    assert [e["detail"]["kept"] for e in events(shop, run, "changes_applied")] == [True]
    for again in (shop.engine.keep_changes, shop.engine.reopen):  # decided: neither kept twice nor opened again
        with pytest.raises(ValueError):
            again(run.id)


def test_a_pass_whose_changes_cannot_be_applied_leaves_the_task_under_review(shop):  # noqa: F811
    task, item, run = reviewed(shop, "copy", {JUMP: b"int jump = 3;\n", "Source/old.cpp": None}, repo(shop))
    project, remote = places(shop)
    (project / JUMP).write_bytes(b"int jump = 7;\n")  # the owner changed the same line in the original meanwhile
    refused(shop.lab.decide(item, "pass"), 409, "engine_error")
    assert shop.store.get_task(task["id"])["status"] == "review"
    assert shop.store.get_inbox_item(item["id"])["status"] == "pending"
    left = shop.engine.load(run.id)
    assert (left.status, left.changes_status, left.workspace_cleaned) == ("done", "ready", False)
    assert (project / JUMP).read_bytes() == b"int jump = 7;\n" and commits(project) == 1
    assert pack(shop, item)["can"] == {"pass": YES, "rework": YES, "revert": YES}  # (가): all three are open
    out = ok(shop.lab.decide(item, "revert"))
    assert out["task"]["status"] == "backlog" and shop.engine.load(run.id).changes_status == "discarded"
    assert (project / JUMP).read_bytes() == b"int jump = 7;\n" and commits(project) == commits(remote) == 1
    assert (project / "Source/old.cpp").exists()  # the file the run removed in its copy: the original keeps it


def test_a_commit_that_fails_after_the_copy_was_applied_takes_the_pass_alone_again(shop):  # noqa: F811
    task, item, run = reviewed(shop, "copy", EDIT, repo(shop))
    project, remote = places(shop)
    git(project, "config", "user.name", "")  # git refuses a commit without a name
    refused(shop.lab.decide(item, "pass"), 409, "engine_error")
    assert shop.store.get_task(task["id"])["status"] == "review" and commits(project) == 1
    assert shop.store.get_inbox_item(item["id"])["status"] == "pending"
    assert shop.engine.load(run.id).changes_status == "applied" and (project / JUMP).read_bytes() == EDIT[JUMP]
    # applied and not committed: nothing of the pass that failed stays staged
    assert git(project, "diff", "--cached", "--name-only") == "" and git(project, "status", "--porcelain") != ""
    can = pack(shop, item)["can"]
    assert can["pass"] == YES and not can["rework"]["ok"] and not can["revert"]["ok"]  # (나)
    for decision in ("rework", "revert"):
        body = refused(shop.lab.decide(item, decision, comment="다시 한다"), 409, "precondition_failed")
        assert body["failed"] == ["changes_applied"]
    git(project, "config", "user.name", "owner")
    assert ok(shop.lab.decide(item, "pass"))["task"]["status"] == "done"  # nothing is applied again: the commit alone
    assert commits(project) == 2 and commits(remote) == 1 and git(project, "status", "--porcelain") == ""
    assert len(events(shop, run, "changes_applied")) == len(events(shop, run, "changes_committed")) == 1


# --- 재작업 (T8) -----------------------------------------------------------------------------------------------------
def test_a_rework_opens_the_finished_run_again_in_the_conversation_it_ended_in(shop):  # noqa: F811
    task, item, run = reviewed(shop, "copy", EDIT)
    spec, _ = RelaySpec.load(Path(run.relay))
    names = [stage.name for stage in spec.stages]
    back = next(stage.on_retry for stage in reversed(spec.stages) if stage.on_retry)  # where work is sent back to
    session, calls, runs = shop.engine.load(run.id).done_sessions[back], len(shop.lab.runner.calls), len(shop.engine.list_runs())
    assert events(shop, run, "stage_resumed") == []

    out = ok(shop.lab.decide(item, "rework", comment="점프가 너무 높다"))
    assert (out["task"]["status"], out["task"]["run_id"]) == ("working", run.id) and out["task"]["queued_at"]
    assert out["inbox"]["delivery"] == "delivered"  # the text is in the run: the run was opened again for it
    again = shop.engine.load(run.id)
    assert (again.status, again.index, again.changes_status) == ("pending", names.index(back), "ready")
    assert again.stage_sessions == {back: session} and len(shop.engine.list_runs()) == runs  # no new run
    with pytest.raises(ValueError):
        shop.engine.reopen(run.id)  # only a run that is done

    done = shop.engine.advance(run.id, resume=True)  # what the queue does for a task in the line (queue.py `_go`)
    assert (done.id, done.status, done.changes_status) == (run.id, "done", "ready")
    assert [call.stage for call in shop.lab.runner.calls[calls:]] == names[names.index(back):]
    assert [(e["stage"], e["detail"]["session"]) for e in events(shop, run, "stage_resumed")] == [(back, session)]
    assert [e["detail"] for e in events(shop, run, "run_reopened")] == [{"to": back, "session": session}]
    assert any("점프가 너무 높다" in str(e["detail"]) for e in events(shop, run, "user_notes_applied"))
    assert done.done_sessions[back] == session  # the same conversation, not a new one

    assert shop.lab.hooks.interject(run.id, "남은 일을 마쳐라") is True  # any decision text opens a done run (T5)
    assert shop.engine.load(run.id).status == "pending" and len(events(shop, run, "run_reopened")) == 2


# --- 되돌리기 (T9) · 취소 (T11) ----------------------------------------------------------------------------------------
def test_a_revert_puts_the_folder_back_and_the_task_in_backlog(shop):  # noqa: F811
    task, item, run = reviewed(shop, "inplace", EDIT, repo(shop))
    project, remote = places(shop)
    assert (project / JUMP).read_bytes() == EDIT[JUMP] and (project / AIR).exists()
    out = ok(shop.lab.decide(item, "revert"))
    assert (out["task"]["status"], out["task"]["run_id"], out["task"]["queued_at"]) == ("backlog", None, None)
    assert (project / JUMP).read_bytes() == WAS and not (project / AIR).exists()  # as it was before the run
    assert commits(project) == commits(remote) == 1 and git(project, "status", "--porcelain") == ""
    after = shop.engine.load(run.id)
    assert (after.status, after.changes_status, after.workspace_cleaned) == ("done", "rolled_back", True)
    assert events(shop, run, "changes_committed") == [] and review.held_runs(shop.store) == set()


def test_a_revert_that_would_take_another_tasks_work_with_it_is_refused_by_code(shop):  # noqa: F811
    task, item, run = reviewed(shop, "inplace", EDIT, repo(shop))
    project, remote = places(shop)
    shop.start(shop.task(relay="demo"))  # T9 의 조건 2: another task of the project has begun since
    body = refused(shop.lab.decide(item, "revert"), 409, "precondition_failed")
    assert body["failed"] == ["others_started"] and "재작업" in body["detail"]
    shop.fx.holds = lambda task_id: task_id == task["id"]  # the run's thread has not let go of its slot (§4.6)
    for decision in ("pass", "revert"):
        assert refused(shop.lab.decide(item, decision), 409, "precondition_failed")["failed"] == ["run_running"]
    shop.fx.holds = None
    assert shop.store.get_task(task["id"])["status"] == "review" and (project / JUMP).read_bytes() == EDIT[JUMP]
    assert shop.engine.load(run.id).changes_status == "ready" and commits(project) == 1
    assert ok(shop.lab.decide(item, "pass"))["task"]["status"] == "done" and commits(project) == 2


def test_cancelling_a_blocked_task_rolls_its_changes_back(shop):  # noqa: F811
    row, item, run = blocked(shop)
    project, remote = places(shop)
    assert review.held_runs(shop.store) == review.held_runs_ro(shop.cfg.studio_db) == {run.id}  # kept for the cancel
    cancel = f"/studio/tasks/{row['id']}/cancel"

    shop.fx.holds = lambda task_id: task_id == row["id"]
    assert refused(shop.owner.post(cancel), 409, "precondition_failed")["failed"] == ["run_running"]
    shop.fx.holds = None
    assert shop.store.get_task(row["id"])["status"] == "blocked" and (project / JUMP).read_bytes() == EDIT[JUMP]

    out = ok(shop.owner.post(cancel, json={"reason": "방향을 바꾼다"}))
    assert (out["status"], out["run_id"]) == ("cancelled", None)
    assert (project / JUMP).read_bytes() == WAS and not (project / AIR).exists()
    assert shop.engine.load(run.id).changes_status == "rolled_back" and commits(project) == commits(remote) == 1
    assert shop.store.get_inbox_item(item["id"])["status"] == "cancelled" and review.held_runs(shop.store) == set()


# --- 런이 엔진에서 사라진 작업 (§4.3 `run_gone` 의 예외) ---------------------------------------------------------------
def test_a_review_whose_run_is_gone_is_closed_by_a_revert_that_reverts_nothing(shop):  # noqa: F811
    task, item, run = reviewed(shop, "inplace", EDIT, repo(shop))
    project, remote = places(shop)
    (shop.engine._dir(run.id) / "run.json").write_text("{", encoding="utf-8")  # there, and nobody can read it:
    for decision in ("pass", "revert"):  # not lost — its snapshot may be there, so nothing is taken without it
        assert refused(shop.lab.decide(item, decision), 409, "precondition_failed")["failed"] == ["run_gone"]
    lose(shop, run)
    can = pack(shop, item)["can"]
    assert not can["pass"]["ok"] and can["pass"]["why"].endswith("되돌리기 · 취소로 닫는다")
    assert can["rework"] == can["revert"] == YES
    assert refused(shop.lab.decide(item, "pass"), 409, "precondition_failed")["failed"] == ["run_gone"]
    assert shop.store.get_task(task["id"])["status"] == "review"
    out = ok(shop.lab.decide(item, "revert"))
    assert (out["task"]["status"], out["task"]["run_id"], out["inbox"]["status"]) == ("backlog", None, "decided")
    assert (project / JUMP).read_bytes() == EDIT[JUMP] and (project / AIR).exists()  # nobody could put it back
    assert commits(project) == commits(remote) == 1 and review.held_runs(shop.store) == set()
    row = moved(shop, task["id"])
    assert (row["rule"], row["to_status"]) == ("T9", "backlog") and row["reason"].endswith(review.LOST)


def test_cancelling_a_blocked_task_whose_run_is_gone_leaves_the_folder_and_says_so(shop):  # noqa: F811
    row, item, run = blocked(shop)
    project, remote = places(shop)
    lose(shop, run)
    out = ok(shop.owner.post(f"/studio/tasks/{row['id']}/cancel", json={"reason": "방향을 바꾼다"}))
    assert (out["status"], out["run_id"]) == ("cancelled", None)
    assert (project / JUMP).read_bytes() == EDIT[JUMP] and (project / AIR).exists()  # as the run left it
    assert shop.store.get_inbox_item(item["id"])["status"] == "cancelled" and review.held_runs(shop.store) == set()
    last = moved(shop, row["id"])
    assert last["rule"] == "T11" and last["reason"].startswith("방향을 바꾼다") and last["reason"].endswith(review.LOST)
