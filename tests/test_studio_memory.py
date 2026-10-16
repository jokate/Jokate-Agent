"""An employee's memory (H-4): the reason of a rework becomes lines in the assignee's MEMORY.md through a light
model (a fake one here), the lines reach the next prompt, a file over 4KB is merged to stay within the limit, the
owner fixes or deletes one item, and a model that fails leaves the decision as it was (§9.1 · §5.5 · §6.4).
Real git runs in the temporary folder; no AI is called."""
import logging
from datetime import date
from pathlib import Path

from relay_agent.studio import api, employee_git, employees, memory
from test_studio_inbox import ok, refused, world  # noqa: F401 - the fixture these tests stand on

DOC = Path(__file__).resolve().parent.parent / "design" / "studio" / "ARCHITECTURE.md"
DAY = date(2026, 10, 14)
YAML = "version: 1\ndepartment: program\n"
LEARNED = "- 착지 판정은 발 위치로 한다\n- 완료 기준은 직접 돌려 확인한다\n없음\n"


class Ask:
    """The model: answers per system prompt, and keeps what it was asked."""

    def __init__(self, extract=LEARNED, merge=None, fail=None):
        self.extract, self.merge, self.fail, self.calls = extract, merge, fail, []

    def __call__(self, system, facts):
        which = "merge" if system == memory.MERGE_SYSTEM else "extract"
        self.calls.append((which, facts))
        if self.fail in (which, "all"):
            raise RuntimeError("모델이 답하지 못했다")
        answer = getattr(self, which)
        return (answer, object()) if which == "extract" else answer  # HandoffWriter's (text, usage) is taken too


def settle(world, task, memory_text=""):  # noqa: F811
    """The assignee's folder with its four files, as a hire leaves it."""
    folder = employees.folder(world.dir, task["assignee_id"])
    employees.write_files(folder, {"PERSONA.md": "나는 직원이다.\n", "GUIDE.md": "지침\n", "MEMORY.md": memory_text,
                                   "employee.yaml": YAML})
    return folder


def teach(world, ask):  # noqa: F811
    """The server's hook, run at once: what the thread of `api.learn_later` does."""
    world.hk.learn = lambda item: memory.learn(world.store, world.dir, item, ask, today=DAY)


def rework(world, task, item, comment="착지 판정이 틀렸다"):  # noqa: F811
    return ok(world.decide(item, "rework", comment=comment))


def read(folder):
    return (folder / "MEMORY.md").read_bytes().decode("utf-8")


def file_events(world):  # noqa: F811
    return [e["data"] for e in world.store.list_stream_events(limit=10_000)
            if e["type"] == "employee.updated" and e["data"]["change"] == "file"]


def lines(*texts):
    return "".join(f"- {t} (출처: tsk_old · 2026-01-0{i + 1})\n" for i, t in enumerate(texts))


# --- the lines --------------------------------------------------------------------------------------------------
def test_an_item_is_one_line_with_its_source_and_day():
    text = "# 배운 점\n" + lines("하나") + "- 손으로 쓴 줄\n  들여쓴 줄\n"
    found = memory.parse(text)
    assert [(i["text"], i["source"], i["date"]) for i in found] == [("하나", "tsk_old", "2026-01-01"),
                                                                     ("손으로 쓴 줄", None, None)]
    assert len({i["id"] for i in found}) == 2 and all(len(i["id"]) == 12 for i in found)
    assert memory.line_of("  둘 \n 이다", "tsk_9", "2026-10-14") == "- 둘 이다 (출처: tsk_9 · 2026-10-14)"
    assert memory.line_of("- 둘", memory.MERGED, "2026-10-14").endswith("(출처: 합침 · 2026-10-14)")


def test_only_a_final_review_with_a_reason_is_learned_from():
    item = {"kind": "final_review", "status": "decided", "decision": "rework", "decision_comment": "다시"}
    assert memory.wanted(item)
    assert not memory.wanted({**item, "decision_comment": "  "}) and not memory.wanted({**item, "decision_comment": None})
    assert not memory.wanted({**item, "kind": "question"}) and not memory.wanted({**item, "status": "pending"})


