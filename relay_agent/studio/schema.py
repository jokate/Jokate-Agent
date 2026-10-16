"""Studio DB schema as ordered migration steps (design/studio/ARCHITECTURE.md §3.2).

MIGRATIONS is ((version, statements), ...) in ascending version order. A statement is one parameterless
SQL string; StudioStore runs a step's statements one by one inside a single transaction (never
executescript: it commits first). Shipped steps are frozen — a change is appended as a new version, and
there is no downgrade. Dropping a column/table or changing a CHECK needs the owner's approval.

Version 1 = the 12 tables and 21 indexes of §3.3–§3.14 (the document's text with the comments left out)
followed by the §3.15 seed. Version 2 = stream_events (§7.2). Version 3 = rooms made again for the group
rooms (§3.2: `group` in the CHECK of kind, and exchange_count). Version 4 = the art part and its part room (§3.15). Version 5 = inbox made again for the kind
`hire_recommendation`, and `hire_signals` (§3.2).
tests/test_studio_store.py compares the four with the document.
"""

from __future__ import annotations


def _statements(script: str) -> tuple[str, ...]:
    """One string per statement. A plain split on ';' — no statement below has one inside a literal."""
    return tuple(s.strip() for s in script.split(";") if s.strip())


_TABLES_V1 = """
CREATE TABLE projects (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    path            TEXT NOT NULL,
    engine_version  TEXT NOT NULL DEFAULT '',
    mcp_port        INTEGER CHECK (mcp_port IS NULL OR mcp_port BETWEEN 1 AND 65535),
    repo            TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT
);
CREATE UNIQUE INDEX projects_mcp_port ON projects(mcp_port)
    WHERE mcp_port IS NOT NULL AND archived_at IS NULL;

CREATE TABLE departments (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL UNIQUE,
    builtin              INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
    default_relay        TEXT,
    default_mcp_json     TEXT NOT NULL DEFAULT '[]',
    default_models_json  TEXT NOT NULL DEFAULT '{}',
    guide_template       TEXT NOT NULL DEFAULT '',
    sort_order           INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    archived_at          TEXT
);

CREATE TABLE employees (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    department_id  TEXT NOT NULL REFERENCES departments(id),
    project_id     TEXT REFERENCES projects(id),
    status         TEXT NOT NULL DEFAULT 'idle'
                   CHECK (status IN ('idle', 'working', 'blocked', 'on_leave')),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    retired_at     TEXT
);
CREATE INDEX employees_department ON employees(department_id);
CREATE INDEX employees_project ON employees(project_id);
CREATE UNIQUE INDEX employees_one_hr ON employees(department_id)
    WHERE department_id = 'hr' AND retired_at IS NULL;

CREATE TABLE rooms (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('dm', 'task', 'department', 'system')),
    title          TEXT NOT NULL DEFAULT '',
    employee_id    TEXT UNIQUE REFERENCES employees(id),
    task_id        TEXT UNIQUE REFERENCES tasks(id),
    department_id  TEXT UNIQUE REFERENCES departments(id),
    owner_read_id  INTEGER NOT NULL DEFAULT 0,
    muted          INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0, 1)),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    archived_at    TEXT,
    CHECK ((kind = 'dm') = (employee_id IS NOT NULL)),
    CHECK ((kind = 'task') = (task_id IS NOT NULL)),
    CHECK ((kind = 'department') = (department_id IS NOT NULL))
);
CREATE UNIQUE INDEX rooms_one_system ON rooms(kind) WHERE kind = 'system';

CREATE TABLE room_members (
    room_id          TEXT NOT NULL REFERENCES rooms(id),
    employee_id      TEXT NOT NULL REFERENCES employees(id),
    session_id       TEXT,
    conversation_id  TEXT,
    fed_message_id   INTEGER NOT NULL DEFAULT 0,
    joined_at        TEXT NOT NULL,
    left_at          TEXT,
    PRIMARY KEY (room_id, employee_id)
);
CREATE INDEX room_members_employee ON room_members(employee_id);

CREATE TABLE messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id           TEXT NOT NULL REFERENCES rooms(id),
    author_kind       TEXT NOT NULL CHECK (author_kind IN ('owner', 'employee', 'system')),
    author_id         TEXT REFERENCES employees(id),
    body              TEXT NOT NULL DEFAULT '',
    mentions_json     TEXT NOT NULL DEFAULT '[]',
    attachments_json  TEXT NOT NULL DEFAULT '[]',
    meta_json         TEXT NOT NULL DEFAULT '{}',
    run_id            TEXT,
    created_at        TEXT NOT NULL,
    CHECK ((author_kind = 'employee') = (author_id IS NOT NULL))
);
CREATE INDEX messages_room ON messages(room_id, id);

CREATE TABLE campaigns (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id),
    title        TEXT NOT NULL CHECK (length(trim(title)) > 0),
    goal         TEXT NOT NULL CHECK (length(trim(goal)) > 0),
    mode         TEXT NOT NULL DEFAULT 'milestone' CHECK (mode IN ('nonstop', 'milestone', 'each')),
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft', 'running', 'paused', 'done', 'cancelled')),
    runner_id    TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX campaigns_project ON campaigns(project_id, status);

CREATE TABLE tasks (
    id                 TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL REFERENCES projects(id),
    campaign_id        TEXT REFERENCES campaigns(id),
    parent_id          TEXT REFERENCES tasks(id),
    title              TEXT NOT NULL CHECK (length(trim(title)) > 0),
    reason             TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    spec               TEXT NOT NULL CHECK (length(trim(spec)) > 0),
    done_criteria      TEXT NOT NULL CHECK (length(trim(done_criteria)) > 0),
    type               TEXT NOT NULL CHECK (type IN ('feature', 'fix', 'chore', 'doc', 'data')),
    status             TEXT NOT NULL CHECK (status IN
                       ('proposed', 'backlog', 'working', 'blocked', 'review', 'done', 'rejected', 'cancelled')),
    proposer_kind      TEXT NOT NULL CHECK (proposer_kind IN ('owner', 'employee')),
    proposer_id        TEXT REFERENCES employees(id),
    assignee_id        TEXT REFERENCES employees(id),
    is_milestone       INTEGER NOT NULL DEFAULT 0 CHECK (is_milestone IN (0, 1)),
    relay              TEXT,
    stage_models_json  TEXT NOT NULL DEFAULT '{}',
    run_id             TEXT UNIQUE,
    queued_at          TEXT,
    exchange_count     INTEGER NOT NULL DEFAULT 0 CHECK (exchange_count >= 0),
    upstream_rejected  INTEGER NOT NULL DEFAULT 0 CHECK (upstream_rejected IN (0, 1)),
    sort_order         INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT,
    CHECK ((proposer_kind = 'employee') = (proposer_id IS NOT NULL)),
    CHECK (parent_id IS NULL OR parent_id <> id),
    CHECK (status NOT IN ('proposed', 'backlog', 'rejected', 'cancelled') OR run_id IS NULL),
    CHECK (status NOT IN ('working', 'blocked', 'review') OR (run_id IS NOT NULL AND assignee_id IS NOT NULL)),
    CHECK (queued_at IS NULL OR status IN ('backlog', 'working')),
    CHECK ((status IN ('done', 'rejected', 'cancelled')) = (finished_at IS NOT NULL))
);
CREATE INDEX tasks_project_status ON tasks(project_id, status);
CREATE INDEX tasks_campaign ON tasks(campaign_id, sort_order);
CREATE INDEX tasks_assignee ON tasks(assignee_id, status);
CREATE INDEX tasks_parent ON tasks(parent_id);
CREATE INDEX tasks_queue ON tasks(queued_at) WHERE queued_at IS NOT NULL;

CREATE TABLE task_deps (
    task_id        TEXT NOT NULL REFERENCES tasks(id),
    depends_on_id  TEXT NOT NULL REFERENCES tasks(id),
    created_at     TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_id),
    CHECK (task_id <> depends_on_id)
);
CREATE INDEX task_deps_reverse ON task_deps(depends_on_id);

CREATE TABLE task_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL REFERENCES tasks(id),
    at           TEXT NOT NULL,
    actor_kind   TEXT NOT NULL CHECK (actor_kind IN ('owner', 'employee', 'system')),
    actor_id     TEXT REFERENCES employees(id),
    kind         TEXT NOT NULL,
    rule         TEXT,
    from_status  TEXT,
    to_status    TEXT,
    reason       TEXT NOT NULL DEFAULT '',
    detail_json  TEXT NOT NULL DEFAULT '{}',
    CHECK ((actor_kind = 'employee') = (actor_id IS NOT NULL)),
    CHECK ((rule IS NULL) = (to_status IS NULL))
);
CREATE INDEX task_events_task ON task_events(task_id, id);

CREATE TABLE inbox (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN
                      ('task_proposal', 'hire', 'design_approval', 'structure_choice', 'question',
                       'pre_approval', 'budget', 'risky_command', 'final_review')),
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'decided', 'cancelled')),
    title             TEXT NOT NULL,
    project_id        TEXT REFERENCES projects(id),
    task_id           TEXT REFERENCES tasks(id),
    requester_kind    TEXT NOT NULL CHECK (requester_kind IN ('employee', 'system')),
    requester_id      TEXT REFERENCES employees(id),
    run_id            TEXT,
    payload_json      TEXT NOT NULL DEFAULT '{}',
    decision          TEXT,
    decision_comment  TEXT NOT NULL DEFAULT '',
    decision_json     TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    closed_at         TEXT,
    reminded_at       TEXT,
    delivery          TEXT NOT NULL DEFAULT 'none'
                      CHECK (delivery IN ('none', 'pending', 'delivered')),
    delivered_at      TEXT,
    CHECK ((requester_kind = 'employee') = (requester_id IS NOT NULL)),
    CHECK ((status = 'pending') = (closed_at IS NULL)),
    CHECK ((status = 'decided') = (decision IS NOT NULL)),
    CHECK ((delivery = 'delivered') = (delivered_at IS NOT NULL)),
    CHECK (delivery = 'none' OR status = 'decided')
);
CREATE INDEX inbox_status ON inbox(status, created_at);
CREATE INDEX inbox_task ON inbox(task_id);
CREATE INDEX inbox_run ON inbox(run_id) WHERE run_id IS NOT NULL;
CREATE UNIQUE INDEX inbox_one_pending_per_task ON inbox(task_id)
    WHERE status = 'pending' AND task_id IS NOT NULL;
CREATE INDEX inbox_undelivered ON inbox(run_id) WHERE delivery = 'pending';

CREATE TABLE devices (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    platform       TEXT NOT NULL DEFAULT 'android',
    token_hash     TEXT NOT NULL UNIQUE,
    fcm_token      TEXT,
    settings_json  TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    last_seen_at   TEXT,
    revoked_at     TEXT
);
CREATE UNIQUE INDEX devices_fcm ON devices(fcm_token)
    WHERE fcm_token IS NOT NULL AND revoked_at IS NULL;
"""

