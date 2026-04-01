"""File change tracker — SHA-256 based deduplication for folder scanning.

Persists a ``file_index.yaml`` inside the ``architecture/`` directory so the
index is GitOps-committable and team-shareable.

Usage::

    from strata.tracker import FileTracker

    tracker = FileTracker()            # loads existing index
    if tracker.is_changed(path):       # True when file is new or content changed
        ids = do_scan(path)
        tracker.record(path, ids)      # update hash + staging IDs
    tracker.save()                     # persist to YAML
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .workspace import find_workspace_root

# ── Constants ──────────────────────────────────────────────────────────────────

INDEX_FILE = "file_index.yaml"
_BUF_SIZE = 65_536  # 64 KiB read chunks


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sha256(path: str) -> str:
    """Return hex SHA-256 digest of *path*."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_BUF_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── FileTracker ────────────────────────────────────────────────────────────────

class FileTracker:
    """Track file content hashes to avoid re-scanning unchanged documents.

    The index is a dict keyed by **resolved absolute path** with values::

        sha256: <hex>
        mtime: <float>
        last_scanned: <ISO-8601>
        staging_ids: [stg-001, stg-002, …]

    Parameters
    ----------
    root : str | Path | None
        Explicit workspace root.  When *None* (default) the tracker calls
        :func:`find_workspace_root` to locate the ``architecture/`` directory.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            root = find_workspace_root()
        self._root = Path(root)
        self._index_path = self._root / INDEX_FILE
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    # ── Persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._index_path.exists():
            raw = yaml.safe_load(self._index_path.read_text()) or {}
            self._entries = {str(k): v for k, v in raw.items()}

    def save(self) -> None:
        """Write the index back to ``architecture/file_index.yaml``."""
        self._index_path.write_text(
            yaml.dump(self._entries, default_flow_style=False, sort_keys=True)
        )

    # ── Public API ─────────────────────────────────────────────────────────

    def is_changed(self, path: str | Path) -> bool:
        """Return *True* if *path* is new or its content has changed."""
        resolved = str(Path(path).resolve())
        if not Path(resolved).exists():
            return False
        current_hash = _sha256(resolved)
        entry = self._entries.get(resolved)
        if entry is None:
            return True  # never seen
        return entry.get("sha256") != current_hash

    def record(self, path: str | Path, staging_ids: list[str] | None = None) -> None:
        """Update the index entry for *path* with current hash + metadata."""
        resolved = str(Path(path).resolve())
        p = Path(resolved)
        if not p.exists():
            return
        self._entries[resolved] = {
            "sha256": _sha256(resolved),
            "mtime": p.stat().st_mtime,
            "last_scanned": _now_iso(),
            "staging_ids": staging_ids or [],
        }

    def forget(self, path: str | Path) -> None:
        """Remove *path* from the index (forces rescan next time)."""
        resolved = str(Path(path).resolve())
        self._entries.pop(resolved, None)

    def tracked_paths(self) -> list[str]:
        """Return all paths currently in the index."""
        return list(self._entries.keys())

    def staging_ids_for(self, path: str | Path) -> list[str]:
        """Return staging IDs previously produced from *path*."""
        resolved = str(Path(path).resolve())
        entry = self._entries.get(resolved)
        return list(entry.get("staging_ids", [])) if entry else []

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def summary(self) -> dict[str, int]:
        """Return a compact summary dict for display."""
        return {
            "tracked_files": len(self._entries),
            "total_staging_ids": sum(
                len(e.get("staging_ids", [])) for e in self._entries.values()
            ),
        }
