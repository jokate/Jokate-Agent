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
S-6: sat.py (actor tokens `sat1.` — who a studio MCP call comes from; the secret lives in memory) and
     tools.py (the tools behind `POST /studio/mcp/<tool>`, on TaskMachine and Inbox; no FastAPI). api.py
     checks the token and serves the paths. files.py (what becomes an attachment, and the check of a path
     an employee hands in) and drafts.py (HR's drafts, runs/studio/drafts/). The process a run talks through
     is mcp_servers/studio.py; the engine gives it its env through `RelayEngine.mcp_env` (api.mcp_env).
S-7: employees.py (the four files of an employee folder, the checks of an `employee.yaml` and the values read
     from it, the employee's part of a prompt, the derived status, the seed employee of studio/seed/).
     chat.py (ChatTurns — the owner's message in a 1:1 room becomes a run of relays/employee-chat.yaml, the
     answer a message of the employee; the conversation is resumed turn after turn). server.py connects it.
S-8: work.py (the values a task's run is made with — relay, slots, the override, the cap — and WorkEffects,
     the Effects that makes the run for T3; what `GET /studio/models` lists). server.py connects it. The queue
     and what follows a run come with the next parts.
S-12: review.py (what a final review shows — the package computed when the item is read, the `run` of a task —
     and the runs whose workspace the engine's cleanup keeps; no FastAPI). server.py connects it.
"""

from .inbox import Hooks, Inbox, InboxError
from .store import StudioSchemaError, StudioStore, StudioStoreError
from .tasks import Actor, Effects, TaskMachine, TaskNotFound, TransitionError

__all__ = ["Actor", "Effects", "Hooks", "Inbox", "InboxError", "StudioSchemaError", "StudioStore",
           "StudioStoreError", "TaskMachine", "TaskNotFound", "TransitionError"]
