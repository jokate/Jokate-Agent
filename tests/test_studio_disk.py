"""V-3 (c): the disk warning (studio/disk.py; ARCHITECTURE.md §5.9). A fake disk and fake timers: the real disk is
not asked. The system room is the record, so a store opened anew on the same folder tells nothing twice."""
from types import SimpleNamespace

import pytest

from relay_agent.studio import disk, push
from relay_agent.studio.inbox import SYSTEM_ROOM
from relay_agent.studio.store import StudioStore

GB = 1024 ** 3
WATCH = disk.DiskWatch.watch  # the real one: conftest.py lets no test's server start the timer


class Disk:
    """`machine.disk` of a test: `free` GB, or None = the drive cannot be asked."""

    def __init__(self, free):
        self.free, self.asked = free, []

    def __call__(self, folder):
        self.asked.append(folder)
        return {"path": "D:/runs", "free_bytes": None if self.free is None else int(self.free * GB),
                "total_bytes": 500 * GB}


class Timers:
    def __init__(self):
        self.waits, self.cancelled = [], 0

    def __call__(self, delay, fn):
        self.waits.append((delay, fn))
        return SimpleNamespace(cancel=self._cancel)

    def _cancel(self):
        self.cancelled += 1

    def fire(self):
        delay, fn = self.waits.pop(0)
        fn()
        return delay


class Folder:
    """The data folder: `open()` opens the studio DB in it, as the server does at its start."""

    def __init__(self, path):
        self.path, self.stores = path, []
        self.first = self.open()

    def open(self):
        self.stores.append(StudioStore(self.path / "studio" / "studio.sqlite"))
        return self.stores[-1]


@pytest.fixture
def folder(tmp_path):
    found = Folder(tmp_path)
    yield found
    for store in found.stores:
        store.close()


def watch_of(store, fake, minimum=5.0):
    return disk.DiskWatch(lambda: store, folder=lambda: "runs", min_free_gb=lambda: minimum, disk=fake)


def events(store):
    return [m["meta"]["event"] for m in store.list_messages(SYSTEM_ROOM)
            if (m["meta"] or {}).get("event") in (disk.DISK_LOW, disk.DISK_OK)]


def told(message, store):
    return push.classify({"type": "message.created", "data": {"message": message}}, store)


def test_a_shortage_is_told_once_in_the_system_room_and_pushed_at_once(folder):
    store, fake = folder.first, Disk(2.0)
    watcher = watch_of(store, fake)
    message = watcher.look()
    assert message["author_kind"] == "system" and message["meta"]["event"] == "disk_low"
    assert message["meta"]["free_bytes"] == 2 * GB and message["meta"]["min_free_gb"] == 5.0
    assert "2.0GB" in message["body"] and "5GB" in message["body"]
    assert fake.asked == ["runs"]
    grade, title, line, link = told(message, store)
    assert (grade, title, link) == (push.URGENT, "디스크 부족", {"type": "room", "id": SYSTEM_ROOM})
    assert "2.0GB" in line
    assert watcher.look() is None and events(store) == ["disk_low"]  # the same shortage: nothing more


def test_a_restart_on_the_same_data_folder_does_not_tell_the_same_shortage_again(folder):
    first = folder.first
    assert watch_of(first, Disk(2.0)).look() is not None
    first.close()
    again = folder.open()  # the server killed and started again
    assert watch_of(again, Disk(1.0)).look() is None
    assert events(again) == ["disk_low"]


def test_a_recovery_is_written_and_not_pushed_and_a_later_shortage_is_told_again(folder):
    store, fake = folder.first, Disk(2.0)
    watcher = watch_of(store, fake)
    watcher.look()
    fake.free = 50.0
    recovered = watcher.look()
    assert recovered["meta"]["event"] == "disk_ok" and told(recovered, store) is None
    assert watcher.look() is None  # fine and told fine
    fake.free = 1.0
    assert watcher.look()["meta"]["event"] == "disk_low"
    assert events(store) == ["disk_low", "disk_ok", "disk_low"]


def test_fine_from_the_start_writes_nothing(folder):
    store = folder.first
    assert watch_of(store, Disk(50.0)).look() is None and events(store) == []


def test_no_answer_from_the_disk_a_zero_threshold_or_no_store_writes_nothing(folder):
    store = folder.first
    assert watch_of(store, Disk(None)).look() is None
    assert watch_of(store, Disk(0.0), minimum=0).look() is None
    assert watch_of(store, Disk(0.0), minimum=-1).look() is None
    assert disk.DiskWatch(lambda: None, folder=lambda: "runs", min_free_gb=lambda: 5, disk=Disk(0.0)).look() is None

    def broken():
        raise RuntimeError("studio DB could not start")

    assert disk.DiskWatch(broken, folder=lambda: "runs", min_free_gb=lambda: 5, disk=Disk(0.0)).look() is None
    assert events(store) == []


def test_the_threshold_is_read_at_every_look(folder):
    store, fake = folder.first, Disk(10.0)
    minimum = [5.0]
    watcher = disk.DiskWatch(lambda: store, folder=lambda: "runs", min_free_gb=lambda: minimum[0], disk=fake)
    assert watcher.look() is None
    minimum[0] = 20.0
    assert watcher.look()["meta"]["event"] == "disk_low"


def test_watch_looks_at_once_on_a_timer_then_every_interval_until_unwatch(folder):
    store, fake, timers = folder.first, Disk(2.0), Timers()
    watcher = watch_of(store, fake)
    WATCH(watcher, timer=timers)
    WATCH(watcher, timer=timers)  # a second call is none
    assert [delay for delay, _ in timers.waits] == [0]
    assert timers.fire() == 0 and events(store) == ["disk_low"]
    assert [delay for delay, _ in timers.waits] == [disk.CHECK_EVERY]
    assert timers.fire() == disk.CHECK_EVERY and len(fake.asked) == 2 and events(store) == ["disk_low"]
    watcher.unwatch()
    assert timers.cancelled == 1
    timers.fire()  # a timer that fires all the same: no look, no next one
    assert len(fake.asked) == 2 and timers.waits == []
