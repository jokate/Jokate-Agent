"""Chat turns (S-7, part 2): the owner writes in an employee's 1:1 room and the employee answers in it. And the
read turns of a task room (S-9): a member who is not the task's assignee answers what it was mentioned for.

Contract: design/studio/ARCHITECTURE.md §3.7 (`room_members`: the one conversation of an employee in a room),
§6.6 (the owner's message asks for the turn), §7.4 (`run.live` — `api.track_run`), §9.5 (the values of a chat
turn, its relay and its folder, and what happens when it cannot be made), §11.2 (`create` and `mcp_only`).

A turn is one run of `relays/employee-chat.yaml` — a single stage, `chat` — on the server's engine:
- what it is made with is read at the start of every turn: the `chat` slot under the cap, `chat_mcp` + `studio`
  and nothing else (`mcp_only` drops the project's other servers, so unreal is off unless the employee's file
  turns it on), the employee's project as `repo`, or the employee's own folder for one who is placed nowhere
  (`studio` is attached there too, whether or not that folder has a project context — §9.5);
- the three columns of `room_members`: `session_id` is the session of history.sqlite the turns of this room are
  filed under (one per employee and room, kept when the conversation changes), `conversation_id` the Claude Code
  conversation the next turn resumes (NULL = a new one), `fed_message_id` the last message a finished turn read;
- a turn that resumes is made like any run and then told to continue that conversation: the engine resumes the
  conversation `RunState.stage_sessions` names and sends only what `interject` left — the new messages and, for
  the HR employee, the values of a hire as they are now (§8.4) — and, when it changed since the conversation was
  last sent it, the policy of the project (`_policy_note`) and the employee's GUIDE.md (`_guide_note`, H-6; after a restart nothing is remembered, so the first resumed turn carries both once more). The whole goal stays the prompt of a conversation
  that could not be resumed (`StageCall.fresh_prompt`);
- when a turn's input passes HANDOFF_TOKENS, or its conversation could not be resumed, the conversation is
  closed: the next turn starts a new one from the hand-over — what the last finished turn said the conversation
  had come to (`state` · `open_issues` · `next_steps`). No model is called for it;
- the answer is the stage's `output`, stored as the employee's message with `message.created`.

A read turn (S-9; §8.5 · §9.5) is the same run in a task room: `mode` `read`, the task's project as `repo`, the
task in its actor token. It changes no file — the relay is `workspace: none` with Read · Grep · Glob — and its
answer stays in the room: what must reach the assignee's run the member writes with `room_post(mentions=[…])`,
and the turn is told so. The assignee gets no turn here: its turn in that room is the task's run (studio/work.py
keeps its row of `room_members`), and a mention of it goes into that run (`Hooks.tell_run`). `put` is what a
mention calls: the run is made before it returns, so `room_post` knows whether a turn went in; the mention is
counted there, not here.

A group turn (§6.6 · §9.5) is a chat turn in a group room, on the member's own row of `room_members`. The turns
of one room run one after the other (`ChatTurns.group`), so a later member reads what an earlier one just
answered: the owner's message asks every member's turn, or the ones it mentions; a member's mention of another
(`room_post`) asks that one's. A member whose business the new messages are not leaves its output empty and
writes nothing: then no message is left, and none says so. The turns members put in for each other are counted
in `rooms.exchange_count` (§4.6), and at `Config.studio_group_max_exchanges` the room is held — no turn, one
`system` message — until the owner writes there or continues (`POST /studio/rooms/{id}/continue`).

A discussion (§6.6): when the line of an owner's message with no mention is empty — and the quick judgment is
on —, the members of that round are asked again, each at once and alone, whether the round's messages leave
something to object to, add or ask (`_next_round`); the ones who say yes take a turn one after the other, as turns
a mention put in (looked at against the limit, counted). The rounds stop at the first of: nobody says yes,
`Config.studio_group_rounds` rounds, the exchange limit. A new message of the owner's in the room drops the turns
that have not begun and starts again from round 1; the turn that runs goes on to its end.

It takes no slot of the queue and changes no employee's status (§4.6 · §4.9), files nothing in the inbox and
counts no exchange of a task (§9.5). What it cannot do it says in the room, as a `system` message (`meta.event`
`run_error`).

No FastAPI here: server.py connects it (`runs.attach(chat=…)`), and tests use it with an engine of their own.

Provisional (S-7): HANDOFF_TOKENS has no measurement behind it, and a turn that called many tools counts its
input once per call, so it may hand over early.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..pipeline import RelaySpec
from . import api, awake as studio_awake, employees, group_judge, harness_root, memory
from .store import StudioStore

log = logging.getLogger(__name__)

RELAY = "employee-chat.yaml"
STAGE = "chat"
CONCLUSION = "conclusion"  # `meta.kind` of the closing message of a discussion (T2, §6.6)
HANDOFF_TOKENS = 100_000  # input tokens of a finished turn from which the next turn starts a new conversation
FEED_MESSAGES = 50        # messages one turn reads; what is left is read by the turn that follows at once
HANDOFF_LOOKBACK = 5      # turns of the session looked through for the last one that finished
HANDOFF_TITLE = "이전 대화의 인계 (HANDOFF)"
RUN_ERROR = "run_error"   # `meta.event` of what a turn says about itself (§7.6)
UNANSWERED = (RUN_ERROR, api.GROUP_LIMIT, api.GROUP_CONTINUED, api.GROUP_NOBODY)  # system messages no turn reads: they are the owner's
STAFFING_SIGNAL = "staffing.signal"  # `meta.event` of the server's question to HR (T5): read by the turn, answered quietly


class Refused(Exception):
    """The turn is not made (§9.5 '넣지 못할 때'). Its text is what the room is told. `failed` holds the codes of
    §9.5 의 5 (가) — what a mention is refused with before its message is made (`ChatTurns.failed`)."""

    def __init__(self, text: str, failed: tuple[str, ...] = ()):
        super().__init__(text)
        self.failed = tuple(failed)


@dataclass(frozen=True)
class Where:
    """What a turn in a room is (§9.5): its `mode` of §7.4 — `chat`, or `read` in a task room —, the project it
    runs in, and the task of a task room."""
    mode: str
    project_id: str | None
    task: dict | None = None

    @property
    def task_id(self) -> str | None:
        return self.task["id"] if self.task else None


@dataclass
class Begun:
    """A turn whose run is made and not advanced yet: what `ChatTurns._finish` goes on with. `posted` is what
    `_finish` found: the turn left a message in the room — its answer, or what its run wrote there itself.
    `quiet`: all that is new is the server's own lines and one is a question to HR (`STAFFING_SIGNAL`) — the
    turn may end with no answer, and then the room is not told that it did not answer (T5)."""
    store: StudioStore
    room: dict
    employee: dict
    listed: list
    run: Any
    where: Where
    posted: bool = False
    quiet: bool = False
    extra: str = ""  # a line of the server after the room's note (a discussion's round note, the conclusion's note)
    usage_stage: str | None = None  # the turn's usage is filed under this, not `chat` (T2)
    meta: dict | None = None  # the `meta` of the answer's message (`kind: conclusion`)


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True, name="studio-chat").start()


def _own(message: dict, employee_id: str) -> bool:
    """Nothing to answer: the employee's own message, what a turn said about itself, and what a group room is
    told of its limit (§4.6) — that is the owner's to read."""
    if message["author_kind"] == "employee":
        return message["author_id"] == employee_id
    return message["author_kind"] == "system" and (message["meta"] or {}).get("event") in UNANSWERED


