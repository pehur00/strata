from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import yaml

from .models import (
    ArchitectureWorkspace, DataArchitecture, EnterpriseArchitecture,
    SolutionDesign, StagedItem, WorkspaceManifest,
)

WORKSPACE_DIR = "architecture"
MANIFEST_FILE = "strata.yaml"
STAGING_FILE = "staging.yaml"
_LOCK_FILE = ".strata.lock"


class WorkspaceError(Exception):
    """Raised when the workspace cannot be found or loaded."""


@contextmanager
def _workspace_lock(root: Path) -> Iterator[None]:
    """Acquire an exclusive advisory lock on the workspace directory.

    Uses ``fcntl.flock`` which is released automatically when the file
    descriptor is closed, even on crash.  Falls back gracefully on
    platforms that do not support ``flock`` (e.g. some NFS mounts).
    """
    lock_path = root / _LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via a temp-file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)  # atomic on POSIX; overwrites atomically on Windows (Py3.3+)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def find_workspace_root(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        if (parent / WORKSPACE_DIR / MANIFEST_FILE).exists():
            return parent
    return None


def _arch_dir(root: Path) -> Path:
    return root / WORKSPACE_DIR


def load_workspace(root: Path | None = None) -> ArchitectureWorkspace:
    ws_root = root or find_workspace_root()
    if ws_root is None:
        raise WorkspaceError(
            "No architecture workspace found. Run 'strata init' to create one."
        )
    arch = _arch_dir(ws_root)
    manifest_path = arch / MANIFEST_FILE
    try:
        manifest = WorkspaceManifest.model_validate(
            yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        )
    except Exception as exc:
        raise WorkspaceError(f"Invalid workspace manifest: {exc}") from exc

    enterprise_path = arch / "enterprise" / "architecture.yaml"
    enterprise = (
        EnterpriseArchitecture.model_validate(
            yaml.safe_load(enterprise_path.read_text(encoding="utf-8")) or {}
        )
        if enterprise_path.exists()
        else EnterpriseArchitecture()
    )
    data_path = arch / "data" / "architecture.yaml"
    data_arch = (
        DataArchitecture.model_validate(
            yaml.safe_load(data_path.read_text(encoding="utf-8")) or {}
        )
        if data_path.exists()
        else DataArchitecture()
    )
    solutions: list[SolutionDesign] = []
    solutions_dir = arch / "solutions"
    if solutions_dir.exists():
        for sol_file in sorted(solutions_dir.glob("*.yaml")):
            solutions.append(
                SolutionDesign.model_validate(
                    yaml.safe_load(sol_file.read_text(encoding="utf-8")) or {}
                )
            )
    return ArchitectureWorkspace(
        manifest=manifest, enterprise=enterprise, data=data_arch, solutions=solutions,
    )


def save_workspace(workspace: ArchitectureWorkspace, root: Path | None = None) -> Path:
    ws_root = root or find_workspace_root() or Path.cwd()
    arch = _arch_dir(ws_root)
    arch.mkdir(parents=True, exist_ok=True)

    with _workspace_lock(ws_root):
        _atomic_write(
            arch / MANIFEST_FILE,
            yaml.dump(workspace.manifest.model_dump(exclude_none=True), sort_keys=False),
        )

        _atomic_write(
            arch / "enterprise" / "architecture.yaml",
            yaml.dump(workspace.enterprise.model_dump(exclude_none=True), sort_keys=False),
        )

        _atomic_write(
            arch / "data" / "architecture.yaml",
            yaml.dump(workspace.data.model_dump(exclude_none=True), sort_keys=False),
        )

        solutions_dir = arch / "solutions"
        solutions_dir.mkdir(exist_ok=True)

        # Write current solutions atomically
        current_ids: set[str] = set()
        for solution in workspace.solutions:
            _atomic_write(
                solutions_dir / f"{solution.id}.yaml",
                yaml.dump(solution.model_dump(exclude_none=True), sort_keys=False),
            )
            current_ids.add(solution.id)

        # Remove stale solution files that are no longer in the workspace
        for existing in solutions_dir.glob("*.yaml"):
            if existing.stem not in current_ids:
                existing.unlink()

    return ws_root