# --- a rework -> the lines -> the next prompt -------------------------------------------------------------------
def test_the_reason_of_a_rework_becomes_lines_the_next_prompt_carries(world):  # noqa: F811
    task, item = world.review()
    folder = settle(world, task)
    ask = Ask()
    teach(world, ask)
    events_before = len(file_events(world))
    out = rework(world, task, item)
    assert out["task"]["status"] == "working"

    text = read(folder)
    assert text == (f"- 착지 판정은 발 위치로 한다 (출처: {task['id']} · 2026-10-14)\n"
                    f"- 완료 기준은 직접 돌려 확인한다 (출처: {task['id']} · 2026-10-14)\n")
    (kind, facts), = ask.calls
    assert kind == "extract" and "착지 판정이 틀렸다" in facts and "rework" in facts and "테스트 통과" in facts
    employee = world.store.get_employee(task["assignee_id"])
    assert "착지 판정은 발 위치로 한다" in employees.prompt_for(world.store, world.dir, employee)
    assert employee_git.history(folder)[0]["message"] == f"memory: learn {task['id']}"
    assert "MEMORY.md" in employee_git.history(folder)[0]["files"]  # the first commit of a folder takes all four
    events = file_events(world)
    assert len(events) == events_before + 1 and events[-1]["employee"]["id"] == task["assignee_id"]

    # a second lesson that says what the file says already is not written twice
    again = Ask(extract="- 착지 판정은 발 위치로 한다\n- 새 교훈이다\n")
    memory.learn(world.store, world.dir, world.item(item), again, today=DAY)
    assert read(folder).count("착지 판정은 발 위치로 한다") == 1 and "새 교훈이다" in read(folder)


def test_an_answer_is_cut_to_three_lines_of_200_characters(world):  # noqa: F811
    task, item = world.review()
    folder = settle(world, task)
    answer = "\n".join(["* " + "가" * 300, "- 둘", "- 셋", "- 넷", "없음"])
    teach(world, Ask(extract=answer))
    rework(world, task, item)
    found = memory.parse(read(folder))
    assert [i["text"] for i in found][1:] == ["둘", "셋"] and len(found[0]["text"]) == 200 and len(found) == 3


def test_a_model_that_fails_leaves_the_decision_and_the_file(world):  # noqa: F811
    task, item = world.review()
    folder = settle(world, task, lines("전에 배운 것"))
    ask = Ask(fail="extract")
    teach(world, ask)
    out = rework(world, task, item)
    assert out["task"]["status"] == "working" and out["inbox"]["status"] == "decided"
    assert read(folder) == lines("전에 배운 것") and ask.calls and employee_git.head(folder) is None

    world.hk.learn = lambda it: (_ for _ in ()).throw(RuntimeError("훅이 터졌다"))  # even a hook that raises
    task, item = world.review()
    assert rework(world, task, item)["inbox"]["status"] == "decided"


def test_a_decision_without_a_reason_asks_no_model(world):  # noqa: F811
    task, item = world.review()
    settle(world, task)
    ask = Ask()
    teach(world, ask)
    assert ok(world.decide(item, "pass"))["task"]["status"] == "done"
    assert ask.calls == []


def test_no_assignee_or_no_lesson_writes_nothing(world):  # noqa: F811
    task, item = world.review()
    folder = settle(world, task)
    teach(world, Ask(extract="없음"))
    rework(world, task, item)
    assert read(folder) == "" and employee_git.head(folder) is None
    assert memory.learn(world.store, world.dir, {"task_id": None}, Ask(), today=DAY) is None


# --- the 4KB limit ----------------------------------------------------------------------------------------------
def big(count=None, size=30, up_to=4096):
    """Item lines of Korean text, `count` of them, or as many as fit in `up_to` bytes: within the limit, but
    with no room for the lines a learning adds."""
    out, i = "", 0
    while i < count if count is not None else len((out + "x" * 40).encode("utf-8")) <= up_to:
        out += f"- {chr(0xAC00 + i) * size} (출처: tsk_{i} · 2026-01-01)\n"
        i += 1
    return out


def test_a_file_over_the_limit_is_merged_and_stays_within_it(world):  # noqa: F811
    task, item = world.review()
    old = big()
    assert 3600 < len(old.encode("utf-8")) <= employees.MEMORY_LIMIT
    folder = settle(world, task, old)
    merged = "- 합쳐진 첫 교훈\n- 합쳐진 둘째 교훈\n"
    ask = Ask(merge=merged)
    teach(world, ask)
    rework(world, task, item)
    assert [c[0] for c in ask.calls] == ["extract", "merge"]
    text = read(folder)
    assert len(text.encode("utf-8")) <= employees.MEMORY_LIMIT and not employees.memory_problems(text)
    assert [i["text"] for i in memory.parse(text)] == ["합쳐진 첫 교훈", "합쳐진 둘째 교훈"]
    assert all(i["source"] == memory.MERGED for i in memory.parse(text))  # a line without a tail gets the merge's
    assert employee_git.history(folder)[0]["message"] == f"memory: learn {task['id']}"


