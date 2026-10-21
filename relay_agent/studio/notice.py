"""What a studio run tells its room instead of stopping (T1 of the 'owner decides less' campaign).

A task run and a chat turn are not stopped by the run · stage budget or by a human gate of the relay
(`RelayEngine.studio_mode`). The engine calls `tell` instead, and the room gets a `system` message:
- `spend`: '이 작업이 지금까지 $N 을 썼습니다(단계 ○○)' — at the first budget and at every
  `studio_spend_notice_step_usd` more (`meta.event` spend_notice; push.py sends it quietly);
- `gate`: the stage whose design gate was passed without asking, with its summary (`meta.event` gate_passed;
  not pushed);
- `restart`: a run that failed was started over by the server, the changes kept (`meta.event` restart_notice;
  push.py sends it quietly);
- `review`: a run ended without asking for a review and the server filed it (`meta.event` review_filed; the
  final_review item has its own push).

server.py registers `mode` and `tell` on the engine. Both never raise into a run: no store, no room, no
message — nothing is told.
"""

from __future__ import annotations

import logging

from . import api

log = logging.getLogger(__name__)

SPEND_EVENT, GATE_EVENT = "spend_notice", "gate_passed"
RESTART_EVENT, REVIEW_EVENT = "restart_notice", "review_filed"
EDITOR_NOTICE_EVENT = "editor_notice"  # the editor does not answer; the run goes on (pushed quietly)
SUMMARY_MAX = 300


def _head(run_id: str) -> dict | None:
    """(store, head) of a run studio started: None for any other run and while the store is not open."""
    with api._lock:
        store = api._store
    if store is None:
        return None
    head = api.run_head(store, run_id)
    if head is None or not head.get("employee_id") or not head.get("room_id"):
        return None
    return {**head, "_store": store}


def mode(run_id: str) -> str | None:
    """'work' for a task run, 'chat' (or 'read') for a tracked turn, None for a run that is not studio's —
    the same test as `api.guard_stage`."""
    found = _head(run_id)
    return found["mode"] if found else None


def _text(kind: str, detail: dict) -> tuple[str, str] | None:
    stage = str(detail.get("stage") or "")
    if kind == "spend":
        spent = float(detail.get("spent_usd") or 0)
        return SPEND_EVENT, f"이 작업이 지금까지 ${spent:.2f} 을 썼습니다(단계 {stage})"
    if kind == "gate":
        summary = " ".join(str(detail.get("summary") or "").split())[:SUMMARY_MAX]
        return GATE_EVENT, f"`{stage}` 단계를 승인 없이 넘겼습니다" + (f" — {summary}" if summary else "")
    if kind == "editor":
        why = " ".join(str(detail.get("why") or "").split())[:SUMMARY_MAX]
        return EDITOR_NOTICE_EVENT, ("에디터가 응답하지 않습니다" + (f" — {why}" if why else "") +
                                     ". 작업은 멈추지 않았습니다. 모달 창이 떠 있거나 에디터가 꺼졌다면 확인해 주세요 "
                                     "(담당은 `editor_build` 로 에디터를 다시 띄울 수 있습니다)")
    if kind == "restart":
        why = " ".join(str(detail.get("why") or "").split())[:SUMMARY_MAX]
        return RESTART_EVENT, ("런이 실패해 바뀐 파일은 두고 지난 요약을 실어 새 대화로 다시 시작했습니다" +
                               (f" — {why}" if why else ""))
    if kind == "review":
        return REVIEW_EVENT, "검수 요청 없이 끝나 서버가 검수를 대신 올렸습니다"
    return None


def tell(run_id: str, kind: str, detail: dict) -> None:
    """`engine.run_notice`: write the notice of `kind` ('spend' | 'gate' | 'editor' | 'restart' | 'review') into the run's room."""
    try:
        said = _text(kind, detail)
        found = _head(run_id) if said else None
        if said is None or found is None:
            return
        store = found.pop("_store")
        room = store.get_room(found["room_id"])
        if room is None:
            return
        event, body = said
        meta = {"event": event, "run_id": run_id, "stage": detail.get("stage"),
                **({"task_id": found["task_id"]} if found.get("task_id") else {}),
                **({"spent_usd": detail.get("spent_usd")} if kind == "spend" else {})}
        with store.transaction() as tx:
            message = store.create_message(room["id"], "system", tx=tx, body=body, meta=meta)
            store.emit(tx, "message.created", {"message": message}, room_id=room["id"], task_id=room["task_id"],
                       project_id=api._room_project(store, room, tx))
    except Exception:  # noqa: BLE001 - a notice must not break a run
        log.exception("studio: 런 %s 의 %s 안내를 방에 남기지 못했다", run_id, kind)
