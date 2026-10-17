"""Studio: 'continue' of a group room held at its limit opens the next round of its discussion (T1.1 -
ARCHITECTURE.md section 4.6 and 6.6), also after a restart: the round is remembered in the limit notice's `meta`
(`round` - `start_id` - `ids`), not in memory. The chat relay on the mock runner as in test_studio_group_judge.py."""
from test_studio_group_judge import judged, party  # noqa: F401 - the fixture `party` and the fake light model
from test_studio_runs import lab  # noqa: F401 - the fixture `party` stands on


def test_continue_of_a_room_held_in_a_discussion_opens_the_next_round(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(party.cfg, "studio_group_max_exchanges", 2)
    judged(party, {}, later={a: "예", b: "예", c: "예"})
    party.cast.moves = {a: ["a1", "a2", "a3"], b: ["b1", "b2", "b3"], c: ["c1", "c2", "c3"]}
    party.say("끝까지 이야기해 봅시다")
    party.go()
    assert party.turns() == [a, b, c, a, b] and party.count() == 2
    (notice,) = party.notices("group_exchange_limit")
    assert notice["meta"]["round"] == 2 and isinstance(notice["meta"]["start_id"], int)
    assert sorted(notice["meta"]["ids"]) == sorted(party.order)
    assert party.desk.turns._round == {}  # nothing of the discussion is in memory: as after a restart

    monkeypatch.setattr(party.cfg, "studio_group_max_exchanges", 6)
    r = party.owner.post(f"/studio/rooms/{party.room}/continue")
    assert r.status_code == 200 and r.json()["exchange_count"] == 0
    party.go()
    assert party.turns() == [a, b, c, a, b, a, b, c, a]  # (the last: the conclusion's turn) round 3 was judged and made; the most rounds (3) ends it
    assert party.count() == 3 and len(party.notices("group_exchange_limit")) == 1


def test_continue_of_a_room_held_in_the_last_round_opens_nothing(party, monkeypatch):
    a, b, c = party.order
    monkeypatch.setattr(party.cfg, "studio_group_max_exchanges", 2)
    monkeypatch.setattr(party.cfg, "studio_group_rounds", 2)
    judged(party, {}, later={a: "예", b: "예", c: "예"})
    party.cast.moves = {a: ["a1", "a2"], b: ["b1", "b2"], c: ["c1", "c2"]}
    party.say("이야기해 봅시다")
    party.go()
    before = party.turns()
    assert party.owner.post(f"/studio/rooms/{party.room}/continue").status_code == 200
    party.go()
    assert party.turns() == before + [a]  # round 2 was the last: no round 3 to open, the discussion is closed (T2)


def test_continue_of_an_old_notice_without_a_round_puts_in_only_the_held_mentions(party, monkeypatch):
    a, b, _ = party.order
    party.hold(monkeypatch)
    (notice,) = party.notices("group_exchange_limit")
    assert "round" not in notice["meta"] and "start_id" not in notice["meta"]
    party.cast.moves = {b: ["이어서 답합니다"]}
    assert party.owner.post(f"/studio/rooms/{party.room}/continue").status_code == 200
    party.go()
    assert party.turns() == [a, b, a, b]
