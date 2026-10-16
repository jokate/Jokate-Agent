"""Studio live stream (S-4): WS /studio/ws.

Contract: design/studio/ARCHITECTURE.md §7.1 (connection, authentication, order of the messages), §7.2 (the
source is stream_events: a connection remembers the last id it sent and reads the rows after it from the DB
whenever a commit is announced — no memory queue, so nothing is lost or doubled between replay and live),
§7.3 (envelope), §7.4 (`run.live`, the one event sent from here that is not stored).

server.py calls `install(app, network_allowed=...)`. Its guard is an HTTP middleware and never sees a
WebSocket, so the handler does §6.2 itself: origin · network (4403), then the token (4401), owner only (4403).

Threads: a commit is announced on the thread that committed (`Hub.notify`), liveness is read on the hub's
ticker thread, and every connection sends on its own event loop. The first two only set a connection's `wake`
through `loop.call_soon_threadsafe`. A connection has one sender (`_feed`), which is what keeps the id order.

The sender and the reader of a connection run in an anyio task group, and timeouts are anyio's: the server
(and Starlette's test client) may cancel the handler through an anyio scope, and bare asyncio tasks awaited
in a `finally` lose that cancellation's origin — the handler then ends as "cancelled" instead of returning.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections import deque
from typing import Callable
from urllib.parse import urlparse

import anyio
from fastapi import FastAPI, WebSocket
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from . import api
from .store import StudioStore, StudioStoreError, now

log = logging.getLogger(__name__)

AUTH_TIMEOUT = 10.0   # seconds a connection has to finish authenticating (§7.1)
PING_INTERVAL = 30.0  # seconds between two {type: "ping"} (§7.1)
LIVE_INTERVAL = 2.0   # seconds between two liveness reads of the running runs (§7.4)
LIVE_MIN_GAP = 0.5    # the least seconds between two liveness reads, however many commits ask for one (§7.4)
PAGE = 500           # stream_events rows per read
LIVE_BACKLOG = 100    # run.live waiting for a slow connection; older ones are dropped — the next one covers them
SAID_BACKLOG = 100    # the same for what `Hub.say` sends (`job.log`): a queue of its own, so neither drops the other
UNAUTHORIZED, FORBIDDEN, TRY_LATER = 4401, 4403, 1013  # close codes: §7.1, and RFC 6455 "try again later"
ENVELOPE = ("id", "type", "at", "project_id", "room_id", "task_id", "data")  # §7.3


class Conn:
    """One authenticated connection: what its sender needs, reachable from other threads."""

    def __init__(self, store: StudioStore, device_id: str | None):
        self.loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()   # set (through the loop) when there may be something to send
        self.store = store            # the store this connection reads; it is never looked up again (_pump)
        self.device_id = device_id    # None: the shared token
        self.cursor = 0               # the last stream_events id sent
        self.kick: int | None = None  # close code asked for from outside (a revoked device)
        self.live: deque[dict] = deque(maxlen=LIVE_BACKLOG)  # run.live envelopes waiting to be sent
        self.said: deque[dict] = deque(maxlen=SAID_BACKLOG)  # what `Hub.say` sent (`job.log`), apart from run.live

    def poke(self) -> bool:
        """Wake the sender, from any thread. False when the connection's loop is gone."""
        try:
            self.loop.call_soon_threadsafe(self.wake.set)
        except RuntimeError:
            return False
        return True