def test_a_merge_that_fails_or_stays_big_drops_the_oldest_lines(world):  # noqa: F811
    for merge, fail in ((None, "merge"), (big(60), None)):
        task, item = world.review()
        folder = settle(world, task, big())
        teach(world, Ask(merge=merge, fail=fail))
        rework(world, task, item)
        text = read(folder)
        assert len(text.encode("utf-8")) <= employees.MEMORY_LIMIT
        assert "tsk_0 " not in text  # the oldest went
        assert (task["id"] in text) == (fail == "merge")  # the new line stays when the merge failed
    assert memory.fit("- 가\n" * 3000).endswith("- 가\n") and len(memory.fit("가" * 5000).encode("utf-8")) <= 4096


# --- the owner's change of one item ----------------------------------------------------------------------------
def test_the_owner_fixes_and_deletes_an_item_in_the_file_and_the_commit(world):  # noqa: F811
    task = world.task()
    folder = settle(world, task, lines("하나", "둘") + "- 손으로 쓴 줄\n")
    emp = task["assignee_id"]
    shown = ok(world.owner.get(f"/studio/employees/{emp}/memory"))
    assert [i["text"] for i in shown["items"]] == ["하나", "둘", "손으로 쓴 줄"] and shown["limit"] == 4096
    assert shown["bytes"] == len(read(folder).encode("utf-8")) and shown["commit"] is None
    first, second, hand = (i["id"] for i in shown["items"])
    before = len(file_events(world))

    out = ok(world.owner.patch(f"/studio/employees/{emp}/memory/{first}", json={"text": "고친  하나\n"}))
    assert read(folder).splitlines()[0] == "- 고친 하나 (출처: tsk_old · 2026-01-01)"  # the source is kept
    assert out["commit"] == employee_git.head(folder) and employee_git.history(folder)[0]["message"] == "memory: edit"
    assert [i["text"] for i in out["items"]][0] == "고친 하나" and len(file_events(world)) == before + 1
    assert refused(world.owner.patch(f"/studio/employees/{emp}/memory/{first}", json={"text": "또"}), 409,
                   "precondition_failed")["failed"] == ["item_unknown"]  # the old line's id is gone

    out = ok(world.owner.patch(f"/studio/employees/{emp}/memory/{hand}", json={"text": "고친 손글씨"}))
    assert read(folder).splitlines()[2] == "- 고친 손글씨"
    out = ok(world.owner.delete(f"/studio/employees/{emp}/memory/{second}"))
    assert read(folder) == "- 고친 하나 (출처: tsk_old · 2026-01-01)\n- 고친 손글씨\n"
    assert employee_git.history(folder)[0]["message"] == "memory: delete" and len(out["items"]) == 2
    assert len(file_events(world)) == before + 3 and out["sha256"] == memory.view(world.dir, emp)["sha256"]
    # the next prompt has the fixed line, not the deleted one
    prompt = employees.prompt_for(world.store, world.dir, world.store.get_employee(emp))
    assert "고친 하나" in prompt and "둘" not in prompt.split("===== MEMORY.md =====")[1]


def test_a_refused_change_writes_nothing(world):  # noqa: F811
    task = world.task()
    folder = settle(world, task, big())
    emp = task["assignee_id"]
    first = memory.parse(read(folder))[0]["id"]
    before = read(folder)
    body = refused(world.owner.patch(f"/studio/employees/{emp}/memory/{first}", json={"text": "가" * 2000}), 409,
                   "precondition_failed")
    assert body["failed"] == ["memory_too_large"] and read(folder) == before
    assert refused(world.owner.patch(f"/studio/employees/{emp}/memory/nope", json={"text": "x"}), 409,
                   "precondition_failed")["failed"] == ["item_unknown"]
    assert refused(world.owner.delete(f"/studio/employees/{emp}/memory/nope"), 409,
                   "precondition_failed")["failed"] == ["item_unknown"]
    assert refused(world.owner.patch(f"/studio/employees/{emp}/memory/{first}", json={"text": "  "}), 422,
                   "missing_fields")["fields"] == ["text"]
    refused(world.owner.get("/studio/employees/emp_nobody/memory"), 404, "not_found")
    refused(world.owner.delete(f"/studio/employees/emp_nobody/memory/{first}"), 404, "not_found")
    assert read(folder) == before and employee_git.head(folder) is None


