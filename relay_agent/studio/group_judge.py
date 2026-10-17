"""The quick judgment of a group room (T5 · ARCHITECTURE.md §4.6 · §6.6): the owner's message without a mention
asks every member, and before the members' turns are made a light model is asked once per member — at the same
time — whether the message is that member's business. Only the ones who say yes get a turn.

The model is `ask(system, facts) -> text | (text, usage)` as in studio/memory.py (`memory.writer`). A member is
judged on: its name, its part, the line of what it is in charge of (`job_line`, from PERSONA.md), the room's
recent messages (§9.1) and the new message. The answer starts with `예` or `아니오`, then a short reason.

A judgment that fails — the call raises, the answer is not a yes or a no, or the time is up — reads as yes: the
member is asked for a turn as if there were no judgment. From the second round of a discussion on (§6.6) the same is asked again of the members of the round before, with
`ROUND_SYSTEM` and `round_facts`: do the round's messages leave something to object to, add or ask?
Nothing here touches the store or the line."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

Ask = Callable[[str, str], Any]
TIMEOUT_S = 20   # all the judgments together: one that is not back by then reads as yes
USAGE_STAGE = "studio:group_judge"  # the `stage` its usage is filed under (`usage` table; run_id `room:<room id>`)
ROUND_USAGE_STAGE = "studio:group_round"  # a member's turn in a discussion (the turn's own run; `chat` relabelled)
CONCLUSION_USAGE_STAGE = "studio:group_conclusion"  # the closing turn of a discussion of two or more
JOB = re.compile(r"^\s*[-*]\s*맡은 일\s*:\s*(?P<job>.*)$", re.MULTILINE)
YES = ("예", "네", "yes", "y")
NO = ("아니오", "아니요", "아니", "no", "n")

SYSTEM = """너는 단체방의 '답할 차례' 를 빠르게 가른다. 아래 사실은 한 직원의 이름·파트·맡은 일, 방의 최근 글, 새 글이다.
새 글이 이 직원이 답해야 할 일인지 판단하라.
- 이 직원의 몫이거나 이 직원의 의견이 필요하면 예, 다른 구성원의 몫이거나 이 직원이 보탤 것이 없으면 아니오.
- 애매하면 예.
- 첫 단어는 `예` 또는 `아니오` 만 쓰고, 이어서 이유를 한 줄로 쓴다. 다른 설명은 붙이지 않는다."""

ROUND_SYSTEM = """너는 단체방 토론의 '다음 라운드에 말할지' 를 빠르게 가른다. 아래 사실은 한 직원의 이름·파트·맡은 일, 방의 이전 글, 직전 라운드 글이다.
직전 라운드 글을 보고 이 직원이 반박하거나 보태거나 물을 것이 있는지 판단하라.
- 반박·보탬·질문할 것이 있으면 예, 동의하거나 보탤 것이 없으면 아니오.
- 애매하면 예.
- 첫 단어는 `예` 또는 `아니오` 만 쓰고, 이어서 이유를 한 줄로 쓴다. 다른 설명은 붙이지 않는다."""


def job_line(persona: str | None) -> str:
    """The `- 맡은 일:` line of a PERSONA.md, its text; '' when there is none or it is empty."""
    found = JOB.search(persona or "")
    return found.group("job").strip() if found else ""


def facts(name: str, part: str, job: str, summary: str, text: str) -> str:
    return (f"이름: {name}\n파트: {part or '없음'}\n맡은 일: {job or '적혀 있지 않음'}\n\n"
            f"방의 최근 글:\n{summary or '(없음)'}\n\n새 글:\n{text}")


def round_facts(name: str, part: str, job: str, summary: str, said: str) -> str:
    """As `facts`, for a round after the first: `said` is what the round before left in the room."""
    return (f"이름: {name}\n파트: {part or '없음'}\n맡은 일: {job or '적혀 있지 않음'}\n\n"
            f"방의 이전 글:\n{summary or '(없음)'}\n\n직전 라운드 글:\n{said or '(없음)'}")


def parse(answer: Any) -> bool | None:
    """True = yes, False = no, None = neither (a blank answer, or one that does not start with either word)."""
    raw = answer[0] if isinstance(answer, tuple) and answer else answer
    words = re.split(r"[\s,.:;!?—\-]+", str(raw or "").strip().lower(), maxsplit=1)
    first = words[0] if words else ""
    if first in NO:
        return False
    if first in YES:
        return True
    return None


def decide(asks: Mapping[str, str], ask: Ask, timeout: float | None = None,
           record: Callable[[Any], None] | None = None, system: str = SYSTEM) -> set[str]:
    """Who says yes: `asks` is member id -> facts. All are asked at once; an answer that is not back within
    `timeout` (TIMEOUT_S), that failed or that is neither yes nor no counts as yes. `record(usage)` is called for
    an answer that carries its usage (`(text, usage)`); it is never allowed to fail the judgment."""
    if not asks:
        return set()
    limit = TIMEOUT_S if timeout is None else timeout
    pool = ThreadPoolExecutor(max_workers=len(asks), thread_name_prefix="studio-judge")
    try:
        futures = {pool.submit(ask, system, text): employee_id for employee_id, text in asks.items()}
        wait(futures, timeout=limit)
        yes: set[str] = set()
        for future, employee_id in futures.items():
            verdict: bool | None = None
            if future.done():
                try:
                    answer = future.result()
                except Exception as e:  # noqa: BLE001 - a judgment that failed reads as yes
                    log.warning("studio: 단체방 판단이 실패해 '예' 로 친다 (%s): %s", employee_id, e)
                else:
                    verdict = parse(answer)
                    if isinstance(answer, tuple) and len(answer) > 1 and record is not None:
                        try:
                            record(answer[1])
                        except Exception:  # noqa: BLE001
                            log.exception("studio: 판단 사용량을 적지 못했다 (%s)", employee_id)
            else:
                log.warning("studio: 단체방 판단이 시간을 넘겨 '예' 로 친다 (%s)", employee_id)
            if verdict is not False:
                yes.add(employee_id)
        return yes
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
