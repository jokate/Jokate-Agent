"""Workspaces: snapshot the target folder before a run, then return the work as a patch.

Disk-conscious design:
- ONE shadow git object store per target folder (runs/shadow/<hash>.git), shared by all runs on it.
  Each run is a ref (refs/katae/<run id>) plus its own index file, so repeated runs only add the
  files that actually changed instead of another full copy.
- The target's .gitignore is respected by default (build output, caches, Unity Library, ...);
  a repo can opt in with include_ignored.
- A size guard runs before anything is written: over max_snapshot_mb the run stops and names the
  biggest folders; single files over max_file_mb are skipped (and reported).
- copy:    stages work in runs/<id>/workspace (a checkout of the snapshot). Result: result.patch,
           applied to the original folder or discarded.
- inplace: stages edit the original folder. Result: the same patch, plus rollback to the snapshot.
The target repo's own git is never touched; uncommitted work is included.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

# Heavy or generated folders never snapshotted (any depth), plus file globs.
DEFAULT_EXCLUDES = [
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".cache", ".gradle", ".idea", ".vs", ".vscode-test", "runs",
    "dist", "build", "out", "target", "obj", "bin", ".next", ".nuxt", ".turbo", ".parcel-cache", "coverage",
    "Binaries", "Intermediate", "Saved", "DerivedDataCache", "Content",          # Unreal
    "Library", "Temp", "Logs", "UserSettings",                                    # Unity
    "*.uasset", "*.umap", "*.pdb", "*.dll", "*.exe", "*.so", "*.dylib", "*.lib", "*.a", "*.obj", "*.o",
    "*.zip", "*.7z", "*.rar", "*.tar", "*.gz", "*.pak", "*.fbx", "*.psd", "*.mp4", "*.mov", "*.wav",
    "*.log", "*.sqlite", "*.db",
]
IDENTITY = ["-c", "user.name=agent-katae", "-c", "user.email=agent-katae@localhost", "-c", "core.autocrlf=false",
            "-c", "core.safecrlf=false", "-c", "core.longpaths=true", "-c", "gc.auto=0"]
MAX_SNAPSHOT_MB = 500
MAX_FILE_MB = 20


class WorkspaceError(RuntimeError):
    pass


def shadow_dir_for(shadow_root: Path, source: Path) -> Path:
    key = hashlib.sha1(os.path.normcase(os.path.abspath(source)).encode()).hexdigest()[:16]
    return shadow_root / f"{key}.git"


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class Workspace:
    def __init__(self, run_dir: Path, source: Path, mode: str, excludes: list[str] | None = None,
                 shadow_root: Path | None = None, include_ignored: bool = False,
                 max_snapshot_mb: float = MAX_SNAPSHOT_MB, max_file_mb: float = MAX_FILE_MB):
        if mode not in ("copy", "inplace"):
            raise WorkspaceError(f"unknown workspace mode: {mode}")
        self.run_dir = run_dir
        self.source = source
        self.mode = mode
        self.excludes = DEFAULT_EXCLUDES if excludes is None else excludes
        self.shadow_root = shadow_root or run_dir.parent / "shadow"
        self.git_dir = shadow_dir_for(self.shadow_root, source)
        self.ref = f"refs/katae/{run_dir.name}"
        self.index_file = run_dir / "snapshot.index"
        self.copy_dir = run_dir / "workspace"
        self.patch_path = run_dir / "result.patch"
        self.marker = run_dir / "snapshot.ready"
        self.include_ignored = include_ignored
        self.max_snapshot_mb = max_snapshot_mb
        self.max_file_mb = max_file_mb

    # --- git plumbing ------------------------------------------------------------
    @property
    def path(self) -> Path:
        """Where stages run."""
        return self.copy_dir if self.mode == "copy" else self.source

    def _run(self, work_tree: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
        env = {**os.environ, "GIT_INDEX_FILE": str(self.index_file)}  # per-run index on a shared store
        proc = subprocess.run(
            ["git", *IDENTITY, f"--git-dir={self.git_dir}", f"--work-tree={work_tree}", *args],
            capture_output=True, cwd=work_tree, env=env, input=input_bytes,
        )
        if proc.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args[:2])} failed: {proc.stderr.decode('utf-8', 'replace')[-400:]}")
        return proc.stdout

    def _git(self, work_tree: Path, *args: str) -> str:
        return self._run(work_tree, *args).decode("utf-8", "replace")

    def _excluded(self, rel: str) -> bool:
        parts = rel.split("/")
        for pattern in self.excludes:
            if "*" in pattern:
                if fnmatch.fnmatch(parts[-1], pattern):
                    return True
            elif pattern in parts[:-1] or pattern == rel:
                return True
        return False

    def _candidates(self, work_tree: Path, extra: set[str] = frozenset()) -> tuple[list[str], list[tuple[str, int]], int]:
        """Files to snapshot: tracked-in-index + untracked (respecting .gitignore unless include_ignored),
        minus excludes and oversized files. Returns (paths, skipped_big, total_bytes)."""
        args = ["ls-files", "-z", "--cached", "--others"]
        if not self.include_ignored:
            args.append("--exclude-standard")
        dir_excludes = [e for e in self.excludes if "*" not in e]
        args += ["--", "."] + [f":(exclude,glob)**/{e}/**" for e in dir_excludes]
        listed = {p for p in self._git(work_tree, *args).split("\0") if p} | set(extra)
        paths, skipped, total = [], [], 0
        limit = self.max_file_mb * 1_048_576
        for rel in sorted(listed):
            if self._excluded(rel):
                continue
            full = work_tree / rel
            size = full.stat().st_size if full.is_file() else 0
            if size > limit:
                skipped.append((rel, size))
                continue
            total += size
            paths.append(rel)
        return paths, skipped, total

    def _stage(self, work_tree: Path, paths: list[str]) -> None:
        if not paths:
            return
        # -A with an explicit list also records deletions of listed paths that no longer exist
        self._run(work_tree, "add", "-A", "-f", "--pathspec-from-file=-", "--pathspec-file-nul",
                  input_bytes="\0".join(paths).encode("utf-8"))

    # --- lifecycle -----------------------------------------------------------------
    @property
    def prepared(self) -> bool:
        # Only a finished snapshot counts; an interrupted prepare() leaves no marker and is redone.
        return self.marker.exists() and self.git_dir.exists()

    @property
    def snapshot(self) -> str:
        return self.marker.read_text(encoding="utf-8").strip()

    def nested_repos(self) -> list[str]:
        """Sub-folders with their own .git: git records them as a bare pointer, not their files."""
        skip = {e for e in self.excludes if "*" not in e}
        found = []
        for root, dirs, _files in os.walk(self.source):
            rel = Path(root).relative_to(self.source)
            if rel != Path(".") and ".git" in dirs:
                found.append(str(rel).replace("\\", "/"))
            dirs[:] = [d for d in dirs if d not in skip]
            if len(found) >= 20:
                break
        return found

    def prepare(self) -> dict:
        """Snapshot the source folder (size-checked); for copy mode, check it out as the workspace."""
        if self.prepared:
            return {"mode": self.mode, "path": str(self.path)}
        if shutil.which("git") is None:
            raise WorkspaceError("git is required for workspaces")
        for leftover in (self.copy_dir, self.index_file, self.marker):
            _remove(leftover)
        nested = self.nested_repos()
        if nested:
            raise WorkspaceError(
                "하위 폴더에 별도 git 저장소가 있어 스냅샷·되돌리기를 보장할 수 없습니다: "
                + ", ".join(nested[:5]) + " — 저장소 excludes 에 추가하거나 workspace: none 으로 실행하세요"
            )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.shadow_root.mkdir(parents=True, exist_ok=True)
        # idempotent: creates the shared store, or repairs a folder left empty by an interrupted first run
        subprocess.run(["git", "init", "--bare", "-q", str(self.git_dir)], check=True, capture_output=True)

        paths, skipped, total = self._candidates(self.source)
        if total > self.max_snapshot_mb * 1_048_576:
            raise WorkspaceError(self._too_big_message(paths, total))
        self._stage(self.source, paths)
        tree = self._git(self.source, "write-tree").strip()
        commit = self._git(self.source, "commit-tree", tree, "-m", f"snapshot {self.run_dir.name}").strip()
        self._git(self.source, "update-ref", self.ref, commit)
        if self.mode == "copy":
            self.copy_dir.mkdir(exist_ok=True)
            self._git(self.copy_dir, "checkout-index", "-a", "-f")
        self.marker.write_text(commit, encoding="utf-8")
        return {"mode": self.mode, "path": str(self.path), "files": len(paths), "snapshot_mb": round(total / 1_048_576, 1),
                "skipped_large": [f"{p} ({s / 1_048_576:.0f}MB)" for p, s in skipped[:10]]}

    def _too_big_message(self, paths: list[str], total: int) -> str:
        by_dir: dict[str, int] = {}
        for rel in paths:
            top = rel.split("/")[0] if "/" in rel else "(루트 파일)"
            try:
                by_dir[top] = by_dir.get(top, 0) + (self.source / rel).stat().st_size
            except OSError:
                pass
        biggest = sorted(by_dir.items(), key=lambda kv: -kv[1])[:5]
        listing = ", ".join(f"{d} {s / 1_048_576:.0f}MB" for d, s in biggest)
        return (f"스냅샷 대상이 {total / 1_048_576:.0f}MB 로 한도 {self.max_snapshot_mb:.0f}MB 를 넘습니다. 큰 폴더: {listing} — "
                "저장소 excludes 에 추가하거나(relay.config), max_snapshot_mb 를 올리거나, workspace: none 으로 실행하세요")

    def collect(self) -> dict:
        """Write result.patch (changes since the snapshot) and return a summary."""
        if not self.prepared:
            return {"files": 0, "insertions": 0, "deletions": 0, "stat": ""}
        if not self.path.is_dir():
            raise WorkspaceError(f"workspace folder is gone: {self.path}")
        base = self.snapshot
        in_snapshot = {p for p in self._git(self.path, "ls-tree", "-r", "-z", "--name-only", base).split("\0") if p}
        paths, _skipped, _total = self._candidates(self.path, extra=in_snapshot)
        self._stage(self.path, paths)
        self.patch_path.write_bytes(self._run(self.path, "diff", "--cached", "--binary", base))
        files, ins, dels = 0, 0, 0
        for line in self._git(self.path, "diff", "--cached", "--numstat", base).splitlines():
            a, d, _ = line.split("\t", 2)
            files += 1
            ins += int(a) if a.isdigit() else 0
            dels += int(d) if d.isdigit() else 0
        stat_text = self._git(self.path, "diff", "--cached", "--stat=100", base)
        return {"files": files, "insertions": ins, "deletions": dels, "stat": stat_text.strip()}

    def apply(self) -> str:
        """copy mode: apply result.patch to the original folder (checked first, nothing half-applied)."""
        if self.mode != "copy":
            raise WorkspaceError("apply is for copy mode; inplace changes are already in the folder")
        if self.prepared and self.copy_dir.is_dir():
            self.collect()
        if not self.patch_path.exists() or not self.patch_path.read_bytes().strip():
            raise WorkspaceError("적용할 패치가 없습니다")
        with tempfile.TemporaryDirectory(prefix="katae-apply-") as tmp:
            # a throwaway repo so patch paths are relative to the target folder, whatever git it lives in
            git_dir = Path(tmp) / "g.git"
            subprocess.run(["git", "init", "--bare", "-q", str(git_dir)], check=True, capture_output=True)
            base = ["git", *IDENTITY, f"--git-dir={git_dir}", f"--work-tree={self.source}", "apply", "--whitespace=nowarn"]
            check = subprocess.run([*base, "--check", str(self.patch_path)], cwd=self.source, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace")
            if check.returncode != 0:
                raise WorkspaceError("원본 폴더가 스냅샷 이후 바뀌어 패치가 그대로 적용되지 않습니다: "
                                     + check.stderr.strip()[-300:])
            subprocess.run([*base, str(self.patch_path)], cwd=self.source, check=True, capture_output=True)
        self.cleanup()
        return str(self.patch_path)

    def discard(self) -> None:
        """copy mode: drop the workspace; the patch file is kept for reference."""
        if self.mode != "copy":
            raise WorkspaceError("discard is for copy mode; use rollback for inplace changes")
        if self.prepared and self.copy_dir.is_dir():
            self.collect()
        self.cleanup()

    def rollback(self) -> dict:
        """inplace mode: restore every snapshotted file and delete files the relay created."""
        if self.mode != "inplace":
            raise WorkspaceError("rollback is for inplace mode; use discard for copy mode")
        if not self.prepared:
            raise WorkspaceError("스냅샷이 정리되어 되돌릴 수 없습니다")
        summary = self.collect()
        # index now mirrors the folder; --reset -u makes the folder match the snapshot, removing added files
        self._git(self.source, "read-tree", "--reset", "-u", self.snapshot)
        self.cleanup()
        return summary

    def cleanup(self) -> int:
        """Drop this run's copy, index and ref; keep result.patch. Returns bytes freed from the run folder.
        Shared objects are reclaimed later by gc_shadow(). After this an inplace run can't be rolled back."""
        freed = 0
        if self.copy_dir.exists():
            freed += dir_size(self.copy_dir)
        for p in (self.index_file,):
            if p.exists():
                freed += p.stat().st_size
        if self.git_dir.exists() and self.marker.exists():
            try:
                self._git(self.run_dir, "update-ref", "-d", self.ref)
            except WorkspaceError:
                pass
        for p in (self.copy_dir, self.index_file, self.marker, self.run_dir / "shadow.git"):
            _remove(p)  # shadow.git: per-run store from older versions
        return freed


