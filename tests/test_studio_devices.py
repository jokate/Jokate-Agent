"""Studio device pairing (M-2, §6.5 · §6.2 · §3.14): the one-time code kept in memory, its exchange for a device
token, the device list, revoking (401 from the next request, the device's stream closed with 4401) and the
address a phone is told (`qr.url`). Tailscale and the LAN addresses are fakes: no subprocess runs."""
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from relay_agent.studio import api, devices, ws

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "design" / "studio" / "ARCHITECTURE.md"
TOKEN = "shared-Token-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PORT = 8123
TS = {"installed": True, "running": True, "ips": ["100.101.1.2"], "dns": "katae-pc.tail1234.ts.net"}
NO_TS = {"installed": False, "running": False, "ips": [], "dns": ""}
TS_URL = f"http://katae-pc.tail1234.ts.net:{PORT}"


def lan(port):
    return [f"http://100.101.1.2:{port}", f"http://192.168.0.5:{port}"]


class World:
    """The server app on a studio DB of its own, with remote access on and a Tailscale that is a fake."""

    def __init__(self, server):
        self.server, self.cfg = server, server.cfg
        self.owner = TestClient(server.app, headers=AUTH)                    # this PC, with the shared token
        self.local = TestClient(server.app)                                  # this PC, no token
        self.remote = TestClient(server.app, client=("192.168.0.7", 50000))  # another device, no token
        self.store = api.get_store()

    def code(self):
        r = self.owner.post("/studio/devices/pairing")
        assert r.status_code == 200, r.text
        return r.json()["code"]

    def pair(self, code=None, client=None, **body):
        """The exchange as a phone sends it; without a code a new one is asked for first."""
        body = {"code": self.code() if code is None else code, "name": "내 폰", **body}
        return (client or self.remote).post("/studio/devices/pair", json=body)

    def phone(self, **body):
        """A paired device: its row and the header its token goes in."""
        r = self.pair(**body)
        assert r.status_code == 201, r.text
        return r.json()["device"], {"Authorization": f"Bearer {r.json()['token']}"}


def wait(done, seconds=5.0):
    end = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < end, "기다리던 일이 일어나지 않았다"
        time.sleep(0.01)


@pytest.fixture
def world(tmp_path, monkeypatch):
    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")  # studio_dir · studio_db follow it
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    monkeypatch.setattr(server.cfg, "serve_host", "0.0.0.0")        # `relay remote on`
    monkeypatch.setattr(server.cfg, "server_url", f"http://127.0.0.1:{PORT}")
    monkeypatch.setattr(devices, "tailscale_info", lambda: dict(TS))
    monkeypatch.setattr(devices, "lan_urls", lan)
    monkeypatch.setattr(api, "_pairings", devices.Pairings())       # no code of another test
    monkeypatch.setattr(api, "_liveness", None)
    monkeypatch.setattr(ws, "PING_INTERVAL", 0.05)                  # reading a socket never blocks for good
    monkeypatch.setattr(ws, "LIVE_INTERVAL", 0.05)
    monkeypatch.setattr(ws, "LIVE_MIN_GAP", 0.01)
    api.close_store()
    yield World(server)
    wait(lambda: len(ws.hub) == 0 and ws.hub._ticker is None)  # every connection ended, the ticker with them
    api.close_store()


def refused(r, status, code):
    assert (r.status_code, r.json().get("code")) == (status, code), r.text
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]
    return r.json()


def connect(client, **kw):
    return client.websocket_connect("/studio/ws", **kw)


def hello(sock):
    """The first message that is not a ping."""
    for _ in range(100):
        message = sock.receive_json()
        if message["type"] != "ping":
            return message["type"]
    raise AssertionError("hello 가 오지 않았다")


def idle(sock, pings=2):
    """What comes besides pings while `pings` pings pass."""
    seen, n = [], 0
    while n < pings:
        message = sock.receive_json()
        if message["type"] == "ping":
            n += 1
        else:
            seen.append(message)
    return seen