def said(messages: list[dict], names: dict[str, str]) -> str:
    """The messages as `author: body` lines, whole — the room's summary in the prompt cuts them short."""
    lines = []
    for message in messages:
        who = {"owner": "owner", "system": "시스템"}.get(message["author_kind"])
        who = who or names.get(message["author_id"]) or message["author_id"] or "직원"
        body = (message["body"] or "").strip()
        files = ", ".join(str(a.get("name")) for a in message.get("attachments") or [] if isinstance(a, dict))
        lines.append(f"{who}: {body}" + (f"{' ' if body else ''}(첨부: {files})" if files else ""))
    return "\n".join(lines)


@dataclass
class Screen:
    """The quick judgment of one owner message without a mention (T5): who it asks (`ids`), the message (`text`,
    `message_id`) and, once the judgment has been made, who said yes (`yes`; None = not made yet). `noted` = the
    room has been told that nobody was found."""
    room_id: str
    ids: list[str]
    message_id: int
    text: str
    yes: set[str] | None = None
    noted: bool = False


@dataclass
class Round:
    """A discussion going on in a group room (§6.6): `number` = the round whose turns are in the line or have
    just ended, `ids` = the members the owner's message asked, `start_id` = the last message before the round
    that comes next (the messages after it are what that round reacts to), `gen` = the room's generation when
    it began (a new message of the owner's raises it, and what was judged for the old one is dropped)."""
    gen: int
    number: int
    ids: list[str]
    start_id: int
    # the owner's message that opened the discussion: its first speaker is who reads the conclusion. None = an old
    # limit notice had none and no owner message was found before `start_id`: the discussion gets no conclusion
    first_id: int | None


