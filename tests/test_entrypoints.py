"""The CLI parser and the server's routes must build: a duplicated subcommand only fails when `relay` starts."""
import sys

import pytest


def test_relay_cli_parser_builds_with_every_subcommand(monkeypatch, capsys):
    from relay_agent import cli

    for argv in (["relay", "--help"], ["relay", "cache", "--help"], ["relay", "repo", "--help"]):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as done:
            cli.main()
        assert done.value.code == 0
    assert "check" in capsys.readouterr().out


def test_server_routes_are_registered_once():
    from relay_agent import server

    seen = set()
    for route in server.app.routes:
        for method in getattr(route, "methods", None) or ():
            key = (method, route.path)
            assert key not in seen, f"duplicate route {key}"
            seen.add(key)
