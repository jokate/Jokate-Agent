import pytest

from relay_agent.pipeline import RelayEngine


@pytest.fixture(autouse=True)
def gates_always_pause(monkeypatch):
    """Most tests exercise the approval gate itself; the product default ("ai") is tested explicitly."""
    monkeypatch.setattr(RelayEngine, "DEFAULT_APPROVAL", "always")


@pytest.fixture(autouse=True)
def no_chat_turn_on_the_servers_engine(monkeypatch):
    """An owner's message in a 1:1 room asks for a chat turn (S-7). The server module's own ChatTurns would run it
    on the server's own engine — the real runs folder, a real AI — so its thread is never started in a test. A
    test of the turns makes a ChatTurns of its own and gives it a `spawn` (tests/test_studio_chat.py)."""
    from relay_agent.studio import chat

    monkeypatch.setattr(chat, "_thread", lambda work: None)