def gc_shadow(shadow_root: Path) -> int:
    """Reclaim objects no longer referenced by any run; delete stores with no runs left. Returns bytes freed."""
    freed = 0
    if not shadow_root.exists():
        return 0
    for store in shadow_root.glob("*.git"):
        before = dir_size(store)
        refs = subprocess.run(["git", f"--git-dir={store}", "for-each-ref", "refs/katae"],
                              capture_output=True, text=True).stdout.strip()
        if not refs:
            _remove(store)
            freed += before
            continue
        subprocess.run(["git", *IDENTITY, f"--git-dir={store}", "gc", "--prune=now", "--quiet"], capture_output=True)
        freed += max(0, before - dir_size(store))
    return freed


MAX_BASELINE_MB = 200
GIT_QUIET = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.longpaths=true"]


def git_toplevel(folder: Path) -> Path | None:
    """Repository root if `folder` is inside a git work tree that has at least one commit."""
    def run(*args):
        return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    top = run("rev-parse", "--show-toplevel")
    if top.returncode != 0 or run("rev-parse", "--verify", "-q", "HEAD").returncode != 0:
        return None
    return Path(top.stdout.strip())


class GitBaselineWorkspace:
    """In-place work on a git repository WITHOUT snapshotting it.

    Baseline for every path = its HEAD blob, except files that were already modified, deleted or
    untracked when the run started: only those are saved (code/config sized, excludes applied).
    A multi-GB Unreal repo therefore costs only its uncommitted work, not a copy of the project.
    Nothing is written to the repository: no index updates, stashes, refs or objects.
    """

    mode = "inplace"

    def __init__(self, run_dir: Path, source: Path, root: Path, excludes: list[str] | None = None,
                 max_baseline_mb: float = MAX_BASELINE_MB, max_file_mb: float = MAX_FILE_MB):
        self.run_dir = run_dir
        self.source = source
        self.root = root
        sub = os.path.relpath(source, root).replace("\\", "/")
        self.sub = "." if sub in (".", "") else sub
        self.excludes = DEFAULT_EXCLUDES if excludes is None else excludes
        self.baseline_dir = run_dir / "baseline"
        self.manifest = run_dir / "baseline.json"
        self.patch_path = run_dir / "result.patch"
        self.max_baseline_mb = max_baseline_mb
        self.max_file_mb = max_file_mb
        self._excluded = Workspace._excluded.__get__(self)  # same exclude rules as snapshots

    @property
    def path(self) -> Path:
        return self.source

    @property
    def prepared(self) -> bool:
        return self.manifest.exists()

    def _git(self, *args: str, input_bytes: bytes | None = None, ok_codes=(0,)) -> bytes:
        proc = subprocess.run(["git", *GIT_QUIET, "-C", str(self.root), *args], capture_output=True, input=input_bytes)
        if proc.returncode not in ok_codes:
            raise WorkspaceError(f"git {' '.join(args[:2])} failed: {proc.stderr.decode('utf-8', 'replace')[-400:]}")
        return proc.stdout

    def _dirty_now(self) -> set[str]:
        """Root-relative paths under the target folder that differ from HEAD or are untracked (not ignored)."""
        spec = ["--", self.sub]
        changed = self._git("diff", "--name-only", "-z", "--no-renames", "HEAD", *spec).decode("utf-8", "replace")
        untracked = self._git("ls-files", "-z", "--others", "--exclude-standard", "--full-name", *spec).decode("utf-8", "replace")
        return {p for p in (changed + "\0" + untracked).split("\0") if p and not self._excluded(p)}

    def _head_blob(self, rel: str) -> bytes | None:
        proc = subprocess.run(["git", *GIT_QUIET, "-C", str(self.root), "cat-file", "blob", f"HEAD:{rel}"], capture_output=True)
        return proc.stdout if proc.returncode == 0 else None

    def prepare(self) -> dict:
        if self.prepared:
            return {"mode": "inplace", "baseline": "git", "path": str(self.source)}
        head = self._git("rev-parse", "HEAD").decode().strip()
        saved: dict[str, bool] = {}   # path -> existed at start (True: copy saved, False: absent at start)
        skipped, total = [], 0
        limit = self.max_file_mb * 1_048_576
        for rel in sorted(self._dirty_now()):
            full = self.root / rel
            if not full.exists():
                saved[rel] = False
                continue
            size = full.stat().st_size
            if size > limit:
                skipped.append((rel, size))
                continue
            total += size
            if total > self.max_baseline_mb * 1_048_576:
                raise WorkspaceError(
                    f"실행 시작 시점의 미커밋 변경이 {self.max_baseline_mb:.0f}MB 를 넘습니다 — 먼저 커밋하거나 "
                    "저장소 excludes 에 큰 폴더를 추가하세요")
            dest = self.baseline_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(full, dest)
            saved[rel] = True
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(json.dumps({"head": head, "saved": saved, "skipped": [p for p, _ in skipped]},
                                            ensure_ascii=False), encoding="utf-8")
        return {"mode": "inplace", "baseline": "git", "path": str(self.source), "head": head[:10],
                "saved_files": sum(saved.values()), "baseline_mb": round(total / 1_048_576, 2),
                "skipped_large": [f"{p} ({s / 1_048_576:.0f}MB)" for p, s in skipped[:10]]}

    def _load(self) -> dict:
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def _baseline(self, rel: str, man: dict) -> bytes | None:
        if rel in man["saved"]:
            return (self.baseline_dir / rel).read_bytes() if man["saved"][rel] else None
        return self._head_blob(rel)

    def _changes(self) -> list[tuple[str, bytes | None, bytes | None]]:
        man = self._load()
        candidates = self._dirty_now() | set(man["saved"])
        out = []
        for rel in sorted(candidates):
            if rel in man.get("skipped", []):
                continue
            before = self._baseline(rel, man)
            full = self.root / rel
            after = full.read_bytes() if full.is_file() else None
            if before != after:
                out.append((rel, before, after))
        return out

    def collect(self) -> dict:
        if not self.prepared:
            return {"files": 0, "insertions": 0, "deletions": 0, "stat": ""}
        patches, stat_lines, ins_total, del_total = [], [], 0, 0
        with tempfile.TemporaryDirectory(prefix="katae-diff-") as tmp:
            for rel, before, after in self._changes():
                a = Path(tmp) / "a" / rel
                b = Path(tmp) / "b" / rel
                for p, data in ((a, before), (b, after)):
                    if data is not None:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(data)
                left = f"a/{rel}" if before is not None else "/dev/null"
                right = f"b/{rel}" if after is not None else "/dev/null"
                base = ["git", *GIT_QUIET, "diff", "--no-index", "--no-color", "--src-prefix=", "--dst-prefix="]
                diff = subprocess.run([*base, "--binary", left, right], capture_output=True, cwd=tmp).stdout
                lines = diff.split(b"\n")
                if lines and lines[0].startswith(b"diff --git"):
                    lines[0] = f"diff --git a/{rel} b/{rel}".encode()  # normalise /dev/null and a/ b/ roots
                patches.append(b"\n".join(lines))
                num = subprocess.run([*base, "--numstat", left, right], capture_output=True, cwd=tmp).stdout.decode()
                parts = num.split("\t")
                ins = int(parts[0]) if parts and parts[0].isdigit() else 0
                dels = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                ins_total, del_total = ins_total + ins, del_total + dels
                stat_lines.append(f" {rel} | {ins + dels} {'+' * min(ins, 20)}{'-' * min(dels, 20)}")
        self.patch_path.write_bytes(b"".join(patches))
        return {"files": len(stat_lines), "insertions": ins_total, "deletions": del_total, "stat": "\n".join(stat_lines)}

    def rollback(self) -> dict:
        """Put every changed path back to its baseline; files created during the run are deleted."""
        if not self.prepared:
            raise WorkspaceError("기준선 정보가 정리되어 되돌릴 수 없습니다")
        summary = self.collect()
        for rel, before, _after in self._changes():
            full = self.root / rel
            if before is None:
                full.unlink(missing_ok=True)
            else:
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_bytes(before)
        self.cleanup()
        return summary

    def apply(self) -> str:
        raise WorkspaceError("apply is for copy mode; inplace changes are already in the folder")

    def discard(self) -> None:
        raise WorkspaceError("discard is for copy mode; use rollback for inplace changes")

    def cleanup(self) -> int:
        freed = dir_size(self.baseline_dir) if self.baseline_dir.exists() else 0
        _remove(self.baseline_dir)
        _remove(self.manifest)
        return freed


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, onerror=_force_remove)
    elif path.exists():
        path.unlink()


def _force_remove(func, path, _exc):
    Path(path).chmod(stat.S_IWRITE)
    func(path)
