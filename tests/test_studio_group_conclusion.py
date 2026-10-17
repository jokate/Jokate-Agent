"""Studio: a discussion's turn notice, its conclusion and its usage (T2 - ARCHITECTURE.md section 4.6, 6.6, 8.4 and
12). From round 2 a turn is told the round; when the discussion stops and two or more members spoke, the one who
spoke first in round 1 writes the last message (`meta.kind = conclusion`); one who spoke alone gets none, nor does a
room held at its limit. The usage of the turns is filed under stage names of their own. The chat relay on the mock
runner as in test_studio_group_resume.py; the light model is a fake - no AI is called."""
from relay_agent.runners import Usage
from relay_agent.studio import employees, group_judge
from relay_agent.usage import UsageStore
from test_studio_group_judge import judged, party  # noqa: F401 - the fixture `party` and the fake light model
from test_studio_runs import lab  # noqa: F401 - the fixture `party` stands on


def conclusions(party):
    return [m for m in party.messages() if (m["meta"] or {}).get("kind") == "conclusion"]


def test_a_turn_of_round_two_is_told_the_round_and_one_of_round_one_is_not(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(party.cfg, "studio_group_rounds", 2)
    judged(party, {}, later={a: "아니오", b: "예 — 반박한다", c: "아니오"})
    party.cast.moves = {a: ["a1"], b: ["b1", "b2"], c: ["c1"]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    first, second = party.cast.seen[:3], party.cast.seen[3]
    assert second["employee_id"] == b and "지금 토론 2 라운드다" in second["prompt"] and "5줄 안팎" in second["prompt"]
    assert employees.round_note(2) in second["prompt"]
    assert all("라운드다" not in seen["prompt"] for seen in first)  # round 1 answers the owner's question


def test_two_who_spoke_end_with_the_conclusion_of_the_first_speaker_of_round_one(party):
    a, b, c = party.order
    judged(party, {})  # every later answer is no: the discussion stops after round 1
    party.cast.moves = {a: ["a1", "결론: 합의한 것 …"], b: ["b1"], c: [""]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    assert party.turns() == [a, b, c, a]
    (done,) = conclusions(party)
    assert (done["author_id"], done["body"]) == (a, "결론: 합의한 것 …") and party.messages()[-1]["id"] == done["id"]
    assert done["meta"]["kind"] == "conclusion" and done["run_id"] == party.cast.seen[-1]["run_id"]
    last = party.cast.seen[-1]["prompt"]
    assert employees.conclusion_note() in last and "합의한 것 / 갈린 것 / owner 가 정할 것" in last
    assert f"{party.names[b]}: b1" in last  # it reads what the others said
    assert party.count() == 0  # the conclusion is no exchange of the limit


def test_one_who_spoke_alone_gets_no_conclusion(party):
    a, b, c = party.order
    judged(party, {})
    party.cast.moves = {a: ["a1", "결론이 되면 안 된다"], b: [""], c: [""]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    assert party.turns() == [a, b, c] and conclusions(party) == []
    assert party.said()[1:] == [(a, "a1")]


def test_the_first_speaker_is_the_one_who_spoke_first_not_the_first_in_the_rooms_order(party):
    a, b, c = party.order
    judged(party, {a: "아니오 — 내 일이 아니다"})  # a is not asked, so b speaks first
    party.cast.moves = {b: ["b1", "결론: b 가 정리"], c: ["c1"], a: ["a 는 끼지 않는다"]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    assert party.turns() == [b, c, b]
    (done,) = conclusions(party)
    assert done["author_id"] == b and done["body"] == "결론: b 가 정리"


def test_a_room_held_at_its_limit_writes_no_conclusion(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(party.cfg, "studio_group_max_exchanges", 2)
    judged(party, {}, later={a: "예", b: "예", c: "예"})
    party.cast.moves = {a: ["a1", "a2", "a3"], b: ["b1", "b2", "b3"], c: ["c1", "c2", "c3"]}
    party.say("끝까지 이야기해 봅시다")
    party.go()
    assert party.turns() == [a, b, c, a, b] and len(party.notices("group_exchange_limit")) == 1
    assert conclusions(party) == []
    (notice,) = party.notices("group_exchange_limit")
    assert notice["meta"]["first_id"] == party.messages()[1]["id"]  # the owner's message that opened the discussion


def test_continue_of_a_held_discussion_ends_it_with_the_conclusion_of_its_first_speaker(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(party.cfg, "studio_group_max_exchanges", 2)
    monkeypatch.setattr(party.cfg, "studio_group_rounds", 2)
    judged(party, {}, later={a: "예", b: "예", c: "예"})
    party.cast.moves = {a: ["a1", "a2", "결론: 이어 가기 뒤"], b: ["b1", "b2"], c: ["c1", "c2"]}
    party.say("이야기해 봅시다")
    party.go()
    assert conclusions(party) == [] and party.desk.turns._round == {}  # nothing of the discussion is in memory
    assert party.owner.post(f"/studio/rooms/{party.room}/continue").status_code == 200
    party.go()
    (done,) = conclusions(party)
    assert (done["author_id"], done["body"]) == (a, "결론: 이어 가기 뒤")  # first_id came from the notice


def test_the_usage_of_a_discussion_is_filed_by_stage(party):
    a, b, c = party.order
    usage = Usage("claude", "haiku", input_tokens=5, output_tokens=3, cost_usd=0.001)
    judged(party, {a: ("예", usage), b: ("예", usage), c: ("예", usage)}, later={a: ("아니오", usage)})
    party.cast.moves = {a: ["a1", "결론"], b: ["b1"], c: ["c1"]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    store = party.lab.engine.usage
    stages = {row["stage"] for row in store.summary()}
    assert {group_judge.USAGE_STAGE, group_judge.ROUND_USAGE_STAGE, group_judge.CONCLUSION_USAGE_STAGE} <= stages
    assert "chat" not in stages  # the turns of a discussion are not filed twice, nor as plain chat
    (done,) = conclusions(party)
    assert {row["stage"] for row in store.summary(done["run_id"])} == {group_judge.CONCLUSION_USAGE_STAGE}
    first = party.messages()[2]  # a's answer of round 1
    assert {row["stage"] for row in store.summary(first["run_id"])} == {group_judge.ROUND_USAGE_STAGE}


def test_relabel_moves_only_the_rows_of_one_run_and_stage(tmp_path):
    store = UsageStore(tmp_path / "usage.db")
    u = Usage("claude", "m", input_tokens=1, output_tokens=1, cost_usd=0.5)
    for run_id, stage in (("r1", "chat"), ("r1", "other"), ("r2", "chat")):
        store.record(run_id, stage, u)
    assert store.relabel("r1", "chat", "studio:group_round") == 1
    assert sorted((row["stage"], row["calls"]) for row in store.summary("r1")) == [("other", 1), ("studio:group_round", 1)]
    assert [row["stage"] for row in store.summary("r2")] == ["chat"]
    assert sum(v["cost_usd"] for v in store.by_runner_since("1970-01-01").values()) == 1.5  # no row was added
