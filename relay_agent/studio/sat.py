"""Actor tokens (S-6): `sat1.<claims>.<signature>` — who a studio MCP call comes from.

Contract: design/studio/ARCHITECTURE.md §8.2. `claims` is the JSON `{v: 1, run, emp, room, task, mode, iat}`
in base64url (no padding); the signature is the hex of HMAC-SHA256(server secret, claims). The secret is
made when the server starts and lives in memory only, so a restart voids every token handed out before it.

Only the shape and the signature are looked at here (check 1). Whether the run is still running and the
employee still employed (checks 2 · 3) needs the engine and the DB: studio/api.py does that. No FastAPI and
no store in this file.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time

PREFIX = "sat1."
MODES = ("work", "read", "chat")  # §7.4: the task run, a read-only turn of a task room, a chat turn
KEYS = ("v", "run", "emp", "room", "task", "mode", "iat")
_secret = secrets.token_bytes(32)


def reset_secret() -> None:
    """A new server secret: what a restart does. Every token issued so far stops verifying."""
    global _secret
    _secret = secrets.token_bytes(32)


def _sign(claims: str) -> str:
    return hmac.new(_secret, claims.encode("ascii"), hashlib.sha256).hexdigest()


def issue(*, run: str, emp: str, room: str, mode: str, task: str | None = None, iat: int | None = None) -> str:
    """A token for one stage of one run: the employee, the room and (in a task room) the task of that run."""
    if mode not in MODES:
        raise ValueError(f"actor token mode must be one of {MODES}: {mode!r}")
    for name, value in (("run", run), ("emp", emp), ("room", room)):
        if not (isinstance(value, str) and value):
            raise ValueError(f"actor token needs {name}: {value!r}")
    if task is not None and not (isinstance(task, str) and task):
        raise ValueError(f"actor token task is an id or None: {task!r}")
    body = {"v": 1, "run": run, "emp": emp, "room": room, "task": task, "mode": mode,
            "iat": int(time.time()) if iat is None else int(iat)}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    claims = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{PREFIX}{claims}.{_sign(claims)}"


def verify(token: object) -> dict | None:
    """The claims of a token this server signed, or None — for any other text, never an exception.
    The signature is compared before the claims are decoded, with `hmac.compare_digest`."""
    if not isinstance(token, str) or not token.startswith(PREFIX):
        return None
    parts = token.split(".")
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    claims, signature = parts[1], parts[2]
    try:
        expected = _sign(claims)
    except ValueError:  # not ASCII: nothing this server wrote
        return None
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8")):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(body, dict) or type(body.get("v")) is not int or body["v"] != 1:
        return None
    if not all(isinstance(body.get(key), str) and body[key] for key in ("run", "emp", "room")):
        return None
    if body.get("task") is not None and not (isinstance(body["task"], str) and body["task"]):
        return None
    if not isinstance(body.get("mode"), str) or body["mode"] not in MODES:
        return None
    if type(body.get("iat")) is not int:
        return None
    return {key: body.get(key) for key in KEYS}