# §3.15: seed rows carry no parameters, so their timestamps are this SQL expression — the same format as
# store.now() (local time, to the second).
_NOW = "strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')"

_SEED_DEPARTMENTS = (  # (id, name, builtin, default_relay, sort_order)
    "'hr', '인사', 1, NULL, 0",
    "'design_doc', '기획(문서)', 1, 'doc-write', 1",
    "'program', '프로그램', 1, 'auto', 2",
    "'design_setup', '기획(세팅)', 1, 'editor-task', 3",
)
_SEED_ROOMS = (  # (id, kind, title, department_id)
    "'room_system', 'system', '시스템 공지', NULL",
    "'room_dep_hr', 'department', '인사', 'hr'",
    "'room_dep_design_doc', 'department', '기획(문서)', 'design_doc'",
    "'room_dep_program', 'department', '프로그램', 'program'",
    "'room_dep_design_setup', 'department', '기획(세팅)', 'design_setup'",
)


def _seed(table: str, columns: str, rows: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        f"INSERT INTO {table} ({columns}, created_at, updated_at)\n"
        f"VALUES ({row},\n        {_NOW},\n        {_NOW})"
        for row in rows
    )


_SEED_V1 = (
    _seed("departments", "id, name, builtin, default_relay, sort_order", _SEED_DEPARTMENTS)
    + _seed("rooms", "id, kind, title, department_id", _SEED_ROOMS)
)

