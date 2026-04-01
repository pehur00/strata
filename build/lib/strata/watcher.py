"""Real-time filesystem watcher for Strata watch folders.

Uses watchdog which delegates to OS-native APIs:
  macOS   → FSEvents  (low CPU, instant)
  Linux   → inotify   (low CPU, instant)
  Windows → ReadDirectoryChangesW

Gracefully skips if watchdog is not installed — the rest of Strata
continues to work without live watching.

Usage
-----
    from strata.watcher import FolderWatcher

    def on_change(path: str, event_type: str) -> None:
        print(event_type, path)   # e.g. "modified /docs/arch.md"

    watcher = FolderWatcher(["/docs", "/architecture"], on_event=on_change)
    watcher.start()
    ...
    watcher.stop()
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# File extensions that are worth scanning for architecture content
WATCHED_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".yaml", ".yml", ".txt", ".json", ".rst"}
)

# ── Optional dependency ────────────────────────────────────────────────────────

_AVAILABLE = False
try:
    from watchdog.observers import Observer
    from watchdog.events import (
        FileSystemEventHandler,
        FileSystemEvent,
    )
    _AVAILABLE = True
except ImportError:
    pass


def is_available() -> bool:
    """Return True if watchdog is installed."""
    return _AVAILABLE


# ── Internal handler (only defined when watchdog is present) ───────────────────

if _AVAILABLE:
    class _ArchHandler(FileSystemEventHandler):  # type: ignore[misc]
        """Forward relevant file-system events to a user-supplied callback."""

        def __init__(self, callback: Callable[[str, str], None]) -> None:
            super().__init__()
            self._cb = callback

        def _emit(self, path: str, etype: str) -> None:
            if Path(path).suffix.lower() in WATCHED_EXTENSIONS:
                try:
                    self._cb(path, etype)
                except Exception as exc:
                    log.debug("watcher callback error: %s", exc)

        def on_created(self, event: FileSystemEvent) -> None:
            if not event.is_directory:
                self._emit(event.src_path, "created")

        def on_modified(self, event: FileSystemEvent) -> None:
            if not event.is_directory:
                self._emit(event.src_path, "modified")

        def on_moved(self, event: FileSystemEvent) -> None:
            if not event.is_directory:
                dest = getattr(event, "dest_path", event.src_path)
                self._emit(dest, "moved")

        def on_deleted(self, event: FileSystemEvent) -> None:
            if not event.is_directory:
                self._emit(event.src_path, "deleted")


# ── Public API ─────────────────────────────────────────────────────────────────


class FolderWatcher:
    """Watch a list of folders for file-system changes.

    Parameters
    ----------
    folders:
        Absolute paths to watch (sub-directories are watched recursively).
    on_event:
        Callback invoked from a background thread with ``(path, event_type)``.
        Use ``app.call_from_thread(handler, path, etype)`` in Textual apps so
        the callback safely runs on the UI event loop.

    Notes
    -----
    * Only files with extensions in ``WATCHED_EXTENSIONS`` trigger callbacks.
    * If watchdog is not installed, ``start()`` returns ``False`` and the
      instance is otherwise inert.
    """

    def __init__(
        self,
        folders: list[str],
        on_event: Callable[[str, str], None],
    ) -> None:
        self._folders = list(folders)
        self._on_event = on_event
        self._observer: Any = None

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return _AVAILABLE

    @property
    def is_running(self) -> bool:
        return (
            _AVAILABLE
            and self._observer is not None
            and self._observer.is_alive()
        )

    @property
    def active_folder_count(self) -> int:
        """Number of folders that exist and are being watched."""
        return sum(1 for f in self._folders if Path(f).is_dir())

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the observer thread.

        Returns ``True`` if watching started, ``False`` if watchdog is missing
        or no folders are valid directories.
        """
        if not _AVAILABLE:
            log.info(
                "watchdog not installed — live folder watching unavailable. "
                "Run: pip install 'strata-cli[watch]'"
            )
            return False

        if self.is_running:
            return True

        self._observer = Observer()
        handler = _ArchHandler(self._on_event)
        scheduled = 0

        for folder in self._folders:
            fp = Path(folder)
            if fp.exists() and fp.is_dir():
                self._observer.schedule(handler, str(fp), recursive=True)
                scheduled += 1
                log.debug("watching: %s", folder)
            else:
                log.debug("watch folder not found, skipping: %s", folder)

        if scheduled == 0:
            log.debug("no valid folders to watch")
            return False

        self._observer.start()
        log.debug("watcher started (%d folders)", scheduled)
        return True

    def stop(self) -> None:
        """Stop the observer thread (blocks briefly for a clean join)."""
        if self._observer is not None and self._observer.is_alive():
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception as exc:
                log.debug("error stopping watcher: %s", exc)
        self._observer = None

    def add_folder(self, folder: str) -> bool:
        """Hot-add a folder to an already-running watcher.

        Returns True if the folder was successfully scheduled.
        """
        if not self.is_running:
            return False
        fp = Path(folder)
        if not (fp.exists() and fp.is_dir()):
            return False
        handler = _ArchHandler(self._on_event)
        self._observer.schedule(handler, str(fp), recursive=True)
        if folder not in self._folders:
            self._folders.append(folder)
        return True
