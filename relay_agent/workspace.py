"""Workspaces: snapshot the target folder before a run, then return the work as a patch.

A private "shadow" git repository (runs/<id>/shadow.git) records the folder as it was
when the run started. The target repo's own git (if any) is never touched, and uncommitted
work is included, because the snapshot is of the files on disk, not of HEAD.

- copy:    stages work in runs/<id>/workspace (a copy of the snapshot). The result is
           result.patch, which the user applies to the original folder or discards.
- inplace: stages edit the original folder. The result is the same patch, plus rollback
           to the snapshot. For projects too heavy to copy (e.g. Unreal Content).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

# Heavy or generated folders never snapshotted (relative to the target folder).
DEFAULT_EXCLUDES = [
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", "runs",
    "Binaries", "Intermediate", "Saved", "DerivedDataCache", "Content", ".vs", ".idea",
    "*.uasset", "*.umap", "*.pdb", "*.dll", "*.exe", "*.zip",
]
IDENTITY = ["-c", "user.name=agent-katae", "-c", "user.email=agent-katae@localhost", "-c", "core.autocrlf=false",
            "-c", "core.safecrlf=false", "-c", "core.longpaths=true"]


class WorkspaceError(RuntimeError):
    pass


class Workspace:
    def __init__(self, run_dir: Path, source: Path, mode: str, excludes: list[str] | None = None):
        if mode not in ("copy", "inplace"):
            raise WorkspaceError(f"unknown workspace mode: {mode}")
        self.run_dir = run_dir
        self.source = source
        self.mode = mode
        self.git_dir = run_dir / "shadow.git"
        self.copy_dir = run_dir / "workspace"
        self.patch_path = run_dir / "result.patch"
        self.excludes = DEFAULT_EXCLUDES if excludes is None else excludes

    # --- helpers ---------------------------------------------------------------
    @property
    def path(self) -> Path:
        """Where stages run."""
        return self.copy_dir if self.mode == "copy" else self.source

    def _git(self, work_tree: Path, *args: str, input: str | None = None, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *IDENTITY, f"--git-dir={self.git_dir}", f"--work-tree={work_tree}", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", input=input, cwd=work_tree,
        )
        if check and proc.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[-400:]}")
        return proc.stdout

    def _git_bytes(self, work_tree: Path, *args: str) -> bytes:
        """Raw output: patches must keep exact bytes (CRLF/LF mixes break text-mode round trips)."""
        proc = subprocess.run(
            ["git", *IDENTITY, f"--git-dir={self.git_dir}", f"--work-tree={work_tree}", *args],
            capture_output=True, cwd=work_tree,
        )
        if proc.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args[:2])} failed: {proc.stderr.decode('utf-8', 'replace')[-400:]}")
        return proc.stdout

    def _pathspec(self) -> list[str]:
        # glob magic so folder names are excluded at any depth (a plain ":(exclude)node_modules" is top-level only)
        return ["--", "."] + [f":(exclude,glob)**/{e}" if "*" in e else f":(exclude,glob)**/{e}/**"
                              for e in self.excludes]

    def nested_repos(self) -> list[str]:
        """Sub-folders with their own .git: git snapshots them as a bare pointer, not their files."""
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

    def _stage_all(self, work_tree: Path) -> None:
        # -f: include files the target's own .gitignore hides (build inputs can live there)
        self._git(work_tree, "add", "-A", "-f", *self._pathspec())

    # --- lifecycle -------------------------------------------------------------
    @property
    def ready_marker(self) -> Path:
        return self.git_dir / "katae-ready"

    @property
    def prepared(self) -> bool:
        # Only a finished snapshot counts; an interrupted prepare() leaves no marker and is redone.
        return self.ready_marker.exists()

    def prepare(self) -> dict:
        """Snapshot the source folder; for copy mode, materialize the snapshot as the workspace."""
        if self.prepared:
            return {"mode": self.mode, "path": str(self.path)}
        if shutil.which("git") is None:
            raise WorkspaceError("git is required for workspaces")
        for leftover in (self.git_dir, self.copy_dir):
            if leftover.exists():
                shutil.rmtree(leftover, onerror=_force_remove)
        nested = self.nested_repos()
        if nested:
            raise WorkspaceError(
                "하위 폴더에 별도 git 저장소가 있어 스냅샷·되돌리기를 보장할 수 없습니다: "
                + ", ".join(nested[:5]) + " — workspace_excludes 에 추가하거나 workspace: none 으로 실행하세요"
            )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "-q", str(self.git_dir)], check=True, capture_output=True)
        self._stage_all(self.source)
        self._git(self.source, "commit", "-q", "--allow-empty", "-m", "snapshot before relay")
        files = len(self._git(self.source, "ls-files").splitlines())
        if self.mode == "copy":
            self.copy_dir.mkdir(exist_ok=True)
            self._git(self.copy_dir, "checkout", "-f", "HEAD", "--", ".")
        self.ready_marker.write_text("ok", encoding="utf-8")
        return {"mode": self.mode, "path": str(self.path), "files": files}

    def collect(self) -> dict:
        """Write result.patch (changes since the snapshot) and return a summary."""
        if not self.prepared:
            return {"files": 0, "insertions": 0, "deletions": 0, "stat": ""}
        if not self.path.is_dir():
            raise WorkspaceError(f"workspace folder is gone: {self.path}")
        self._stage_all(self.path)
        self.patch_path.write_bytes(self._git_bytes(self.path, "diff", "--cached", "--binary", "HEAD"))
        numstat = self._git(self.path, "diff", "--cached", "--numstat", "HEAD")
        files, ins, dels = 0, 0, 0
        for line in numstat.splitlines():
            a, d, _ = line.split("\t", 2)
            files += 1
            ins += int(a) if a.isdigit() else 0
            dels += int(d) if d.isdigit() else 0
        stat_text = self._git(self.path, "diff", "--cached", "--stat=100", "HEAD")
        return {"files": files, "insertions": ins, "deletions": dels, "stat": stat_text.strip()}

    def apply(self) -> str:
        """copy mode: apply result.patch to the original folder (checked first, nothing half-applied)."""
        if self.mode != "copy":
            raise WorkspaceError("apply is for copy mode; inplace changes are already in the folder")
        if not self.prepared:  # workspace cleaned up after the retention period: apply the saved patch
            return self._apply_saved_patch()
        self.collect()
        if not self.patch_path.read_bytes().strip():
            raise WorkspaceError("patch is empty")
        # Apply through the shadow repo so paths are relative to the target folder, even when the
        # target is a subfolder of some other git repository.
        try:
            self._git(self.source, "apply", "--check", "--whitespace=nowarn", str(self.patch_path))
        except WorkspaceError as e:
            raise WorkspaceError(f"원본 폴더가 스냅샷 이후 바뀌어 패치가 그대로 적용되지 않습니다: {e}") from e
        self._git(self.source, "apply", "--whitespace=nowarn", str(self.patch_path))
        self._remove_copy()
        return str(self.patch_path)

    def discard(self) -> None:
        """copy mode: drop the workspace; the patch file is kept for reference."""
        if self.mode != "copy":
            raise WorkspaceError("discard is for copy mode; use rollback for inplace changes")
        self.collect()
        self._remove_copy()

    def rollback(self) -> dict:
        """inplace mode: restore every snapshotted file and delete files the relay created."""
        if self.mode != "inplace":
            raise WorkspaceError("rollback is for inplace mode; use discard for copy mode")
        summary = self.collect()
        self._git(self.source, "reset", "-q", "--hard", "HEAD")
        return summary

    def _apply_saved_patch(self) -> str:
        if not self.patch_path.exists() or not self.patch_path.read_bytes().strip():
            raise WorkspaceError("적용할 패치가 없습니다")
        with tempfile.TemporaryDirectory(prefix="katae-apply-") as tmp:
            git_dir = Path(tmp) / "g.git"
            subprocess.run(["git", "init", "--bare", "-q", str(git_dir)], check=True, capture_output=True)
            base = ["git", *IDENTITY, f"--git-dir={git_dir}", f"--work-tree={self.source}", "apply", "--whitespace=nowarn"]
            check = subprocess.run([*base, "--check", str(self.patch_path)], cwd=self.source, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace")
            if check.returncode != 0:
                raise WorkspaceError("원본 폴더가 바뀌어 패치가 그대로 적용되지 않습니다: " + check.stderr.strip()[-300:])
            subprocess.run([*base, str(self.patch_path)], cwd=self.source, check=True, capture_output=True)
        return str(self.patch_path)

    def cleanup(self) -> int:
        """Delete the copy and the snapshot repo, keep result.patch. Returns bytes freed.
        After this an inplace run can no longer be rolled back."""
        freed = 0
        for d in (self.copy_dir, self.git_dir):
            if d.exists():
                freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                shutil.rmtree(d, onerror=_force_remove)
        return freed

    def _remove_copy(self) -> None:
        if self.copy_dir.exists():
            shutil.rmtree(self.copy_dir, onerror=_force_remove)


def _force_remove(func, path, _exc):
    Path(path).chmod(stat.S_IWRITE)
    func(path)