class Hub:
    """The open connections. `notify` is the commit listener, `drop_device` closes a revoked device's
    connections, and a ticker thread reads liveness for as long as somebody is connected: every
    LIVE_INTERVAL, sooner when a commit announced a change, and never twice within LIVE_MIN_GAP."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: list[Conn] = []
        self._ticker: threading.Thread | None = None
        self._tick = threading.Event()  # set: read liveness at the next moment allowed, not at the next interval
        self._live: dict[str, tuple[dict, dict]] = {}  # run id -> (whose run, the `data` last sent); ticker only

    def __len__(self) -> int:
        with self._lock:
            return len(self._conns)

    def attach(self, conn: Conn) -> None:
        with self._lock:
            self._conns.append(conn)
            if self._ticker is None:
                self._ticker = threading.Thread(target=self._run_ticker, daemon=True, name="katae-studio-live")
                self._ticker.start()

    def detach(self, conn: Conn) -> None:
        with self._lock:
            if conn in self._conns:
                self._conns.remove(conn)
        self._tick.set()  # the ticker sees at once whether anybody is left

    def notify(self, ids: tuple[int, ...] = ()) -> None:
        """Stream events were committed (any thread): wake every sender — it reads the rows itself — and the
        ticker, because a run may have started or stopped with that change. The ticker reads at the next
        moment allowed (LIVE_MIN_GAP after its last read): a burst of commits costs one read, not one each."""
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            if not conn.poke():
                self.detach(conn)
        if conns:
            self._tick.set()

    def drop_device(self, device_id: str) -> int:
        """Close the connections of a device with 4401 — the device was revoked (§7.1, §6.5). How many.

        Only the connections attached at this moment. One that is still between its token check and `attach`
        is not here to be dropped: `stream` closes it, reading `revoked_at` again once it has attached."""
        with self._lock:
            conns = [c for c in self._conns if c.device_id == device_id]
        for conn in conns:
            conn.kick = UNAUTHORIZED
            conn.poke()
        return len(conns)

    def say(self, type: str, data: dict, *, project_id: str | None = None) -> None:
        """Send an event that is not stored (§7.2: `job.log`, M-8) to every connection, from any thread: the
        envelope of §7.3 with `id` null, put in a queue of its own beside the one `run.live` waits in (a burst
        of one does not drop the other). Nobody connected: nobody is told — the next one covers it. Against the
        stored events its place is not kept: a sender reads the rows first."""
        event = {"id": None, "type": type, "at": now(), "project_id": project_id, "room_id": None,
                 "task_id": None, "data": data}
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            conn.said.append(event)
            if not conn.poke():
                self.detach(conn)

    # --- run.live (§7.4) ------------------------------------------------------------------------------------
    def _run_ticker(self) -> None:
        due = 0.0  # time.monotonic() before which liveness is not read: LIVE_MIN_GAP after the last read
        while True:
            self._tick.clear()  # before the read: a change announced during it starts the next round
            with self._lock:
                conns = list(self._conns)
                if not conns:  # nobody is connected: liveness is not read
                    self._ticker = None
                    self._live.clear()
                    return
            early = due - time.monotonic()
            if early > 0:
                # Woken too soon after the last read (every commit wakes this thread): the read waits for
                # its moment. Any wake-up goes back to the top, so a connection that left is seen at once.
                self._tick.wait(early)
                continue
            try:
                self._read_live(conns)
            except Exception:  # noqa: BLE001 - e.g. the store was closed under a connection; try again next round
                log.debug("studio: run.live 를 읽지 못했습니다", exc_info=True)
            due = time.monotonic() + LIVE_MIN_GAP
            self._tick.wait(LIVE_INTERVAL)

    def _read_live(self, conns: list[Conn]) -> None:
        """One round: every run that may be running, and every run last reported as alive, is read once.

        Sent when the `data` differs from the last one sent — but a run that is not alive is reported once:
        its `quiet_seconds` keeps counting, and that is not news. So: at the start or resume, every round
        while it runs (the seconds change), and once more when it has stopped."""
        if api._liveness is None:
            return
        runs = api.live_runs(conns[0].store)  # the store a connection holds — never api.get_store() from here
        watch = {run_id: head for run_id, (head, last) in self._live.items() if last.get("alive")}
        watch.update(runs)
        for run_id, head in watch.items():
            try:
                data = api.live_data(head)
            except Exception:  # noqa: BLE001 - one unreadable run does not stop the others
                log.debug("studio: 런 %s 의 liveness 를 읽지 못했습니다", run_id, exc_info=True)
                continue
            last = self._live.get(run_id, (None, None))[1]
            if data is None:  # the engine has no such run (any more)
                if run_id not in runs:
                    self._live.pop(run_id, None)
                continue
            if last is None or (data != last and (data.get("alive") or last.get("alive"))):
                self._live[run_id] = (head, data)
                event = {"id": None, "type": "run.live", "at": now(), "project_id": head.get("project_id"),
                         "room_id": head.get("room_id"), "task_id": head.get("task_id"), "data": data}
                for conn in conns:
                    conn.live.append(event)
                    conn.poke()
            if run_id not in runs and not data.get("alive"):
                self._live.pop(run_id, None)  # it left the list and its end was reported: forget it
        for run_id in [r for r in self._live if r not in watch]:
            del self._live[run_id]  # stopped, reported, and no longer a working task's or a tracked run


hub = Hub()
_network_allowed: Callable[[str], bool] | None = None


def install(app: FastAPI, *, network_allowed: Callable[[str], bool]) -> None:
    """Add WS /studio/ws to the server and have every commit that wrote stream events announced to the hub.
    A revoked device's connections are closed the same way: the revoke route of api.py tells the hub (§6.5).

    `network_allowed(host)` is the network check of the server's guard, asked for every connection. The
    config and the token check are api.py's (`api.install` comes first)."""
    global _network_allowed
    _network_allowed = network_allowed
    app.add_api_websocket_route("/studio/ws", stream)
    api.on_commit(hub.notify)
    api.on_revoke(hub.drop_device)  # api.py does not import this module: it is told whom to call
    api.on_live(hub.say)  # the events nobody stores: `job.log` of a project being made (M-8)


