"""Studio: the owner writing around a discussion's conclusion, an old limit notice without `first_id`, and a mention
in the conclusion (T2.1 - ARCHITECTURE.md section 4.6 and 6.6). The chat relay on the mock runner as in
test_studio_group_conclusion.py; the light model is a fake - no AI is called."""
from test_studio_group_conclusion import conclusions
from test_studio_group_judge import judged, party  # noqa: F401 - the fixture `party` and the fake light model
from test_studio_runs import lab  # noqa: F401 - the fixture `party` stands on


def test_the_owner_writing_before_the_conclusion_begins_leaves_the_old_conclusion_out(party, monkeypatch):
    a, b, c = party.order
    judged(party, {})
    turns = party.desk.turns
    first = turns._conclude
    state = {"done": False}

    def late(room_id, r, gen=None):  # the owner writes after the discussion ended, before the conclusion turn begins
        if not state["done"]:
            state["done"] = True
            party.say("아니요, 다른 이야기를 합시다")
        return first(room_id, r, gen)

    monkeypatch.setattr(turns, "_conclude", late)
    party.cast.moves = {a: ["a1", "새 토론 a", "결론: 새 토론"], b: ["b1", "새 토론 b"], c: ["c1", "새 토론 c"]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    assert party.turns() == [a, b, c, a, b, c, a]  # no conclusion between the two discussions
    (done,) = conclusions(party)
    assert done["body"] == "결론: 새 토론"
    assert done["id"] > max(m["id"] for m in party.messages() if m["author_kind"] == "owner")


def test_a_mention_in_the_conclusion_makes_no_turn(party):
    a, b, c = party.order
    judged(party, {})
    party.cast.moves = {a: ["a1", ("결론: 합의 — 현우 씨 확인", [c])], b: ["b1"], c: ["c1"]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    assert party.turns() == [a, b, c, a]  # c is mentioned in the conclusion and gets no turn
    assert party.count() == 0


def test_the_owner_writing_during_the_conclusion_lets_it_stand_and_it_is_no_speech_of_the_new_discussion(party):
    a, b, c = party.order
    judge = judged(party, {})

    def interject():  # a's conclusion turn has begun: the owner writes
        party.say("그건 그렇고 다른 문제입니다")
        return "결론: 첫 토론"

    party.cast.moves = {a: ["a1", interject, "a2", "결론: 둘째"], b: ["b1", "b2"], c: ["c1", "c2"]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    assert party.turns() == [a, b, c, a, a, b, c, a]
    assert [m["body"] for m in conclusions(party)] == ["결론: 첫 토론", "결론: 둘째"]
    assert judge.later_asked and not any("결론: 첫 토론" in facts for _, facts in judge.later_asked)


def test_continue_of_an_old_notice_without_first_id_finds_the_owner_message_that_opened_the_discussion(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(party.cfg, "studio_group_max_exchanges", 2)
    monkeypatch.setattr(party.cfg, "studio_group_rounds", 2)
    judged(party, {}, later={a: "예", b: "예", c: "예"})
    party.cast.moves = {a: ["a1", "a2", "결론: 옛 알림"], b: ["b1", "b2"], c: ["c1", "c2"]}
    party.say("이야기해 봅시다")
    party.go()
    turns = party.desk.turns
    resume = turns.resume
    monkeypatch.setattr(turns, "resume",
                        lambda room, owed, number, ids, start, first=None: resume(room, owed, number, ids, start))
    assert party.owner.post(f"/studio/rooms/{party.room}/continue").status_code == 200
    party.go()
    (done,) = conclusions(party)
    assert (done["author_id"], done["body"]) == (a, "결론: 옛 알림")  # a spoke first after the owner's message


def test_the_opening_message_is_the_newest_owner_message_at_or_before_the_start(party):
    a, b, c = party.order
    judged(party, {})
    party.cast.moves = {a: ["a1", "a2"], b: ["b1", "b2"], c: ["c1", "c2"]}
    first = party.say("처음 글")
    party.go()
    second = party.say("두 번째 글")
    party.go()
    opening = party.desk.turns._opening
    last = party.messages()[-1]["id"]
    assert opening(party.store, party.room, last) == second["id"]
    assert opening(party.store, party.room, second["id"] - 1) == first["id"]
    assert opening(party.store, party.room, first["id"] - 1) is None  # nothing of the owner's before: no conclusion
