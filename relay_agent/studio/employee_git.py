"""An employee's folder as a git repository of its own (H-3, part 1): what is written there is committed, so a
guide that went wrong can be looked up and put back.

Contract: design/studio/ARCHITECTURE.md §2.2 (`employees/<id>/.git`), §9.6 (the order of a write), §6.4 (the
`history` · `revert` paths), §5.5 (the commit of a `hire` · `guide_amend` approval).

Every call names the repository itself — `--git-dir=<folder>/.git --work-tree=<folder>` — so git never climbs
to a repository above the folder (the server's own: runs/ is ignored there), and the environment's `GIT_DIR` and
the like are taken out. Who commits is fixed (`workspace.IDENTITY`): the global git settings are not asked, and
neither are `GIT_AUTHOR_*` · `GIT_COMMITTER_*`. Nothing here needs a git newer than 2.37.

`lock` is held around a commit; the REST paths and the approval of a `guide_amend` (`Inbox._amend`) hold it
around their whole read-check-write-commit, so two writes of one file cannot both pass the `base_sha256` check.
It is re-entrant for that.

No FastAPI and no store here: api.py (the paths, the start) and runs.py (`EngineHooks.commit`) use it.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from ..workspace import IDENTITY

GIT_TIMEOUT_S = 30
HISTORY_LIMIT = 200  # the newest commits `history` gives
# What the global git settings must not bring into a commit here (§9.6): hooks (`--no-verify` skips pre-commit
# and commit-msg only), an attributes file, a conversion of line endings — a revert writes the commit's bytes.
# The global file itself stays in use: `safe.directory` is written there.
GLOBAL_OUT = ["-c", f"core.hooksPath={os.devnull}", "-c", f"core.attributesFile={os.devnull}",
              "-c", "core.autocrlf=false"]
SETTINGS = ["-c", "commit.gpgsign=false", "-c", "core.quotepath=false", "-c", "init.defaultBranch=main",
            "-c", "log.showRoot=true", *GLOBAL_OUT]
COMMITISH = re.compile(r"[0-9a-fA-F]{7,40}")  # a sha or its start: never a ref name, never an option
ENV_OUT = frozenset({  # what would point git elsewhere, or sign the commit as somebody else
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"})

lock = threading.RLock()


class GitError(Exception):
    """git could not be run in the folder, did not come back in time, or refused."""


def _git(folder: Path | str, *args: str, stdin: bytes | None = None,
         own: bool = True) -> subprocess.CompletedProcess:
    """git on that folder's own repository (`own=False`: no repository is named — `init`). It asks nothing: no
    terminal, no prompt, no window."""
    folder = Path(folder)
    where = [f"--git-dir={folder / '.git'}", f"--work-tree={folder}"] if own else []
    env = {key: value for key, value in os.environ.items() if key.upper() not in ENV_OUT}
    env["GIT_TERMINAL_PROMPT"] = "0"
    feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(["git", *IDENTITY, *SETTINGS, *where, *args], capture_output=True, cwd=str(folder),
                              timeout=GIT_TIMEOUT_S, env=env,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **feed)
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {args[0]} 이 {GIT_TIMEOUT_S}초 안에 끝나지 않았다 ({folder})") from e
    except OSError as e:  # no git on this PC, or no such folder
        raise GitError(f"git {args[0]} 을 돌리지 못했다 ({folder}): {e}") from e


def _must(folder: Path | str, *args: str, stdin: bytes | None = None, own: bool = True) -> str:
    done = _git(folder, *args, stdin=stdin, own=own)
    if done.returncode != 0:
        said = (done.stderr or done.stdout or b"").decode("utf-8", "replace").strip()
        raise GitError(f"git {args[0]} 실패 ({folder}): {said[-300:] or f'exit {done.returncode}'}")
    return done.stdout.decode("utf-8", "replace").strip()


def is_repo(folder: Path | str) -> bool:
    """The folder has a `.git` of its own. A repository above it does not count."""
    return (Path(folder) / ".git").exists()


def head(folder: Path | str) -> str | None:
    """The sha of the last commit; None while the folder is no repository or has no commit."""
    if not is_repo(folder):
        return None
    done = _git(folder, "rev-parse", "--verify", "-q", "HEAD")
    return done.stdout.decode("ascii", "replace").strip() or None if done.returncode == 0 else None


def commit(folder: Path | str, message: str) -> str | None:
    """One commit of everything in the folder as it is now, and its sha. A folder without `.git` is made a
    repository first (`git init`): its files are read, never written. None when nothing differs from the last
    commit (or the folder is empty). GitError when git cannot be run or refuses — the files stay as they are,
    and the next commit takes them along."""
    folder = Path(folder)
    with lock:
        if not is_repo(folder):
            _must(folder, "init", "-q", str(folder), own=False)
        _must(folder, "add", "-A", "--", ".")
        if not _must(folder, "status", "--porcelain"):
            return None
        _must(folder, "commit", "-q", "--no-verify", "-F", "-", stdin=message.encode("utf-8"))
        return _must(folder, "rev-parse", "HEAD")


def history(folder: Path | str, file: str | None = None, limit: int = HISTORY_LIMIT) -> list[dict]:
    """`[{commit, at, message, files[]}]`, the newest first, `limit` at most (§6.4). `at` is local time to the
    second, the format of `store.now()`; `files` are all the files that commit changed. With `file`, only the
    commits that changed that file. [] while there is no repository or no commit."""
    if head(folder) is None:
        return []
    args = ["log", f"-n{int(limit)}", "--full-diff", "--name-only", "--pretty=format:%x1e%H%x1f%ct%x1f%s"]
    if file:
        args += ["--", file]
    rows = []
    for record in _must(folder, *args).split("\x1e"):
        first, _, rest = record.partition("\n")
        parts = first.split("\x1f", 2)
        if len(parts) != 3:
            continue
        sha, stamp, subject = parts
        rows.append({"commit": sha.strip(),
                     "at": datetime.fromtimestamp(int(stamp)).isoformat(timespec="seconds"),
                     "message": subject.strip(),
                     "files": [line.strip() for line in rest.splitlines() if line.strip()]})
    return rows


def resolve(folder: Path | str, commit: str) -> str | None:
    """The full sha of a commit of this folder's history named by its sha or the start of one (7–40 hex digits);
    None for anything else — a ref name, an option, a sha that is not there."""
    name = commit.strip() if isinstance(commit, str) else ""
    if not COMMITISH.fullmatch(name) or not is_repo(folder):
        return None
    done = _git(folder, "rev-parse", "--verify", "-q", name + "^{commit}")
    return done.stdout.decode("ascii", "replace").strip() or None if done.returncode == 0 else None


def show(folder: Path | str, sha: str, name: str) -> bytes | None:
    """The bytes of that file in that commit; None when the commit does not have it."""
    done = _git(folder, "cat-file", "blob", f"{sha}:{name}")
    return done.stdout if done.returncode == 0 else None