# --- one connection -------------------------------------------------------------------------------------------
async def _send(ws: WebSocket, message: dict) -> None:
    await ws.send_text(json.dumps(message, ensure_ascii=False))


async def _close(ws: WebSocket, code: int, reason: str) -> None:
    """Close with a code the client can read. `reason` is short English: a close frame carries 123 bytes."""
    try:
        await ws.close(code=code, reason=reason)
    except (WebSocketDisconnect, OSError, RuntimeError):  # the client has left already
        pass


async def _first_message_token(ws: WebSocket) -> str | None:
    """The token of a first message `{type: "auth", token}` that arrives within AUTH_TIMEOUT (a browser
    cannot set the header). None for anything else: silence, another message, not JSON, a client that left."""
    message = None
    with anyio.move_on_after(AUTH_TIMEOUT):
        message = await ws.receive()
    if message is None or message.get("type") != "websocket.receive":
        return None
    try:
        body = json.loads(message.get("text") or "")
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("type") != "auth" or not isinstance(body.get("token"), str):
        return None
    return body["token"]


def _as_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


async def stream(ws: WebSocket) -> None:
    """WS /studio/ws?after_id=<n> (§7.1).

    Accepted first: a close code reaches the client only on an accepted socket (a refused handshake is a
    plain HTTP 403). Then origin · network -> token -> owner, and the stream: `_feed` sends, `_listen`
    reads, and when one of them ends the other is stopped.

    A device's connection reads its row once more after it is attached. A revoke that was committed between
    the token check and `hub.attach` told the hub when this connection was not there to drop; read after the
    attach, either the revoke is seen here (4401, nothing sent) or the connection is in the hub when the
    revoke tells it. 1013 when the row cannot be read. The shared token has no row and reads nothing."""
    await ws.accept()
    origin = ws.headers.get("origin")
    if origin and urlparse(origin).netloc != ws.headers.get("host"):
        # The guard skips this check for GET, and a WebSocket handshake is a GET: a page of another site
        # must not open the stream through the owner's browser.
        return await _close(ws, FORBIDDEN, "origin not allowed")
    host = ws.client.host if ws.client else ""
    if _network_allowed is not None and not _network_allowed(host):
        return await _close(ws, FORBIDDEN, "network not allowed")
    token = api._bearer(ws)
    if token is None:  # no header: the first message. This PC is asked too (§6.2 의 4)
        token = await _first_message_token(ws)
    if not token:
        return await _close(ws, UNAUTHORIZED, "token required")
    try:
        who = await run_in_threadpool(api.identify, token, api._config())  # a device token reads the DB
    except api.StudioError:
        return await _close(ws, TRY_LATER, "studio unavailable")
    if who is None:
        return await _close(ws, UNAUTHORIZED, "token not accepted")
    if who["kind"] != "owner":  # an actor token, once S-6 establishes one: a subject, but not this path's
        return await _close(ws, FORBIDDEN, "owner only")
    try:
        store = await run_in_threadpool(api.get_store)
    except api.StudioError:
        return await _close(ws, TRY_LATER, "studio unavailable")
    conn = Conn(store, who.get("device_id"))
    hub.attach(conn)  # before the first read: nothing committed from here on goes unannounced
    try:
        if conn.device_id is not None:  # revoked between the token check and the attach? (see the docstring)
            try:
                device = await run_in_threadpool(store.get_device, conn.device_id)
            except (StudioStoreError, sqlite3.Error):
                return await _close(ws, TRY_LATER, "studio unavailable")
            if device is None or device["revoked_at"] is not None:
                return await _close(ws, UNAUTHORIZED, "device revoked")
        async with anyio.create_task_group() as both:
            async def run(part, *args) -> None:
                await part(*args)
                both.cancel_scope.cancel()  # one side has ended, so the other does too

            both.start_soon(run, _feed, ws, conn)
            both.start_soon(run, _listen, ws)
    finally:
        hub.detach(conn)


