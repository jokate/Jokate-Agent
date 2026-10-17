import os
from pathlib import Path

import pytest

from relay_agent.pipeline import RelayEngine


@pytest.fixture(autouse=True)
def no_recovery_of_this_pcs_jobs(monkeypatch, tmp_path_factory):
    """A server that starts makes the job files a stopped server left open `failed` (studio/api.py
    `recover_jobs`, the startup hook of server.py). With the real settings that is this PC's own
    `runs/studio/jobs`: a test that opens a TestClient in a `with` would fail the jobs of a server that is
    running here. The real one is called only when the studio folder is under pytest's tmp folder."""
    from relay_agent.studio import api

    real, base = api.recover_jobs, tmp_path_factory.getbasetemp().resolve()

    def recover_jobs() -> list[dict]:
        try:
            ours = Path(api._config().studio_dir).resolve().is_relative_to(base)
        except Exception:  # noqa: BLE001 - no settings: nothing to look at
            ours = False
        return real() if ours else []

    monkeypatch.setattr(api, "recover_jobs", recover_jobs)


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
def no_daily_report_from_a_tests_server(monkeypatch):
    """A test that starts the server app must not have the daily report written into its studio DB — whether
    it is would follow the time of day the test runs at — nor the light model asked to word it (S-13). The
    timer is not started; tests/test_studio_stats.py keeps the real `watch` and calls it with timers of its own."""
    from relay_agent.studio import stats

    monkeypatch.setattr(stats.Reporter, "watch", lambda self, *args, **kwargs: None)


@pytest.fixture(autouse=True)
def no_push_with_this_pcs_key(monkeypatch):
    """The server module's FCM sender (N-1, studio/push.py) reads the key file `Config.studio_fcm_key` names. On a
    PC that has one, every test that files an inbox item through the server would send a real push — so no key
    file is read in a test. A test of the sender puts `load_key` back and names a key of its own
    (tests/test_studio_push.py)."""
    from relay_agent.studio import push

    monkeypatch.setattr(push, "load_key", lambda path: None)


@pytest.fixture(autouse=True)
def no_memory_model_from_a_tests_server(monkeypatch):
    """A decided final review with a reason asks a light model for what to learn (H-4, studio/memory.py). The server
    module's hooks would call the real `claude` — so no model is made in a test. A test of the memory gives
    `memory.learn` an `ask` of its own (tests/test_studio_memory.py)."""
    from relay_agent.studio import memory

    monkeypatch.setattr(memory, "writer", lambda model: None)


@pytest.fixture(autouse=True)
def no_awake_or_disk_look_from_a_tests_server(monkeypatch):
    """A test that starts the server app must not ask the real power API to keep this PC awake, nor look at the
    real disk (V-3, studio/awake.py · studio/disk.py). tests/test_studio_awake.py and tests/test_studio_disk.py keep
    the real classes and give them a setter, a disk and timers of their own."""
    from relay_agent.studio import awake, disk

    monkeypatch.setattr(awake.KEEPER, "enable", lambda *args, **kwargs: None)  # this instance only
    monkeypatch.setattr(disk.DiskWatch, "watch", lambda self, *args, **kwargs: None)


@pytest.fixture(autouse=True)
def harness_is_not_this_pcs(monkeypatch, tmp_path_factory):
    """`harness_root.default_home` is this PC's home: a test with no `studio_harness_root` must not get the
    owner's real harness into its prompts. Empty folder in its place; a test of the junction passes `home`."""
    from relay_agent.studio import harness_root

    empty = tmp_path_factory.mktemp("no_home")
    monkeypatch.setattr(harness_root, "default_home", lambda: empty)