def closed(sock):
    """The code the server closes the connection with."""
    with pytest.raises(WebSocketDisconnect) as end:
        for _ in range(2000):
            sock.receive_json()
    return end.value.code


def cut(sock):
    """The code of a connection the server closes before it has sent anything on it."""
    with pytest.raises(WebSocketDisconnect) as end:
        sock.receive_json()
    return end.value.code


def locked(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


def doc_section(start, end):
    text = DOC.read_text(encoding="utf-8")
    return text[text.index(start):text.index(end)]


# --- the codes (§3.14) --------------------------------------------------------------------------------------
def test_pairings_keep_hashes_and_give_a_code_once():
    t = [datetime(2026, 1, 1, 12, 0, 0, 500_000)]
    book = devices.Pairings(clock=lambda: t[0])
    code, expires_at = book.issue()
    assert expires_at == "2026-01-01T12:05:00" and len(code) >= 22
    assert list(book._codes) == [hashlib.sha256(code.encode()).hexdigest()]  # the hash, not the code
    assert book.redeem("wrong") is False and book.redeem("") is False and len(book) == 1  # costs the real one nothing
    assert book.redeem(code) is True and book.redeem(code) is False and len(book) == 0    # once

    late, _ = book.issue()
    t[0] += devices.PAIRING_TTL
    assert book.redeem(late) is False  # at the moment it expires it is over
    t[0] -= devices.PAIRING_TTL
    assert book.redeem(late) is False  # and the try used it up

    old, _ = book.issue()
    t[0] += timedelta(minutes=6)
    new, _ = book.issue()
    assert len(book) == 1  # the expired one was dropped when the next was issued
    assert book.redeem(old) is False and book.redeem(new) is True


def test_a_device_token_is_handed_out_with_its_hash_and_a_row_goes_out_without_it():
    token, token_hash = devices.new_token()
    assert token.startswith(api.DEVICE_PREFIX) and api.DEVICE_PREFIX == "sdt_" and len(token) >= 47
    assert token_hash == hashlib.sha256(token.encode()).hexdigest() and devices.new_token()[0] != token
    row = {"id": "dev_1", "name": "폰", "token_hash": token_hash, "fcm_token": None}
    assert devices.public(row) == {"id": "dev_1", "name": "폰", "fcm_token": None} and "token_hash" in row


# --- qr.url (§6.5 의 1) ---------------------------------------------------------------------------------------
def cfg(host="0.0.0.0", networks=(), url=f"http://127.0.0.1:{PORT}"):
    return SimpleNamespace(serve_host=host, remote_networks=list(networks), server_url=url)


def test_reach_url_is_picked_the_way_remote_status_lists_the_addresses(monkeypatch):
    from relay_agent.cli import TAILNET

    base = "http://testserver"

    def not_asked(*args):
        raise AssertionError("원격 접속이 꺼져 있으면 아무것도 묻지 않는다")

    monkeypatch.setattr(devices, "tailscale_info", not_asked)
    monkeypatch.setattr(devices, "lan_urls", not_asked)
    for host in ("127.0.0.1", "localhost", "::1"):
        assert devices.reach_url(cfg(host), base) == base  # remote access is off: where the request came

    monkeypatch.setattr(devices, "lan_urls", lan)
    monkeypatch.setattr(devices, "tailscale_info", lambda: dict(TS))
    assert devices.reach_url(cfg(), base) == TS_URL
    assert devices.reach_url(cfg(networks=TAILNET), base) == TS_URL
    assert devices.reach_url(cfg(url="http://127.0.0.1"), base) == "http://katae-pc.tail1234.ts.net:8020"
    monkeypatch.setattr(devices, "tailscale_info", lambda: dict(TS, dns=""))
    assert devices.reach_url(cfg(), base) == f"http://100.101.1.2:{PORT}"  # no name: the first IPv4

    monkeypatch.setattr(devices, "tailscale_info", lambda: dict(NO_TS))
    assert devices.reach_url(cfg(), base) == f"http://192.168.0.5:{PORT}"  # the LAN, not the 100.x address
    assert devices.reach_url(cfg(networks=["192.168.0.0/16"]), base) == f"http://192.168.0.5:{PORT}"
    assert devices.reach_url(cfg(networks=TAILNET), base) == base          # tailnet only, and no tailnet
    monkeypatch.setattr(devices, "lan_urls", lambda port: [f"http://100.101.1.2:{port}"])
    assert devices.reach_url(cfg(), base) == base
    monkeypatch.setattr(devices, "tailscale_info", lambda: dict(TS, dns="", ips=[]))  # running, no address
    assert devices.reach_url(cfg(networks=TAILNET), base) == base


def test_the_addresses_are_asked_of_cli(monkeypatch):
    from relay_agent import cli

    monkeypatch.setattr(cli, "tailscale_info", lambda: dict(NO_TS))
    monkeypatch.setattr(cli, "lan_urls", lambda port: [f"http://10.0.0.9:{port}"])
    assert devices.tailscale_info() == NO_TS and devices.lan_urls(1) == ["http://10.0.0.9:1"]


# --- POST /studio/devices/pairing ---------------------------------------------------------------------------
def test_pairing_is_the_owners_and_answers_code_expiry_and_qr(world):
    for client in (world.local, world.remote):  # this PC is asked for the token too (§6.2 의 4)
        refused(client.post("/studio/devices/pairing"), 401, "unauthorized")
        refused(client.get("/studio/devices"), 401, "unauthorized")
        refused(client.post("/studio/devices/dev_x/revoke"), 401, "unauthorized")
    assert len(api._pairings) == 0
    r = world.owner.post("/studio/devices/pairing")
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"code", "expires_at", "qr"}
    assert out["qr"] == {"v": 1, "url": TS_URL, "code": out["code"], "name": api.machine_brief()["name"]}
    left = (datetime.fromisoformat(out["expires_at"]) - datetime.now()).total_seconds()
    assert 290 < left <= 300  # five minutes
    assert list(api._pairings._codes) == [hashlib.sha256(out["code"].encode()).hexdigest()]  # the hash only
    assert world.store.list_devices(include_revoked=True) == []  # a code is no device, and no row
    assert world.owner.post("/studio/devices/pairing").json()["code"] != out["code"]


