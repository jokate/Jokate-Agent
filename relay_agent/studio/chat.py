"""Chat turns (S-7, part 2): the owner writes in an employee's 1:1 room and the employee answers in it.

Contract: design/studio/ARCHITECTURE.md §3.7 (`room_members`: the one conversation of an employee in a room),
§6.6 (the owner's message asks for the turn), §7.4 (`run.live` — `api.track_run`), §9.5 (the values of a chat
turn, its relay and its folder, and what happens when it cannot be made), §11.2 (`create` and `mcp_only`).

A turn is one run of `relays/employee-chat.yaml` — a single stage, `chat` — on the server's engine:
- what it is made with is read at the start of every turn: the `chat` slot under the cap, `chat_mcp` + `studio`
  and nothing else (`mcp_only` drops the project's other servers, so unreal is off unless the employee's file
  turns it on), the employee's project as `repo`, or the employee's own folder for one who is placed nowhere;
- the three columns of `room_members`: `session_id` is the session of history.sqlite the turns of this room are
  filed under (one per employee and room, kept when the conversation changes), `conversation_id` the Claude Code
  conversation the next turn resumes (NULL = a new one), `fed_message_id` the last message a finished turn read;
- a turn that resumes is made like any run and then told to continue that conversation: the engine resumes the
  conversation `RunState.stage_sessions` names and sends only what `interject` left — the new messages. The whole
  goal stays the prompt of a conversation that could not be resumed (`StageCall.fresh_prompt`);
- when a turn's input passes HANDOFF_TOKENS, or its conversation could not be resumed, the conversation is
  closed: the next turn starts a new one from the hand-over — what the last finished turn said the conversation
  had come to (`state` · `open_issues` · `next_steps`). No model is called for it;
- the answer is the stage's `output`, stored as the employee's message with `message.created`.

It takes no slot of the queue and changes no employee's status (§4.6 · §4.9), files nothing in the inbox and
counts no exchange (§9.5). What it cannot do it says in the room, as a `system` message (`meta.event` `run_error`).

No FastAPI here: server.py connects it (`runs.attach(chat=…)`), and tests use it with an engine of their own.

Provisional (S-7): HANDOFF_TOKENS has no measurement behind it, and a turn that called many tools counts its
input once per call, so it may hand over early.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from ..pipeline import RelaySpec
from . import api, employees
from .store import StudioStore

log = logging.getLogger(__name__)

RELAY = "employee-chat.yaml"
STAGE = "chat"
HANDOFF_TOKENS = 100_000  # input tokens of a finished turn from which the next turn starts a new conversation
FEED_MESSAGES = 50        # messages one turn reads; what is left is read by the turn that follows at once
HANDOFF_LOOKBACK = 5      # turns of the session looked through for the last one that finished
HANDOFF_TITLE = "이전 대화의 인계 (HANDOFF)"
RUN_ERROR = "run_error"   # `meta.event` of what a turn says about itself (§7.6)


class Refused(Exception):
    """The turn is not made (§9.5 '넣지 못할 때'). Its text is what the room is told."""


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True, name="studio-chat").start()


def _own(message: dict, employee_id: str) -> bool:
    """Nothing to answer: the employee's own message, and what a turn said about itself."""
    if message["author_kind"] == "employee":
        return message["author_id"] == employee_id
    return message["author_kind"] == "system" and (message["meta"] or {}).get("event") == RUN_ERROR


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


