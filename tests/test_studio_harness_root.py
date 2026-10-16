"""The harness root and the '하네스' section of a prompt (ARCHITECTURE §9.1 · §12.5). A fake harness in a tmp folder:
the real one's README / SKILL.md heads and the junction on this PC are M-5 of VERIFICATION.md."""
import os
from pathlib import Path

import pytest

from relay_agent.studio import employees, harness_root


def fake_harness(base: Path, skills=("a", "b")) -> Path:
    root = base / "game-harness"
    (root / "skills").mkdir(parents=True)
    (root / "README.md").write_text("# Jokate\n\n행동하기 전에 읽는다.\n원칙은 둘이다.\n\n셋째 줄은 안 쓴다.\n", encoding="utf-8")
    for name in skills:
        (root / "skills" / name).mkdir()
        (root / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} 을 할 때\n둘째 줄\n---\n본문\n".replace("description: " + name + " 을 할 때\n둘째 줄",
            f"description: |\n  {name} 을 할 때\n  둘째 줄"), encoding="utf-8")
    return root


def link_to(home: Path, root: Path) -> Path:
    """`home/.claude/skills/game-onboard` -> `root/skills/a`: a symlink, else a junction, else skip."""
    link = home.joinpath(*harness_root.LINK)
    link.parent.mkdir(parents=True)
    target = root / "skills" / "a"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        except Exception:  # noqa: BLE001
            pytest.skip("neither a symlink nor a junction can be made here")
    return link


def test_a_set_folder_is_the_root_and_a_missing_one_is_none(tmp_path):
    root = fake_harness(tmp_path)
    assert harness_root.resolve_root(str(root)) == root
    assert harness_root.resolve_root(str(tmp_path / "none")) is None
    assert harness_root.section_for(str(tmp_path / "none")) == ""


def test_an_empty_setting_follows_the_junction_two_levels_up(tmp_path):
    root = fake_harness(tmp_path)
    home = tmp_path / "home"
    link_to(home, root)
    assert harness_root.resolve_root("", home) == root.resolve()
    assert harness_root.resolve_root(None, home) == root.resolve()
    assert harness_root.resolve_root("", str(home)) == root.resolve()


def test_no_link_and_a_copy_give_none_and_the_default_home_is_not_this_pcs(tmp_path):
    assert harness_root.resolve_root("", tmp_path / "home") is None
    assert harness_root.resolve_root("") is None  # conftest: default_home() is an empty folder
    copy = tmp_path / "copy" / ".claude" / "skills" / "game-onboard"
    copy.mkdir(parents=True)
    (copy / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    assert harness_root.resolve_root("", tmp_path / "copy") is None  # ~/.claude has a skills/ too: not the root
    assert harness_root.section_for("", tmp_path / "copy") == ""


def test_the_section_holds_the_principle_and_a_line_per_skill(tmp_path):
    text = harness_root.section(fake_harness(tmp_path))
    assert text.splitlines() == ["행동하기 전에 읽는다.", "원칙은 둘이다.", "- a — a 을 할 때", "- b — b 을 할 때"]
    assert harness_root.section(None) == ""


def test_a_skill_with_a_broken_head_is_left_out_and_a_nameless_one_has_its_folder_name(tmp_path):
    root = fake_harness(tmp_path, skills=("a",))
    (root / "skills" / "bad").mkdir()
    (root / "skills" / "bad" / "SKILL.md").write_text("---\nname: [oops\n---\n", encoding="utf-8")
    (root / "skills" / "plain").mkdir()
    (root / "skills" / "plain" / "SKILL.md").write_text("---\ndescription: 이름 없이\n---\n", encoding="utf-8")
    assert harness_root.section(root).splitlines()[2:] == ["- a — a 을 할 때", "- plain — 이름 없이"]


def test_a_long_list_is_cut_at_whole_lines_within_the_limit(tmp_path):
    root = fake_harness(tmp_path, skills=[f"skill{n:03d}" for n in range(200)])
    text = harness_root.section(root)
    assert len(text.encode("utf-8")) <= harness_root.SECTION_LIMIT == 1500
    lines = text.splitlines()
    assert lines[0] == "행동하기 전에 읽는다." and lines[-1].startswith("- skill") and lines[-1].endswith(" 을 할 때")
    assert len(lines) < 200 and lines[-1] == f"- skill{len(lines) - 3:03d} — skill{len(lines) - 3:03d} 을 할 때"  # from the front
    one = harness_root.section(root, limit=7)  # one line longer than the limit: cut between characters
    assert len(one.encode("utf-8")) <= 7 and one.startswith("행동")


def test_the_section_comes_after_the_policy_and_before_the_approval_rule():
    text = employees.assemble("P", "G", None, "정책", "최근", rule="규칙", harness="원칙\n- a — 언제")
    marks = ["===== PERSONA.md", "===== GUIDE.md", "===== 프로젝트 정책", "===== 하네스 =====\n원칙\n- a — 언제",
             "===== 결재 규칙", "===== 방의 최근 글"]
    assert [text.index(mark) for mark in marks] == sorted(text.index(mark) for mark in marks)
    assert "하네스" not in employees.assemble("P", "G", None, "정책", "최근", rule="규칙")
    assert employees.HARNESS_TITLE == harness_root.SECTION_TITLE