def test_with_remote_access_off_the_url_is_where_the_request_came(world, monkeypatch):
    monkeypatch.setattr(world.cfg, "serve_host", "127.0.0.1")
    assert world.owner.post("/studio/devices/pairing").json()["qr"]["url"] == "http://testserver"


# --- POST /studio/devices/pair ------------------------------------------------------------------------------
def test_pair_trades_a_code_once_for_a_device_token(world):
    code = world.code()
    r = world.pair(code, name="  내 폰  ", fcm_token="fcm-1")  # no token on the request
    assert r.status_code == 201, r.text
    out = r.json()
    assert set(out) == {"device", "token", "machine"} and out["machine"] == api.machine_brief()
    token, device = out["token"], out["device"]
    row = world.store.get_device(device["id"])
    assert token.startswith("sdt_") and row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in json.dumps(row, ensure_ascii=False)  # the text of the token is not in the DB
    assert device == {key: value for key, value in row.items() if key != "token_hash"}
    assert (device["name"], device["platform"], device["fcm_token"], device["revoked_at"]) == (
        "내 폰", "android", "fcm-1", None)
    assert len(api._pairings) == 0

    refused(world.pair(code), 400, "pairing_invalid")  # used: a code works once
    assert len(world.store.list_devices()) == 1

    bearer = {"Authorization": f"Bearer {token}"}
    assert world.remote.get("/studio/projects", headers=bearer).status_code == 200
    items = world.remote.get("/studio/devices", headers=bearer).json()["items"]
    assert [d["id"] for d in items] == [device["id"]] and "token_hash" not in items[0]
    assert items[0]["last_seen_at"] is not None
    assert world.owner.get("/studio/devices").json()["items"] == items


