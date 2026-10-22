"""T3 of the 'owner decides less' campaign: a studio task run that fails is started over once by the server (the
`restart` path: changes stay, a new conversation) and the room is only told; the failure after that is asked to the
owner as before. A run outside studio keeps the old question. A studio run that ends without a review request has
its review filed by the server. No AI is called: the mock runner raises the failures."""
import pytest

from relay_agent.studio import api, notice, push, work
from test_studio_editor_check import questions, reasons
from test_studio_inbox import ok
from test_studio_queue import line  # noqa: F401 - the fixture the tests stand on
from test_studio_runs import lab  # noqa: F401 - the fixture `line` stands on


def wire(line, monkeypatch, *, studio=True, failures=1):  # noqa: F811
    """The runner fails its first stage `failures` times; `told` is what the room would be told."""
    line.engine.studio_auto_restarts, line.engine.studio_auto_review = 1, True
    monkeypatch.setattr(work, "studio_run", lambda run_id: studio)
    told = []
    monkeypatch.setattr(notice, "tell", lambda run_id, kind, detail: told.append((run_id, kind)))
    line.gate.script = {"scout": [RuntimeError(f"boom {n}") for n in range(failures)]}
    line.gate.go.set()
    return told


def start(line):  # noqa: F811
    task = line.backlog()
    ok(line.ask(task))
    line.settled(task)
    return task, None


def kinds(line, task):  # noqa: F811
    return [(item["kind"], (item["payload"] or {}).get("reason")) for item in line.lab.pending() if item["task_id"] == task["id"]]


def test_the_first_failure_starts_the_task_over_and_only_tells_the_room(line, monkeypatch):  # noqa: F811
    told = wire(line, monkeypatch)
    task, _ = start(line)
    row = line.row(task)
    events = line.store.list_task_events(task["id"])
    restarted, = [e for e in events if e["kind"] == "queued" and (e["detail"] or {}).get("restart")]
    old, new = restarted["detail"]["restarted_from"], restarted["detail"]["run_id"]
    assert old != new and row["run_id"] == new and row["status"] != "blocked"
    assert line.engine.load(old).status in ("cancelled", "failed")
    assert not any(reason == "run_failed" for _, reason in kinds(line, task))
    assert ("restart" in [kind for _, kind in told]) and told[0] == (old, "restart")
    assert work.auto_restarts(api.run_inbox(), task["id"]) == 1


def test_the_run_started_over_goes_on_to_a_review_filed_for_it(line, monkeypatch):  # noqa: F811
    told = wire(line, monkeypatch)
    task, _ = start(line)
    assert line.run_of(task).status == "done"
    assert ("final_review", None) in kinds(line, task) and "no_review_request" not in reasons(line, task)
    assert [kind for _, kind in told] == ["restart", "continue", "review"]  # continued once, then the review is filed


def test_a_second_failure_is_asked_to_the_owner_with_restart_advised(line, monkeypatch):  # noqa: F811
    told = wire(line, monkeypatch, failures=2)
    task, _ = start(line)
    assert line.row(task)["status"] == "blocked"
    item, = [i for i in questions(line, task)]
    payload = item["payload"]
    assert payload["reason"] == "run_failed" and payload["recommended"] == "restart" and payload["auto_restarts"] == 1
    assert work.RESTARTED_AGAIN.format(n=1) in payload["body"]
    assert [kind for _, kind in told] == ["restart"]  # told once: the second failure is a question


def test_a_run_outside_studio_is_asked_at_its_first_failure(line, monkeypatch):  # noqa: F811
    told = wire(line, monkeypatch, studio=False)
    task, _ = start(line)
    assert line.row(task)["status"] == "blocked" and reasons(line, task) == ["run_failed"]
    assert told == [] and "recommended" not in questions(line, task)[0]["payload"]


def test_no_auto_restart_when_the_setting_is_zero(line, monkeypatch):  # noqa: F811
    told = wire(line, monkeypatch)
    line.engine.studio_auto_restarts = 0
    task, _ = start(line)
    assert line.row(task)["status"] == "blocked" and reasons(line, task) == ["run_failed"] and told == []


def test_a_studio_run_without_a_review_request_gets_its_review_filed(line, monkeypatch):  # noqa: F811
    told = wire(line, monkeypatch, failures=0)
    task, _ = start(line)
    assert ("final_review", None) in kinds(line, task) and "no_review_request" not in reasons(line, task)
    assert [kind for _, kind in told] == ["continue", "review"]


def continues(line, task):  # noqa: F811
    return [e for e in line.store.list_task_events(task["id"])
            if e["kind"] == "run" and (e["detail"] or {}).get("event") == "auto_continue"]