_STREAM_V2 = """
CREATE TABLE stream_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    at          TEXT NOT NULL,
    project_id  TEXT,
    room_id     TEXT,
    task_id     TEXT,
    data_json   TEXT NOT NULL DEFAULT '{}'
);
"""

# Version 3 (§3.2): a CHECK cannot be altered, so rooms is made again — the owner asked for the group rooms.
# messages and room_members point at rooms and foreign keys are on. Inside the step's transaction the one thing
# that can be changed is defer_foreign_keys (PRAGMA foreign_keys does nothing there; it goes back to off by
# itself at COMMIT). The order is the point: the implicit DELETE of DROP TABLE counts one violation for every
# row that pointed at a room, and putting the rows back INTO A TABLE OF THE SAME NAME takes them off again, so
# COMMIT finds none. A room that did not come back leaves its violations: COMMIT fails and the whole step goes
# back. Making `rooms_new` and renaming it does not work — nothing takes the violations of the DROP off.
# The executor (StudioStore._apply_step) is as it was.
_ROOMS_V3 = """
PRAGMA defer_foreign_keys = ON;
CREATE TABLE rooms_hold AS SELECT * FROM rooms;
DROP TABLE rooms;
CREATE TABLE rooms (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('dm', 'task', 'department', 'system', 'group')),
    title           TEXT NOT NULL DEFAULT '',
    employee_id     TEXT UNIQUE REFERENCES employees(id),
    task_id         TEXT UNIQUE REFERENCES tasks(id),
    department_id   TEXT UNIQUE REFERENCES departments(id),
    owner_read_id   INTEGER NOT NULL DEFAULT 0,
    muted           INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0, 1)),
    exchange_count  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT,
    CHECK ((kind = 'dm') = (employee_id IS NOT NULL)),
    CHECK ((kind = 'task') = (task_id IS NOT NULL)),
    CHECK ((kind = 'department') = (department_id IS NOT NULL))
);
INSERT INTO rooms (id, kind, title, employee_id, task_id, department_id, owner_read_id, muted,
                   created_at, updated_at, archived_at)
SELECT id, kind, title, employee_id, task_id, department_id, owner_read_id, muted,
       created_at, updated_at, archived_at
FROM rooms_hold ORDER BY rowid;
DROP TABLE rooms_hold;
CREATE UNIQUE INDEX rooms_one_system ON rooms(kind) WHERE kind = 'system';
"""

