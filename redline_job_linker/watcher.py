"""Cross-platform directory watcher with native filesystem events and CAM file-write debouncing."""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import struct
import sys
import threading
import time
from typing import Callable

_LOGGER = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".ngc", ".gcode", ".nc", ".tap"}


def natural_sort_key(s: str) -> list[int | str]:
    """Return key for natural alphanumeric case-insensitive sorting (e.g. Job_2 before Job_10)."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", str(s))]


# Win32 API setup for native directory change notifications
_kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None
if _kernel32:
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL
    _kernel32.ReadDirectoryChangesW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]

# Optional watchdog integration if installed
try:
    import watchdog.events  # type: ignore[import-untyped]
    import watchdog.observers  # type: ignore[import-untyped]

    _HAS_WATCHDOG = True
except ImportError:
    _HAS_WATCHDOG = False


@dataclass
class WatchedFileState:
    path: str
    size: int
    mtime: float
    stable_count: int = 0
    is_ready: bool = False


class DirectoryWatcher:
    """Watches a directory recursively with native filesystem events and CAM write debouncing."""

    def __init__(
        self,
        watch_dir: str,
        on_change_callback: Callable[[list[str]], None],
        poll_interval: float = 1.0,
        debounce_seconds: float = 0.5,
    ) -> None:
        self.watch_dir = os.path.abspath(watch_dir) if watch_dir else ""
        self.on_change = on_change_callback
        self.poll_interval = poll_interval
        self.debounce_seconds = debounce_seconds

        self._running = False
        self._thread: threading.Thread | None = None
        self._debounce_thread: threading.Thread | None = None
        self._watchdog_observer: Any = None
        self._files: dict[str, WatchedFileState] = {}
        self._lock = threading.Lock()
        self._pending_debounce: dict[str, float] = {}
        self._wake_debounce = threading.Event()
        self._stop_event = threading.Event()
        self._hDir = None
        self._use_native = False

    def is_alive(self) -> bool:
        """Check if watcher background thread is running."""
        return self._running and (
            (self._thread is not None and self._thread.is_alive())
            or (self._watchdog_observer is not None and self._watchdog_observer.is_alive())
        )

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        # Initial baseline scan to discover pre-existing files
        self._initial_scan()

        # 1. Try watchdog if installed
        if _HAS_WATCHDOG and self.watch_dir and os.path.isdir(self.watch_dir):
            try:
                self._start_watchdog()
                self._use_native = True
                _LOGGER.info("Started directory watcher (watchdog) on: %s", self.watch_dir)
                return
            except Exception as err:
                _LOGGER.debug("Watchdog start failed, falling back: %s", err)

        # 2. Try native Win32 ReadDirectoryChangesW on Windows
        if sys.platform == "win32" and _kernel32 and self.watch_dir and os.path.isdir(self.watch_dir):
            if self._start_win32_watcher():
                self._use_native = True
                _LOGGER.info("Started directory watcher (Win32 native) on: %s", self.watch_dir)
                return

        # 3. Fallback to periodic polling loop
        self._use_native = False
        self._thread = threading.Thread(target=self._run_poll_loop, daemon=True, name="JobWatcherThread")
        self._thread.start()
        _LOGGER.info("Started directory watcher (polling fallback) on: %s", self.watch_dir)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        self._wake_debounce.set()

        if self._watchdog_observer:
            try:
                self._watchdog_observer.stop()
                self._watchdog_observer.join(timeout=1.0)
            except Exception:
                pass
            self._watchdog_observer = None

        if self._use_native and sys.platform == "win32":
            # Wake up ReadDirectoryChangesW blocking call with a quick sentinel touch
            try:
                if self.watch_dir and os.path.isdir(self.watch_dir):
                    sentinel = os.path.join(self.watch_dir, ".redline_watcher_stop")
                    try:
                        with open(sentinel, "w") as f:
                            f.write("0")
                        if os.path.exists(sentinel):
                            os.remove(sentinel)
                    except OSError:
                        pass
            except Exception:
                pass

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        if self._debounce_thread and self._debounce_thread.is_alive():
            self._debounce_thread.join(timeout=1.5)

        if self._hDir and _kernel32:
            try:
                _kernel32.CloseHandle(self._hDir)
            except Exception:
                pass
            self._hDir = None

        _LOGGER.info("Stopped directory watcher")

    def get_ready_files(self) -> list[str]:
        """Return ready files in natural alphanumeric sorted order."""
        with self._lock:
            ready = [p for p, st in self._files.items() if st.is_ready and os.path.exists(p)]
            return sorted(ready, key=natural_sort_key)

    def _initial_scan(self) -> None:
        if not self.watch_dir or not os.path.isdir(self.watch_dir):
            return
        with self._lock:
            try:
                for root, _, files in os.walk(self.watch_dir):
                    for fname in files:
                        if Path(fname).suffix.lower() in SUPPORTED_EXTENSIONS:
                            full_path = os.path.abspath(os.path.join(root, fname))
                            try:
                                stat = os.stat(full_path)
                                self._files[full_path] = WatchedFileState(
                                    path=full_path,
                                    size=stat.st_size,
                                    mtime=stat.st_mtime,
                                    stable_count=1,
                                    is_ready=True,
                                )
                            except OSError:
                                continue
            except OSError:
                pass

    def _start_win32_watcher(self) -> bool:
        assert _kernel32 is not None
        self._hDir = _kernel32.CreateFileW(
            self.watch_dir,
            0x0001,  # FILE_LIST_DIRECTORY
            0x0001 | 0x0002 | 0x0004,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
            None,
            3,  # OPEN_EXISTING
            0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
            None,
        )
        if not self._hDir or self._hDir == wintypes.HANDLE(-1).value:
            self._hDir = None
            return False

        self._thread = threading.Thread(target=self._win32_watch_loop, daemon=True, name="JobWatcherWin32")
        self._thread.start()
        self._debounce_thread = threading.Thread(target=self._debounce_loop, daemon=True, name="JobWatcherDebounce")
        self._debounce_thread.start()
        return True

    def _win32_watch_loop(self) -> None:
        assert _kernel32 is not None
        buf = ctypes.create_string_buffer(16384)
        bytes_ret = wintypes.DWORD()
        filters = 0x0001 | 0x0002 | 0x0008 | 0x0010  # FILE_NAME, DIR_NAME, SIZE, LAST_WRITE

        while self._running and self._hDir:
            ok = _kernel32.ReadDirectoryChangesW(
                self._hDir, buf, len(buf), True, filters, ctypes.byref(bytes_ret), None, None
            )
            if not ok or not self._running:
                break
            if bytes_ret.value > 0:
                raw = buf.raw
                offset = 0
                while offset < bytes_ret.value:
                    next_e, action, fn_len = struct.unpack_from("<III", raw, offset)
                    rel_name = raw[offset + 12 : offset + 12 + fn_len].decode("utf-16le")
                    self._handle_fs_event(action, rel_name)
                    if next_e == 0:
                        break
                    offset += next_e

    def _start_watchdog(self) -> None:
        class Handler(watchdog.events.FileSystemEventHandler):
            def __init__(handler_self, outer_self: DirectoryWatcher):
                handler_self.outer = outer_self

            def on_any_event(handler_self, event):
                if event.is_directory:
                    return
                ext = Path(event.src_path).suffix.lower()
                if ext in SUPPORTED_EXTENSIONS:
                    action = 2 if event.event_type == "deleted" else 1
                    handler_self.outer._handle_fs_event(action, event.src_path)

        self._watchdog_observer = watchdog.observers.Observer()
        self._watchdog_observer.schedule(Handler(self), self.watch_dir, recursive=True)
        self._watchdog_observer.start()
        self._debounce_thread = threading.Thread(target=self._debounce_loop, daemon=True, name="JobWatcherDebounce")
        self._debounce_thread.start()

    def _handle_fs_event(self, action: int, rel_or_abs_path: str) -> None:
        if not rel_or_abs_path or ".redline_watcher_stop" in rel_or_abs_path:
            return
        if os.path.isabs(rel_or_abs_path):
            full_path = os.path.abspath(rel_or_abs_path)
        else:
            full_path = os.path.abspath(os.path.join(self.watch_dir, rel_or_abs_path))
        ext = Path(full_path).suffix.lower()

        # Action 2: FILE_ACTION_REMOVED / deleted
        if action == 2:
            changed = False
            with self._lock:
                to_remove = [p for p in self._files if p == full_path or p.startswith(full_path + os.sep)]
                for r in to_remove:
                    del self._files[r]
                    self._pending_debounce.pop(r, None)
                    changed = True
            if changed:
                self._notify_change()
            return

        if ext in SUPPORTED_EXTENSIONS:
            with self._lock:
                self._pending_debounce[full_path] = time.time()
            self._wake_debounce.set()

    def _debounce_loop(self) -> None:
        while self._running:
            self._wake_debounce.wait(timeout=0.15)
            self._wake_debounce.clear()
            if not self._running:
                break
            self._check_pending_debounce()

    def _check_pending_debounce(self) -> None:
        with self._lock:
            pending_items = list(self._pending_debounce.keys())
        if not pending_items:
            return

        changed = False
        for full_path in pending_items:
            if not os.path.exists(full_path):
                with self._lock:
                    self._pending_debounce.pop(full_path, None)
                    if full_path in self._files:
                        del self._files[full_path]
                        changed = True
                continue

            try:
                stat = os.stat(full_path)
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
                            if st.stable_count >= 1:
                                if not st.is_ready:
                                    st.is_ready = True
                                    changed = True
                                self._pending_debounce.pop(full_path, None)
                        else:
                            st.size = cur_size
                            st.mtime = cur_mtime
                            st.stable_count = 0
                            if st.is_ready:
                                st.is_ready = False
            except OSError:
                continue

        if changed:
            self._notify_change()

    def _notify_change(self) -> None:
        ready = self.get_ready_files()
        try:
            self.on_change(ready)
        except Exception as err:
            _LOGGER.error("Callback error on watcher change: %s", err)

    def _run_poll_loop(self) -> None:
        while self._running:
            try:
                self._scan_once()
            except Exception as err:
                _LOGGER.debug("Error during directory scan: %s", err)
            if self._stop_event.wait(timeout=self.poll_interval):
                break

    def _scan_once(self) -> None:
        if not self.watch_dir or not os.path.isdir(self.watch_dir):
            return

        current_disk_paths: set[str] = set()
        changed = False

        try:
            for root, _, files in os.walk(self.watch_dir):
                for fname in files:
                    try:
                        ext = Path(fname).suffix.lower()
                        if ext not in SUPPORTED_EXTENSIONS:
                            continue

                        full_path = os.path.abspath(os.path.join(root, fname))
                        current_disk_paths.add(full_path)
                        stat = os.stat(full_path)
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
        except OSError:
            return

        with self._lock:
            removed = set(self._files.keys()) - current_disk_paths
            for r in removed:
                del self._files[r]
                changed = True

        if changed:
            self._notify_change()
