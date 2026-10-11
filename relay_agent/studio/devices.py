"""Device pairing (M-2, ARCHITECTURE.md §6.5 · §3.14): the one-time codes of this process, the device tokens,
and the address a phone is told to reach this server at. No FastAPI and no store here — the four routes are
api.py's (`/studio/devices…`), and what they write goes through StudioStore."""
from __future__ import annotations

import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlparse

PAIRING_TTL = timedelta(minutes=5)  # §3.14: how long a pairing code can be exchanged
TOKEN_PREFIX = "sdt_"               # what a device token starts with; api.identify tells it by this
DEFAULT_PORT = 8020                 # when server_url names none (cli.py `remote_command` says the same)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Pairings:
    """The pairing codes that can still be exchanged: SHA-256 of a code -> when it expires.

    Memory only (§3.14): a restarted server starts with an empty one, so every code left is invalid. The code
    itself is not kept. `clock` is what "now" is read from; a test puts its own there."""

    def __init__(self, clock: Callable[[], datetime] = datetime.now, ttl: timedelta = PAIRING_TTL) -> None:
        self.clock, self.ttl = clock, ttl
        self._lock = threading.Lock()
        self._codes: dict[str, datetime] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._codes)

    def issue(self) -> tuple[str, str]:
        """A new code and its `expires_at` (local time to the second, like store.now()). The codes that have
        expired are dropped here, so the book does not grow with codes nobody came for."""
        code = secrets.token_urlsafe(16)
        with self._lock:
            now = self.clock()
            for key in [key for key, end in self._codes.items() if end <= now]:
                del self._codes[key]
            expires = now + self.ttl
            self._codes[_digest(code)] = expires
        return code, expires.isoformat(timespec="seconds")

    def redeem(self, code: str) -> bool:
        """Use a code up: True once, for a code that was issued here and has not expired. A wrong code
        touches no other code; an expired one is gone after the try."""
        with self._lock:
            expires = self._codes.pop(_digest(code), None)
            return expires is not None and self.clock() < expires


def new_token() -> tuple[str, str]:
    """A device token and its SHA-256 hex. The hash is what devices.token_hash keeps; the text is handed out
    once, in the answer of the exchange."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, _digest(token)


def public(row: dict) -> dict:
    """A device row as the API gives it: without `token_hash` (§6.4)."""
    return {key: value for key, value in row.items() if key != "token_hash"}


def tailscale_info() -> dict:
    """cli.py `tailscale_info`, imported when asked: cli imports the engine, and a test replaces this."""
    from .. import cli

    return cli.tailscale_info()


def lan_urls(port: int) -> list[str]:
    """cli.py `lan_urls`, imported when asked (see tailscale_info)."""
    from .. import cli

    return cli.lan_urls(port)


def reach_url(cfg: Any, base: str) -> str:
    """`qr.url` of §6.5: the address a phone reaches this server at.

    The config holds no such address — `relay remote on --tailscale` writes serve_host, auth_token and
    remote_networks only — so it is worked out the way `relay remote status` lists the addresses:
    1. serve_host is loopback (remote access is off): `base`, where the request came. Nothing is asked.
    2. Tailscale is running: its MagicDNS name, or its first IPv4 when there is no name.
    3. remote_networks is not the tailnet only: the first LAN IPv4 of this machine (not a 100.x one).
    4. otherwise `base`.
    The port is server_url's. No cache: step 2 runs `tailscale status --json` once per call (10 s at most),
    and a code is asked for rarely."""
    from ..cli import LOOPBACK_HOSTS, TAILNET

    if cfg.serve_host in LOOPBACK_HOSTS:
        return base
    port = urlparse(cfg.server_url).port or DEFAULT_PORT
    ts = tailscale_info()
    if ts.get("running"):
        host = ts.get("dns") or next(iter(ts.get("ips") or []), "")
        if host:
            return f"http://{host}:{port}"
    networks = cfg.remote_networks
    only_tailnet = bool(networks) and set(networks) <= set(TAILNET)
    if not only_tailnet:
        for url in lan_urls(port):
            if not url.startswith("http://100."):
                return url
    return base
