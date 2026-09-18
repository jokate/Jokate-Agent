import pytest

from relay_agent.pipeline import RelayEngine


@pytest.fixture(autouse=True)
def gates_always_pause(monkeypatch):
    """Most tests exercise the approval gate itself; the product default ("ai") is tested explicitly."""
    monkeypatch.setattr(RelayEngine, "DEFAULT_APPROVAL", "always")