# Version 4 (§3.4 · §3.15): the fifth built-in part, art, and its room. Two INSERT … SELECT, so a DB that runs
# gets them at its next start and nothing the owner changed is touched. Not made when a part with the id `art`
# or the name '아트' is there (archived ones count): `name` is UNIQUE, a failing INSERT would send the whole step
# back, and renaming or taking over the owner's part is a write over what the owner made. The migration runs
# once, so such a DB never gets the built-in art. sort_order is MAX+1: fifth on an empty DB, after the owner's
# own parts on a DB that has some. The room is made only for a built-in `art` (never for the owner's part).
_ART_V4 = f"""
INSERT INTO departments (id, name, builtin, default_relay, sort_order, created_at, updated_at)
SELECT 'art', '아트', 1, 'editor-task', (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM departments), {_NOW}, {_NOW}
WHERE NOT EXISTS (SELECT 1 FROM departments WHERE id = 'art' OR name = '아트');
INSERT INTO rooms (id, kind, title, department_id, created_at, updated_at)
SELECT 'room_dep_art', 'department', '아트', 'art', {_NOW}, {_NOW}
WHERE EXISTS (SELECT 1 FROM departments WHERE id = 'art' AND builtin = 1)
  AND NOT EXISTS (SELECT 1 FROM rooms WHERE id = 'room_dep_art' OR department_id = 'art');
"""

