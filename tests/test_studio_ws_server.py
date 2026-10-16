"""Studio live stream on a real server (S-4): the app on uvicorn in a thread, and a WebSocket spoken by hand
over a plain socket — no client library.

tests/test_studio_ws.py drives the handler through Starlette's test client, which hands it ASGI messages
directly. What that cannot show is here: the server the CLI starts (`uvicorn.run`) upgrades a connection at
all — uvicorn needs a WebSocket library for it — a refusal reaches the client as a close frame with the code
of §7.1, and a client that just drops the socket ends the handler (the hub empties). No AI is called."""
import base64
import json
import os
import socket
import struct
import threading

import pytest
import uvicorn

from relay_agent.studio import ws
from test_studio_ws import TOKEN, wait, world  # noqa: F401 - `world` is that file's fixture, used here too

TEXT, CLOSE, PING, PONG = 0x1, 0x8, 0x9, 0xA  # frame opcodes (RFC 6455 §5.2)


@pytest.fixture
def port(world):  # noqa: F811
    """The app served by uvicorn on a port of this PC that the system chose.

    `lifespan="off"`: the startup of server.py (recovering runs, the hourly cleanup) is not run — the test
    client of the other files does not run it either. `log_config=None`: uvicorn's warnings go to pytest's
    log capture, so a failure shows e.g. that no WebSocket library was found."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    number = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(world.server.app, lifespan="off", log_config=None,
                                           log_level="warning", timeout_graceful_shutdown=3))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True,
                              name="katae-test-uvicorn")
    thread.start()
    try:
        wait(lambda: server.started or not thread.is_alive())
        assert server.started, "uvicorn 이 뜨지 못했다"
        yield number
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
    assert not thread.is_alive(), "uvicorn 이 끝나지 않았다"


class Wire:
    """A WebSocket client on a plain socket: the opening handshake (RFC 6455 §4.1) and the frames the server
    sends. It sends no frame of its own; leaving is dropping the socket."""

    def __init__(self, port, *, token=None, origin=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.reader = self.sock.makefile("rb")
        lines = ["GET /studio/ws HTTP/1.1", f"Host: 127.0.0.1:{port}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}", "Sec-WebSocket-Version: 13"]
        if token is not None:
            lines.append(f"Authorization: Bearer {token}")
        if origin is not None:
            lines.append(f"Origin: {origin}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        self.status = self.reader.readline().decode("latin-1").strip()
        while self.reader.readline() not in (b"\r\n", b""):  # the rest of the answer's head
            pass

    def __enter__(self):
        # Accepted also when it is about to be refused: a close code only reaches an accepted socket (§7.1)
        assert self.status.startswith("HTTP/1.1 101"), f"WebSocket 으로 바뀌지 않았다: {self.status}"
        return self

    def __exit__(self, *exc):
        self.reader.close()
        self.sock.close()

    def frame(self):
        """The next frame that is not a ping or pong of the protocol: (opcode, payload). A server does not
        mask what it sends, and nothing sent here is long enough to be split."""
        for _ in range(100):
            head = self.reader.read(2)
            assert len(head) == 2, "서버가 프레임 없이 연결을 끊었다"
            assert head[0] & 0x80 and not head[1] & 0x80, "나뉘었거나 마스크된 프레임이 왔다"
            opcode, size = head[0] & 0x0F, head[1] & 0x7F
            if size == 126:
                size = struct.unpack("!H", self.reader.read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.reader.read(8))[0]
            payload = self.reader.read(size)
            assert len(payload) == size, "프레임이 중간에 끊겼다"
            if opcode not in (PING, PONG):
                return opcode, payload
        raise AssertionError("ping 만 왔다")

    def closed(self):
        """The code of the close frame the server ends with: the first two bytes of its payload."""
        opcode, payload = self.frame()
        assert opcode == CLOSE, (opcode, payload)
        return struct.unpack("!H", payload[:2])[0]


def test_a_real_server_upgrades_says_hello_and_notices_a_socket_that_is_dropped(port):
    with Wire(port, token=TOKEN) as wire:
        opcode, payload = wire.frame()
        hello = json.loads(payload)
        assert opcode == TEXT and hello["type"] == "hello" and set(hello) == {"type", "last_id", "now", "machine"}
        assert len(ws.hub) == 1
    wait(lambda: len(ws.hub) == 0)  # no close frame was sent: the server saw the socket go and the handler ended


def test_a_real_server_closes_a_silent_connection_with_4401(port, monkeypatch):
    monkeypatch.setattr(ws, "AUTH_TIMEOUT", 0.3)
    with Wire(port) as wire:  # this PC, and no token in a header or a first message
        assert wire.closed() == 4401
    assert len(ws.hub) == 0


def test_a_real_server_closes_another_origin_with_4403(port):
    with Wire(port, token=TOKEN, origin="http://other.example") as wire:  # the token is right; the page is not
        assert wire.closed() == 4403
    assert len(ws.hub) == 0


def test_a_real_server_closes_a_wrong_token_with_4401(port):
    with Wire(port, token="wrong") as wire:
        assert wire.closed() == 4401
    assert len(ws.hub) == 0
