import os

import pytest

from relay_agent.pipeline import RelayEngine


@pytest.fixture(autouse=True)
def git_looks_no_higher_than_the_tmp_folder(monkeypatch, tmp_path_factory):
    """A pass commits in the project folder when that folder is in a git repository (studio/review.py `commit`).
    A test's project is a folder under pytest's tmp folder: git must not look for a repository above it —
    whatever this PC keeps there is not a test's to commit into."""
    ceiling = tmp_path_factory.getbasetemp().resolve().as_posix()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES",
                       os.pathsep.join(filter(None, [ceiling, os.environ.get("GIT_CEILING_DIRECTORIES")])))


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


@pytest.fixture(autouse=True)
def no_push_with_this_pcs_key(monkeypatch):
    """The server module's FCM sender (N-1, studio/push.py) reads the key file `Config.studio_fcm_key` names. On a
    PC that has one, every test that files an inbox item through the server would send a real push — so no key
    file is read in a test. A test of the sender puts `load_key` back and names a key of its own
    (tests/test_studio_push.py)."""
    from relay_agent.studio import push

    monkeypatch.setattr(push, "load_key", lambda path: None)
