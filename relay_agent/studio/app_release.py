"""The app update the phone is offered (A-13 — ARCHITECTURE.md §6.4 `GET /studio/app/latest`, §12.4 #48).

The owner builds the APK by hand and puts it in one folder (`Config.studio_app_folder`, `~/.katae/app` by default)
with a small `latest.json` beside it:

    {"version_name": "1.4.0", "build_number": 14, "notes": "what changed, one line",
     "apks": {"arm64-v8a": "app-arm64-v8a-release.apk", "armeabi-v7a": "...", "universal": "..."}}

The server only reads that folder: it builds nothing, uploads nothing, creates nothing. Nothing here raises for
a missing folder or file — "there is no update" is the normal answer (`available: false`)."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

INFO_NAME = "latest.json"
DEFAULT_ABI = "arm64-v8a"   # the abi taken when the phone does not say which it runs
FALLBACK_ABI = "universal"  # the key of a fat APK, used when `apks` has none for the abi asked
APK_MIME = "application/vnd.android.package-archive"
FILE_URL = "/studio/app/files/"
CHUNK = 1024 * 1024

_digests: dict[str, tuple[int, int, str]] = {}  # str(resolved path) -> (st_mtime_ns, st_size, sha256 hex)
_lock = threading.Lock()


def safe_file(folder: Path, name: Any) -> Path | None:
    """The APK `name` of `folder`, or None. Only a plain file name of the folder itself ends in `.apk`: no path
    parts (`..`, absolute, drive), no link (a symlink is refused even when it points inside)."""
    if not isinstance(name, str) or not name or name in (".", "..") or name != Path(name).name:
        return None
    if not name.lower().endswith(".apk"):
        return None
    try:
        path = folder / name
        if path.is_symlink() or path.resolve().parent != folder.resolve() or not path.is_file():
            return None
        return path
    except (OSError, ValueError):
        return None


def _digest(path: Path) -> str:
    """sha256 of the file, read in 1 MB pieces (kept apart so that a test can count the calculations)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def sha256_of(path: Path) -> str:
    """The file's sha256, calculated again only when its mtime or size changed since the last time."""
    st = path.stat()
    key = str(path.resolve())
    stamp = (st.st_mtime_ns, st.st_size)
    with _lock:
        hit = _digests.get(key)
        if hit and hit[:2] == stamp:
            return hit[2]
    digest = _digest(path)
    with _lock:
        _digests[key] = (*stamp, digest)
    return digest


def _none(reason: str) -> dict:
    return {"available": False, "reason": reason}


def _first_line(notes: Any) -> str:
    lines = notes.strip().splitlines() if isinstance(notes, str) else []
    return lines[0].strip() if lines else ""


def latest(folder: Path, abi: str = DEFAULT_ABI) -> dict:
    """The update on offer for a phone that runs `abi`, or `{available: false, reason}`."""
    try:
        if not folder.is_dir():
            return _none("no_folder")
        info_path = folder / INFO_NAME
        if not info_path.is_file():
            return _none("no_info")
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return _none("bad_info")
    if not isinstance(info, dict):
        return _none("bad_info")
    version, build, apks = info.get("version_name"), info.get("build_number"), info.get("apks")
    if not isinstance(version, str) or not version.strip() or type(build) is not int or not isinstance(apks, dict):
        return _none("bad_info")
    for key in (abi, FALLBACK_ABI):
        path = safe_file(folder, apks.get(key))
        if path is not None:
            break
    else:
        return _none("no_apk")
    try:
        st = path.stat()
        digest = sha256_of(path)
    except OSError:
        return _none("no_apk")
    return {
        "available": True,
        "version_name": version.strip(),
        "build_number": build,
        "size": st.st_size,
        "sha256": digest,
        "uploaded_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "notes": _first_line(info.get("notes")),
        "abi": key,
        "file": path.name,
        "url": FILE_URL + path.name,
    }