# ── Staging helpers ────────────────────────────────────────────────────────────

def load_staging(root: Path | None = None) -> list[StagedItem]:
    """Load staged items from the workspace staging file."""
    ws_root = root or find_workspace_root()
    if ws_root is None:
        return []
    path = _arch_dir(ws_root) / STAGING_FILE
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        return [StagedItem.model_validate(item) for item in data]
    except Exception:
        return []


def save_staging(items: list[StagedItem], root: Path | None = None) -> None:
    """Persist staged items to the workspace staging file."""
    ws_root = root or find_workspace_root() or Path.cwd()
    arch = _arch_dir(ws_root)
    _atomic_write(
        arch / STAGING_FILE,
        yaml.dump([item.model_dump(exclude_none=True) for item in items], sort_keys=False),
    )


def next_staging_id(items: list[StagedItem]) -> str:
    """Return the next sequential staging ID, e.g. 'stg-001'."""
    return f"stg-{len(items) + 1:03d}"


# ── Watch-folder helpers ───────────────────────────────────────────────────────

def load_watch_folders(root: Path | None = None) -> list[str]:
    """Return the list of configured watch folders from the workspace manifest."""
    try:
        ws = load_workspace(root)
        return list(ws.manifest.watch_folders)
    except WorkspaceError:
        return []


def add_watch_folder(path: str, root: Path | None = None) -> list[str]:
    """Append *path* to the workspace watch-folder list (idempotent).

    Returns the updated folder list.
    """
    ws = load_workspace(root)
    resolved = str(Path(path).expanduser().resolve())
    folders = list(ws.manifest.watch_folders)
    if resolved not in folders:
        folders.append(resolved)
        ws = ws.model_copy(
            update={"manifest": ws.manifest.model_copy(update={"watch_folders": folders})}
        )
        save_workspace(ws, root)
    return folders


def remove_watch_folder(path: str, root: Path | None = None) -> list[str]:
    """Remove *path* from the workspace watch-folder list.

    Matches both the raw value and its resolved form.  Returns updated list.
    """
    ws = load_workspace(root)
    resolved = str(Path(path).expanduser().resolve())
    folders = [
        f for f in ws.manifest.watch_folders
        if f != path and f != resolved
    ]
    ws = ws.model_copy(
        update={"manifest": ws.manifest.model_copy(update={"watch_folders": folders})}
    )
    save_workspace(ws, root)
    return folders


# ── File-index helpers (for FileTracker) ───────────────────────────────────────

FILE_INDEX_FILE = "file_index.yaml"


def load_file_index(root: Path | None = None) -> dict:
    """Load the file-tracking index from the workspace."""
    ws_root = root or find_workspace_root()
    if ws_root is None:
        return {}
    path = _arch_dir(ws_root) / FILE_INDEX_FILE
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_file_index(index: dict, root: Path | None = None) -> None:
    """Persist the file-tracking index to the workspace."""
    ws_root = root or find_workspace_root() or Path.cwd()
    arch = _arch_dir(ws_root)
    arch.mkdir(parents=True, exist_ok=True)
    path = arch / FILE_INDEX_FILE
    path.write_text(
        yaml.dump(index, default_flow_style=False, sort_keys=True),
        encoding="utf-8",
    )


def set_scan_interval(minutes: int, root: Path | None = None) -> None:
    """Update the ``scan_interval_minutes`` field in the workspace manifest."""
    ws = load_workspace(root)
    ws = ws.model_copy(
        update={"manifest": ws.manifest.model_copy(update={"scan_interval_minutes": minutes})}
    )
    save_workspace(ws, root)


def set_advisor_interval(minutes: int, root: Path | None = None) -> None:
    """Update the ``advisor_interval_minutes`` field in the workspace manifest."""
    ws = load_workspace(root)
    enabled = minutes > 0
    ws = ws.model_copy(
        update={
            "manifest": ws.manifest.model_copy(
                update={
                    "advisor_interval_minutes": minutes,
                    "advisor_enabled": enabled,
                }
            )
        }
    )
    save_workspace(ws, root)