class ChatTurns:
    """The chat turns of one server. `request` is what `Hooks.chat_turn` calls: it returns at once and the turn
    runs on a thread of its own (`spawn(work)` — tests give one that runs in place).

    One turn at a time for an employee in a room: two runs on one conversation would split it. A request that
    comes while the turn runs makes it run once more when it has ended — it then reads what came in between.

    A group room has one turn at a time for the whole room (`group`): its members wait in a line, and one
    thread takes them one after the other."""

    def __init__(self, engine, cfg, spawn: Callable[[Callable[[], None]], Any] | None = None,
                 awake: studio_awake.Awake | None = None, judge_ask: group_judge.Ask | None = None):
        self.engine, self.cfg, self.spawn = engine, cfg, spawn
        self._judge_ask = judge_ask  # the light model of a group room's quick judgment; None = `memory.writer` of the setting
        self.awake = awake or studio_awake.KEEPER  # V-3: the PC stays awake while a turn's thread is at work
        self.relay = Path(cfg.relays_dir) / RELAY
        self._lock = threading.Lock()
        self._busy: dict[tuple[str, str], bool] = {}  # (room, employee) of a running turn -> asked again meanwhile
        # a group room whose turns are running -> who waits there, in order: (employee, a mention put it in, the
        # judgment that decides whether the turn is made: a `Screen`, or None = no judgment)
        self._rooms: dict[str, deque[tuple[str, bool, Screen | None]]] = {}
        # a group room -> where a mention puts a turn in its line: right after the turn that runs now (0 when a turn
        # has just been taken off the line; one more for each turn put in, so the mentions keep their order)
        self._next: dict[str, int] = {}
        self._round: dict[str, Round] = {}  # the discussion of a room, while there is one (memory only)
        self._gen: dict[str, int] = {}
        # a room whose conclusion turn is being made -> the employee who writes it: a mention in that turn makes no turn
        self._concluding: dict[str, str] = {}
        # (room, employee) -> (project, sha256 of the policy text) last sent to that conversation. In memory: after
        # a restart the policy is sent once more to a conversation that resumes
        self._policy_seen: dict[tuple[str, str], tuple[str, str]] = {}
        # (room, employee) -> sha256 of the GUIDE.md last sent to that conversation (H-6): an approved amendment
        # reaches a conversation that is open. In memory like the policy's: after a restart the first resumed turn sends it again
        self._guide_seen: dict[tuple[str, str], str] = {}

    def busy(self, room_id: str) -> list[str]:
        """`Hooks.room_busy`: why a turn of that room is at work — `turn_running` (a turn runs there) and/or
        `turn_waiting` (a group room's line has someone in it). [] = none: the room may be deleted."""
        out: list[str] = []
        with self._lock:
            if any(room == room_id for room, _ in self._busy):
                out.append("turn_running")
            if self._rooms.get(room_id):
                out.append("turn_waiting")
        return out

    def forget(self, room_id: str) -> None:
        """`Hooks.room_forget`: drop what is remembered of that room — the policy and guide last sent to its
        conversations (a deleted room's conversations are gone; a cleared room starts over)."""
        with self._lock:
            for seen in (self._policy_seen, self._guide_seen):
                for key in [key for key in seen if key[0] == room_id]:
                    del seen[key]

    def request(self, employee_id: str, room_id: str) -> None:
        key = (room_id, employee_id)
        with self._lock:
            if key in self._busy:
                self._busy[key] = True
                return
            self._busy[key] = False
        try:
            (self.spawn or _thread)(lambda: self._work(employee_id, room_id))
        except BaseException:
            with self._lock:
                self._busy.pop(key, None)
            raise

    def put(self, employee_id: str, room_id: str) -> bool:
        """`Hooks.room_turn` (S-9): the turn a mention asks for. The run is made here, on the caller's thread, so
        the caller knows whether a turn went in (§9.5: one that did not is not counted); the rest runs as in
        `request`. True = a run was made, or a turn is running and will run once more. False = no run: the room
        was told why, or there is nothing for that employee to read there — the assignee of a task room has no
        turn of this kind at all."""
        key = (room_id, employee_id)
        with self._lock:
            if key in self._busy:
                self._busy[key] = True
                return True
            self._busy[key] = False
        try:
            begun = self._begin(employee_id, room_id)
            if begun is None:
                with self._lock:
                    if not self._busy.get(key):
                        self._busy.pop(key, None)
                        return False
                    self._busy[key] = False  # asked for meanwhile: that request is owed a look at the room
            (self.spawn or _thread)(lambda: self._work(employee_id, room_id, begun))
        except BaseException:
            with self._lock:
                self._busy.pop(key, None)
            raise
        return begun is not None

    def judge(self, signal: dict) -> bool:
        """`Hooks.staffing_ask` (T5 · §5.3 '판단 요청'): put the trigger to the HR employee in service as a system
        line of its 1:1 room (`meta.event` `staffing.signal`, the brief of `staffing.brief`) and give it a turn
        (`put`). The turn is a chat turn: it can change no file, and it may raise a recommendation with
        `hire_recommend` or nothing — an empty output leaves no line in the room (`Begun.quiet`).

        False = nobody to ask (no HR employee in service, or no 1:1 room for it): only logged, the caller takes
        back its record of the question. True = the question is in the room; when the turn itself could not be
        put the line stays unread and the next turn of HR reads it (logged), so the same trigger is not asked
        again within the cooldown."""
        from . import staffing
        store = api.get_store()
        hr = next(iter(store.list_employees(department_id=employees.HR_DEPARTMENT)), None)
        rooms = store.list_rooms(kind="dm", employee_id=hr["id"]) if hr is not None else []
        if hr is None or not rooms:
            log.info("studio: 채용 계기 %s 를 물을 인사 직원이나 1:1 방이 없다", signal.get("key"))
            return False
        room = rooms[0]
        self._say(store, room, staffing.brief(store, signal), event=STAFFING_SIGNAL, key=signal["key"],
                  trigger=signal["trigger"])
        try:
            if not self.put(hr["id"], room["id"]):
                log.info("studio: 인사의 판단 턴이 들어가지 않았다 (%s) — 글은 방에 남아 다음 턴이 읽는다", signal["key"])
        except Exception:  # noqa: BLE001 - the line is in the room; a turn that cannot be put is not the trigger's failure
            log.exception("studio: 인사의 판단 턴을 넣지 못했다 (%s)", signal["key"])
        return True

    def failed(self, employee_id: str, room_id: str) -> list[str]:
        """`Hooks.turn_failed`: §9.5 의 5 (가) for that employee's turn, asked before a mention's message is made —
        `["provider_unguarded"]`, or [] when that does not stand in the way. What else keeps a turn from being
        made (a file that cannot be read, an engine that refuses) is found when it is made, and said in the
        room. The room decides nothing yet: a chat turn and a read turn are made with the same values."""
        store = api.get_store()
        employee = store.get_employee(employee_id)
        if employee is None or employee["retired_at"] is not None:
            return []
        try:
            self._values(store, employee)
        except Refused as e:
            return list(e.failed)
        return []

    # --- the turns of a group room (§6.6 · §4.6) -------------------------------------------------------------
    def group(self, room_id: str, employee_ids: list[str], mentioned: bool = False, judged: bool = False,
              owed: bool = False) -> list[str]:
        """`Hooks.group_turns`: put those members' turns in the room's line and return at once. The owner's turns
        go to its end, in the order given; a member's mention (`mentioned`) goes right after the turn that is
        running — before who waits —, and is asked for with no judgment. `judged` = the owner's message has no
        mention: when the turn of such a member comes, a light model says for all of them at once whether the
        message is their business (`_screen`; none is called when `studio_group_judge_model` is blank). It
        returns — who is in the line now, of the ones asked for. One who waits there already waits once; a mention
        moves such a one to the seat right after the running turn.
        Only who is seated and has not retired gets a turn; a room that is no group room, or that the owner
        left, gives none.

        `mentioned` = an employee's mention asks (`room_post`): such a turn is looked at against the limit when
        its time comes and counted when it left a message (`_group_turn`). The owner's asking is neither, and it
        wins: a member the owner's message put in the line is not a turn a later mention put in, even when the
        mention moved it to the next seat (its mark stays, so it is not counted).

        A message of the owner's (`not mentioned`) in a room that has a discussion going on drops every turn that
        has not begun (the line is emptied, the discussion forgotten) before its own members are put in: the
        discussion starts over at round 1 with the new message. The running turn is not touched. With the quick
        judgment on, the message opens a discussion (`Round`) that `_group_line` goes on with.

        While the room's conclusion turn is being made a mention (`mentioned`) puts in no turn and gives [] — the
        mention is the conclusion's, which is told not to mention. `owed` = the turns 'continue' puts back (`resume`):
        they are not the conclusion's mentions and go in all the same."""
        store = api.get_store()
        room = store.get_room(room_id)
        if room is None or room["kind"] not in api.GROUP_KINDS or room["archived_at"] is not None:
            return []
        seated = set(api.group_order(store, room_id))
        asked = [employee_id for employee_id in dict.fromkeys(employee_ids) if employee_id in seated]
        if not asked:
            return []
        screen = self._screen_of(store, room_id, asked) if judged and not mentioned else None
        with self._lock:
            if mentioned and not owed and room_id in self._concluding:
                return []
            line = self._rooms.get(room_id)
            start = line is None
            if start:
                line = self._rooms[room_id] = deque()
            if not mentioned and room_id in self._round:  # the owner writes in a discussion: it starts again
                line.clear()
                self._next[room_id] = 0
                self._round.pop(room_id, None)
                self._gen[room_id] = self._gen.get(room_id, 0) + 1
            waiting ={item[0]: at for at, item in enumerate(line)}
            for employee_id in asked:
                if mentioned:  # right after the running turn, in the order of the mentions
                    at = min(self._next.get(room_id, 0), len(line))
                    idx = next((i for i, item in enumerate(line) if item[0] == employee_id), None)
                    if idx is None:
                        line.insert(at, (employee_id, True, None))
                        self._next[room_id] = at + 1
                    elif idx < at:  # already among the mentioned ones: it stays, asked with no judgment
                        line[idx] = (employee_id, line[idx][1], None)
                    else:  # waits behind: it is moved to the next seat, its owner-turn mark kept (not counted)
                        flag = line[idx][1]
                        del line[idx]
                        line.insert(at, (employee_id, flag, None))
                        self._next[room_id] = at + 1
                elif employee_id not in waiting:
                    line.append((employee_id, False, screen))
                else:
                    line[waiting[employee_id]] = (employee_id, False, screen)
            if screen is not None and self._rounds() > 1:
                self._round[room_id] = Round(self._gen.get(room_id, 0), 1, list(asked), screen.message_id,
                                              screen.message_id)
        if start:
            self._start(room_id)
        return asked

    def _start(self, room_id: str) -> None:
        """Run the line of a room that has just got one (`self._rooms[room_id]` is set) on a thread of its own. When the
        thread cannot be started the room's line and discussion are taken back and the error goes on."""
        try:
            (self.spawn or _thread)(lambda: self._group_work(room_id))
        except BaseException:
            with self._lock:
                self._rooms.pop(room_id, None)
                self._next.pop(room_id, None)
                self._round.pop(room_id, None)
            raise

    def resume(self, room_id: str, owed: list[str], number: int, ids: list[str], start_id: int,
               first_id: int | None = None) -> list[str]:
        """`Hooks.group_resume` (the owner's 'continue' of a room held at its limit, section 4.6): the discussion the
        notice remembered (`number` = the round whose turns were in the line when it stopped, `ids` = the members the
        owner's message asked, `start_id` = where that round began - what the notice's `meta` holds) is set up again
        when the room has none (a restart forgets them), the turns it kept back (`owed`) are put in the line as
        mentions, and when the line is empty the round after `number` is judged, as at the end of any round. Who is
        in the line for the owed turns. `first_id` = the message that opened the discussion (None, in an old notice: the newest message of the owner's
        at or before `start_id` is looked for - the owner's message restarts a discussion, so it is the one that opened
        it; none found = the discussion gets no conclusion).
        A room that is no group room, or that the owner left, does nothing."""
        store = api.get_store()
        room = store.get_room(room_id)
        if room is None or room["kind"] not in api.GROUP_KINDS or room["archived_at"] is not None:
            return []
        keep = [i for i in api.group_order(store, room_id) if i in ids]
        if first_id is None:
            first_id = self._opening(store, room_id, start_id)
        with self._lock:
            if room_id not in self._round and keep and self._rounds() > 1:
                self._round[room_id] = Round(self._gen.get(room_id, 0), number, keep, start_id, first_id)
        if owed:
            return self.group(room_id, owed, mentioned=True, owed=True)
        with self._lock:
            start = room_id not in self._rooms and room_id in self._round
            if start:
                self._rooms[room_id] = deque()
        if start:
            self._start(room_id)
        return []

    @staticmethod
    def _opening(store: StudioStore, room_id: str, start_id: int) -> int | None:
        """The newest message of the owner's at or before `start_id`: the one that opened the discussion (the owner's
        message always starts a discussion over). None = there is none."""
        cursor = start_id + 1
        while True:
            page = store.list_messages(room_id, before_id=cursor, limit=api.MAX_PAGE)
            for m in reversed(page):
                if m["author_kind"] == "owner":
                    return m["id"]
            if len(page) < api.MAX_PAGE:
                return None
            cursor = page[0]["id"]

    def _rounds(self) -> int:
        """`Config.studio_group_rounds`: the most rounds of a discussion. 1 or less = one round, as before."""
        try:
            return int(getattr(self.cfg, "studio_group_rounds", 3) or 1)
        except (TypeError, ValueError):
            return 1

    def _group_work(self, room_id: str) -> None:
        """The line of one group room, until it is empty: one turn at a time, so each is made after the one
        before it has ended and reads what that one left. A turn may add to the line while it runs (a mention).
        The PC is held awake meanwhile (V-3)."""
        with self.awake.hold():
            self._group_line(room_id)

    def _group_line(self, room_id: str) -> None:
        """The turns of the line, one at a time. When the line is empty and the room has a discussion, the next
        round is judged (`_next_round`) outside the lock: what it finds is dropped if the owner wrote meanwhile
        (the generation changed) or the line is no longer empty; the ones who said yes are put in the line as
        turns a mention put in. No discussion and an empty line = the room is done."""
        while True:
            r, gen, ended = None, 0, None
            with self._lock:
                line = self._rooms.setdefault(room_id, deque())
                if not line:
                    r = self._round.get(room_id)
                    if r is None:
                        self._rooms.pop(room_id, None)
                        self._next.pop(room_id, None)
                        self._gen.pop(room_id, None)
                        return
                    gen = self._gen.get(room_id, 0)
                else:
                    employee_id, mentioned, screen = line.popleft()
                    self._next[room_id] = 0
            if r is not None:
                try:
                    yes = self._next_round(room_id, r)
                except Exception:  # noqa: BLE001 - the discussion ends; nobody waits for this thread
                    log.exception("studio: 단체방 %s 의 다음 라운드를 열지 못해 토론을 멈춘다", room_id)
                    yes = []
                with self._lock:
                    line = self._rooms.setdefault(room_id, deque())
                    if self._round.get(room_id) is not r or self._gen.get(room_id, 0) != gen or line:
                        continue  # the owner wrote meanwhile: this round is not the room's any more
                    if yes:
                        line.extend((employee_id, True, None) for employee_id in yes)
                    else:
                        self._round.pop(room_id, None)
                        ended = r
                if ended is not None:  # the discussion is over: its first speaker closes it (T2)
                    try:
                        self._conclude(room_id, ended, gen)
                    except Exception:  # noqa: BLE001 - the room is left without a conclusion; the line goes on
                        log.exception("studio: 단체방 %s 의 결론을 쓰지 못했다", room_id)
                continue
            try:
                if screen is not None:
                    yes = self._said_yes(room_id, screen)
                    self._drop_no(room_id, screen, yes)
                    if employee_id not in yes:
                        continue  # the judgment: this message is not that member's business
                self._group_turn(employee_id, room_id, mentioned)
            except Exception:  # noqa: BLE001 - nobody waits for this thread; the rest of the line goes on
                log.exception("studio: 직원 %s 의 단체방 턴(방 %s)이 실패했다", employee_id, room_id)

    def _next_round(self, room_id: str, r: Round) -> list[str]:
        """Who takes a turn in the round after `r`, in the room's order; [] = the discussion is over. It stops at
        the first of: `studio_group_rounds` rounds were made; the round left no message of an employee's; the
        room is at its exchange limit (the room is told once, `_held_notice`). Else every member of the first
        asking who is still seated and had a message of another to read is asked at once (`group_judge.decide`
        with `ROUND_SYSTEM`; a judgment that fails says yes). When nobody says yes the room is told nothing.
        `r` is moved to the next round (`number`, `start_id`) when this returns with the judgment made."""
        store = api.get_store()
        room = store.get_room(room_id)
        if room is None or r.number >= self._rounds():
            return []
        posts, last_id = [], r.start_id
        while True:
            page = store.list_messages(room_id, after_id=last_id, limit=api.MAX_PAGE)
            posts += [m for m in page if m["author_kind"] == "employee"
                      and (m["meta"] or {}).get("kind") != CONCLUSION]  # a conclusion of the discussion before is no speech
            if page:
                last_id = page[-1]["id"]
            if len(page) < api.MAX_PAGE:
                break
        if not posts:
            return []
        limit = api._group_max_exchanges()
        if limit is not None and room["exchange_count"] >= limit:
            self._held_notice(store, room, limit, r)
            return []
        model = (getattr(self.cfg, "studio_group_judge_model", "") or "").strip()
        ask = self._judge_ask or (memory.writer(model) if model else None)
        if ask is None:
            return []
        ids = [i for i in api.group_order(store, room_id) if i in r.ids and any(m["author_id"] != i for m in posts)]
        names = {row["id"]: row["name"] for row in store.list_employees(include_retired=True)}
        before = store.list_messages(room_id, before_id=r.start_id + 1, limit=employees.SUMMARY_MESSAGES + 1)
        summary, said = employees.room_summary(before, names), employees.room_summary(posts, names)
        asks = {}
        for employee_id in ids:
            employee = store.get_employee(employee_id)
            if employee is None:
                continue
            department = store.get_department(employee["department_id"]) if employee.get("department_id") else None
            persona = employees.read_files(self.cfg.studio_dir, employee_id)["PERSONA.md"]
            asks[employee_id] = group_judge.round_facts(employee["name"], (department or {}).get("name") or "",
                                                        group_judge.job_line(persona), summary, said)
        yes = group_judge.decide(asks, ask, record=self._judge_record(room_id), system=group_judge.ROUND_SYSTEM)
        r.number, r.start_id = r.number + 1, last_id
        return [i for i in ids if i in yes]

    def _conclude(self, room_id: str, r: Round, gen: int | None = None) -> None:
        """The end of a discussion (§6.6): when two or more members spoke in it, the one who spoke first in round 1
        has one more turn that writes the conclusion — the last message of the discussion (`meta.kind =
        conclusion`). One who spoke alone, or none, gets none; and a room held at its limit gets none: 'continue'
        goes on with it. The conclusion is no exchange of the limit (`mentioned` False).
        `gen` = the room's generation when the discussion ended. The owner writing before the conclusion turn begins
        (the generation changed, or a new discussion or turns are there) = the conclusion is left out: the old
        discussion is not needed. Once it has begun it is made and stays in the room; the new discussion goes on after
        it and does not count it as a speech. While it is made a mention makes no turn (`group`)."""
        if r.first_id is None:
            return
        store = api.get_store()
        room = store.get_room(room_id)
        if room is None or room["archived_at"] is not None or api.group_halted(store, room_id):
            return
        posts, last_id = [], r.first_id
        while True:
            page = store.list_messages(room_id, after_id=last_id, limit=api.MAX_PAGE)
            posts += [m for m in page if m["author_kind"] == "employee"
                      and (m["meta"] or {}).get("kind") != CONCLUSION]
            if page:
                last_id = page[-1]["id"]
            if len(page) < api.MAX_PAGE:
                break
        if len({m["author_id"] for m in posts}) < 2:
            return
        with self._lock:
            if gen is not None and (self._gen.get(room_id, 0) != gen or room_id in self._round
                                    or self._rooms.get(room_id)):
                return  # the owner wrote meanwhile: this discussion is not the room's any more
            self._concluding[room_id] = posts[0]["author_id"]
        try:
            self._group_turn(posts[0]["author_id"], room_id, False, concluding=True)
        finally:
            with self._lock:
                self._concluding.pop(room_id, None)

    def _judge_record(self, room_id: str):
        """Where a judgment's usage goes: the usage DB as `room:<room id>` · `studio:group_judge`; None = nowhere."""
        usage = getattr(self.engine, "usage", None)
        return (lambda u: usage.record(f"room:{room_id}", group_judge.USAGE_STAGE, u)) if usage is not None else None

    def _held_notice(self, store: StudioStore, room: dict, limit: int, r: Round | None = None) -> None:
        """§4.6: the room is at its limit — it is told once (a `system` message, pushed to the owner). With a
        discussion (`r`) the notice's `meta` also holds where it stands (`round` · `start_id` · `ids` · `first_id`), so that
        'continue' can open the next round, also after a restart (§7.6)."""
        if not api.group_halted(store, room["id"]):  # one notice for a room that is held
            more = ({"round": r.number, "start_id": r.start_id, "ids": list(r.ids), "first_id": r.first_id}
                    if r is not None else {})
            self._say(store, room, f"직원들끼리 주고받은 턴이 한도 {limit}회에 닿아 멈췄습니다. 이 방에 글을 쓰거나 "
                      "'이어 가기'를 하면 다시 세고 이어 갑니다", event=api.GROUP_LIMIT, limit=limit, **more)

    def _screen_of(self, store: StudioStore, room_id: str, ids: list[str]) -> Screen | None:
        """The judgment of the owner's message just stored in that room, for those members. None = no judgment:
        the setting is blank, there is no light model (nothing to ask), or the newest message is not the owner's."""
        model = (getattr(self.cfg, "studio_group_judge_model", "") or "").strip()
        if not model or (self._judge_ask is None and memory.writer(model) is None):
            return None
        last = store.list_messages(room_id, limit=1)
        if not last or last[-1]["author_kind"] != "owner":
            return None
        return Screen(room_id, list(ids), last[-1]["id"], (last[-1]["body"] or "").strip())

    def _said_yes(self, room_id: str, screen: Screen) -> set[str]:
        """Who said yes to that message — judged once, when the first of its members has its turn. When nobody
        did, the room is told so once (`api.GROUP_NOBODY`). Never raises: a judgment that cannot be made says
        yes for all."""
        if screen.yes is None:
            try:
                screen.yes = self._screen(room_id, screen)
            except Exception:  # noqa: BLE001 - no judgment is no filter
                log.exception("studio: 단체방 %s 의 판단을 하지 못해 모두 '예' 로 친다", room_id)
                screen.yes = set(screen.ids)
        if not screen.yes and not screen.noted:
            screen.noted = True
            store = api.get_store()
            room = store.get_room(room_id)
            if room is not None:
                self._say(store, room, "답할 직원이 없다고 판단했습니다 — 멘션하면 그 직원이 답합니다", event=api.GROUP_NOBODY)
        return screen.yes

    def _drop_no(self, room_id: str, screen: Screen, yes: set[str]) -> None:
        """Take the members the judgment said no to off the line now, so a later mention of one of them is a new
        turn (right after the running one, counted) and not a turn put in its old place."""
        with self._lock:
            line = self._rooms.get(room_id)
            if line:
                kept = [item for item in line if not (item[2] is screen and item[0] not in yes)]
                line.clear()
                line.extend(kept)

    def _screen(self, room_id: str, screen: Screen) -> set[str]:
        """Ask the light model for each member of the screen at once (`group_judge.decide`): the member's name, part
        and line of work, the room's recent messages before the new one (§9.1) and the new message. The usage
        goes in the usage DB as `room:<room id>` · `studio:group_judge`."""
        store = api.get_store()
        ask = self._judge_ask or memory.writer(self.cfg.studio_group_judge_model)
        if ask is None:
            return set(screen.ids)
        names = {row["id"]: row["name"] for row in store.list_employees(include_retired=True)}
        recent = [m for m in store.list_messages(room_id, limit=employees.SUMMARY_MESSAGES + 1)
                  if m["id"] < screen.message_id]
        summary = employees.room_summary(recent, names)
        asks = {}
        for employee_id in screen.ids:
            employee = store.get_employee(employee_id)
            if employee is None:
                continue
            department = store.get_department(employee["department_id"]) if employee.get("department_id") else None
            persona = employees.read_files(self.cfg.studio_dir, employee_id)["PERSONA.md"]
            asks[employee_id] = group_judge.facts(employee["name"], (department or {}).get("name") or "",
                                                  group_judge.job_line(persona), summary, screen.text)
        return group_judge.decide(asks, ask, record=self._judge_record(room_id)) | (set(screen.ids) - set(asks))

    def _group_turn(self, employee_id: str, room_id: str, mentioned: bool, *, concluding: bool = False) -> None:
        """One member's turn in a group room. §4.6: a turn a mention put in is not made when the room's count is
        at the limit — the room is told once (a `system` message, pushed to the owner) and waits for the owner —,
        and is one more in `exchange_count` when it left a message. One that passed is not counted.
        In a discussion the turn is told the round (from round 2, `employees.round_note`) and its usage is filed as
        `studio:group_round`; `concluding` = the closing turn (`employees.conclusion_note`, `studio:group_conclusion`,
        `meta.kind = conclusion`, made even when nothing is new)."""
        store = api.get_store()
        with self._lock:
            r = self._round.get(room_id)
        if concluding:
            how = dict(extra=employees.conclusion_note(), stage=group_judge.CONCLUSION_USAGE_STAGE,
                       meta={"kind": CONCLUSION}, force=True)
        elif r is not None:
            how = dict(extra=employees.round_note(r.number) if r.number >= 2 else "",
                       stage=group_judge.ROUND_USAGE_STAGE)
        else:
            how = {}
        if mentioned:
            room, limit = store.get_room(room_id), api._group_max_exchanges()
            if room is None:
                return
            if limit is not None and room["exchange_count"] >= limit:
                with self._lock:
                    r = self._round.get(room_id)
                self._held_notice(store, room, limit, r)
                return
        posted, more = False, True
        while more:  # as `_work`: a full page read means more may be waiting
            begun = self._begin(employee_id, room_id, **how)
            if begun is None:
                break
            more = self._finish(begun) and not concluding  # the conclusion is one turn
            posted = posted or begun.posted
        if mentioned and posted:
            with store.transaction() as tx:
                room = store.get_room(room_id, tx=tx)
                if room is not None:
                    room = store.update_room(room_id, tx=tx, exchange_count=room["exchange_count"] + 1)
                    members = store.list_room_members(room_id=room_id, tx=tx)
                    store.emit(tx, "room.updated", {"room": room, "members": members}, room_id=room_id)

    def _work(self, employee_id: str, room_id: str, begun: Begun | None = None) -> None:
        """The turns of one request, one after the other. `begun` is a first turn whose run `put` has made.
        The PC is held awake meanwhile (V-3)."""
        with self.awake.hold():
            self._turns(employee_id, room_id, begun)

    def _turns(self, employee_id: str, room_id: str, begun: Begun | None) -> None:
        key = (room_id, employee_id)
        while True:
            more = False
            try:
                more = self._finish(begun) if begun is not None else self.turn(employee_id, room_id)
            except Exception:  # noqa: BLE001 - nobody waits for this thread; the room keeps the unanswered message
                log.exception("studio: 직원 %s 의 채팅 턴(방 %s)이 실패했다", employee_id, room_id)
            begun = None
            with self._lock:
                if not (more or self._busy.get(key)):
                    self._busy.pop(key, None)
                    return
                self._busy[key] = False

    # --- one turn -------------------------------------------------------------------------------------------
    def turn(self, employee_id: str, room_id: str) -> bool:
        """Read what is new in the room, run the employee on it, store the answer. Nothing is new -> no run.
        A turn that ends `done` with no answer — an empty output, and nothing the run wrote in the room itself —
        says so in the room and counts as read all the same: its messages are in the conversation (§3.7 · §9.5).
        In a group room it says nothing: that is how a member passes what is not its business (§6.6).
        True when it read a full page: more may be waiting, and `_work` goes on."""
        begun = self._begin(employee_id, room_id)
        return self._finish(begun) if begun is not None else False

    def _begin(self, employee_id: str, room_id: str, *, extra: str = "", stage: str | None = None,
               meta: dict | None = None, force: bool = False) -> Begun | None:
        """The first half of a turn: read what is new and make the run of it, not advanced yet. None = no run —
        the room or the member is gone, the employee is the assignee of that task room (`_where`), nothing is
        new, or the run could not be made: then the room has been told why. `extra` · `stage` · `meta` · `force`: a
        group turn of a discussion (T2) — the line after the room's note, the usage stage, the answer's `meta`, and a
        turn that is made though nothing is new (the conclusion)."""
        store = api.get_store()
        room, employee = store.get_room(room_id), store.get_employee(employee_id)
        member = store.get_room_member(room_id, employee_id)
        if room is None or room["archived_at"] is not None or employee is None or employee["retired_at"] is not None:
            return None
        if member is None or member["left_at"] is not None:
            return None
        where = self._where(store, employee, room)
        if where is None:
            return None
        listed = store.list_messages(room_id, after_id=member["fed_message_id"] or 0, limit=FEED_MESSAGES)
        fresh = [message for message in listed if not _own(message, employee_id)]
        if not fresh and not force:
            return None
        names = {row["id"]: row["name"] for row in store.list_employees(include_retired=True)}
        try:
            run = self._make(store, employee, room, member, fresh, said(fresh, names), where, extra)
        except Refused as e:
            self._say(store, room, f"{employee['name']} 의 턴을 시작하지 못했다 — {e}")
            return None
        quiet = (all(m["author_kind"] == "system" for m in fresh)
                 and any((m["meta"] or {}).get("event") == STAFFING_SIGNAL for m in fresh))
        return Begun(store, room, employee, listed, run, where, quiet=quiet, extra=extra, usage_stage=stage, meta=meta)

    def _finish(self, begun: Begun) -> bool:
        """The second half: advance the run and store its answer. True when it read a full page."""
        store, room, employee, listed, run = begun.store, begun.room, begun.employee, begun.listed, begun.run
        employee_id, room_id, where = employee["id"], room["id"], begun.where
        api.track_run(run.id, mode=where.mode, employee_id=employee_id, room_id=room_id, task_id=where.task_id,
                      project_id=where.project_id)  # §7.4 `run.live`, and the actor token of §8.2
        try:
            run = self.engine.advance(run.id)
        finally:
            api.untrack_run(run.id)
        usage = getattr(self.engine, "usage", None)
        if begun.usage_stage and usage is not None:  # T2: a discussion's turn is filed under its own stage
            usage.relabel(run.id, STAGE, begun.usage_stage)
        if run.status != "done":  # the message stays unread: the next turn reads it again
            self._say(store, room, f"{employee['name']} 이(가) 답하지 못했다 — {run.error or run.status}", run.id)
            return False
        fell_back = any(event["kind"] == "resume_fallback" for event in self.engine.history.events(run.id))
        spent = run.history[-1].total_input if run.history else 0
        keep = not fell_back and spent < HANDOFF_TOKENS  # a conversation that fell back was not saved by the CLI
        answer = (run.baton.outputs.get(STAGE) or "").strip()
        # no output is an answer all the same when the run wrote in the room itself (`room_post` keeps `run_id`)
        posted = bool(answer) or any(message["run_id"] == run.id
                                     for message in store.list_messages(room_id, limit=FEED_MESSAGES))
        begun.posted = posted
        with store.transaction() as tx:
            if answer:
                message = store.create_message(room_id, "employee", tx=tx, author_id=employee_id, body=answer,
                                               run_id=run.id, **({"meta": begun.meta} if begun.meta else {}))
                store.emit(tx, "message.created", {"message": message}, room_id=room_id, task_id=room["task_id"],
                           project_id=api._room_project(store, room, tx))
            store.update_room_member(room_id, employee_id, tx=tx, session_id=run.session_id,
                                     conversation_id=run.done_sessions.get(STAGE) if keep else None,
                                     **({"fed_message_id": listed[-1]["id"]} if listed else {}))
        # after the block: `_say` opens a transaction of its own. Not in a group room: there no answer is a pass
        if not posted and room["kind"] not in api.GROUP_KINDS and not begun.quiet:
            self._say(store, room, f"{employee['name']} 이(가) 답하지 못했다 — 턴은 끝났지만 답이 비어 있다", run.id)
        return len(listed) >= FEED_MESSAGES

    def _where(self, store: StudioStore, employee: dict, room: dict) -> Where | None:
        """§9.5: a task room -> a read turn in the task's project, with the task; any other room -> a chat turn
        in the project the employee is placed in. None = no turn of this kind: the room's task is gone, or the
        employee is its assignee — the one who writes there, whose turn in that room is the task's run (§8.5)."""
        if room["kind"] != "task":
            return Where("chat", employee["project_id"])
        task = store.get_task(room["task_id"]) if room["task_id"] else None
        if task is None or task["assignee_id"] == employee["id"]:
            return None
        return Where("read", task["project_id"], task)

    @staticmethod
    def _read_note(store: StudioStore, task: dict) -> str:
        """What a read turn is told after the new messages, as a line of the server (prompts/employee-chat.md
        reads `시스템:` so): it changes no file, and what must reach the assignee's run goes through `room_post`
        — the answer in `output` stays in the room, where that run does not look until its next stage."""
        assignee = store.get_employee(task["assignee_id"]) if task["assignee_id"] else None
        who = f"{assignee['name']} (id `{assignee['id']}`)" if assignee else "없다"
        return (f"시스템: 이 방은 작업 `{task['title']}` 의 작업방이고, 이 턴은 읽기 전용 턴이다 — 파일을 고치거나 명령을 "
                f"돌리지 않는다. 이 작업의 담당은 {who} 이고 파일은 담당의 작업 런만 고친다. 답을 output 에 쓰면 방에 "
                "올라간다. 담당의 작업 런에 바로 전해야 하는 말은 output 을 비우고 room_post(text, mentions=[담당의 id]) "
                "로 쓴다 — 그 글이 담당의 런에 끼어든다.")

    @staticmethod
    def _group_note(store: StudioStore, room: dict, employee: dict) -> str:
        """What a turn of a group room (a department room takes its turns the same way) is told after the new messages, as a line of the server: who is in the
        room, who the reader is, how to pass what is not its business and how to ask another member (§6.6). It
        goes with the messages, so a conversation that resumes — which is sent these only — is told it too."""
        names = {row["id"]: row["name"] for row in store.list_employees(include_retired=True)}
        who = ", ".join(f"{names.get(id) or id} (id `{id}`)" for id in api.group_order(store, room["id"]))
        kind = "파트방" if room["kind"] == "department" else "단체방"
        return (f"시스템: 이 방은 {kind} `{room['title']}` 이다. 구성원은 owner 와 직원 {who} 이고, 너는 "
                f"{employee['name']} (id `{employee['id']}`) 이다. 위 글이 네가 답할 일이면 답을 output 에 쓴다. 네 일이 "
                "아니면 넘어간다 — output 을 비우고 room_post 도 쓰지 않으면 방에 글이 남지 않는다. 다른 직원의 답이 "
                "필요할 때만 room_post(text, mentions=[그 직원의 id]) 로 쓴다 — 그 직원의 턴이 네 턴 바로 다음에 들어가고, 같은 "
                "말을 output 에 또 쓰지 않는다. 다른 구성원의 몫이라 넘길 때도 그 직원을 멘션한다 — 멘션하지 않으면 "
                "그 직원은 이 글에 답하지 않을 수 있다. 자기 자신과 이 방의 구성원이 아닌 직원은 멘션할 수 없다. "
                + employees.plan_note().removeprefix("시스템: "))

    def _make(self, store: StudioStore, employee: dict, room: dict, member: dict, fresh: list[dict], text: str,
              where: Where, extra: str = ""):
        """The run of this turn, not advanced yet. `Refused` when it cannot be made."""
        values = self._values(store, employee)
        repo, folder = self._folder(store, employee, where.project_id)
        session_id, conversation = self._session(store, member, employee, repo, folder, where, room)
        if where.task is not None:
            text = f"{text}\n{self._read_note(store, where.task)}"
        elif room["kind"] in api.GROUP_KINDS:
            text = f"{text}\n{self._group_note(store, room, employee)}" + (f"\n{extra}" if extra else "")
        elif room["kind"] == "dm":
            text = f"{text}\n{employees.plan_note()}"
        hiring =self._hiring(store, employee, where)
        goal = self._goal(store, employee, room, text, session_id, where.project_id, hiring)
        try:
            run = self.engine.create(
                self.relay, goal, None if repo else folder, session_id=session_id, repo=repo,
                stage_models={STAGE: dict(values["slot"])}, attachments=self._files(fresh) or None, approval="ai",
                mcp=list(values["mcp"]), mcp_only=list(values["mcp"]), model_cap=values["model_cap"],
                effort_cap=values["effort_cap"])
        except ValueError as e:
            raise Refused(f"엔진이 런을 만들지 않았다: {e}") from e
        if conversation:
            # the engine resumes the conversation stage_sessions names and sends what interject left, not the goal
            run.stage_sessions[STAGE] = conversation
            self.engine.save(run)
            files = "".join(f"\n- `{path}`" for path in run.baton.attachments)
            said = text + (f"\n첨부 파일 (필요하면 Read 로 읽는다):{files}" if files else "")
            # HR's values are made anew every turn: a project made since the conversation began is in this one,
            # and so are the frames of the parts (`hiring_notes`) — a part made or a frame changed meanwhile
            note = self._policy_note(member, where.project_id, True)
            guide_note = self._guide_note(member, employee, True)
            self.engine.interject(run.id, "\n\n".join(part for part in (said, note, guide_note, hiring) if part))
        else:
            self._policy_note(member, where.project_id, False)
            self._guide_note(member, employee, False)
        return run

    def _guide_note(self, member: dict, employee: dict, resumed: bool) -> str:
        """H-6: what a turn that resumes a conversation adds when the employee's GUIDE.md changed since it was last
        sent to that conversation (the first prompt carries it): the guide as it is now. After a restart nothing is
        remembered, so the first resumed turn carries it once more (like `_policy_note`). "" when nothing changed
        and for a turn that starts a conversation."""
        key = (member["room_id"], member["employee_id"])
        text = employees._read(employees.folder(self.cfg.studio_dir, employee["id"]) / "GUIDE.md") or ""
        seen = hashlib.sha256(text.encode("utf-8")).hexdigest()
        previous = self._guide_seen.get(key)
        self._guide_seen[key] = seen
        if not resumed or previous == seen:
            return ""
        lead = ("시스템: 서버가 다시 시작돼 네 지침(GUIDE.md)을 다시 싣는다. 아래가 지금 내용이다 — 앞의 것과 다르면 이것을 따른다."
                if previous is None else
                "시스템: 네 지침(GUIDE.md)이 대화를 연 뒤 바뀌었다. 아래가 지금 내용이다 — 앞의 것 대신 따른다.")
        return lead + "\n\n" + (text.strip() or "(지금은 지침이 비어 있다)")

    def _policy_note(self, member: dict, project_id: str | None, resumed: bool) -> str:
        """What a turn that resumes a conversation adds when the policy changed since it was last sent to that
        conversation (the first prompt carries it, `_goal`): the policy and the rule to ask for approval as they are
        now. "" when nothing changed, for a turn that starts a conversation and for a room without a project."""
        key = (member["room_id"], member["employee_id"])
        if not project_id:
            self._policy_seen.pop(key, None)
            return ""
        text = employees._read(employees.policy_path(self.cfg.studio_dir, project_id)) or ""
        seen = (project_id, hashlib.sha256(text.encode("utf-8")).hexdigest())
        previous = self._policy_seen.get(key)
        self._policy_seen[key] = seen
        if not resumed or previous == seen:
            return ""
        body = employees.assemble(None, None, None, text.strip() or "(지금은 정책이 비어 있다)",
                                  rule=employees.APPROVAL_RULE)
        return ("시스템: 이 프로젝트의 정책이나 결재 규칙이 대화를 연 뒤 바뀌었다. 아래가 지금 내용이다 — 앞의 것 대신 따른다."
                "\n\n" + body)

    def _values(self, store: StudioStore, employee: dict) -> dict:
        """§9.5: the `chat` slot under the cap, `chat_mcp` + `studio`, the two caps. Refused when the file cannot
        be read (§9.6) or the provider — the slot's, else the relay stage's — is no Claude Code runner (5 의 가)."""
        department = store.get_department(employee["department_id"])
        try:
            settings = employees.read_settings(self.cfg.studio_dir, employee, department,
                                               employees.config_cap(self.cfg))
        except employees.Unreadable as e:
            raise Refused(f"employee.yaml 을 읽지 못했다 ({', '.join(e.codes)}): {e}") from e
        checks = employees.context(self.cfg)
        values = employees.chat_values(settings, checks)
        try:
            spec, _ = RelaySpec.load(self.relay)
            stage = next(s for s in spec.stages if s.name == STAGE)
        except (OSError, ValueError, StopIteration) as e:
            raise Refused(f"채팅 릴레이 {RELAY} 를 읽지 못했다: {type(e).__name__}: {e}") from e
        failed = list(values["failed"])
        if not failed and not values["slot"].get("provider") and not checks.guarded(stage.primary):
            failed.append("provider_unguarded")
        if failed:
            raise Refused(f"채팅 턴의 provider 가 Claude Code 가 아니다 ({', '.join(failed)})", tuple(failed))
        return values

    def _folder(self, store: StudioStore, employee: dict, project_id: str | None) -> tuple[str | None, Path]:
        """§9.5: (repo, its folder) of the project the turn is of — the task's for a read turn, else the one the
        employee is placed in; the project's instructions come with it —, or (None, the employee's own folder)
        when there is none: an employee placed nowhere, a project without a repo."""
        project = store.get_project(project_id) if project_id else None
        if project and project["repo"]:
            try:
                spec = self.engine.repos.get(project["repo"])
            except KeyError as e:
                raise Refused(str(e.args[0] if e.args else e)) from e
            if not spec.exists:
                raise Refused(f"저장소 {project['repo']} 의 경로가 이 PC 에 없다: {spec.path or '(미지정)'}")
            return project["repo"], spec.resolved
        folder = employees.folder(self.cfg.studio_dir, employee["id"])
        folder.mkdir(parents=True, exist_ok=True)
        return None, folder.resolve()

    def _session(self, store: StudioStore, member: dict, employee: dict, repo: str | None,
                 folder: Path, where: Where, room: dict | None = None) -> tuple[str, str | None]:
        """(the session the turn is filed under, the conversation it resumes). A new session — the first turn,
        or the employee now works elsewhere — starts a new conversation: Claude Code keeps one per folder. It
        is named after the task, after the group room, or `1:1`."""
        history = self.engine.history
        session = history.get_session(member["session_id"]) if member["session_id"] else None
        if session is not None and (session.get("repo") or None) == repo and (
                repo is not None or Path(session["workdir"]) == folder):
            return session["id"], member["conversation_id"]
        title = where.task["title"] if where.task is not None else "1:1"
        if room is not None and room["kind"] in api.GROUP_KINDS:
            title = room["title"] or ("파트방" if room["kind"] == "department" else "단체방")
        session = history.create_session(f"{employee['name']} · {title}", str(folder), repo)
        store.update_room_member(member["room_id"], member["employee_id"], session_id=session["id"],
                                 conversation_id=None)
        return session["id"], None

    def _goal(self, store: StudioStore, employee: dict, room: dict, text: str, session_id: str,
              project_id: str | None, hiring: str = "") -> str:
        """The new messages first — the session's list of earlier requests shows the head of a goal —, then the
        hand-over of the conversation before, then the employee's part of the prompt (§9.1) and last `hiring`:
        for the HR employee, the values of a hire (§8.4; `_hiring`). The goal is sent when a conversation
        starts (the '하네스' section of the employee's part is in it only then): a turn that resumes sends the new messages, `hiring` again and, when the policy changed since it was
        last sent, the policy with the rule to ask for approval (`_policy_note`)."""
        handoff = self._handoff(session_id)
        parts = [text, f"===== {HANDOFF_TITLE} =====\n{handoff}" if handoff else "",
                 employees.prompt_for(store, self.cfg.studio_dir, employee, room["id"], project_id,
                                      harness=harness_root.section_for(self.cfg.studio_harness_root)), hiring]
        return "\n\n".join(part for part in parts if part.strip())

    def _hiring(self, store: StudioStore, employee: dict, where: Where) -> str:
        """`employees.hiring_notes` for a chat turn of an employee of the HR part, "" for anyone else and for a
        read turn of a task room — no hire is drafted there (§8.4): the project ids, the parts and the models HR
        writes in a draft — the REST paths that list them are the owner's. Read from the store at every turn.
        The models are the engine's catalog, as `GET /studio/models` reads it (work.py `catalog`); when it
        cannot be read the turn is made without them."""
        if employee["department_id"] != employees.HR_DEPARTMENT or where.mode != "chat":
            return ""
        checks = employees.context(self.cfg)
        try:
            catalog = (getattr(self.engine, "providers", None) or checks.providers).catalog()
        except Exception:  # noqa: BLE001 - a list that cannot be read must not stop the turn
            log.exception("studio: 채용에 쓰는 모델 목록을 읽지 못했다")
            catalog = []
        return employees.hiring_notes(store, checks, catalog)

    def _handoff(self, session_id: str) -> str:
        """What the session's last finished turn said the conversation had come to. A turn that resumes does not
        send it — it is in the prompt of a conversation that is new, or could not be resumed."""
        for turn in reversed(self.engine.history.turns(session_id)[-HANDOFF_LOOKBACK:]):
            try:
                run = self.engine.load(turn["run_id"])
            except (OSError, ValueError):
                continue
            if run.status != "done":
                continue
            baton = run.baton
            lines = [f"지금까지의 대화: {baton.state.strip()}"] if baton.state.strip() else []
            for title, items in (("아직 남은 것", baton.open_issues), ("이어서 할 일", baton.next_steps)):
                if items:
                    lines += [f"{title}:"] + [f"- {item}" for item in items]
            return "\n".join(lines)
        return ""

    def _files(self, fresh: list[dict]) -> list[Path]:
        """The files attached to those messages, where the server keeps them (§6.6: `path` is relative to
        runs/studio/attachments/). `create` copies them into the run's folder, which the stage may read."""
        root = Path(self.cfg.studio_dir) / "attachments"
        found = [root / a["path"] for message in fresh for a in message.get("attachments") or []
                 if isinstance(a, dict) and a.get("path")]
        return [path for path in found if path.is_file()]

    def _say(self, store: StudioStore, room: dict, text: str, run_id: str | None = None, event: str = RUN_ERROR,
             **more) -> None:
        """§9.5: why there is no answer, as a `system` message of the room. No inbox item, no exchange counted.
        `event` · `more` are its `meta`: a group room that is held says so the same way (§4.6)."""
        meta = {"event": event, **({"run_id": run_id} if run_id else {}), **more}
        with store.transaction() as tx:
            message = store.create_message(room["id"], "system", tx=tx, body=text, meta=meta)
            store.emit(tx, "message.created", {"message": message}, room_id=room["id"], task_id=room["task_id"],
                       project_id=api._room_project(store, room, tx))