def test_a_wrong_code_and_a_bad_body_are_refused_and_cost_the_real_code_nothing(world):
    code = world.code()
    for wrong in ("", "nope", code[:-1], code + "x", code.swapcase()):
        refused(world.pair(wrong), 400, "pairing_invalid")
    bodies = ({}, {"code": code}, {"code": code, "name": "   "}, {"code": code, "name": "가" * 81},
              {"code": code, "name": "폰", "platform": ""}, {"code": code, "name": "폰", "extra": 1})
    for body in bodies:
        r = world.remote.post("/studio/devices/pair", json=body)
        assert r.status_code == 422 and "code" not in r.json(), r.text  # FastAPI's own body (§6.3)
    assert world.store.list_devices(include_revoked=True) == []
    r = world.pair(code, platform="ios")
    assert r.status_code == 201 and r.json()["device"]["platform"] == "ios"


def test_an_expired_code_is_refused(world):
    t = [datetime(2026, 1, 1, 12, 0, 0)]
    api._pairings.clock = lambda: t[0]
    first = world.owner.post("/studio/devices/pairing").json()
    assert first["expires_at"] == "2026-01-01T12:05:00"
    t[0] += timedelta(minutes=4, seconds=59)
    second = world.code()
    assert world.pair(first["code"]).status_code == 201  # a second before its end
    t[0] += timedelta(minutes=5)
    refused(world.pair(second), 400, "pairing_invalid")  # five minutes after it was made
    t[0] -= timedelta(minutes=5)
    refused(world.pair(second), 400, "pairing_invalid")  # and it does not come back
    assert len(world.store.list_devices()) == 1


def test_a_new_server_knows_none_of_the_codes_left(world, monkeypatch):
    code = world.code()
    assert len(api._pairings) == 1
    monkeypatch.setattr(api, "_pairings", devices.Pairings())  # what a restarted process starts with (§3.14)
    refused(world.pair(code), 400, "pairing_invalid")
    assert world.store.list_devices(include_revoked=True) == []
    assert world.pair().status_code == 201  # a code of the new one works


