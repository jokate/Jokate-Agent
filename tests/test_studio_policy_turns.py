"""H-6: the policy of a project and the rule to ask for approval reach an employee's chat turn (fixtures of
test_studio_chat.py)."""
from pathlib import Path

from relay_agent.studio import employees
from test_studio_chat import desk  # noqa: F401
from test_studio_runs import lab  # noqa: F401 - the fixture `desk` stands on


def test_a_chat_turn_carries_the_policy_and_the_rule_to_ask_for_approval(desk):  # noqa: F811
    policy = Path(desk.cfg.studio_dir) / "projects" / desk.lab.world.pid / "POLICY.md"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("# 정책\n기획 문서 변경은 결재\n", encoding="utf-8")
    desk.answers("알겠습니다")
    desk.say("기획 문서를 고쳐 줘")
    prompt = desk.calls[0].prompt
    for text in ("기획 문서 변경은 결재", "===== " + employees.APPROVAL_RULE_TITLE + " =====", "ask_owner", "kind=approval",
                 "안 하면 어떻게 되는지"):
        assert text in prompt
    assert prompt.index("기획 문서 변경은 결재") < prompt.index("===== " + employees.APPROVAL_RULE_TITLE + " =====")


def test_a_turn_that_resumes_carries_the_policy_once_when_it_changed(desk):  # noqa: F811
    policy = Path(desk.cfg.studio_dir) / "projects" / desk.lab.world.pid / "POLICY.md"
    desk.answers("하나", "둘", "셋")
    desk.say("첫 글")
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("# 정책\n새 결재 기준 하나\n", encoding="utf-8")
    desk.say("둘째 글")
    desk.say("셋째 글")  # nothing changed
    first, second, third = desk.calls
    title = "===== " + employees.APPROVAL_RULE_TITLE + " ====="
    assert "새 결재 기준 하나" not in first.prompt
    assert "새 결재 기준 하나" in second.prompt and title in second.prompt and "owner: 둘째 글" in second.prompt
    assert "새 결재 기준 하나" not in third.prompt and title not in third.prompt


def test_the_policy_goes_again_to_a_conversation_resumed_after_a_restart(desk):  # noqa: F811
    policy = Path(desk.cfg.studio_dir) / "projects" / desk.lab.world.pid / "POLICY.md"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("# 정책\n재시작 기준\n", encoding="utf-8")
    desk.answers("하나", "둘", "셋")
    desk.say("첫 글")
    desk.say("둘째 글")
    desk.turns._policy_seen.clear()  # as after a restart
    desk.say("셋째 글")
    first, second, third = desk.calls
    assert "재시작 기준" in first.prompt and "재시작 기준" not in second.prompt and "재시작 기준" in third.prompt
