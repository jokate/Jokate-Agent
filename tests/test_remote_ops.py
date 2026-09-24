import sys

import yaml

from relay_agent import supervise
from relay_agent.cli import TAILNET, autostart_command, remote_command
from relay_agent.config import Config

NO_TS = {"installed": False, "running": False, "ips": [], "dns": ""}


def config_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("KATAE_TOKEN", raising=False)
    monkeypatch.delenv("KATAE_HOST", raising=False)
    (tmp_path / "relay.config.yaml").write_text("server_url: http://127.0.0.1:8020\n", encoding="utf-8")
    return tmp_path / "relay.config.yaml", tmp_path / "relay.config.local.yaml"


def test_remote_on_tailscale_limits_to_the_tailnet_and_anywhere_lifts_it(tmp_path, monkeypatch):
    shared, local = config_dir(tmp_path, monkeypatch)
    ts = {"installed": True, "running": True, "ips": ["100.101.1.2"], "dns": "katae-pc.tail1234.ts.net"}
    out = remote_command("on", Config.load(shared), local, tailscale=True, ts=ts)
    data = yaml.safe_load(local.read_text(encoding="utf-8"))
    assert data["serve_host"] == "0.0.0.0" and data["remote_networks"] == TAILNET and data["auth_token"]
    assert Config.load(shared).remote_networks == TAILNET
    assert "원격 접속: 켜짐 (Tailscale 전용)" in out
    assert "  Tailscale 주소: http://katae-pc.tail1234.ts.net:8020" in out
    assert not any(line.startswith("  같은 공유기 주소") for line in out)  # LAN addresses won't be accepted

    remote_command("on", Config.load(shared), local, ts=NO_TS)  # no flag: the limit stays
    assert yaml.safe_load(local.read_text(encoding="utf-8"))["remote_networks"] == TAILNET
    out = remote_command("on", Config.load(shared), local, tailscale=False, ts=NO_TS)
    assert "remote_networks" not in yaml.safe_load(local.read_text(encoding="utf-8"))
    assert any("Tailscale: 설치 안 됨" in line for line in out)


def test_guard_refuses_devices_outside_the_tailnet_even_with_the_token(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import relay_agent.server as server

    monkeypatch.setattr(server.cfg, "auth_token", "t")
    monkeypatch.setattr(server.cfg, "remote_networks", list(TAILNET))
    auth = {"Authorization": "Bearer t"}
    lan = TestClient(server.app, client=("192.168.219.50", 50000))
    assert lan.get("/version", headers=auth).status_code == 403
    tailnet = TestClient(server.app, client=("100.101.1.2", 50000))
    assert tailnet.get("/version", headers=auth).status_code == 200
    assert tailnet.get("/version").status_code == 401  # the token is still required
    assert TestClient(server.app).get("/version").status_code == 200  # this PC is always trusted


def test_restart_under_the_supervisor_just_exits(monkeypatch):
    from fastapi.testclient import TestClient

    import relay_agent.server as server

    timers = []

    class FakeTimer:
        def __init__(self, delay, fn, args):
            timers.append((fn, args))

        def start(self):
            pass

    monkeypatch.setenv("KATAE_SUPERVISED", "1")
    monkeypatch.setattr(server.threading, "Timer", FakeTimer)
    monkeypatch.setattr(server, "_active_runs", lambda: [])
    r = TestClient(server.app).post("/admin/restart")
    assert r.status_code == 200 and r.json()["supervised"] is True
    assert timers and timers[0][0] is server.os._exit  # no new window: the supervisor starts the new code


def test_autostart_writes_a_startup_script_that_runs_the_supervisor(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: r"C:\Users\me\.local\bin\uv.exe" if name == "uv" else None)
    out = autostart_command("on", 8021, folder=tmp_path, start_now=False)
    script = (tmp_path / "Agent Katae Server.cmd").read_bytes().decode("utf-8")
    assert "relay_agent.supervise 8021" in script and r'"C:\Users\me\.local\bin\uv.exe" run python' in script
    assert "\r\n" in script and out[0].startswith("자동 시작 켜짐")
    assert autostart_command("status", 8021, folder=tmp_path)[0].startswith("자동 시작: 켜짐")
    autostart_command("off", 8021, folder=tmp_path)
    assert not (tmp_path / "Agent Katae Server.cmd").exists()


def test_supervisor_restarts_the_server_until_asked_to_stop(tmp_path, monkeypatch):
    stop, count = tmp_path / "supervise.stop", tmp_path / "count"
    child = ("import pathlib, sys; c = pathlib.Path(sys.argv[1]); n = int(c.read_text()) + 1 if c.exists() else 1; "
             "c.write_text(str(n)); n == 3 and pathlib.Path(sys.argv[2]).write_text('x'); sys.exit(3)")
    monkeypatch.setattr(supervise, "STOP_FILE", stop)
    monkeypatch.setattr(supervise, "port_busy", lambda port: False)
    monkeypatch.setattr(supervise.time, "sleep", lambda s: None)
    monkeypatch.setattr(supervise, "serve_command", lambda port: [sys.executable, "-c", child, str(count), str(stop)])
    assert supervise.main(["8099"]) == 0
    assert count.read_text() == "3" and not stop.exists()  # crashed twice, restarted, stopped on request
    assert [supervise.backoff(n) for n in (0, 1, 3, 9)] == [1, 2, 8, 60]