def test_a_studio_run_without_a_review_request_is_continued_once_and_then_reviewed(line, monkeypatch):  # noqa: F811
    told = wire(line, monkeypatch, failures=0)
    task, _ = start(line)
    run = line.run_of(task)
    rows = continues(line, task)
    assert len(rows) == 1 and rows[0]["detail"]["run_id"] == run.id  # the same run, once
    assert work.continued(api.run_inbox(), task["id"], run.id) and not work.continued(api.run_inbox(), task["id"], "run_other")
    assert [i for i in kinds(line, task) if i[0] == "question"] == []  # no question to the owner at any point
    assert [kind for _, kind in told] == ["continue", "review"]


def test_recovery_continues_an_old_open_question_once_and_then_the_review_is_filed(line, monkeypatch):  # noqa: F811
    told = wire(line, monkeypatch, studio=False, failures=0)
    task, _ = start(line)
    assert reasons(line, task) == ["no_review_request"] and line.row(task)["status"] == "blocked"  # outside studio: asked
    inbox, out = api.run_inbox(), {}
    with monkeypatch.context() as patch:  # a run that is not studio's: left alone
        patch.setattr(work, "_saved_mode", lambda engine, run_id: None)
        patch.setattr(notice, "mode", lambda run_id: None)
        work._release_no_review(line.engine, inbox, out)
    assert out["no_review_released"] == [] and reasons(line, task) == ["no_review_request"]
    monkeypatch.setattr(work, "studio_run", lambda run_id: True)
    monkeypatch.setattr(notice, "mode", lambda run_id: "work")
    work._release_no_review(line.engine, inbox, out)
    assert len(out["no_review_released"]) == 1 and reasons(line, task) == []
    assert len(continues(line, task)) == 1 and ("continue" in [kind for _, kind in told])
    line.settled(task)
    assert ("final_review", None) in kinds(line, task) and len(continues(line, task)) == 1  # the second end: the review


def test_recovery_leaves_the_question_of_a_run_the_server_already_continued(line, monkeypatch):  # noqa: F811
    wire(line, monkeypatch, studio=False, failures=0)
    task, _ = start(line)
    inbox, run = api.run_inbox(), line.run_of(task)
    monkeypatch.setattr(work, "studio_run", lambda run_id: True)
    monkeypatch.setattr(notice, "mode", lambda run_id: "work")
    inbox.machine.record(task["id"], work.SYSTEM, "run", work.AUTO_CONTINUE, detail=dict(run_id=run.id, event="auto_continue"))
    out = {}
    work._release_no_review(line.engine, inbox, out)
    assert out["no_review_released"] == [] and reasons(line, task) == ["no_review_request"]


def test_recovery_keeps_the_question_left_when_the_review_could_not_be_filed(line, monkeypatch):  # noqa: F811
    told = wire(line, monkeypatch, failures=0)
    monkeypatch.setattr(work, "wrap_review", lambda *a, **k: None)  # the review cannot be made
    task, _ = start(line)
    assert len(continues(line, task)) == 1  # continued once, then the review failed
    assert reasons(line, task) == ["no_review_request"] and line.row(task)["status"] == "blocked"
    assert "review" not in [kind for _, kind in told]
    monkeypatch.setattr(notice, "mode", lambda run_id: "work")
    inbox = api.run_inbox()
    for _ in range(2):  # a restart of the server, twice: the question stays, nothing is continued again
        out = {}
        work._release_no_review(line.engine, inbox, out)
        assert out["no_review_released"] == [] and reasons(line, task) == ["no_review_request"]
        assert len(continues(line, task)) == 1 and [kind for _, kind in told].count("continue") == 1


def test_a_run_outside_studio_without_a_review_request_still_asks(line, monkeypatch):  # noqa: F811
    wire(line, monkeypatch, studio=False, failures=0)
    task, _ = start(line)
    assert reasons(line, task) == ["no_review_request"]


def test_the_notices_and_the_quiet_push():
    assert push.QUIET_NOTICES["restart_notice"]
    event, body = notice._text("restart", {"why": "boom"})
    assert event == notice.RESTART_EVENT == "restart_notice" and "다시 시작" in body and "boom" in body
    assert notice._text("review", {})[0] == notice.REVIEW_EVENT
    assert "review_filed" not in push.QUIET_NOTICES  # the final_review item has its own push
    # only the room is told, no push (T4.1 decision): 작업방 안내만, 푸시는 하지 않는다
    assert notice.UNREAL_NOTICE_EVENT not in push.QUIET_NOTICES
    assert notice.CONTINUE_EVENT not in push.QUIET_NOTICES
