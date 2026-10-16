"""Studio campaigns (S-14): a running campaign goes on by itself; one that is paused or cancelled starts nothing
new; and what the owner must hear of is said in the system room.

Contract: design/studio/ARCHITECTURE.md §4.10 (campaign status, the automatic progress, the hold), §4.6 (the
line, what counts as a filled dependency in each mode), §4.3 T3 (its trigger), §5.9 · §7.6 (the notices).

No planner and no loop. `Autopilot.look` is called at the occasions of LOOKS and does two things for a `running`
campaign: as the system it asks to start every backlog task that has an assignee and whose dependencies are
filled — the task joins the line, and the queue (studio/queue.py) gives it a slot and does T3, exactly as for a
start the owner asked for —, and it closes the campaign (`running` → `done`) once all its tasks have ended.
Nothing waits here: a task that stopped for a decision or a review leaves the dependencies of what follows it
unfilled, and lets its slot go, so the branches that do not wait for it go on (§4.6). The campaign's `mode` is
read at every look, so a mode that was changed on the way holds from the next start.

The hold: the queue does not know of campaigns, so the change that pauses or cancels a campaign takes the backlog
tasks a look had put in the line out of it again, in its own transaction (`TaskMachine.transition_campaign`) —
no task of a held campaign is left in the line, whenever the server stops. The look at such a campaign does the
same once more (`_hold`): the net for a look that was on its way at that change and put a task in the line after
it. When the campaign runs again they are put back — that stay in the line does not count as "stood in the line
once" (`_stood`). What the owner asked to start stays in the line, and a task that is at work is not touched.

The notices, each a system message in the system room and each said once (`_Told`): "배치 필요" for a backlog
task nobody is assigned to, "선행 거절" for one that waits for a rejected or cancelled task and so can never
start. Said again: "배치 필요" when the task's assignee was changed since (`meta.assigned_event_id`), both at the
look that follows a start or a resume of the campaign (`again`) — not after an edit of it. The third of §5.9 — "선행 반려", T8 · T9 behind a task that had started — is the task machine's mark
(`TaskMachine._flag_later`) and the inbox's line in that task's room.

The runner of the campaigns that were there before (relay_agent/campaign.py) gets `look` as its `studio` and
passes it on (`CampaignRunner.advance_studio`); `work.WorkEffects.after` calls that, and the server at its start.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .inbox import SYSTEM_ROOM
from .tasks import (CAMPAIGN_HELD, CAMPAIGN_REASON, CAMPAIGN_STARTED, SYSTEM, TERMINAL, TransitionError,
                    campaign_queued)

log = logging.getLogger(__name__)

# The task machine's, under the names they have here: it takes the tasks out of the line in the transaction that
# pauses or cancels a campaign, so the words are kept there (tasks.py imports nothing of this module).
REASON = CAMPAIGN_REASON  # the `reason` of the `queued` event the system leaves (§4.3 T3 의 계기)
HELD = CAMPAIGN_HELD      # status -> the `reason` of the system's `unqueued` event (§4.10 '멈춤'). `_stood` reads it
_ours = campaign_queued   # is the task's place in the line one a look took?
REFUSED = ("rejected", "cancelled")  # a predecessor that ended like this is never filled (§4.6): "선행 거절"
NEEDS, UPSTREAM = "needs_assignment", "upstream_rejected"  # `meta.event` of the two notices (§7.6)
TEXT = {
    NEEDS: '배치 필요: 캠페인 "{campaign}" 의 작업 "{title}" ({id}) 은 담당이 없어 시작하지 못한다. '
           "담당을 정하면 시작한다.",
    UPSTREAM: '선행 거절: 캠페인 "{campaign}" 의 작업 "{title}" ({id}) 은 선행 작업 "{before}" ({before_id}) 이 '
              "{status} 라 시작하지 못한다. 선행 관계를 바꾸거나 이 작업을 취소한다.",
}
# What `Effects.after` hears when a task of a running campaign may have become ready, or the campaign may have
# ended: a task made in backlog or approved (C2 · T1), one that asked for its review or was passed (T6 · T7: a
# dependency is filled, §4.6), one that ended another way (T2 · T10 · T11), a task whose assignee, dependencies
# or fields changed, a campaign that was made, changed or whose status changed. Not the frequent ones
# (`progress` · `run`), and not T9: a task that stood in the line once is not started again by itself.
# CAMPAIGN_STARTED is the campaign that has just gone to `running`: that look says its notices anew (`again`).
LOOKS = ("C2", "T1", "T2", "T6", "T7", "T10", "T11", "assigned", "deps", "edited", "campaign", CAMPAIGN_STARTED)
EVERY = "*"  # what `target` says when every running campaign is to be looked at


def target(what: str, row: dict) -> str | None:
    """Which campaign the change `what` of `row` is looked at for: its id, EVERY, or None = none. `row` is the
    campaign for `campaign` · CAMPAIGN_STARTED, else the task: an edit may have moved it out of a campaign, so
    every running one is looked at; a task of no campaign starts nothing."""
    if what not in LOOKS:
        return None
    if what in ("campaign", CAMPAIGN_STARTED):
        return row.get("id") or None
    if what == "edited":
        return EVERY
    return row.get("campaign_id") or None


def _stood(events: list[dict]) -> bool:
    """Condition 3 of §4.10: did the task stand in the line in a way that leaves its next start to the owner?
    A `queued` row says so — unless what follows it is the system taking the task out because its campaign was
    paused or cancelled (`_hold`): that task is put back by itself when its campaign runs."""
    stood = False
    for event in events:
        if event["kind"] == "queued":
            stood = True
        elif event["kind"] == "unqueued" and event["actor_kind"] == "system" and event["reason"] in HELD.values():
            stood = False
    return stood


def _named(meta: dict) -> int:
    """`meta.assigned_event_id` of a "배치 필요" line (§7.6); 0 for a line from before that key was written."""
    value = meta.get("assigned_event_id")
    return value if type(value) is int else 0


def _lines(store, room: dict | None, page: int = 500) -> dict[tuple, dict]:
    """(event, task id, predecessor id or None) -> the `meta` of the newest such notice in that room. The
    whole room is read, a page at a time: a notice may be far older than the newest messages. Empty when there
    is no room or it cannot be read — a task is then told of again rather than not at all."""
    last: dict[tuple, dict] = {}
    if room is None:
        return last
    try:
        after = 0
        while True:
            rows = store.list_messages(room["id"], after_id=after, limit=page)
            for row in rows:
                meta = row["meta"] or {}
                if meta.get("event") in TEXT and meta.get("task_id"):
                    last[(meta["event"], meta["task_id"], meta.get("upstream_task_id"))] = meta
            if len(rows) < page:
                return last
            after = rows[-1]["id"]
    except Exception:  # noqa: BLE001
        log.exception("studio: 캠페인 자동 진행이 시스템 방의 지난 알림을 읽지 못했다")
        return {}


class _Told:
    """What a look says in the system room (§5.9 · §7.6), and what it said of the same before: the room is read
    for that at the first notice of a look only, as `work._told_before` does (work imports this module, so that
    function is not used here). `again`: the look follows a start or a resume of its campaign, so what was
    said before that look is said once more."""

    def __init__(self, inbox, again: bool = False):
        self.inbox, self.store, self.again = inbox, inbox.store, again
        self.room: dict | None = None
        self.seen: dict[tuple, dict] | None = None
        self.said: set[tuple] = set()  # what this look has said itself: `again` does not say that twice

    def _read(self) -> dict[tuple, dict]:
        if self.seen is None:
            self.room = self.store.get_room(SYSTEM_ROOM)
            self.seen = _lines(self.store, self.room)
        return self.seen

    def last(self, event: str, task_id: str, upstream_id: str | None = None) -> dict | None:
        """The `meta` of the newest such notice; None when there is none, or when it is to be said anew."""
        key = (event, task_id, upstream_id)
        seen = self._read()
        return None if self.again and key not in self.said else seen.get(key)

    def say(self, event: str, body: str, task_id: str, upstream_id: str | None = None, **more: Any) -> None:
        """Write the notice; `more` are the keys its `meta` gets besides those of every notice (§7.6)."""
        seen = self._read()
        meta = {"event": event, "task_id": task_id, **more}
        if upstream_id is not None:
            meta["upstream_task_id"] = upstream_id
        message = self.inbox._say(self.room, body, meta)
        if message is None:
            log.warning("studio: 시스템 방이 없어 작업 %s 의 알림(%s)을 남기지 못했다", task_id, event)
            return
        seen[(event, task_id, upstream_id)] = meta
        self.said.add((event, task_id, upstream_id))


class Autopilot:
    """`inbox` gives an `Inbox` outside a request (`api.run_inbox`; None while the store is not open — then
    nothing is looked at): its store and its task machine are what `look` works with, and it writes the
    notices. No state and no lock: a start two looks ask for at once is refused for the second (`queued_at IS
    NULL`), which is logged."""

    def __init__(self, inbox: Callable[[], Any]):
        self.inbox = inbox

    def look(self, campaign_id: str | None = None, again: bool = False) -> dict:
        """Look at one campaign, or at all of them (None): only those that are `running` go on. Returns
        `{started, unassigned, finished}` — the tasks that were put in the line, the backlog tasks that could
        not be because nobody is assigned ("배치 필요", §4.10: all of them, told of now or before), the campaigns
        that were closed. One campaign that is `paused` or `cancelled` is held instead (`_hold`); that is not in
        what is returned. `again` (with a campaign id only): the look follows that campaign's start or resume
        (`tasks.CAMPAIGN_STARTED`) — the notices said before are said once more (§5.9)."""
        out: dict[str, list[str]] = {"started": [], "unassigned": [], "finished": []}
        inbox = self.inbox()
        if inbox is None:
            return out
        store = inbox.store
        if campaign_id is None:
            found = store.list_campaigns(status="running")
        else:
            one = store.get_campaign(campaign_id)
            if one is not None and one["status"] in HELD:
                self._hold(store, inbox.machine, one)
            found = [one] if one is not None and one["status"] == "running" else []
        told = _Told(inbox, again and campaign_id is not None)
        for campaign in found:
            self._advance(inbox, campaign, told, out)
        return out

    def _hold(self, store, machine, campaign: dict) -> None:
        """A paused or cancelled campaign starts nothing new (§4.10): the backlog tasks that stand in the line
        because a look put them there leave it. The change of the status has done that in its own transaction
        (`TaskMachine._hold_line`); this is the net behind it, for a look that was on its way then and put a
        task in the line after that commit. A task the owner asked to start stays — that was the owner's
        word, not the campaign's —, and so does a working task that waits for a slot to go on."""
        why = HELD[campaign["status"]]
        for task in store.list_tasks(campaign_id=campaign["id"], status="backlog"):
            if task["queued_at"] is None or not _ours(store.list_task_events(task["id"])):
                continue
            try:
                machine.unqueue(task["id"], SYSTEM, reason=why)
            except TransitionError as e:  # the queue started it meanwhile, or the owner took it out
                log.info("studio: 캠페인 %s 의 작업 %s 을 줄에서 빼지 못했다: %s", campaign["id"], task["id"], e.detail)

    def _advance(self, inbox, campaign: dict, told: _Told, out: dict) -> None:
        store, machine, campaign_id = inbox.store, inbox.machine, campaign["id"]
        for task in store.list_tasks(campaign_id=campaign_id):  # by sort_order
            if task["status"] != "backlog" or task["queued_at"] is not None:
                continue
            if not task["assignee_id"]:
                out["unassigned"].append(task["id"])
                self._needs(told, campaign, task)
                continue
            if _stood(store.list_task_events(task["id"])):
                continue  # it stood in the line once (reverted, taken out, no run made): the owner starts it
            before = [store.get_task(dep["depends_on_id"]) for dep in store.list_task_deps(task_id=task["id"])]
            gone = [row for row in before if row is not None and row["status"] in REFUSED]
            if gone:  # never filled, whatever the mode: said once, and the owner changes the task
                for row in gone:
                    self._refused(told, campaign, task, row)
                continue
            try:
                if not machine.deps_ready(task):
                    continue
                machine.request_start(task["id"], SYSTEM, reason=REASON)
            except TransitionError as e:  # the assignee is on leave or placed elsewhere, another look was first
                log.info("studio: 캠페인 %s 의 작업 %s 을 줄에 세우지 못했다: %s", campaign_id, task["id"], e.detail)
                continue  # a refusal writes nothing (no `queued` event): the next look asks again
            out["started"].append(task["id"])
        rows = store.list_tasks(campaign_id=campaign_id)
        if not rows or any(row["status"] not in TERMINAL for row in rows):
            return  # a campaign without a task stays `running`: nothing leads back from `done`
        try:
            machine.transition_campaign(campaign_id, "done", SYSTEM)
        except TransitionError as e:  # a task was added meanwhile, or the owner changed the campaign
            log.info("studio: 캠페인 %s 을 끝내지 못했다: %s", campaign_id, e.detail)
            return
        out["finished"].append(campaign_id)

    def _needs(self, told: _Told, campaign: dict, task: dict) -> None:
        """The notice "배치 필요" (§5.9): once for a task, again when its assignee was changed since — the newest
        `assigned` row of the task has a later id than the one the line names (`meta.assigned_event_id`: 0 when
        the task had none, and for a line from before that key) — or at the look that follows a start or a
        resume of the campaign (`told.again`). Ids, not times: a change in the very second of the line is seen,
        and an edit of the campaign's title, goal or mode says nothing anew."""
        try:
            newest = max((event["id"] for event in told.store.list_task_events(task["id"])
                          if event["kind"] == "assigned"), default=0)
            last = told.last(NEEDS, task["id"])
            if last is not None and newest <= _named(last):
                return
            told.say(NEEDS, TEXT[NEEDS].format(campaign=campaign["title"], title=task["title"], id=task["id"]),
                     task["id"], assigned_event_id=newest)
        except Exception:  # noqa: BLE001 - a notice that fails does not keep the look from the next task
            log.exception("studio: 작업 %s 의 배치 필요를 알리지 못했다", task["id"])

    def _refused(self, told: _Told, campaign: dict, task: dict, before: dict) -> None:
        """The notice "선행 거절" (§5.9): once for a task and that predecessor, again only at the look that
        follows a start or a resume of the campaign (`told.again`)."""
        try:
            if told.last(UPSTREAM, task["id"], before["id"]) is not None:
                return
            body = TEXT[UPSTREAM].format(campaign=campaign["title"], title=task["title"], id=task["id"],
                                         before=before["title"], before_id=before["id"], status=before["status"])
            told.say(UPSTREAM, body, task["id"], before["id"])
        except Exception:  # noqa: BLE001 - as above
            log.exception("studio: 작업 %s 의 선행 거절을 알리지 못했다", task["id"])