class ChatTurns:
    """The chat turns of one server. `request` is what `Hooks.chat_turn` calls: it returns at once and the turn
    runs on a thread of its own (`spawn(work)` — tests give one that runs in place).

    One turn at a time for an employee in a room: two runs on one conversation would split it. A request that
    comes while the turn runs makes it run once more when it has ended — it then reads what came in between."""

    def __init__(self, engine, cfg, spawn: Callable[[Callable[[], None]], Any] | None = None):
        self.engine, self.cfg, self.spawn = engine, cfg, spawn
        self.relay = Path(cfg.relays_dir) / RELAY
        self._lock = threading.Lock()
        self._busy: dict[tuple[str, str], bool] = {}  # (room, employee) of a running turn -> asked again meanwhile

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

    def _work(self, employee_id: str, room_id: str) -> None:
        key = (room_id, employee_id)
        while True:
            more = False
            try:
                more = self.turn(employee_id, room_id)
            except Exception:  # noqa: BLE001 - nobody waits for this thread; the room keeps the unanswered message
                log.exception("studio: 직원 %s 의 채팅 턴(방 %s)이 실패했다", employee_id, room_id)
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
        True when it read a full page: more may be waiting, and `_work` goes on."""
        store = api.get_store()
        room, employee = store.get_room(room_id), store.get_employee(employee_id)
        member = store.get_room_member(room_id, employee_id)
        if room is None or room["archived_at"] is not None or employee is None or employee["retired_at"] is not None:
            return False
        if member is None or member["left_at"] is not None:
            return False
        listed = store.list_messages(room_id, after_id=member["fed_message_id"] or 0, limit=FEED_MESSAGES)
        fresh = [message for message in listed if not _own(message, employee_id)]
        if not fresh:
            return False
        names = {row["id"]: row["name"] for row in store.list_employees(include_retired=True)}
        try:
            run = self._make(store, employee, room, member, fresh, said(fresh, names))
        except Refused as e:
            self._say(store, room, f"{employee['name']} 의 턴을 시작하지 못했다 — {e}")
            return False
        api.track_run(run.id, mode="chat", employee_id=employee_id, room_id=room_id,
                      project_id=employee["project_id"])  # §7.4 `run.live`, and the actor token of §8.2
        try:
            run = self.engine.advance(run.id)
        finally:
            api.untrack_run(run.id)
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
        with store.transaction() as tx:
            if answer:
                message = store.create_message(room_id, "employee", tx=tx, author_id=employee_id, body=answer,
                                               run_id=run.id)
                store.emit(tx, "message.created", {"message": message}, room_id=room_id, task_id=room["task_id"],
                           project_id=api._room_project(store, room, tx))
            store.update_room_member(room_id, employee_id, tx=tx, session_id=run.session_id,
                                     conversation_id=run.done_sessions.get(STAGE) if keep else None,
                                     fed_message_id=listed[-1]["id"])
        if not posted:  # after the block: `_say` opens a transaction of its own
            self._say(store, room, f"{employee['name']} 이(가) 답하지 못했다 — 턴은 끝났지만 답이 비어 있다", run.id)
        return len(listed) >= FEED_MESSAGES

    def _make(self, store: StudioStore, employee: dict, room: dict, member: dict, fresh: list[dict], text: str):
        """The run of this turn, not advanced yet. `Refused` when it cannot be made."""
        values = self._values(store, employee)
        repo, folder = self._folder(store, employee)
        session_id, conversation = self._session(store, member, employee, repo, folder)
        goal = self._goal(store, employee, room, text, session_id)
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
            self.engine.interject(run.id, text + (f"\n첨부 파일 (필요하면 Read 로 읽는다):{files}" if files else ""))
        return run

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
            raise Refused(f"채팅 턴의 provider 가 Claude Code 가 아니다 ({', '.join(failed)})")
        return values

    def _folder(self, store: StudioStore, employee: dict) -> tuple[str | None, Path]:
        """§9.5: (repo, its folder) of the project the employee is placed in — the project's instructions come
        with it —, or (None, the employee's own folder) for one who is placed nowhere."""
        project = store.get_project(employee["project_id"]) if employee["project_id"] else None
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
                 folder: Path) -> tuple[str, str | None]:
        """(the session the turn is filed under, the conversation it resumes). A new session — the first turn,
        or the employee now works elsewhere — starts a new conversation: Claude Code keeps one per folder."""
        history = self.engine.history
        session = history.get_session(member["session_id"]) if member["session_id"] else None
        if session is not None and (session.get("repo") or None) == repo and (
                repo is not None or Path(session["workdir"]) == folder):
            return session["id"], member["conversation_id"]
        session = history.create_session(f"{employee['name']} · 1:1", str(folder), repo)
        store.update_room_member(member["room_id"], member["employee_id"], session_id=session["id"],
                                 conversation_id=None)
        return session["id"], None

    def _goal(self, store: StudioStore, employee: dict, room: dict, text: str, session_id: str) -> str:
        """The new messages first — the session's list of earlier requests shows the head of a goal —, then the
        hand-over of the conversation before, then the employee's part of the prompt (§9.1)."""
        handoff = self._handoff(session_id)
        parts = [text, f"===== {HANDOFF_TITLE} =====\n{handoff}" if handoff else "",
                 employees.prompt_for(store, self.cfg.studio_dir, employee, room["id"], employee["project_id"])]
        return "\n\n".join(part for part in parts if part.strip())

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

    def _say(self, store: StudioStore, room: dict, text: str, run_id: str | None = None) -> None:
        """§9.5: why there is no answer, as a `system` message of the room. No inbox item, no exchange counted."""
        meta = {"event": RUN_ERROR, **({"run_id": run_id} if run_id else {})}
        with store.transaction() as tx:
            message = store.create_message(room["id"], "system", tx=tx, body=text, meta=meta)
            store.emit(tx, "message.created", {"message": message}, room_id=room["id"], task_id=room["task_id"],
                       project_id=api._room_project(store, room, tx))
