"""Katae Studio layer: the company state (employees, rooms, tasks, inbox) on top of the relay server.

Contract: design/studio/ARCHITECTURE.md. Work list: design/studio/CAMPAIGN.md.
S-1: schema.py (MIGRATIONS) and store.py (StudioStore, runs/studio/studio.sqlite).
S-2: tasks.py (TaskMachine — the only code that changes tasks.status; Effects is where runs are injected).
S-3: api.py (the /studio REST router, its authentication and error bodies). server.py imports it; this
     package does not, so the store and the task machine stay usable without FastAPI.
S-4: ws.py (WS /studio/ws — the live stream, read from stream_events after every commit). server.py installs it.
S-5: inbox.py (Inbox — files and decides the nine kinds of items; Hooks is where the engine's part of a
     decision is injected). Task-bound items move through TaskMachine, so a task and its item stay together.
     runs.py (RunSink · EngineHooks — a run of the existing engine that stops for approval or at a budget
     files an item, and the decision of an item without a task goes back to the engine). server.py attaches it.
"""

from .inbox import Hooks, Inbox, InboxError
from .store import StudioSchemaError, StudioStore, StudioStoreError
from .tasks import Actor, Effects, TaskMachine, TaskNotFound, TransitionError

__all__ = ["Actor", "Effects", "Hooks", "Inbox", "InboxError", "StudioSchemaError", "StudioStore",
           "StudioStoreError", "TaskMachine", "TaskNotFound", "TransitionError"]