# Version 5 (§3.2 · §3.13): the tenth kind of inbox, `hire_recommendation` (채용 권장), and the table that keeps
# when a trigger was last put to HR. kind has a CHECK, so inbox is made again the way rooms was (see _ROOMS_V3 for
# why the order is the point): nothing points at inbox, so the deferred check is only a guard. All 18 columns are
# named, rows come back in rowid order, and the five indexes are the ones of version 1 (the partial unique one
# included). `hire_signals` is written by studio/staffing.py: one row per key (§3.2 'hire_signals').
_INBOX_V5 = """
PRAGMA defer_foreign_keys = ON;
CREATE TABLE inbox_hold AS SELECT * FROM inbox;
DROP TABLE inbox;
CREATE TABLE inbox (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN
                      ('task_proposal', 'hire', 'design_approval', 'structure_choice', 'question',
                       'pre_approval', 'budget', 'risky_command', 'final_review', 'hire_recommendation')),
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'decided', 'cancelled')),
    title             TEXT NOT NULL,
    project_id        TEXT REFERENCES projects(id),
    task_id           TEXT REFERENCES tasks(id),
    requester_kind    TEXT NOT NULL CHECK (requester_kind IN ('employee', 'system')),
    requester_id      TEXT REFERENCES employees(id),
    run_id            TEXT,
    payload_json      TEXT NOT NULL DEFAULT '{}',
    decision          TEXT,
    decision_comment  TEXT NOT NULL DEFAULT '',
    decision_json     TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    closed_at         TEXT,
    reminded_at       TEXT,
    delivery          TEXT NOT NULL DEFAULT 'none'
                      CHECK (delivery IN ('none', 'pending', 'delivered')),
    delivered_at      TEXT,
    CHECK ((requester_kind = 'employee') = (requester_id IS NOT NULL)),
    CHECK ((status = 'pending') = (closed_at IS NULL)),
    CHECK ((status = 'decided') = (decision IS NOT NULL)),
    CHECK ((delivery = 'delivered') = (delivered_at IS NOT NULL)),
    CHECK (delivery = 'none' OR status = 'decided')
);
INSERT INTO inbox (id, kind, status, title, project_id, task_id, requester_kind, requester_id, run_id,
                   payload_json, decision, decision_comment, decision_json, created_at, closed_at, reminded_at,
                   delivery, delivered_at)
SELECT id, kind, status, title, project_id, task_id, requester_kind, requester_id, run_id,
       payload_json, decision, decision_comment, decision_json, created_at, closed_at, reminded_at,
       delivery, delivered_at
FROM inbox_hold ORDER BY rowid;
DROP TABLE inbox_hold;
CREATE INDEX inbox_status ON inbox(status, created_at);
CREATE INDEX inbox_task ON inbox(task_id);
CREATE INDEX inbox_run ON inbox(run_id) WHERE run_id IS NOT NULL;
CREATE UNIQUE INDEX inbox_one_pending_per_task ON inbox(task_id)
    WHERE status = 'pending' AND task_id IS NOT NULL;
CREATE INDEX inbox_undelivered ON inbox(run_id) WHERE delivery = 'pending';
CREATE TABLE hire_signals (
    key            TEXT PRIMARY KEY,
    department_id  TEXT,
    project_id     TEXT,
    trigger        TEXT NOT NULL,
    asked_at       TEXT NOT NULL
);
"""

MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, _statements(_TABLES_V1) + _SEED_V1),
    (2, _statements(_STREAM_V2)),
    (3, _statements(_ROOMS_V3)),
    (4, _statements(_ART_V4)),
    (5, _statements(_INBOX_V5)),
)
