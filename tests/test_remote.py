import yaml

from relay_agent.cli import remote_command
from relay_agent.config import Config


def _local(tmp_path):
    (tmp_path / "relay.config.yaml").write_text("server_url: http://127.0.0.1:8020\n", encoding="utf-8")
    return tmp_path / "relay.config.local.yaml"


def test_remote_on_writes_host_and_token_and_keeps_other_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("KATAE_TOKEN", raising=False)
    monkeypatch.delenv("KATAE_HOST", raising=False)
    local = _local(tmp_path)
    local.write_text("repos:\n  mnys: {path: C:/MNYS}\n", encoding="utf-8")

    out = remote_command("on", Config.load(tmp_path / "relay.config.yaml"), local)

    data = yaml.safe_load(local.read_text(encoding="utf-8"))
    assert data["serve_host"] == "0.0.0.0"
    assert len(data["auth_token"]) >= 24
    assert data["repos"] == {"mnys": {"path": "C:/MNYS"}}
    assert any(data["auth_token"] in line for line in out)
    cfg = Config.load(tmp_path / "relay.config.yaml")
    assert (cfg.serve_host, cfg.auth_token) == ("0.0.0.0", data["auth_token"])


def test_remote_on_keeps_token_unless_new_token_and_off_keeps_it(tmp_path, monkeypatch):
    monkeypatch.delenv("KATAE_TOKEN", raising=False)
    local = _local(tmp_path)
    cfg = Config.load(tmp_path / "relay.config.yaml")
    remote_command("on", cfg, local)
    first = yaml.safe_load(local.read_text(encoding="utf-8"))["auth_token"]

    remote_command("on", cfg, local)
    assert yaml.safe_load(local.read_text(encoding="utf-8"))["auth_token"] == first
    remote_command("on", cfg, local, new_token=True)
    second = yaml.safe_load(local.read_text(encoding="utf-8"))["auth_token"]
    assert second != first

    remote_command("off", cfg, local)
    data = yaml.safe_load(local.read_text(encoding="utf-8"))
    assert (data["serve_host"], data["auth_token"]) == ("127.0.0.1", second)


def test_remote_on_does_not_write_a_token_when_env_supplies_one(tmp_path, monkeypatch):
    monkeypatch.setenv("KATAE_TOKEN", "from-env")
    local = _local(tmp_path)
    remote_command("on", Config.load(tmp_path / "relay.config.yaml"), local)
    assert "auth_token" not in yaml.safe_load(local.read_text(encoding="utf-8"))


def test_env_host_wins_and_status_does_not_write(tmp_path, monkeypatch):
    local = _local(tmp_path)
    monkeypatch.setenv("KATAE_HOST", "0.0.0.0")
    cfg = Config.load(tmp_path / "relay.config.yaml")
    assert cfg.serve_host == "0.0.0.0"
    out = remote_command("status", cfg, local)
    assert not local.exists()
    assert out[0].startswith("원격 접속: 켜짐")
