"""In-place work on projects of any size, without snapshots.

Big game projects (Unreal/Unity, SVN or git, tens of GB, assets the AI also touches) can't be copied or
snapshotted. Instead:

1. Journal:   at run start record only (size, mtime) of every project file — code AND assets. No contents.
              VCS metadata and pure build/cache folders are skipped.
2. Backups:   - a PreToolUse hook copies a file right before an AI edit tool changes it (backup_hook.py);
              - files that were already uncommitted at start (per the VCS) are copied then, capped.
3. Originals: anything else that changed (Bash, the Unreal editor via MCP, builds) is restored from the
              VCS's own pristine copy: git HEAD blob, or SVN BASE read straight from .svn (no svn CLI).
4. Collect:   rescan; changed = new / deleted / size or mtime differs. Text becomes result.patch;
              binaries (assets) become a list with before/after size and whether they can be restored.
5. Rollback:  put back every restorable change, delete files the run created, report the rest.

Disk cost = journal (a few MB for ~100k files) + copies of edited/uncommitted files only.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

# Never journaled: VCS metadata and folders that are pure build output / caches.
SKIP_DIRS = {
    ".git", ".svn", ".hg", ".p4", ".vs", ".idea", "__pycache__", ".venv", "venv", "node_modules",
    "Intermediate", "DerivedDataCache", "Saved", "Binaries",  # Unreal build/cache (Content IS tracked)
    "Library", "Temp", "Logs", "obj",                         # Unity/.NET caches
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "runs",
}
TEXT_LIMIT = 2 * 1_048_576           # larger files are treated as binary (listed, not diffed)
# Game assets are changed by editors/MCP (e.g. Unreal MCP saving .uasset): never copied or cached here.
# They are only listed; rollback uses the VCS's own original when there is one (not for git LFS pointers).
ASSET_EXTS = {
    ".uasset", ".umap", ".uexp", ".ubulk", ".upk", ".pak", ".ucas", ".utoc",
    ".fbx", ".obj", ".blend", ".psd", ".tga", ".png", ".jpg", ".jpeg", ".dds", ".exr", ".hdr", ".tif", ".tiff",
    ".wav", ".ogg", ".mp3", ".bnk", ".wem", ".mp4", ".mov", ".unity", ".prefab", ".asset", ".mat", ".anim",
}
# Top-level folders not scanned at all (an engine source tree is huge): only files AI edit tools touch
# are backed up there, by the pre-edit hook.
HOOK_ONLY_DIRS = ["Engine"]
LFS_POINTER = b"version https://git-lfs.github.com/spec"
START_COPY_FILE_MB = 256             # uncommitted-at-start file bigger than this is not copied
START_COPY_TOTAL_MB = 2048           # total budget for uncommitted-at-start copies


from .workspace import WorkspaceError  # noqa: E402


class JournalError(WorkspaceError):
    pass


def is_asset(rel: str) -> bool:
    return os.path.splitext(rel)[1].lower() in ASSET_EXTS


def is_text(data: bytes) -> bool:
    return len(data) <= TEXT_LIMIT and b"\0" not in data[:8192]


# --------------------------------------------------------------------------- VCS adapters
class NoVcs:
    name = "none"

    def dirty(self, rels: set[str]) -> set[str]:
        return set()  # unknown: nothing can be assumed clean

    def pristine(self, rel: str) -> bytes | None:
        return None

    def knows(self, rel: str) -> bool:
        return False


class GitVcs:
    name = "git"

    def __init__(self, root: Path, prefix: str):
        self.root, self.prefix = root, prefix
        self._tracked: set[str] | None = None

    def _git(self, *args: str) -> bytes:
        proc = subprocess.run(["git", "--no-optional-locks", "-c", "core.quotepath=false", "-C", str(self.root), *args],
                              capture_output=True)  # --no-optional-locks: status must not rewrite the index
        if proc.returncode != 0:
            raise JournalError(proc.stderr.decode("utf-8", "replace")[-300:])
        return proc.stdout

    def _full(self, rel: str) -> str:
        return f"{self.prefix}{rel}"

    def tracked(self) -> set[str]:
        if self._tracked is None:
            out = self._git("ls-files", "-z", "--", self.prefix or ".").decode("utf-8", "replace")
            self._tracked = {p[len(self.prefix):] for p in out.split("\0") if p}
        return self._tracked

    def dirty(self, rels: set[str]) -> set[str]:
        out = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", self.prefix or ".")
        found = set()
        entries = out.decode("utf-8", "replace").split("\0")
        skip_next = False
        for entry in entries:
            if skip_next:
                skip_next = False
                continue
            if len(entry) < 4:
                continue
            code, path = entry[:2], entry[3:]
            if code[0] in "RC":
                skip_next = True
            if path.startswith(self.prefix):
                found.add(path[len(self.prefix):])
        return found

    def knows(self, rel: str) -> bool:
        return rel in self.tracked()

    def pristine(self, rel: str) -> bytes | None:
        if not self.knows(rel):
            return None
        proc = subprocess.run(["git", "-C", str(self.root), "cat-file", "blob", f"HEAD:{self._full(rel)}"], capture_output=True)
        return proc.stdout if proc.returncode == 0 else None


class SvnVcs:
    """Reads the working copy database directly (works with TortoiseSVN, no svn CLI required)."""

    name = "svn"

    def __init__(self, wc_root: Path, prefix: str):
        self.wc_root, self.prefix = wc_root, prefix
        db = wc_root / ".svn" / "wc.db"
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """SELECT n.local_relpath, n.checksum, n.translated_size, n.last_mod_time FROM nodes n
                   WHERE n.kind = 'file' AND n.presence = 'normal' AND n.op_depth = (
                     SELECT MAX(m.op_depth) FROM nodes m WHERE m.wc_id = n.wc_id AND m.local_relpath = n.local_relpath)"""
            ).fetchall()
            self.compression = {c: comp for c, comp in conn.execute("SELECT checksum, compression FROM pristine")}
        finally:
            conn.close()
        self.info = {}
        for relpath, checksum, size, mtime_us in rows:
            if relpath.startswith(prefix) and checksum:
                self.info[relpath[len(prefix):]] = (checksum, size, mtime_us)

    def knows(self, rel: str) -> bool:
        return rel in self.info

    def _pristine_path(self, checksum: str) -> Path | None:
        if not checksum.startswith("$sha1$") or self.compression.get(checksum):
            return None  # compressed pristines are not supported
        digest = checksum[len("$sha1$"):]
        path = self.wc_root / ".svn" / "pristine" / digest[:2] / f"{digest}.svn-base"
        return path if path.is_file() else None

    def pristine(self, rel: str) -> bytes | None:
        if rel not in self.info:
            return None
        path = self._pristine_path(self.info[rel][0])
        return path.read_bytes() if path else None

    def dirty(self, rels: set[str]) -> set[str]:
        """Unversioned files, plus versioned files whose size/mtime differ from wc.db AND whose sha1
        differs from the pristine (the same quick check svn itself uses)."""
        root = self.wc_root / self.prefix if self.prefix else self.wc_root
        found = set()
        for rel in rels:
            if rel not in self.info:
                found.add(rel)
                continue
            checksum, size, mtime_us = self.info[rel]
            try:
                st = (root / rel).stat()
            except OSError:
                found.add(rel)
                continue
            if size == st.st_size and mtime_us and abs(mtime_us - st.st_mtime_ns // 1000) < 1_000_000:
                continue
            h = hashlib.sha1()
            with (root / rel).open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if f"$sha1${h.hexdigest()}" != checksum:
                found.add(rel)
        missing = {rel for rel in self.info if not (root / rel).exists()}
        return found | missing


def detect_vcs(source: Path):
    """git (with a commit) > svn working copy > none. Returns an adapter bound to `source`."""
    top = subprocess.run(["git", "-C", str(source), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    head = subprocess.run(["git", "-C", str(source), "rev-parse", "--verify", "-q", "HEAD"], capture_output=True)
    if top.returncode == 0 and head.returncode == 0:
        root = Path(top.stdout.strip())
        prefix = os.path.relpath(source, root).replace("\\", "/")
        return GitVcs(root, "" if prefix == "." else prefix + "/")
    here = source.resolve()
    for folder in (here, *here.parents):
        if (folder / ".svn" / "wc.db").is_file():
            prefix = os.path.relpath(here, folder).replace("\\", "/")
            try:
                return SvnVcs(folder, "" if prefix == "." else prefix + "/")
            except sqlite3.Error:
                break
    return NoVcs()


# --------------------------------------------------------------------------- workspace
def scan(root: Path, skip_dirs: set[str], hook_only: set[str] = frozenset()) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            top_level = folder == root
                            if entry.name not in skip_dirs and not (top_level and entry.name in hook_only):
                                stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            out[Path(entry.path).relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
                    except OSError:
                        continue
        except OSError:
            continue
    return out


class JournalWorkspace:
    mode = "inplace"

    def __init__(self, run_dir: Path, source: Path, extra_skip: list[str] | None = None, vcs=None,
                 hook_only: list[str] | None = None):
        self.run_dir = run_dir
        self.source = source
        self.skip = SKIP_DIRS | {e for e in (extra_skip or []) if "*" not in e and "/" not in e}
        self.hook_only = set(HOOK_ONLY_DIRS if hook_only is None else hook_only)
        self.journal_path = run_dir / "journal.json.gz"
        self.manifest = run_dir / "journal.meta.json"
        self.baseline_dir = run_dir / "baseline"
        self.touched = run_dir / "touched.jsonl"
        self.patch_path = run_dir / "result.patch"
        self.settings_path = run_dir / "hook-settings.json"
        self._vcs = vcs

    # the interface the engine uses ------------------------------------------------
    @property
    def path(self) -> Path:
        return self.source

    @property
    def prepared(self) -> bool:
        return self.manifest.exists() and self.journal_path.exists()

    @property
    def vcs(self):
        if self._vcs is None:
            self._vcs = detect_vcs(self.source)
        return self._vcs

    def hook_settings(self) -> Path:
        """Claude Code --settings file installing the pre-edit backup hook for this run."""
        hook = Path(__file__).with_name("backup_hook.py")
        command = f'"{sys.executable}" "{hook}" "{self.run_dir}" "{self.source}"'
        self.settings_path.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": command}]}
        ]}}), encoding="utf-8")
        return self.settings_path

    def prepare(self) -> dict:
        if self.prepared:
            return {"mode": "inplace", "tracking": "journal", "vcs": self.vcs.name}
        self.run_dir.mkdir(parents=True, exist_ok=True)
        files = scan(self.source, self.skip, self.hook_only)
        with gzip.open(self.journal_path, "wt", encoding="utf-8") as f:
            json.dump(files, f)
        vcs = self.vcs
        dirty = vcs.dirty(set(files)) if vcs.name != "none" else set()
        dirty = {rel for rel in dirty if rel.split("/", 1)[0] not in self.hook_only}
        copied, skipped, total = 0, [], 0
        assets_not_copied = 0
        for rel in sorted(dirty):
            if rel not in files:
                continue  # deleted at start: nothing to copy
            if is_asset(rel):
                skipped.append(rel)  # assets are never cached (only listed); rollback can't restore this one
                assets_not_copied += 1
                continue
            size = files[rel][0]
            if size > START_COPY_FILE_MB * 1_048_576 or total + size > START_COPY_TOTAL_MB * 1_048_576:
                skipped.append(rel)
                continue
            dest = self.baseline_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(self.source / rel, dest)
            except OSError:
                skipped.append(rel)
                continue
            copied += 1
            total += size
        self.manifest.write_text(json.dumps({
            "vcs": vcs.name, "dirty_at_start": sorted(dirty), "not_backed_up": skipped,
        }, ensure_ascii=False), encoding="utf-8")
        self.hook_settings()
        return {"mode": "inplace", "tracking": "journal", "vcs": vcs.name, "files_tracked": len(files),
                "uncommitted_at_start": len(dirty), "backup_mb": round(total / 1_048_576, 2),
                "uncommitted_assets_not_cached": assets_not_copied, "hook_only": sorted(self.hook_only),
                "not_backed_up": skipped[:10]}

    def _load(self) -> tuple[dict, dict, dict[str, bool]]:
        with gzip.open(self.journal_path, "rt", encoding="utf-8") as f:
            journal = {k: tuple(v) for k, v in json.load(f).items()}
        meta = json.loads(self.manifest.read_text(encoding="utf-8"))
        touched: dict[str, bool] = {}
        if self.touched.exists():
            for line in self.touched.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    touched.setdefault(entry["rel"], entry["existed"])
        return journal, meta, touched

    def _before(self, rel: str, journal: dict, meta: dict, touched: dict[str, bool]) -> tuple[str, bytes | None]:
        """(source, bytes). source: backup | absent | pristine | unknown."""
        backup = self.baseline_dir / rel
        if rel in touched and not touched[rel]:
            return "absent", None
        if backup.is_file():
            return "backup", backup.read_bytes()
        if rel not in journal:
            return "absent", None  # created during the run
        if rel in meta["dirty_at_start"] or rel in meta["not_backed_up"]:
            return "unknown", None
        data = self.vcs.pristine(rel)
        if data is not None and data.startswith(LFS_POINTER):
            return "unknown", None  # the committed blob is only an LFS pointer, restoring it would break the asset
        return ("pristine", data) if data is not None else ("unknown", None)

    def _changed(self) -> tuple[list[str], dict, dict, dict]:
        journal, meta, touched = self._load()
        now = scan(self.source, self.skip, self.hook_only)
        changed = {rel for rel, stat in now.items() if journal.get(rel) != stat}
        changed |= {rel for rel in journal if rel not in now}
        changed |= set(touched)
        return sorted(changed), journal, meta, touched

    def collect(self) -> dict:
        if not self.prepared:
            return {"files": 0, "insertions": 0, "deletions": 0, "stat": ""}
        rels, journal, meta, touched = self._changed()
        patches, stat_lines, binaries, unknown = [], [], [], []
        ins_total = del_total = 0
        with tempfile.TemporaryDirectory(prefix="katae-journal-") as tmp:
            for rel in rels:
                full = self.source / rel
                after = full.read_bytes() if full.is_file() and full.stat().st_size <= TEXT_LIMIT else None
                after_exists = full.is_file()
                source, before = self._before(rel, journal, meta, touched)
                if source == "unknown":
                    unknown.append(rel)
                if source != "unknown" and before is not None and after_exists and len(before) == full.stat().st_size:
                    if (after if after is not None else full.read_bytes()) == before:
                        continue  # touched but identical (e.g. an asset re-saved without changes)
                if source == "absent" and not after_exists:
                    continue  # created and removed again
                text = (before is None or is_text(before)) and (not after_exists or (after is not None and is_text(after)))
                if not text or source == "unknown":
                    binaries.append({
                        "path": rel, "asset": is_asset(rel), "before_bytes": len(before) if before is not None else None,
                        "after_bytes": full.stat().st_size if after_exists else None,
                        "restorable": source != "unknown", "source": source,
                    })
                    stat_lines.append(f" {rel} | {'바이너리' if text is False else '원본 없음'}")
                    continue
                a, b = Path(tmp) / "a" / rel, Path(tmp) / "b" / rel
                for p, data in ((a, before), (b, after if after_exists else None)):
                    if data is not None:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(data)
                left = f"a/{rel}" if before is not None else "/dev/null"
                right = f"b/{rel}" if after_exists else "/dev/null"
                base = ["git", "-c", "core.autocrlf=false", "diff", "--no-index", "--no-color", "--src-prefix=", "--dst-prefix="]
                diff = subprocess.run([*base, "--binary", left, right], capture_output=True, cwd=tmp).stdout
                lines = diff.split(b"\n")
                if lines and lines[0].startswith(b"diff --git"):
                    lines[0] = f"diff --git a/{rel} b/{rel}".encode()
                patches.append(b"\n".join(lines))
                num = subprocess.run([*base, "--numstat", left, right], capture_output=True, cwd=tmp).stdout.decode()
                parts = num.split("\t")
                ins = int(parts[0]) if parts and parts[0].isdigit() else 0
                dels = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                ins_total, del_total = ins_total + ins, del_total + dels
                stat_lines.append(f" {rel} | {ins + dels} {'+' * min(ins, 20)}{'-' * min(dels, 20)}")
        self.patch_path.write_bytes(b"".join(patches))
        return {"files": len(stat_lines), "insertions": ins_total, "deletions": del_total,
                "stat": "\n".join(stat_lines), "binary": binaries, "unrestorable": unknown, "vcs": meta["vcs"]}

    def rollback(self) -> dict:
        if not self.prepared:
            raise JournalError("기준 기록이 정리되어 되돌릴 수 없습니다")
        summary = self.collect()
        rels, journal, meta, touched = self._changed()
        restored, removed, failed = [], [], []
        for rel in rels:
            source, before = self._before(rel, journal, meta, touched)
            full = self.source / rel
            try:
                if source == "absent":
                    if full.exists():
                        full.unlink()
                        removed.append(rel)
                elif before is not None:
                    full.parent.mkdir(parents=True, exist_ok=True)
                    full.write_bytes(before)
                    restored.append(rel)
                else:
                    failed.append(rel)
            except OSError:
                failed.append(rel)  # e.g. the Unreal editor still has the asset open
        summary.update(restored=len(restored), removed=len(removed), not_restored=failed)
        self.cleanup()
        return summary

    def apply(self) -> str:
        raise JournalError("apply is for copy mode; inplace changes are already in the folder")

    def discard(self) -> None:
        raise JournalError("discard is for copy mode; use rollback for inplace changes")

    def cleanup(self) -> int:
        freed = 0
        for p in (self.baseline_dir, self.journal_path, self.manifest, self.touched, self.settings_path):
            if p.is_dir():
                freed += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                freed += p.stat().st_size
                p.unlink()
        return freed