async def _feed(ws: WebSocket, conn: Conn) -> None:
    """Everything this connection is sent, by one sender: hello -> (resync) -> the stored events after the
    cursor -> live. Returns once the socket is closed or the client has gone."""
    try:
        await _open(ws, conn)
        await _pump(ws, conn)
    except (StudioStoreError, sqlite3.Error):  # e.g. the store was closed under the connection
        await _close(ws, TRY_LATER, "studio store unavailable")
    except (WebSocketDisconnect, OSError):  # the client left while we were sending
        pass


async def _open(ws: WebSocket, conn: Conn) -> None:
    """Steps 1 and the start of 2 (§7.1): hello, and where the cursor starts — with a resync when the
    cursor asked for cannot be resumed."""
    bounds = await run_in_threadpool(conn.store.stream_bounds)
    last_id, oldest_id = bounds["last_id"], bounds["oldest_id"]
    await _send(ws, {"type": "hello", "last_id": last_id, "now": now(), "machine": api.machine_brief()})
    conn.cursor = last_id  # no after_id: from now on
    asked = ws.query_params.get("after_id")
    if asked is not None:
        after_id = _as_int(asked)
        if after_id is None or after_id > last_id or after_id + 1 < oldest_id:
            # Not a cursor of this DB, or rows after it were pruned: the app reads everything again by REST
            await _send(ws, {"type": "resync", "oldest_id": oldest_id})
        else:
            conn.cursor = after_id


async def _listen(ws: WebSocket) -> None:
    """Read until the client leaves. `auth` (done by now), `pong` and anything else are ignored (§7.1)."""
    while True:
        message = await ws.receive()
        if message.get("type") == "websocket.disconnect":
            return


async def _pump(ws: WebSocket, conn: Conn) -> None:
    """Steps 2 and 3 (§7.1), one loop for both: the stored events after the cursor in id order, then the
    waiting `run.live`, then what `Hub.say` left (`job.log`, a queue of its own), and a ping every PING_INTERVAL.
    Returns once it has closed the socket (a kick)."""
    loop = asyncio.get_running_loop()
    ping_at = loop.time() + PING_INTERVAL
    while True:
        conn.wake.clear()  # before the read: a commit announced during it starts the next round
        if conn.kick is not None:
            return await _close(ws, conn.kick, "device revoked")
        while True:
            rows = await run_in_threadpool(conn.store.list_stream_events, after_id=conn.cursor, limit=PAGE)
            for row in rows:
                await _send(ws, {key: row[key] for key in ENVELOPE})
                conn.cursor = row["id"]
            if len(rows) < PAGE:
                break
        while conn.live:
            await _send(ws, conn.live.popleft())
        while conn.said:
            await _send(ws, conn.said.popleft())
        wait = ping_at - loop.time()
        if wait <= 0:
            await _send(ws, {"type": "ping"})
            ping_at = loop.time() + PING_INTERVAL
            continue
        with anyio.move_on_after(wait):
            await conn.wake.wait()
