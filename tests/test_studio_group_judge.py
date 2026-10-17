"""Studio: the quick judgment of a group room (T5 · ARCHITECTURE.md §6.6) and the mention that goes right after the
running turn. The chat relay on the mock runner as in test_studio_group.py; the light model is a fake `Judge`
given to `ChatTurns` — no AI is called."""
import time

import pytest

from relay_agent.runners import Usage
from relay_agent.studio import group_judge
from test_studio_group import Party
from test_studio_runs import lab  # noqa: F401 - the fixture `party` stands on


@pytest.fixture
def party(lab, monkeypatch):  # noqa: F811
    return Party(lab, monkeypatch)


class Judge:
    """The light model of the quick judgment: answers by the name in the facts (`says`: name -> the answer, or an
    exception to raise, or a number of seconds to sleep before a late answer). Keeps what it was asked."""

    def __init__(self, says, later=None):
        """`later`: the answers for the rounds after the first (`ROUND_SYSTEM`), by name; no answer = no."""
        self.says, self.later, self.asked, self.later_asked = says, later or {}, [], []

    def __call__(self, system, facts):
        name = facts.splitlines()[0].removeprefix("이름: ")
        if system == group_judge.ROUND_SYSTEM:
            self.later_asked.append((name, facts))
            what = self.later.get(name, "아니오 — 동의한다")
            if isinstance(what, Exception):
                raise what
            return what
        self.asked.append((name, facts))
        what = self.says.get(name, "예 — 관련이 있다")
        if isinstance(what, Exception):
            raise what
        if isinstance(what, (int, float)):
            time.sleep(what)
            return "아니오 — 늦은 답"
        return what


def judged(party, says, later=None):
    """Give the turns a `Judge`; `says` (and `later`, the rounds after the first) is keyed by employee id (the
    room's order is not the order of the names)."""
    judge = party.desk.turns._judge_ask = Judge({party.names[key]: what for key, what in says.items()},
                                                {party.names[key]: what for key, what in (later or {}).items()})
    return judge


def test_the_judgment_gives_a_turn_only_to_who_says_yes_in_the_rooms_order(party):
    a, b, c = party.order
    judge = judged(party, {a: "아니오 — 내 일이 아니다", b: "예 — 내 일이다", c: "예"})
    party.cast.moves = {b: ["제가 보겠습니다"], c: ["저도 돕겠습니다"], a: ["끼어듭니다"]}
    party.say("점프 버그 누가 볼래요?")
    assert judge.asked == []  # the POST does not wait for a model
    party.go()
    assert sorted(name for name, _ in judge.asked) == sorted(party.names.values()) and party.turns() == [b, c, b]
    assert party.said() == [("owner", "점프 버그 누가 볼래요?"), (b, "제가 보겠습니다"), (c, "저도 돕겠습니다")]
    facts = dict(judge.asked)[party.names[b]]  # name · part · the line of work · recent messages · the new message
    assert "파트: " in facts and "맡은 일: " in facts and "새 글:\n점프 버그 누가 볼래요?" in facts
    assert party.notices("group_nobody") == [] and party.count() == 0


