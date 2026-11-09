"""Cross-platform directory watcher with CAM file-write debouncing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable

_LOGGER = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".ngc", ".gcode", ".nc", ".tap"}


@dataclass
class WatchedFileState:
    path: str
    size: int
    mtime: float
    stable_count: int = 0
    is_ready: bool = False


class DirectoryWatcher:
    """Watches a directory for new or modified G-code files with write debouncing."""

    def __init__(
        self,
        watch_dir: str,
        on_change_callback: Callable[[list[str]], None],
        poll_interval: float = 1.5,
        debounce_seconds: float = 0.8,
    ) -> None:
        self.watch_dir = watch_dir
        self.on_change = on_change_callback
        self.poll_interval = poll_interval
        self.debounce_seconds = debounce_seconds

        self._running = False
        self._thread: threading.Thread | None = None
        self._files: dict[str, WatchedFileState] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_poll_loop, daemon=True, name="JobWatcherThread")
        self._thread.start()
        _LOGGER.info("Started directory watcher on: %s", self.watch_dir)

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        _LOGGER.info("Stopped directory watcher")

    def get_ready_files(self) -> list[str]:
        with self._lock:
            return [p for p, st in self._files.items() if st.is_ready and os.path.exists(p)]

    def _run_poll_loop(self) -> None:
        while self._running:
            try:
                self._scan_once()
            except Exception as err:
                _LOGGER.debug("Error during directory scan: %s", err)
            time.sleep(self.poll_interval)

    def _scan_once(self) -> None:
        if not self.watch_dir or not os.path.isdir(self.watch_dir):
            return

        current_disk_paths: set[str] = set()
        changed = False

        try:
            entries = os.scandir(self.watch_dir)
        except OSError:
            return

        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                ext = Path(entry.name).suffix.lower()
                if ext not in SUPPORTED_EXTENSIONS:
                    continue

                full_path = os.path.abspath(entry.path)
                current_disk_paths.add(full_path)
                stat = entry.stat()
                cur_size = stat.st_size
                cur_mtime = stat.st_mtime

                with self._lock:
                    if full_path not in self._files:
                        self._files[full_path] = WatchedFileState(
                            path=full_path,
                            size=cur_size,
                            mtime=cur_mtime,
                            stable_count=0,
                            is_ready=False,
                        )
                    else:
                        st = self._files[full_path]
                        if st.size == cur_size and st.mtime == cur_mtime:
                            st.stable_count += 1
                            if not st.is_ready and st.stable_count >= 1:
                                st.is_ready = True
                                changed = True
                        else:
                            st.size = cur_size
                            st.mtime = cur_mtime
                            st.stable_count = 0
                            if st.is_ready:
                                st.is_ready = False
            except (OSError, PermissionError):
                continue

        # Check for deleted files
        with self._lock:
            removed = set(self._files.keys()) - current_disk_paths
            for r in removed:
                del self._files[r]
                changed = True

        if changed:
            ready = self.get_ready_files()
            try:
                self.on_change(ready)
            except Exception as err:
                _LOGGER.error("Callback error on watcher change: %s", err)