def test_pair_passes_the_origin_and_network_checks_first(world, monkeypatch):
    code = world.code()
    body = {"code": code, "name": "폰"}
    r = world.remote.post("/studio/devices/pair", json=body, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and "code" not in r.json()
    monkeypatch.setattr(world.cfg, "remote_networks", ["100.64.0.0/10"])
    r = world.pair(code)  # 192.168.0.7 is outside the tailnet
    assert r.status_code == 403 and "code" not in r.json()
    tailnet = TestClient(world.server.app, client=("100.101.1.2", 50000))
    assert world.pair(code, client=tailnet).status_code == 201  # the refused calls did not use the code up


def test_a_push_token_belongs_to_one_device(world):
    old, old_bearer = world.phone(fcm_token="fcm-1")
    new, _ = world.phone(name="새 폰", fcm_token="fcm-1")  # the same phone, paired again
    assert world.store.get_device(old["id"])["fcm_token"] is None and new["fcm_token"] == "fcm-1"
    assert world.remote.get("/studio/projects", headers=old_bearer).status_code == 200  # moved, not revoked
    blanks = [world.phone(fcm_token="")[0], world.phone(fcm_token="  ")[0], world.phone()[0]]
    assert [d["fcm_token"] for d in blanks] == [None, None, None]  # '' is no token: nothing to collide
    assert world.store.get_device(new["id"])["fcm_token"] == "fcm-1"
    assert len(world.store.list_devices()) == 5


def test_a_db_that_is_not_open_leaves_the_code_unused(world, monkeypatch):
    code = world.code()
    api.close_store()

    def cannot_start(path):
        raise OSError("the disk is gone")

    with monkeypatch.context() as mp:
        mp.setattr(api, "StudioStore", cannot_start)
        assert "OSError" in refused(world.pair(code), 503, "unavailable")["detail"]
        assert len(api._pairings) == 1  # get_store comes before redeem: the code was not looked at
    api.close_store()  # the failed start is forgotten (world.store is the closed one from here on)
    r = world.pair(code)
    assert r.status_code == 201, r.text
    assert [d["id"] for d in world.owner.get("/studio/devices").json()["items"]] == [r.json()["device"]["id"]]
    refused(world.pair(code), 400, "pairing_invalid")  # and now it is used


def test_a_write_that_fails_after_the_code_was_checked_spends_the_code(world, monkeypatch):
    first, _ = world.phone(fcm_token="fcm-1")
    code = world.code()
    broken = TestClient(world.server.app, client=("192.168.0.7", 50000), raise_server_exceptions=False)
    with monkeypatch.context() as mp:
        mp.setattr(world.store, "create_device", locked)
        r = world.pair(code, client=broken, name="새 폰", fcm_token="fcm-1")
        assert r.status_code == 500 and "sdt_" not in r.text  # a server error like any failed write, no token
    rows = world.store.list_devices(include_revoked=True)
    assert [d["id"] for d in rows] == [first["id"]]
    assert rows[0]["fcm_token"] == "fcm-1"  # rolled back: the first device did not give its push token up
    refused(world.pair(code), 400, "pairing_invalid")  # §6.5 의 3: a new code is asked for
    assert world.pair().status_code == 201


# --- revoke (§6.5 의 5, §7.1) ---------------------------------------------------------------------------------
def test_revoking_refuses_the_token_and_closes_the_devices_stream(world):
    assert ws.hub.drop_device in api._revoke_listeners  # ws.install registered it
    device, bearer = world.phone()
    other, other_bearer = world.phone(name="태블릿")
    with connect(world.remote, headers=bearer) as phone, connect(world.remote, headers=other_bearer) as tablet, \
            connect(world.owner) as pc:
        assert [hello(sock) for sock in (phone, tablet, pc)] == ["hello"] * 3
        r = world.owner.post(f"/studio/devices/{device['id']}/revoke")
        assert r.status_code == 200, r.text
        gone = r.json()
        assert gone["id"] == device["id"] and gone["revoked_at"] and "token_hash" not in gone
        assert closed(phone) == 4401
        wait(lambda: len(ws.hub) == 2)
        assert idle(tablet) == [] and idle(pc) == []  # the other device and the shared token stay connected

    refused(world.remote.get("/studio/projects", headers=bearer), 401, "unauthorized")  # from the next request
    with connect(world.remote, headers=bearer) as sock:
        assert closed(sock) == 4401
    assert world.remote.get("/studio/projects", headers=other_bearer).status_code == 200
    assert [d["id"] for d in world.owner.get("/studio/devices").json()["items"]] == [other["id"]]
    assert world.store.get_device(device["id"])["revoked_at"] == gone["revoked_at"]  # the row stays

    again = world.owner.post(f"/studio/devices/{device['id']}/revoke")
    assert again.status_code == 200 and again.json() == gone  # revoking twice is the same answer
    refused(world.owner.post("/studio/devices/dev_none/revoke"), 404, "not_found")


def test_a_listener_that_fails_does_not_undo_the_revoke(world, monkeypatch):
    device, bearer = world.phone()
    told = []

    def broken(device_id):
        told.append(device_id)
        raise RuntimeError("boom")

    monkeypatch.setattr(api, "_revoke_listeners", [])
    api.on_revoke(broken)
    api.on_revoke(broken)  # registered once
    api.on_revoke(told.append)
    assert world.owner.post(f"/studio/devices/{device['id']}/revoke").status_code == 200
    assert told == [device["id"]] * 2
    refused(world.remote.get("/studio/projects", headers=bearer), 401, "unauthorized")


def test_a_revoke_between_the_token_check_and_the_hub_closes_the_connection(world, monkeypatch):
    device, bearer = world.phone()
    real, dropped = ws.hub.attach, []

    def revoked_just_before(conn):
        # What the revoke route does, in its order (commit, then the listeners), at the one moment the hub
        # cannot see this connection. The route is not called: this is the event loop's thread.
        if conn.device_id == device["id"]:
            world.store.update_device(device["id"], revoked_at=datetime.now().isoformat(timespec="seconds"))
            dropped.append(ws.hub.drop_device(device["id"]))
        real(conn)

    monkeypatch.setattr(ws.hub, "attach", revoked_just_before)
    with connect(world.remote, headers=bearer) as sock:
        assert cut(sock) == 4401  # the read after the attach saw it: no hello came first
    assert dropped == [0]  # the listener found nobody to drop
    wait(lambda: len(ws.hub) == 0)
    refused(world.remote.get("/studio/projects", headers=bearer), 401, "unauthorized")


def test_a_connection_whose_device_cannot_be_read_again_is_told_to_try_later(world, monkeypatch):
    device, bearer = world.phone()
    assert world.remote.get("/studio/projects", headers=bearer).status_code == 200  # seen: no write at the WS
    asked = []

    def unreadable(device_id, **kwargs):
        asked.append(device_id)
        locked()

    with monkeypatch.context() as mp:
        mp.setattr(world.store, "get_device", unreadable)
        with connect(world.remote, headers=bearer) as sock:
            assert cut(sock) == 1013  # "cannot say" is not "revoked"
        with connect(world.owner) as pc:
            assert hello(pc) == "hello"  # the shared token has no row to read
        assert asked == [device["id"]]
    wait(lambda: len(ws.hub) == 0)
    with connect(world.remote, headers=bearer) as sock:
        assert hello(sock) == "hello"  # nothing was held against the device


# --- what a device says of itself (N-2 · A-11; §6.4 `PATCH /studio/devices/{id}`) ----------------------------
def test_a_device_changes_its_name_its_push_token_and_its_quiet_hours(world):
    phone, bearer = world.phone(fcm_token="fcm-1")
    other, _ = world.phone(name="태블릿", fcm_token="fcm-2")
    night = {"quiet_hours": {"start": "22:00", "end": "07:00", "utc_offset_min": 540}}
    path = f"/studio/devices/{phone['id']}"

    r = world.remote.patch("/studio/devices/me", headers=bearer, json={"settings": night, "fcm_token": "fcm-2"})
    assert r.status_code == 200, r.text
    row = r.json()
    assert (row["id"], row["settings"], row["fcm_token"]) == (phone["id"], night, "fcm-2")
    assert row["name"] == "내 폰" and "token_hash" not in row            # what was not sent stays
    assert world.store.get_device(other["id"])["fcm_token"] is None     # a push token belongs to one device
    assert world.store.get_device(other["id"])["revoked_at"] is None    # moved, not revoked

    row = world.owner.patch(path, json={"name": " 새 이름 ", "fcm_token": ""}).json()  # the shared token, by id
    assert (row["name"], row["fcm_token"], row["settings"]) == ("새 이름", None, night)
    late = {"quiet_hours": {"start": "23:00", "end": "06:30"}}          # no offset: this PC's clock is read
    assert world.owner.patch(path, json={"settings": late}).json()["settings"] == late  # replaced as a whole
    assert world.owner.patch(path, json={"settings": {}}).json()["settings"] == {}
    assert world.owner.patch(path, json={}).json()["name"] == "새 이름"
    assert world.store.get_device(phone["id"])["settings"] == {}

    refused(world.owner.patch("/studio/devices/me", json={"name": "누구"}), 409, "precondition_failed")
    refused(world.owner.patch("/studio/devices/dev_nope", json={"name": "누구"}), 404, "not_found")
    for bad in ({"settings": {"quiet_hours": {"start": "24:00", "end": "07:00"}}},
                {"settings": {"quiet_hours": {"start": "22:00"}}},
                {"settings": {"quiet_hours": {"start": "22:00", "end": "07:00", "utc_offset_min": 900}}},
                {"settings": {"quiet_hours": {"start": "22:00", "end": "7:00"}}},
                {"settings": {"theme": "dark"}}, {"platform": "ios"}, {"name": ""}):
        assert world.owner.patch(path, json=bad).status_code == 422, bad
    assert world.store.get_device(phone["id"])["name"] == "새 이름"

    assert world.owner.post(f"{path}/revoke").status_code == 200
    refused(world.owner.patch(path, json={"name": "누구"}), 404, "not_found")  # revoked: not there any more


# --- the document · the contract · the dashboard------------------------------------------------------------
def test_routes_are_the_m2_rows_of_the_document():
    doc = set()
    for line in doc_section("**컴퓨터 · 기기**", "- `machine` 의 필드").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        row = re.match(r"`(GET|POST|PATCH|PUT|DELETE) (/studio/[^`]+)`", cells[0]) if len(cells) == 5 else None
        if row and re.search(r"M-2|N-2", cells[4]):
            doc.add((row.group(1), row.group(2)))
    built = {(method, r.path) for r in api.router.routes for method in r.methods
             if r.path.startswith("/studio/devices")}
    assert ("PATCH", "/studio/devices/{id}") in doc  # A-11 · N-2's: the name, the push token, the quiet hours
    assert len(doc) == 5 and built == doc
    assert api.TOKENLESS == {("POST", "/studio/devices/pair")}


def test_the_document_settles_where_the_url_comes_from():
    text = DOC.read_text(encoding="utf-8")
    assert "| `relay_agent/studio/devices.py` |" in text  # §2.1
    pairing = doc_section("### 6.5 기기 페어링 (M-2)", "### 6.6")
    assert "확인 필요" not in pairing and "잠정" not in pairing
    assert "`reach_url`" in pairing and "`serve_host`" in pairing and "`tailscale_info`" in pairing
    assert "잠정, M-2" not in doc_section("### 3.14 devices", "### 3.15")  # settings_json stays N-2's


def test_openapi_holds_the_five_device_paths():
    schema = api.openapi_schema()
    paths = {(method.upper(), path) for path, item in schema["paths"].items() for method in item
             if path.startswith("/studio/devices")}
    assert paths == {("POST", "/studio/devices/pairing"), ("POST", "/studio/devices/pair"),
                     ("GET", "/studio/devices"), ("PATCH", "/studio/devices/{id}"),
                     ("POST", "/studio/devices/{id}/revoke")}
    patch = schema["components"]["schemas"]["DevicePatch"]
    assert set(patch["properties"]) == {"name", "fcm_token", "settings"} and not patch.get("required")
    hours = schema["components"]["schemas"]["QuietHours"]
    assert set(hours["properties"]) == {"start", "end", "utc_offset_min"} and set(hours["required"]) == {"start", "end"}
    assert "description" not in schema["paths"]["/studio/devices/{id}"]["patch"]
    pair = schema["components"]["schemas"]["Pair"]
    assert set(pair["properties"]) == {"code", "name", "platform", "fcm_token"}
    assert set(pair["required"]) == {"code", "name"} and pair["properties"]["name"]["maxLength"] == 80
    assert "201" in schema["paths"]["/studio/devices/pair"]["post"]["responses"]
    assert "description" not in schema["paths"]["/studio/devices/pair"]["post"]


def test_the_dashboard_asks_for_a_pairing_code_and_loads_nothing_for_it():
    html = (ROOT / "relay_agent" / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="pairPhone"' in html and 'api("/studio/devices/pairing", { method: "POST" })' in html
    handler = html[html.index('$("pairPhone").onclick'):html.index("/* resizable, collapsible layout")]
    assert "p.qr.url" in handler and "p.code" in handler and "<script" not in handler and "import(" not in handler