def test_a_judgment_that_fails_or_is_late_or_unreadable_reads_as_yes(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(group_judge, "TIMEOUT_S", 0.2)
    judged(party, {a: RuntimeError("모델이 죽었다"), b: 1.0, c: "글쎄요"})
    party.cast.moves = {a: ["a"], b: ["b"], c: ["c"]}
    party.say("누가 볼래요?")
    party.go()
    assert party.turns() == [a, b, c, a]  # + the conclusion: the first speaker of round 1 closes a discussion of two or more (T2)
    assert party.notices("group_nobody") == []


def test_when_nobody_says_yes_the_room_is_told_once_and_no_turn_is_made(party):
    a, b, c = party.order
    judge = judged(party, {a: "아니오", b: "아니요 — 아니다", c: "No, not mine"})
    party.say("그냥 혼잣말입니다")
    party.go()
    assert party.turns() == []
    [notice] = party.notices("group_nobody")
    assert notice["body"] == "답할 직원이 없다고 판단했습니다 — 멘션하면 그 직원이 답합니다"
    assert len(judge.asked) == 3 and party.count() == 0
    # a mention answers all the same, with no judgment
    party.cast.moves = {b: ["네, 말씀하세요"]}
    party.say("지수 씨 들어 보세요", mentions=[b])
    party.go()
    assert party.turns() == [b] and len(judge.asked) == 3
    assert len(party.notices("group_nobody")) == 1


def test_a_blank_setting_asks_no_judgment_and_every_member_gets_a_turn(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(party.cfg, "studio_group_judge_model", "")
    judge = judged(party, {a: "아니오", b: "아니오", c: "아니오"})
    party.cast.moves = {a: ["a"], b: ["b"], c: ["c"]}
    party.say("누가 볼래요?")
    party.go()
    assert judge.asked == [] and party.turns() == [a, b, c]


def test_a_message_that_mentions_asks_no_judgment(party):
    a, b, c = party.order
    judge = judged(party, {a: "아니오", b: "아니오", c: "아니오"})
    party.cast.moves = {a: ["a"], c: ["c"]}
    party.say("둘만 봐 주세요", mentions=[a, c])
    party.go()
    assert judge.asked == [] and party.turns() == [a, c]


def test_an_employees_mention_goes_right_after_the_running_turn_not_to_the_end(party):
    a, b, c = party.order
    judged(party, {})
    party.cast.moves = {a: [("지수 씨 봐 주세요", [b])], b: ["봤습니다"], c: ["저도 있습니다"]}
    party.say("둘만 봐 주세요", mentions=[a, c])  # the line: a, c
    party.go()
    assert party.turns() == [a, b, c]  # b was put after a, before c — not after c
    assert party.count() == 1


def test_a_mention_moves_one_who_waits_to_the_next_seat_and_it_is_not_counted(party):
    a, b, c = party.order
    judged(party, {})
    party.cast.moves = {a: [("지수 씨 봐 주세요", [c])], b: ["봤습니다"], c: ["저도 있습니다"]}
    party.say("모두 봐 주세요", mentions=[a, b, c])  # the line: a, b, c
    party.go()
    assert party.turns() == [a, c, b]  # c waited behind b; the mention moved it right after a
    assert party.count() == 0  # c's turn was the owner's, so it is not counted


def test_a_mention_chain_back_and_forth_stops_at_the_limit_and_skips_the_judgment(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(party.cfg, "studio_group_max_exchanges", 2)
    judge = judged(party, {a: "예", b: "아니오", c: "아니오"})
    party.cast.moves = {a: [("지수 씨 차례입니다", [b]), ("다시 지수 씨 차례입니다", [b])],
                        b: [("민수 씨 차례입니다", [a])]}
    party.say("둘이 이야기해 보세요")
    party.go()
    assert party.turns() == [a, b, a]  # a → b → a, then b's turn is held: the count is at 2
    assert party.count() == 2 and len(party.notices("group_exchange_limit")) == 1
    assert len(judge.asked) == 3  # asked once for the owner's message; the mentions were not judged


def test_the_judgment_usage_is_filed_under_its_own_stage(party, monkeypatch):
    a, b, c = party.order
    filed = []
    monkeypatch.setattr(party.lab.engine.usage, "record", lambda run_id, stage, u: filed.append((run_id, stage, u)))
    usage = Usage("claude", "haiku", input_tokens=5, output_tokens=3, cost_usd=0.001)
    judged(party, {a: ("예 — 맞다", usage), b: ("아니오", usage), c: "예"})
    party.say("누가 볼래요?")
    party.go()
    own = [item for item in filed if item[1] == "studio:group_judge"]  # the turns' own usage goes in as well
    assert [(run_id, stage) for run_id, stage, _ in own] == [(f"room:{party.room}", "studio:group_judge")] * 2
    assert all(u is usage for _, _, u in own)


def test_the_judgment_answers_and_the_job_line_are_read_as_written():
    answers = ("예 — 맞다", "네", "Yes.", "아니오: 아니다", "아니요", "no", "", "글쎄", ("예", None))
    assert [group_judge.parse(x) for x in answers] == [True, True, True, False, False, False, None, None, True]
    assert group_judge.job_line("# 민수\n- 맡은 일: 점프 버그를 고친다\n- 말투: 짧게") == "점프 버그를 고친다"
    assert group_judge.job_line("# 민수\n- 말투: 짧게") == "" and group_judge.job_line(None) == ""
    assert group_judge.decide({}, lambda s, f: "아니오") == set()


# --- the rounds of a discussion (T1 · §6.6) -----------------------------------------------------------------------
def test_a_round_goes_on_with_the_members_who_say_yes_and_they_are_counted(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(party.cfg, "studio_group_rounds", 2)
    judge = judged(party, {}, later={a: "아니오 — 동의한다", b: "예 — 반박할 것이 있다", c: "예"})
    party.cast.moves = {a: ["a1"], b: ["b1", "b2"], c: ["c1", "c2"]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    assert party.turns() == [a, b, c, b, c, a]  # round 1 in the room's order, then who said yes, then the conclusion's turn (a passes)
    assert party.said()[1:] == [(a, "a1"), (b, "b1"), (c, "c1"), (b, "b2"), (c, "c2")]
    assert party.count() == 2 and party.notices("group_nobody") == []  # only the turns of round 2 are exchanges
    assert sorted(name for name, _ in judge.later_asked) == sorted(party.names.values())
    facts = dict(judge.later_asked)[party.names[a]]  # what the round before left, to react to
    assert "직전 라운드 글:" in facts and f"{party.names[b]}: b1" in facts and f"{party.names[c]}: c1" in facts and "점프 버그 어떻게" in facts
    assert len(judge.asked) == 3  # the first round is judged once


def test_the_rounds_stop_when_everybody_says_no_and_the_room_is_told_nothing(party):
    a, b, c = party.order
    judge = judged(party, {})  # every later answer is no
    party.cast.moves = {a: ["a1"], b: ["b1"], c: ["c1"]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    assert party.turns() == [a, b, c, a] and len(judge.later_asked) == 3  # + the conclusion's turn (T2)
    assert party.notices("group_nobody") == [] and party.notices("group_exchange_limit") == [] and party.count() == 0


def test_the_rounds_stop_at_the_most_rounds_of_the_setting(party):
    a, b, c = party.order
    judge = judged(party, {}, later={a: "예", b: "예", c: "예"})  # default studio_group_rounds = 3
    party.cast.moves = {a: ["a1", "a2", "a3", "a4"], b: ["b1", "b2", "b3", "b4"], c: ["c1", "c2", "c3", "c4"]}
    party.say("끝까지 이야기해 봅시다")
    party.go()
    assert party.turns() == [a, b, c] * 3 + [a] and len(judge.later_asked) == 6  # round 2 and 3 were judged, not 4
    assert party.count() == 6 and party.notices("group_exchange_limit") == []  # the rounds came first


def test_the_rounds_stop_at_the_exchange_limit_too_and_it_is_told_once(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(party.cfg, "studio_group_max_exchanges", 2)
    judge = judged(party, {}, later={a: "예", b: "예", c: "예"})
    party.cast.moves = {a: ["a1", "a2", "a3"], b: ["b1", "b2", "b3"], c: ["c1", "c2", "c3"]}
    party.say("끝까지 이야기해 봅시다")
    party.go()
    assert party.turns() == [a, b, c, a, b]  # c's turn of round 2 is held: the count is at 2
    assert party.count() == 2 and len(party.notices("group_exchange_limit")) == 1  # round 3 did not tell again
    assert len(judge.later_asked) == 3


def test_a_judgment_of_a_round_that_fails_or_is_unreadable_reads_as_yes(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(party.cfg, "studio_group_rounds", 2)
    judged(party, {}, later={a: RuntimeError("모델이 죽었다"), b: "글쎄요", c: "아니오"})
    party.cast.moves = {a: ["a1", "a2"], b: ["b1", "b2"], c: ["c1"]}
    party.say("이야기해 봅시다")
    party.go()
    assert party.turns() == [a, b, c, a, b, a]  # + the conclusion: the first speaker of round 1 closes a discussion of two or more (T2)


def test_the_owner_writing_in_a_discussion_drops_the_turns_not_begun_and_starts_at_round_one(party):
    a, b, c = party.order
    judge = judged(party, {})

    def interject():  # the owner writes while a's turn runs; b and c have not begun
        party.say("아니요, 이렇게 합시다")
        return "a1"

    party.cast.moves = {a: [interject, "a2"], b: ["b1"], c: ["c1"]}
    party.say("점프 버그 어떻게 할까요?")
    party.go()
    assert party.turns() == [a, a, b, c, a]  # (the last: the conclusion's turn) a's turn went to its end; b and c began only for the new message
    assert [text for who, text in party.said() if who == "owner"] == ["점프 버그 어떻게 할까요?", "아니요, 이렇게 합시다"]
    assert len(judge.asked) == 6  # the first message was judged once for a, the new one for all
    assert [facts.split("새 글:\n")[1] for _, facts in judge.asked].count("아니요, 이렇게 합시다") == 3


def test_an_owner_message_written_twice_before_the_line_runs_judges_only_the_new_one(party):
    a, b, c = party.order
    judge = judged(party, {})
    party.cast.moves = {a: ["a1"], b: ["b1"], c: ["c1"]}
    party.say("첫째 글")
    party.say("둘째 글")
    party.go()
    assert party.turns() == [a, b, c, a] and {facts.split("새 글:\n")[1] for _, facts in judge.asked} == {"둘째 글"}


def test_a_mention_in_round_two_pulls_the_member_to_the_next_seat(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(party.cfg, "studio_group_rounds", 2)
    judged(party, {}, later={a: "예", b: "예"})
    party.cast.moves = {a: ["a1", ("현우 씨 봐 주세요", [c])], b: ["b1", "b2"], c: ["c1", "c2"]}
    party.say("이야기해 봅시다")
    party.go()
    assert party.turns() == [a, b, c, a, c, b, a]  # (the last: the conclusion) c was not asked in round 2: a's mention put it right after a
    assert party.count() == 3


def test_a_message_that_mentions_or_a_blank_setting_opens_no_discussion(party, monkeypatch):
    a, b, c = party.order
    judge = judged(party, {}, later={a: "예", b: "예", c: "예"})
    party.cast.moves = {a: ["a1", "a2"], b: ["b1"], c: ["c1"]}
    party.say("민수 씨만 봐 주세요", mentions=[a])
    party.go()
    assert party.turns() == [a] and judge.later_asked == []
    monkeypatch.setattr(party.cfg, "studio_group_judge_model", "")
    party.say("모두 이야기해 봅시다")
    party.go()
    assert party.turns() == [a, a, b, c] and judge.later_asked == []


def test_a_one_to_one_room_has_no_judgment_and_no_round(party):
    judge = judged(party, {}, later={party.desk.emp: "예"})
    party.cast.moves = {party.desk.emp: ["안녕하세요", "또 말합니다"]}
    r = party.owner.post(f"/studio/rooms/{party.desk.dm}/messages", json={"body": "안녕"})
    assert r.status_code == 201, r.text
    party.go()
    assert party.turns() == [party.desk.emp] and judge.asked == [] and judge.later_asked == []
