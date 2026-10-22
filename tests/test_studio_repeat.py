"""§4.7: a run that fails for the same reason again is told so and `restart` is recommended; a run the owner told to
`wrap_up` that ends without a review has the server file the final review (T6 by the system) with what could not be
verified. The demo relay on the mock runner, whose `build` stage is made to fail."""
from pathlib import Path

from relay_agent.runners import RunnerError
from relay_agent.studio import api, work
from relay_agent.studio.tasks import Actor
from test_studio_inbox import ok
from test_studio_queue import line  # noqa: F401 - the fixture the tests stand on
from test_studio_runs import lab  # noqa: F401 - the fixture `line` stands on
from test_studio_ws import wait

SAME = "빌드 · PIE 를 못 해 측정 표가 없다"


def _fails(line, errors):  # noqa: F811
    """The build stage raises the next of `errors` (the last one again and again). Returns the real runner."""
    stage, calls = line.gate.run, []

    def breaks(call):
        if call.stage == "build":
            calls.append(call)
            raise RunnerError(errors[min(len(calls), len(errors)) - 1])
        return stage(call)

    line.gate.run = breaks
    line.gate.go.set()
    return stage


def _pending(line, task):  # noqa: F811
    return [i for i in line.store.list_inbox(task_id=task["id"]) if i["status"] == "pending"]


def _next(line, task, item, option, **more):  # noqa: F811
    """Decide `item` with that option and wait for what follows the run: the pending items of the task."""
    ok(line.lab.decide(item, "answer", data={"option_id": option}, **more))
    wait(lambda: line.q.idle(), 30)
    line.settled(task)
    return _pending(line, task)


def test_a_second_failure_for_the_same_reason_recommends_restart_and_questions_do_not_pile_up(line):  # noqa: F811
    _fails(line, [f"{SAME} (시도 1)", f"{SAME} (시도 2)", f"{SAME} (시도 3)", "전혀 다른 문제: 디스크가 가득 찼다"])
    task = line.backlog()
    ok(line.ask(task))
    line.settled(task)
    [first] = line.lab.pending()
    assert "번째 실패" not in first["payload"]["body"] and "recommended" not in first["payload"]
    seen = [first]
    for n in (2, 3):
        [item] = _next(line, task, seen[-1], "retry")
        assert item["id"] != seen[-1]["id"] and item["payload"]["reason"] == "run_failed"
        assert f"같은 이유로 {n}번째 실패입니다 — 다시 시도보다 '새로 시작'이나 '검수 요청'을 권합니다" in item["payload"]["body"]
        assert item["payload"]["recommended"] == "restart" and item["payload"]["failures"] == n
        assert [o["id"] for o in item["payload"]["options"]] == ["retry", "wrap_up", "restart"]
        assert line.store.get_inbox_item(seen[-1]["id"])["status"] == "decided"  # the earlier one is closed
        seen.append(item)
    assert len(_pending(line, task)) == 1
    [other] = _next(line, task, seen[-1], "retry")  # a reason of another kind is the first of its own
    assert "번째 실패" not in other["payload"]["body"] and "recommended" not in other["payload"]


def test_similar_errors_ignore_numbers_ids_and_spacing_but_not_the_reason():
    assert work.similar("빌드 실패: run 20261019-201021-ac5003  (3회)", "빌드 실패: run 20261020-090000-bb1122 (4회)")
    assert work.similar("Build failed at 12:03 in /tmp/ab12cd34ef56", "build  failed at 9:44 in /tmp/99aa88bb77cc")
    assert not work.similar(SAME, "디스크가 가득 찼다")
    assert not work.similar("", "")


def _wrapped(line, errors, *, done=False):  # noqa: F811
    """A run that failed once, with a file it changed. `done`: the next try goes through (the run finishes)."""
    stage = _fails(line, errors)
    task = line.backlog()
    ok(line.ask(task))
    line.settled(task)
    [item] = line.lab.pending()
    patch = "diff --git a/측정.txt b/측정.txt\nnew file mode 100644\n--- /dev/null\n+++ b/측정.txt\n@@ -0,0 +1 @@\n+반쯤 한 일\n"
    (line.engine._dir(line.row(task)["run_id"]) / "result.patch").write_text(patch, encoding="utf-8")
    if done:
        line.gate.run = stage
    return task, item


def test_a_wrap_up_that_fails_again_goes_to_final_review_with_what_could_not_be_verified(line):  # noqa: F811
    task, item = _wrapped(line, ["빌드 도구가 죽었다"])
    [review] = _next(line, task, item, "wrap_up")
    assert (review["kind"], review["requester_kind"], line.row(task)["status"]) == ("final_review", "system", "review")
    payload = review["payload"]
    assert "바뀐 파일:" in payload["summary"] and "측정.txt" in payload["summary"]
    [check] = payload["checklist"]
    assert check["status"] == "unverified" and check["note"].startswith("검증 못 함: ") and "빌드 도구가 죽었다" in check["note"]
    assert not line.lab.pending() or all(i["kind"] == "final_review" for i in line.lab.pending())
    ok(line.lab.decide(review, "rework", comment="측정만 다시"))  # the owner can send it back (pass · revert too)
    assert line.row(task)["status"] == "working"


def test_a_wrap_up_whose_run_finished_without_a_review_goes_to_final_review(line):  # noqa: F811
    task, item = _wrapped(line, ["빌드 도구가 죽었다"], done=True)
    [review] = _next(line, task, item, "wrap_up")
    assert (review["kind"], review["requester_kind"], line.row(task)["status"]) == ("final_review", "system", "review")
    [check] = review["payload"]["checklist"]
    assert check["note"] == "검증 못 함: 런이 검수 요청 없이 끝났다" and "측정.txt" in review["payload"]["summary"]


def test_a_review_the_employee_files_after_a_wrap_up_is_its_own(line):  # noqa: F811
    task, item = _wrapped(line, ["빌드 도구가 죽었다"], done=True)
    line.gate.go.clear()
    ok(line.lab.decide(item, "answer", data={"option_id": "wrap_up"}))
    wait(lambda: line.gate.now >= 1, 30)  # the run goes on, the employee is at work
    mine = Actor("employee", line.row(task)["assignee_id"])
    payload = {"summary": "측정은 못 했다", "checklist": [{"criterion": "표", "status": "unverified", "note": "빌드 못 함"}]}
    review = api.run_inbox().create("final_review", mine, payload=payload, task_id=task["id"])
    line.gate.go.set()
    wait(lambda: line.q.idle(), 30)
    reviews = line.store.list_inbox(task_id=task["id"], kind="final_review")
    assert [(r["id"], r["requester_kind"]) for r in reviews] == [(review["id"], "employee")]  # none from the server
    assert line.row(task)["status"] == "review"