def test_a_change_that_does_not_grow_a_file_over_the_limit_is_taken(world):  # noqa: F811
    task = world.task()
    folder = settle(world, task, big(count=60))
    emp = task["assignee_id"]
    assert len(read(folder).encode("utf-8")) > 5000
    base = f"/studio/employees/{emp}/memory/"
    first = memory.parse(read(folder))[0]["id"]
    before = len(read(folder).encode("utf-8"))
    out = ok(world.owner.delete(base + first))  # shorter, still over the limit: taken
    after = len(read(folder).encode("utf-8"))
    assert 4096 < after < before and employee_git.history(folder)[0]["message"] == "memory: delete"
    assert out["bytes"] == after
    second = memory.parse(read(folder))[0]["id"]
    ok(world.owner.patch(base + second, json={"text": "짧게"}))
    shorter = read(folder)
    assert len(shorter.encode("utf-8")) < after and employee_git.history(folder)[0]["message"] == "memory: edit"
    third = memory.parse(shorter)[0]["id"]
    body = refused(world.owner.patch(base + third, json={"text": "가" * 2000}), 409, "precondition_failed")
    assert body["failed"] == ["memory_too_large"] and read(folder) == shorter


def test_the_server_hook_learns_on_its_own_thread(world, monkeypatch):  # noqa: F811
    task, item = world.review()
    folder = settle(world, task)
    ask, models, threads = Ask(), [], []
    monkeypatch.setattr(memory, "writer", lambda model: models.append(model) or ask)
    world.hk.learn = lambda it: threads.append(api.learn_later(it))
    rework(world, task, item)
    assert len(threads) == 1
    threads[0].join(10)
    assert not threads[0].is_alive() and threads[0].daemon and threads[0].name == "studio-memory"
    assert models == [api._cfg.studio_memory_model]
    assert [i["text"] for i in memory.parse(read(folder))] == ["착지 판정은 발 위치로 한다", "완료 기준은 직접 돌려 확인한다"]
    assert employee_git.history(folder)[0]["message"] == f"memory: learn {task['id']}"


def test_the_server_hook_swallows_what_fails(world, monkeypatch, caplog):  # noqa: F811
    task, item = world.review()
    folder = settle(world, task, lines("전에 배운 것"))
    rework(world, task, item)  # no hook: nothing is learned
    decided = world.item(item)
    before = read(folder)
    monkeypatch.setattr(memory, "writer", lambda model: None)  # (가) no model on this PC
    api._learn(decided)
    assert read(folder) == before
    monkeypatch.setattr(memory, "writer", lambda model: Ask(fail="all"))  # (나) the model fails
    api._learn(decided)
    assert read(folder) == before

    def boom(*args, **kwargs):
        raise RuntimeError("터졌다")

    ask = Ask()
    monkeypatch.setattr(memory, "writer", lambda model: ask)
    monkeypatch.setattr(memory, "learn", boom)  # (다) what the learning raises is logged
    with caplog.at_level(logging.ERROR):
        api._learn(decided)
        api.learn_later(decided).join(10)
    assert "메모리 추출이 실패했다" in caplog.text and read(folder) == before
    calls = len(ask.calls)
    monkeypatch.setattr(api, "_store", None)  # (라) no store: the model is not asked
    monkeypatch.setattr(memory, "writer", lambda model: (_ for _ in ()).throw(AssertionError("not asked")))
    api._learn(decided)
    assert len(ask.calls) == calls


def test_the_document_lists_the_memory_paths():
    text = DOC.read_text(encoding="utf-8")
    for path in ("GET /studio/employees/{id}/memory`", "PATCH /studio/employees/{id}/memory/{item_id}`",
                 "DELETE /studio/employees/{id}/memory/{item_id}`", "item_unknown"):
        assert path in text
    assert "삭제와 줄이는 수정은 넘은 채로도 받는다" in text
    assert "추출 · 합치기 호출의 사용량(토큰 · 비용)은 usage DB 에 적지 않는다" in text
