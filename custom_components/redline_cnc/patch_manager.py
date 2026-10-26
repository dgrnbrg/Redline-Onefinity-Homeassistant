"""Defense-in-depth controller patch manager for Redline CNC (RTS-X).

Provides idempotent, transactional patching and unpatching of RTS1's FastAPI Web_API.py:
  Layer 1: External backup in ~/redline_ha_backups/ outside _internal/
  Layer 2: Write-once read-only Web_API.py.orig (0444) and versioned .orig.<sha12>
  Layer 3: Local Git repository tracking vendor originals with immutable tags
  Layer 4: In-process redline_ha_ext.py extension module with safe QueuedConnection Qt dispatcher
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import pty
import select
import shutil
import stat
import subprocess
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

PATCH_VERSION = "1.0.0"
PATCH_BEGIN_MARKER = "# --- BEGIN REDLINE_HA_PATCH v1 ---"
PATCH_END_MARKER = "# --- END REDLINE_HA_PATCH ---"
PATCH_MARKER_BEGIN = PATCH_BEGIN_MARKER
PATCH_MARKER_END = PATCH_END_MARKER

REMOTE_INTERNAL_DIR = "/home/rtcnc/rtcnc/RTS1/_internal"
REMOTE_WEB_API_PATH = f"{REMOTE_INTERNAL_DIR}/Web_API.py"
REMOTE_WEB_API_ORIG_PATH = f"{REMOTE_INTERNAL_DIR}/Web_API.py.orig"
REMOTE_WEB_API_SHA_PATH = f"{REMOTE_INTERNAL_DIR}/Web_API.py.orig.sha256"
REMOTE_EXT_MODULE_PATH = f"{REMOTE_INTERNAL_DIR}/redline_ha_ext.py"
REMOTE_BACKUP_DIR = "/home/rtcnc/redline_ha_backups"

# Exact 24-line factory Web_API.py shipped with RTS-X
FACTORY_WEB_API_PY = """from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from ResourcePath import resource_path
import mimetypes

mimetypes.init()
mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('application/javascript', '.mjs')
mimetypes.add_type('application/wasm', '.wasm')

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/", StaticFiles(directory=resource_path("webapp"), html=True), name="webapp")

"""

FACTORY_WEB_API_SHA256 = "c5a67c768b8e582a84e995f371eaebb813c3fca38cdd8e8bd717a35420978fc0"
assert hashlib.sha256(FACTORY_WEB_API_PY.encode("utf-8")).hexdigest() == FACTORY_WEB_API_SHA256
VENDOR_ORIGINAL_WEB_API = FACTORY_WEB_API_PY
VENDOR_ORIGINAL_SHA256 = FACTORY_WEB_API_SHA256

PATCH_HOOK_BLOCK = f"""{PATCH_BEGIN_MARKER}
try:
    import redline_ha_ext
    redline_ha_ext.register_routes(app)
except Exception as _ha_err:
    print(f"Redline HA extension load error: {{_ha_err}}")
{PATCH_END_MARKER}"""

# Standalone FastAPI extension module placed at /home/rtcnc/rtcnc/RTS1/_internal/redline_ha_ext.py
REDLINE_HA_EXT_PY = '''"""Redline CNC Home Assistant in-process FastAPI extension."""

from __future__ import annotations

import os
import threading
from typing import Any
import weakref

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import QApplication

try:
    from HardwareEvents import MachState
except Exception:
    MachState = None

try:
    from MessageTypes import MessageTypes
except Exception:
    MessageTypes = None

try:
    from Settings import MachSettings
except Exception:
    MachSettings = None

PATCH_VERSION = "1.0.0"


class _MainThreadDispatcher(QObject):
    """Dispatches callables onto the main Qt GUI thread via QueuedConnection."""

    invoke = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.invoke.connect(self._on_invoke, Qt.ConnectionType.QueuedConnection)

    def _on_invoke(self, task: Any) -> None:
        fn, done_evt, box = task
        # S8: If task timed out before being dispatched, cancel and do not execute!
        if box.get("cancelled", False):
            done_evt.set()
            return
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            done_evt.set()


_dispatcher: _MainThreadDispatcher | None = None
_dispatcher_lock = threading.Lock()


def _get_dispatcher() -> _MainThreadDispatcher | None:
    global _dispatcher
    app = QApplication.instance()
    if app is None:
        return None
    with _dispatcher_lock:
        if _dispatcher is None:
            _dispatcher = _MainThreadDispatcher()
            _dispatcher.moveToThread(app.thread())
        return _dispatcher


def _run_on_qt_thread(fn: Any, timeout: float = 3.0) -> Any:
    app = QApplication.instance()
    if app is None:
        raise HTTPException(status_code=503, detail="QApplication not initialized")
    # S9: Accurate QThread comparison
    if QThread.currentThread() == app.thread():
        return fn()
    dispatcher = _get_dispatcher()
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="Qt dispatcher unavailable")
    done_evt = threading.Event()
    box: dict[str, Any] = {"cancelled": False}
    dispatcher.invoke.emit((fn, done_evt, box))
    if not done_evt.wait(timeout):
        box["cancelled"] = True
        raise HTTPException(status_code=504, detail="Timed out waiting for Qt main thread")
    if "error" in box:
        err = box["error"]
        if isinstance(err, HTTPException):
            raise err
        raise HTTPException(status_code=500, detail=str(err))
    return box.get("result")


_cached_hmi_refs: dict[str, weakref.ref[Any]] = {}


def _locate_hmi_objects() -> dict[str, Any]:
    global _cached_hmi_refs
    resolved: dict[str, Any] = {}
    for k, r in _cached_hmi_refs.items():
        obj = r()
        if obj is not None:
            resolved[k] = obj

    # If critical controller reference is alive and cached, return resolved
    if "controller" in resolved and ("io_widget" in resolved or "spindle_widget" in resolved):
        return resolved

    app = QApplication.instance()
    if app is None:
        return resolved

    for w in app.allWidgets():
        cls_name = w.__class__.__name__
        if cls_name == "MessageView":
            resolved["message_view"] = w
        elif cls_name == "SpindleWidget":
            resolved["spindle_widget"] = w
            if "io_widget" not in resolved and hasattr(w, "output_state"):
                resolved["io_widget"] = w
        elif cls_name == "IoWidget":
            resolved["io_widget"] = w
        elif cls_name == "MacroWidget":
            resolved["macro_widget"] = w
        if "controller" not in resolved and hasattr(w, "controller"):
            ctrl = getattr(w, "controller", None)
            if ctrl is not None and hasattr(ctrl, "comm") and hasattr(ctrl, "state"):
                resolved["controller"] = ctrl

    _cached_hmi_refs = {k: weakref.ref(v) for k, v in resolved.items()}
    return resolved


class OutputControlRequest(BaseModel):
    output: int | None = None
    pin: int | None = None
    state: bool


class MacroRunRequest(BaseModel):
    macro: int | str | None = None
    index: int | None = None


def register_routes(app: FastAPI) -> None:
    """Register Redline CNC Home Assistant extension endpoints before StaticFiles mount."""
    if getattr(app.state, "redline_ha_registered", False):
        return

    router = APIRouter(prefix="/api/ha", tags=["homeassistant"])

    @router.get("/status")
    def get_ha_status() -> dict[str, Any]:
        def _collect() -> dict[str, Any]:
            objs = _locate_hmi_objects()
            ctrl = objs.get("controller")
            msg_view = objs.get("message_view")
            io_widget = objs.get("io_widget") or objs.get("spindle_widget")
            macro_widget = objs.get("macro_widget")

            # 1. Spindle Warmup Status
            warmup_active = False
            warmup_stage = 0
            warmup_remaining = 0
            warmup_status_text = "Idle"
            if msg_view is not None:
                cw = getattr(msg_view, "current_widget", None)
                if cw is not None and cw.__class__.__name__ == "MessageSpindleWarmup":
                    warmup_active = True
                    warmup_stage = int(getattr(cw, "stage", 0) or 0)
                    warmup_remaining = int(getattr(cw, "time_remaining", 0) or 0)
                    lbl = getattr(cw, "status_message", None)
                    if lbl is not None and hasattr(lbl, "text"):
                        warmup_status_text = str(lbl.text()) or "Running"

            # 2. 4 Hardware Auxiliary Outputs (OUT0..OUT3)
            out_states = [False, False, False, False]
            out_names = ["OUT0", "OUT1", "OUT2", "OUT3"]
            if io_widget is not None:
                raw_states = getattr(io_widget, "output_state", None)
                if isinstance(raw_states, (list, tuple)) and len(raw_states) >= 4:
                    out_states = [bool(raw_states[i]) for i in range(4)]
                raw_names = getattr(io_widget, "output_names", None)
                if isinstance(raw_names, (list, tuple)) and len(raw_names) >= 4:
                    out_names = [str(raw_names[i]) for i in range(4)]
            elif ctrl is not None and hasattr(ctrl, "settings") and MachSettings is not None:
                try:
                    cfg_names = ctrl.settings.get_parameter(MachSettings.output_labels)
                    if isinstance(cfg_names, (list, tuple)) and len(cfg_names) >= 4:
                        out_names = [str(cfg_names[i]) for i in range(4)]
                except Exception:  # noqa: BLE001
                    pass

            outputs_list = [
                {"index": i, "name": out_names[i], "state": out_states[i]}
                for i in range(4)
            ]

            # 3. 12 Macros
            macros_list = []
            macro_running = False
            if macro_widget is not None and hasattr(macro_widget, "macro_running"):
                macro_running = bool(getattr(macro_widget, "macro_running", False))
            elif ctrl is not None:
                macro_running = bool(getattr(ctrl.state, "macro_running", False))

            if ctrl is not None:
                try:
                    raw_macros = ctrl.settings.get_parameter("macros") or []
                except Exception:  # noqa: BLE001
                    raw_macros = []
                for idx in range(12):
                    if idx < len(raw_macros) and isinstance(raw_macros[idx], (list, tuple)) and len(raw_macros[idx]) >= 2:
                        m_name = str(raw_macros[idx][0] or "")
                        m_path = raw_macros[idx][1]
                    else:
                        m_name = ""
                        m_path = None
                    assigned = bool(m_path)
                    exists = bool(m_path and os.path.exists(str(m_path)))
                    path_str = str(m_path) if m_path else ""
                    macros_list.append(
                        {
                            "index": idx,
                            "slot": idx + 1,
                            "name": m_name,
                            "path": path_str,
                            "file_path": path_str or None,
                            "assigned": assigned,
                            "exists": exists,
                        }
                    )

            mach_state_name = "UNKNOWN"
            if ctrl is not None and hasattr(ctrl.state, "mach_state"):
                ms = ctrl.state.mach_state
                mach_state_name = getattr(ms, "name", str(ms))

            capabilities = {
                "spindle_warmup": bool(msg_view is not None and ctrl is not None and hasattr(ctrl, "display_message")),
                "aux_outputs": bool(ctrl is not None and hasattr(ctrl, "comm") and hasattr(ctrl.comm, "setOutput")),
                "macros": bool(macro_widget is not None and hasattr(macro_widget, "handle_cell_click")),
            }

            return {
                "ok": True,
                "patched": True,
                "version": PATCH_VERSION,
                "patch_version": PATCH_VERSION,
                "mach_state": mach_state_name,
                "capabilities": capabilities,
                "spindle_warmup": {
                    "active": warmup_active,
                    "stage": warmup_stage,
                    "remaining_seconds": warmup_remaining,
                    "time_remaining": warmup_remaining,
                    "status": warmup_status_text,
                    "status_message": warmup_status_text,
                },
                "outputs": out_states,
                "outputs_detail": outputs_list,
                "output_labels": out_names,
                "macros": macros_list,
                "macro_running": macro_running,
            }

        return _run_on_qt_thread(_collect)

    @router.post("/spindle-warmup/start")
    def start_spindle_warmup() -> dict[str, Any]:
        def _start() -> dict[str, Any]:
            objs = _locate_hmi_objects()
            ctrl = objs.get("controller")
            msg_view = objs.get("message_view")
            if ctrl is None or msg_view is None:
                raise HTTPException(status_code=503, detail="Controller or MessageView not found")

            if MachState is not None and ctrl.state.mach_state != MachState.IDLE:
                raise HTTPException(
                    status_code=409,
                    detail=f"Machine is not IDLE (current state: {getattr(ctrl.state.mach_state, 'name', ctrl.state.mach_state)})",
                )

            cw = getattr(msg_view, "current_widget", None)
            if cw is None or cw.__class__.__name__ != "MessageSpindleWarmup":
                if MessageTypes is not None and hasattr(MessageTypes, "SPINDLE_WARMUP"):
                    ctrl.display_message(MessageTypes.SPINDLE_WARMUP)
                cw = getattr(msg_view, "current_widget", None)

            if cw is None or cw.__class__.__name__ != "MessageSpindleWarmup":
                raise HTTPException(status_code=500, detail="Failed to open MessageSpindleWarmup widget")

            timer = getattr(cw, "timer", None)
            if timer is not None and hasattr(timer, "isActive") and timer.isActive():
                return {"ok": True, "status": "already_running", "stage": getattr(cw, "stage", 1)}

            cw.handle_start()
            return {
                "ok": True,
                "status": "started",
                "stage": getattr(cw, "stage", 1),
                "time_remaining": getattr(cw, "time_remaining", 0),
            }

        return _run_on_qt_thread(_start)

    @router.post("/spindle-warmup/cancel")
    def cancel_spindle_warmup() -> dict[str, Any]:
        def _cancel() -> dict[str, Any]:
            objs = _locate_hmi_objects()
            ctrl = objs.get("controller")
            msg_view = objs.get("message_view")
            if msg_view is not None:
                cw = getattr(msg_view, "current_widget", None)
                if cw is not None and cw.__class__.__name__ == "MessageSpindleWarmup":
                    cw.abort()
                    return {"ok": True, "status": "aborted"}
            if ctrl is not None:
                ctrl.comm.spindleOff()
            return {"ok": True, "status": "not_active"}

        return _run_on_qt_thread(_cancel)

    @router.post("/output")
    def set_aux_output(req: OutputControlRequest) -> dict[str, Any]:
        out_idx = req.output if req.output is not None else req.pin
        if out_idx is None or out_idx < 0 or out_idx > 3:
            raise HTTPException(status_code=400, detail="Output pin/index must be 0..3")

        def _set_out() -> dict[str, Any]:
            objs = _locate_hmi_objects()
            ctrl = objs.get("controller")
            io_widget = objs.get("io_widget") or objs.get("spindle_widget")
            if ctrl is None:
                raise HTTPException(status_code=503, detail="Controller not found")
            ctrl.comm.setOutput(int(out_idx), bool(req.state))
            new_states = [False, False, False, False]
            if io_widget is not None:
                try:
                    new_states = list(getattr(io_widget, "output_state", [False, False, False, False]))
                except Exception:  # noqa: BLE001
                    pass
            new_states[int(out_idx)] = bool(req.state)
            return {
                "ok": True,
                "pin": int(out_idx),
                "output": int(out_idx),
                "state": bool(req.state),
                "outputs": [bool(x) for x in new_states[:4]],
            }

        return _run_on_qt_thread(_set_out)

    @router.post("/macro/run")
    def run_macro(req: MacroRunRequest) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            objs = _locate_hmi_objects()
            ctrl = objs.get("controller")
            macro_widget = objs.get("macro_widget")
            if ctrl is None or macro_widget is None:
                raise HTTPException(status_code=503, detail="MacroWidget or Controller not found")

            if MachState is not None and ctrl.state.mach_state != MachState.IDLE:
                raise HTTPException(
                    status_code=409,
                    detail=f"Machine is not IDLE (current state: {getattr(ctrl.state.mach_state, 'name', ctrl.state.mach_state)})",
                )

            raw_macros = ctrl.settings.get_parameter("macros") or []
            target_idx: int | None = None
            if req.index is not None:
                target_idx = int(req.index)
            elif isinstance(req.macro, int):
                if 1 <= req.macro <= 12:
                    target_idx = req.macro - 1
                elif 0 <= req.macro < 12:
                    target_idx = req.macro
            elif req.macro is not None:
                q = str(req.macro).strip()
                if q.isdigit():
                    num = int(q)
                    if 1 <= num <= 12:
                        target_idx = num - 1
                    elif 0 <= num < 12:
                        target_idx = num
                else:
                    q_low = q.lower()
                    for idx, entry in enumerate(raw_macros[:12]):
                        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                            if str(entry[0]).strip().lower() == q_low:
                                target_idx = idx
                                break

            if target_idx is None or not (0 <= target_idx < 12):
                raise HTTPException(status_code=404, detail=f"Macro '{req.macro or req.index}' not found")

            if target_idx >= len(raw_macros) or not raw_macros[target_idx][1]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Macro slot {target_idx + 1} has no G-code file assigned",
                )

            macro_path = str(raw_macros[target_idx][1])
            if not os.path.exists(macro_path):
                raise HTTPException(
                    status_code=400,
                    detail=f"Macro file '{macro_path}' does not exist on controller",
                )

            row = target_idx // 3
            col = target_idx % 3
            macro_widget.handle_cell_click(row, col)
            return {
                "ok": True,
                "status": "started",
                "index": target_idx,
                "slot": target_idx + 1,
                "name": str(raw_macros[target_idx][0]),
                "file_path": macro_path,
            }

        return _run_on_qt_thread(_run)

    @router.post("/macro/abort")
    def abort_macro() -> dict[str, Any]:
        def _abort() -> dict[str, Any]:
            objs = _locate_hmi_objects()
            macro_widget = objs.get("macro_widget")
            if macro_widget is None:
                raise HTTPException(status_code=503, detail="MacroWidget not found")
            macro_widget.abort()
            return {"ok": True, "status": "aborted"}

        return _run_on_qt_thread(_abort)

    # Insert routes BEFORE the catch-all StaticFiles mount ("/")
    existing_routes = list(app.router.routes)
    mount_idx = None
    for i, r in enumerate(existing_routes):
        if getattr(r, "path", None) == "" or getattr(r, "name", None) == "webapp":
            mount_idx = i
            break

    app.include_router(router)
    if mount_idx is not None:
        new_routes = app.router.routes[len(existing_routes):]
        del app.router.routes[len(existing_routes):]
        for offset, nr in enumerate(new_routes):
            app.router.routes.insert(mount_idx + offset, nr)

    app.state.redline_ha_registered = True
'''


def atomic_write_bytes(path: str, data: bytes, mode: int | None = None) -> None:
    """Write data to path atomically using a temporary file and fsync."""
    dir_name = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_name, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}.{int(time.time() * 1000)}"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        os.chmod(tmp_path, mode)
    elif os.path.exists(path):
        try:
            os.chmod(tmp_path, stat.S_IMODE(os.stat(path).st_mode))
        except OSError:
            pass
    os.replace(tmp_path, path)


def is_web_api_patched(content: str | bytes) -> bool:
    """Return True if content contains our patch marker."""
    s = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    return PATCH_BEGIN_MARKER in s or "redline_ha_ext" in s


def apply_patch_to_web_api_content(content: str) -> str:
    """Idempotently insert or update the PATCH_HOOK_BLOCK in Web_API.py content."""
    if PATCH_BEGIN_MARKER in content and PATCH_END_MARKER in content:
        start_idx = content.index(PATCH_BEGIN_MARKER)
        end_idx = content.index(PATCH_END_MARKER) + len(PATCH_END_MARKER)
        if content[start_idx:end_idx] == PATCH_HOOK_BLOCK:
            return content
        return content[:start_idx] + PATCH_HOOK_BLOCK + content[end_idx:]

    mount_marker = 'app.mount("/",'
    if mount_marker in content:
        idx = content.index(mount_marker)
        return content[:idx] + PATCH_HOOK_BLOCK + "\n\n" + content[idx:]

    if not content.endswith("\n"):
        content += "\n"
    return content + "\n" + PATCH_HOOK_BLOCK + "\n"


def remove_patch_from_web_api_content(content: str) -> str:
    """Remove the PATCH_HOOK_BLOCK from Web_API.py content, restoring the exact original."""
    if PATCH_BEGIN_MARKER not in content or PATCH_END_MARKER not in content:
        return content
    start_idx = content.index(PATCH_BEGIN_MARKER)
    end_idx = content.index(PATCH_END_MARKER) + len(PATCH_END_MARKER)
    if content[end_idx : end_idx + 2] == "\n\n":
        end_idx += 2
    elif content[end_idx : end_idx + 1] == "\n":
        end_idx += 1
    elif start_idx > 0 and content[start_idx - 1] == "\n":
        start_idx -= 1
    return content[:start_idx] + content[end_idx:]


strip_patch_from_web_api_content = remove_patch_from_web_api_content


def run_patch_operation(cfg: dict[str, Any]) -> dict[str, Any]:
    """Execute install, uninstall, or restart operation with transactional safety."""
    action = cfg.get("action", "install")
    internal_dir = cfg.get("internal_dir", REMOTE_INTERNAL_DIR)
    web_api_path = cfg.get("web_api_path", os.path.join(internal_dir, "Web_API.py"))
    orig_path = cfg.get("orig_path", os.path.join(internal_dir, "Web_API.py.orig"))
    sha_path = cfg.get("sha_path", os.path.join(internal_dir, "Web_API.py.orig.sha256"))
    ext_path = cfg.get("ext_path", os.path.join(internal_dir, "redline_ha_ext.py"))
    backup_dir = cfg.get("backup_dir") or os.path.expanduser("~/redline_ha_backups")
    lock_path = cfg.get("lock_path") or "/tmp/redline_ha_patch.lock"
    restart_rts1 = bool(cfg.get("restart_rts1", False))
    force_restart = bool(cfg.get("force_restart", False))
    patch_version = cfg.get("patch_version", PATCH_VERSION)
    ext_py = cfg.get("ext_py", REDLINE_HA_EXT_PY)

    # Concurrency lock
    lock_fd = None
    try:
        import fcntl

        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception as err:
        return {"ok": False, "error": f"Another patch operation is in progress: {err}"}

    def _sha256_bytes(b: bytes) -> str:
        return hashlib.sha256(b).hexdigest()

    def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", internal_dir, *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _check_machine_idle() -> tuple[bool, str]:
        try:
            import urllib.request

            req = urllib.request.Request("http://127.0.0.1:8000/api/ha/status")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                ms = str(data.get("mach_state", "")).upper()
                if ms in ("RUNNING", "RUN", "CUTTING", "TOOL CHANGE", "TOOLCHANGE"):
                    return False, f"Machine is in active state '{ms}'"
                if data.get("macro_running"):
                    return False, "A macro is currently running"
                sw = data.get("spindle_warmup", {})
                if sw.get("active"):
                    return False, "Spindle warmup is currently active"
        except Exception:
            pass
        return True, "OK"

    def _do_restart() -> tuple[bool, str]:
        if not force_restart:
            safe, reason = _check_machine_idle()
            if not safe:
                return False, f"Refusing to restart RTS1: {reason}"
        p = subprocess.run(["pkill", "-TERM", "-x", "RTS1"], capture_output=True, text=True, check=False)
        if p.returncode != 0:
            p = subprocess.run(["pkill", "-TERM", "-f", "/RTS1"], capture_output=True, text=True, check=False)
        runner_script = "/home/rtcnc/rtcnc/rts1-runner.sh"
        if os.path.exists(runner_script):
            try:
                subprocess.Popen(["/bin/bash", runner_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except Exception:
                pass
        return True, f"Sent SIGTERM to RTS1 (exit {p.returncode})"

    try:
        if action == "restart":
            ok, msg = _do_restart()
            return {"ok": ok, "action": "restart", "restarted": ok, "message": msg}

        os.makedirs(internal_dir, exist_ok=True)
        if not os.path.exists(web_api_path):
            return {"ok": False, "error": f"Target Web_API.py not found at {web_api_path}"}

        with open(web_api_path, "rb") as f:
            current_bytes = f.read()
        current_str = current_bytes.decode("utf-8", errors="replace")

        if action == "install":
            clean_sha = None
            if not is_web_api_patched(current_bytes):
                clean_bytes = current_bytes
                clean_sha = _sha256_bytes(clean_bytes)

                # 1. External backup outside _internal
                try:
                    os.makedirs(backup_dir, exist_ok=True)
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    ext_orig = os.path.join(backup_dir, f"{ts}_Web_API.py.orig")
                    ext_sha = os.path.join(backup_dir, f"{ts}_Web_API.py.orig.sha256")
                    atomic_write_bytes(ext_orig, clean_bytes, 0o444)
                    atomic_write_bytes(ext_sha, (clean_sha + "\n").encode("utf-8"), 0o644)
                except Exception:
                    pass

                # 2. Local read-only orig backup
                if os.path.exists(orig_path):
                    try:
                        os.chmod(orig_path, 0o644)
                    except OSError:
                        pass
                atomic_write_bytes(orig_path, clean_bytes, 0o444)
                atomic_write_bytes(sha_path, (clean_sha + "\n").encode("utf-8"), 0o644)
                versioned_orig = f"{orig_path}.{clean_sha[:12]}"
                if not os.path.exists(versioned_orig):
                    atomic_write_bytes(versioned_orig, clean_bytes, 0o444)

                # 3. Git repo
                git_dir = os.path.join(internal_dir, ".git")
                gitignore_path = os.path.join(internal_dir, ".gitignore")
                if not os.path.exists(gitignore_path):
                    atomic_write_bytes(
                        gitignore_path,
                        b"*\n!.gitignore\n!Web_API.py\n!Web_API.py.orig*\n!redline_ha_ext.py\n",
                        0o644,
                    )
                if not os.path.exists(git_dir):
                    _run_git("init")
                _run_git("config", "user.name", "rtcnc")
                _run_git("config", "user.email", "rtcnc@localhost")
                _run_git("add", "-f", ".gitignore", "Web_API.py", "Web_API.py.orig*", "Web_API.py.orig.sha256")
                _run_git("commit", "-m", f"Vendor original Web_API.py ({clean_sha[:12]})")
                tag_list = _run_git("tag", "-l", "vendor-original").stdout.split()
                if "vendor-original" not in tag_list:
                    _run_git("tag", "vendor-original")
                _run_git("tag", "-f", f"vendor-{clean_sha[:12]}")
            else:
                if os.path.exists(sha_path):
                    try:
                        with open(sha_path, "r", encoding="utf-8") as sf:
                            clean_sha = sf.read().strip()
                    except Exception:
                        pass
                if not clean_sha and os.path.exists(orig_path):
                    try:
                        with open(orig_path, "rb") as of:
                            clean_sha = _sha256_bytes(of.read())
                    except Exception:
                        pass

            # Write redline_ha_ext.py atomically
            ext_py_bytes = ext_py.encode("utf-8")
            ext_changed = True
            if os.path.exists(ext_path):
                with open(ext_path, "rb") as ef:
                    if ef.read() == ext_py_bytes:
                        ext_changed = False
            if ext_changed:
                atomic_write_bytes(ext_path, ext_py_bytes, 0o644)

            # Apply patch to Web_API.py
            new_web_api_str = apply_patch_to_web_api_content(current_str)
            new_web_api_bytes = new_web_api_str.encode("utf-8")
            web_api_changed = new_web_api_bytes != current_bytes
            if web_api_changed:
                atomic_write_bytes(web_api_path, new_web_api_bytes, 0o644)

            git_dir = os.path.join(internal_dir, ".git")
            if os.path.isdir(git_dir):
                _run_git("add", "-f", "Web_API.py", "redline_ha_ext.py")
                _run_git("commit", "-m", f"Apply Redline HA extension v{patch_version}")
                _run_git("tag", "-f", "ha-patched")

            restarted = False
            restart_msg = None
            if restart_rts1:
                ok, restart_msg = _do_restart()
                restarted = ok

            orig_verified = False
            if os.path.exists(orig_path):
                with open(orig_path, "rb") as of:
                    orig_verified = not is_web_api_patched(of.read())

            return {
                "ok": True,
                "patched": True,
                "action": "install",
                "patch_version": patch_version,
                "web_api_changed": web_api_changed,
                "ext_changed": ext_changed,
                "orig_sha256": clean_sha,
                "original_sha256": clean_sha,
                "orig_verified": orig_verified,
                "restarted": restarted,
                "restart_message": restart_msg,
            }

        if action == "uninstall":
            if not is_web_api_patched(current_bytes):
                if os.path.exists(ext_path):
                    try:
                        os.remove(ext_path)
                    except OSError:
                        pass
                return {
                    "ok": True,
                    "patched": False,
                    "action": "uninstall",
                    "unchanged": True,
                    "restore_source": "already_unpatched",
                    "restored_from": "already_unpatched",
                    "message": "Web_API.py is already unpatched; no changes made.",
                    "restarted": False,
                }

            expected_sha = None
            if os.path.exists(sha_path):
                try:
                    with open(sha_path, "r", encoding="utf-8") as sf:
                        expected_sha = sf.read().strip()
                except Exception:
                    pass

            restored_bytes = None
            restore_source = None

            # Priority 1: Surgical marker stripping
            candidate_str = remove_patch_from_web_api_content(current_str)
            candidate_bytes = candidate_str.encode("utf-8")
            if not is_web_api_patched(candidate_bytes):
                cand_sha = _sha256_bytes(candidate_bytes)
                if expected_sha is None or cand_sha == expected_sha:
                    restored_bytes = candidate_bytes
                    restore_source = "marker_strip"

            # Priority 2: Verified Web_API.py.orig
            if restored_bytes is None and os.path.exists(orig_path):
                with open(orig_path, "rb") as of:
                    orig_candidate = of.read()
                if not is_web_api_patched(orig_candidate):
                    orig_sha = _sha256_bytes(orig_candidate)
                    if expected_sha is None or orig_sha == expected_sha:
                        restored_bytes = orig_candidate
                        restore_source = "orig_backup"

            # Priority 3: Git vendor-original tag
            git_dir = os.path.join(internal_dir, ".git")
            if restored_bytes is None and os.path.isdir(git_dir):
                res = _run_git("show", "vendor-original:Web_API.py")
                if res.returncode == 0 and res.stdout and not is_web_api_patched(res.stdout):
                    git_bytes = res.stdout.encode("utf-8")
                    git_sha = _sha256_bytes(git_bytes)
                    if expected_sha is None or git_sha == expected_sha:
                        restored_bytes = git_bytes
                        restore_source = "git:vendor-original"

            if restored_bytes is None:
                return {
                    "ok": False,
                    "error": "Failed to safely restore: no verified original backup matched expected SHA-256.",
                }

            atomic_write_bytes(web_api_path, restored_bytes, 0o644)
            if os.path.exists(ext_path):
                try:
                    os.remove(ext_path)
                except OSError:
                    pass

            if os.path.isdir(git_dir):
                _run_git("add", "-f", "Web_API.py")
                _run_git("rm", "--ignore-unmatch", "-f", "redline_ha_ext.py")
                _run_git("commit", "-m", f"Restore vendor Web_API.py via {restore_source}")

            restarted = False
            restart_msg = None
            if restart_rts1:
                ok, restart_msg = _do_restart()
                restarted = ok

            restored_sha = _sha256_bytes(restored_bytes)
            return {
                "ok": True,
                "patched": False,
                "action": "uninstall",
                "restore_source": restore_source,
                "restored_from": restore_source,
                "restored_sha256": restored_sha,
                "matches_factory_sha": restored_sha == cfg.get("factory_sha", FACTORY_WEB_API_SHA256),
                "restarted": restarted,
                "restart_message": restart_msg,
            }

        return {"ok": False, "error": f"Unknown action: {action}"}
    finally:
        if lock_fd is not None:
            try:
                import fcntl

                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass


def install_patch_in_directory(
    internal_dir: str = REMOTE_INTERNAL_DIR,
    restart_process: bool = False,
    backup_dir: str | None = None,
) -> dict[str, Any]:
    """Execute the Git-backed idempotent patch installation in internal_dir."""
    cfg = {
        "action": "install",
        "internal_dir": internal_dir,
        "web_api_path": os.path.join(internal_dir, "Web_API.py"),
        "orig_path": os.path.join(internal_dir, "Web_API.py.orig"),
        "sha_path": os.path.join(internal_dir, "Web_API.py.orig.sha256"),
        "ext_path": os.path.join(internal_dir, "redline_ha_ext.py"),
        "backup_dir": backup_dir,
        "restart_rts1": restart_process,
        "patch_version": PATCH_VERSION,
        "factory_sha": FACTORY_WEB_API_SHA256,
        "ext_py": REDLINE_HA_EXT_PY,
    }
    return run_patch_operation(cfg)


def uninstall_patch_in_directory(
    internal_dir: str = REMOTE_INTERNAL_DIR,
    restart_process: bool = False,
) -> dict[str, Any]:
    """Restore vendor-original Web_API.py using the verified priority chain."""
    cfg = {
        "action": "uninstall",
        "internal_dir": internal_dir,
        "web_api_path": os.path.join(internal_dir, "Web_API.py"),
        "orig_path": os.path.join(internal_dir, "Web_API.py.orig"),
        "sha_path": os.path.join(internal_dir, "Web_API.py.orig.sha256"),
        "ext_path": os.path.join(internal_dir, "redline_ha_ext.py"),
        "restart_rts1": restart_process,
        "patch_version": PATCH_VERSION,
        "factory_sha": FACTORY_WEB_API_SHA256,
        "ext_py": REDLINE_HA_EXT_PY,
    }
    return run_patch_operation(cfg)


def _build_remote_patch_python_script(
    action: str,
    restart_rts1: bool = False,
    force_restart: bool = False,
) -> str:
    """Build a self-contained Python script to execute on the controller via SSH."""
    payload = {
        "action": action,
        "restart_rts1": restart_rts1,
        "force_restart": force_restart,
        "internal_dir": REMOTE_INTERNAL_DIR,
        "web_api_path": REMOTE_WEB_API_PATH,
        "orig_path": REMOTE_WEB_API_ORIG_PATH,
        "sha_path": REMOTE_WEB_API_SHA_PATH,
        "ext_path": REMOTE_EXT_MODULE_PATH,
        "backup_dir": REMOTE_BACKUP_DIR,
        "factory_py": FACTORY_WEB_API_PY,
        "factory_sha": FACTORY_WEB_API_SHA256,
        "begin_marker": PATCH_BEGIN_MARKER,
        "end_marker": PATCH_END_MARKER,
        "hook_block": PATCH_HOOK_BLOCK,
        "ext_py": REDLINE_HA_EXT_PY,
        "patch_version": PATCH_VERSION,
    }
    b64_payload = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

    # The remote script uses the exact same run_patch_operation code
    inner_script = f"""import base64, fcntl, hashlib, json, os, stat, subprocess, time
try:
    import urllib.request
except Exception:
    urllib = None

cfg = json.loads(base64.b64decode("{b64_payload}").decode("utf-8"))

action = cfg.get("action", "install")
internal_dir = cfg.get("internal_dir")
web_api_path = cfg.get("web_api_path")
orig_path = cfg.get("orig_path")
sha_path = cfg.get("sha_path")
ext_path = cfg.get("ext_path")
backup_dir = cfg.get("backup_dir") or os.path.expanduser("~/redline_ha_backups")
lock_path = cfg.get("lock_path") or "/tmp/redline_ha_patch.lock"
restart_rts1 = bool(cfg.get("restart_rts1", False))
force_restart = bool(cfg.get("force_restart", False))
patch_version = cfg.get("patch_version", "1.0.0")
ext_py = cfg.get("ext_py", "")
begin_marker = cfg.get("begin_marker", "{PATCH_BEGIN_MARKER}")
end_marker = cfg.get("end_marker", "{PATCH_END_MARKER}")
hook_block = cfg.get("hook_block", "")
factory_sha = cfg.get("factory_sha", "{FACTORY_WEB_API_SHA256}")

lock_fd = None
try:
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except Exception as err:
    print("RESULT_JSON:" + json.dumps({{"ok": False, "error": f"Another patch operation is in progress: {{err}}"}}))
    raise SystemExit(0)

def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()

def atomic_write(path, data, mode=None):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = f"{{path}}.tmp.{{os.getpid()}}.{{int(time.time() * 1000)}}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    elif os.path.exists(path):
        try:
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        except OSError:
            pass
    os.replace(tmp, path)

def is_patched(b):
    s = b.decode("utf-8", errors="replace") if isinstance(b, bytes) else b
    return begin_marker in s or "redline_ha_ext" in s

def strip_patch(s):
    if begin_marker not in s or end_marker not in s:
        return s
    si = s.index(begin_marker)
    ei = s.index(end_marker) + len(end_marker)
    if s[ei:ei+2] == "\\n\\n":
        ei += 2
    elif s[ei:ei+1] == "\\n":
        ei += 1
    elif si > 0 and s[si-1] == "\\n":
        si -= 1
    return s[:si] + s[ei:]

def add_patch(s):
    if begin_marker in s and end_marker in s:
        si = s.index(begin_marker)
        ei = s.index(end_marker) + len(end_marker)
        if s[si:ei] == hook_block:
            return s
        return s[:si] + hook_block + s[ei:]
    mm = 'app.mount("/",'
    if mm in s:
        idx = s.index(mm)
        return s[:idx] + hook_block + "\\n\\n" + s[idx:]
    if not s.endswith("\\n"):
        s += "\\n"
    return s + "\\n" + hook_block + "\\n"

def run_git(*args):
    return subprocess.run(
        ["git", "-C", internal_dir] + list(args),
        capture_output=True,
        text=True,
        check=False,
    )

def check_machine_idle():
    if urllib is None:
        return True, "OK"
    try:
        req = urllib.request.Request("http://127.0.0.1:8000/api/ha/status")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            ms = str(data.get("mach_state", "")).upper()
            if ms in ("RUNNING", "RUN", "CUTTING", "TOOL CHANGE", "TOOLCHANGE"):
                return False, f"Machine is in active state '{{ms}}'"
            if data.get("macro_running"):
                return False, "A macro is currently running"
            sw = data.get("spindle_warmup", {{}})
            if sw.get("active"):
                return False, "Spindle warmup is currently active"
    except Exception:
        pass
    return True, "OK"

def do_restart():
    if not force_restart:
        safe, reason = check_machine_idle()
        if not safe:
            return False, f"Refusing to restart RTS1: {{reason}}"
    p = subprocess.run(["pkill", "-TERM", "-x", "RTS1"], capture_output=True, text=True, check=False)
    if p.returncode != 0:
        p = subprocess.run(["pkill", "-TERM", "-f", "/RTS1"], capture_output=True, text=True, check=False)
    runner_script = "/home/rtcnc/rtcnc/rts1-runner.sh"
    if os.path.exists(runner_script):
        try:
            subprocess.Popen(["/bin/bash", runner_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception:
            pass
    return True, f"Sent SIGTERM to RTS1 (exit {{p.returncode}})"

try:
    if action == "restart":
        ok, msg = do_restart()
        print("RESULT_JSON:" + json.dumps({{"ok": ok, "action": "restart", "restarted": ok, "message": msg}}))
        raise SystemExit(0)

    os.makedirs(internal_dir, exist_ok=True)
    if not os.path.exists(web_api_path):
        print("RESULT_JSON:" + json.dumps({{"ok": False, "error": f"Target Web_API.py not found at {{web_api_path}}"}}))
        raise SystemExit(0)

    with open(web_api_path, "rb") as f:
        current_bytes = f.read()
    current_str = current_bytes.decode("utf-8", errors="replace")

    if action == "install":
        clean_sha = None
        if not is_patched(current_bytes):
            clean_bytes = current_bytes
            clean_sha = sha256_bytes(clean_bytes)

            try:
                os.makedirs(backup_dir, exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S")
                atomic_write(os.path.join(backup_dir, f"{{ts}}_Web_API.py.orig"), clean_bytes, 0o444)
                atomic_write(os.path.join(backup_dir, f"{{ts}}_Web_API.py.orig.sha256"), (clean_sha + "\\n").encode("utf-8"), 0o644)
            except Exception:
                pass

            if os.path.exists(orig_path):
                try:
                    os.chmod(orig_path, 0o644)
                except OSError:
                    pass
            atomic_write(orig_path, clean_bytes, 0o444)
            atomic_write(sha_path, (clean_sha + "\\n").encode("utf-8"), 0o644)
            v_orig = f"{{orig_path}}.{{clean_sha[:12]}}"
            if not os.path.exists(v_orig):
                atomic_write(v_orig, clean_bytes, 0o444)

            git_dir = os.path.join(internal_dir, ".git")
            gitignore_path = os.path.join(internal_dir, ".gitignore")
            if not os.path.exists(gitignore_path):
                atomic_write(gitignore_path, b"*\\n!.gitignore\\n!Web_API.py\\n!Web_API.py.orig*\\n!redline_ha_ext.py\\n", 0o644)
            if not os.path.exists(git_dir):
                run_git("init")
            run_git("config", "user.name", "rtcnc")
            run_git("config", "user.email", "rtcnc@localhost")
            run_git("add", "-f", ".gitignore", "Web_API.py", "Web_API.py.orig*", "Web_API.py.orig.sha256")
            run_git("commit", "-m", f"Vendor original Web_API.py ({{clean_sha[:12]}})")
            tag_list = run_git("tag", "-l", "vendor-original").stdout.split()
            if "vendor-original" not in tag_list:
                run_git("tag", "vendor-original")
            run_git("tag", "-f", f"vendor-{{clean_sha[:12]}}")
        else:
            if os.path.exists(sha_path):
                try:
                    with open(sha_path, "r", encoding="utf-8") as sf:
                        clean_sha = sf.read().strip()
                except Exception:
                    pass
            if not clean_sha and os.path.exists(orig_path):
                try:
                    with open(orig_path, "rb") as of:
                        clean_sha = sha256_bytes(of.read())
                except Exception:
                    pass

        ext_bytes = ext_py.encode("utf-8")
        ext_changed = True
        if os.path.exists(ext_path):
            with open(ext_path, "rb") as ef:
                if ef.read() == ext_bytes:
                    ext_changed = False
        if ext_changed:
            atomic_write(ext_path, ext_bytes, 0o644)

        new_web_api_str = add_patch(current_str)
        new_web_api_bytes = new_web_api_str.encode("utf-8")
        web_api_changed = new_web_api_bytes != current_bytes
        if web_api_changed:
            atomic_write(web_api_path, new_web_api_bytes, 0o644)

        git_dir = os.path.join(internal_dir, ".git")
        if os.path.isdir(git_dir):
            run_git("add", "-f", "Web_API.py", "redline_ha_ext.py")
            run_git("commit", "-m", f"Apply Redline HA extension v{{patch_version}}")
            run_git("tag", "-f", "ha-patched")

        restarted = False
        restart_msg = None
        if restart_rts1:
            ok, restart_msg = do_restart()
            restarted = ok

        orig_verified = False
        if os.path.exists(orig_path):
            with open(orig_path, "rb") as of:
                orig_verified = not is_patched(of.read())

        print("RESULT_JSON:" + json.dumps({{
            "ok": True,
            "patched": True,
            "action": "install",
            "patch_version": patch_version,
            "web_api_changed": web_api_changed,
            "ext_changed": ext_changed,
            "orig_sha256": clean_sha,
            "original_sha256": clean_sha,
            "orig_verified": orig_verified,
            "restarted": restarted,
            "restart_message": restart_msg,
        }}))

    elif action == "uninstall":
        if not is_patched(current_bytes):
            if os.path.exists(ext_path):
                try:
                    os.remove(ext_path)
                except OSError:
                    pass
            print("RESULT_JSON:" + json.dumps({{
                "ok": True,
                "patched": False,
                "action": "uninstall",
                "unchanged": True,
                "restore_source": "already_unpatched",
                "restored_from": "already_unpatched",
                "message": "Web_API.py is already unpatched; no changes made.",
                "restarted": False,
            }}))
            raise SystemExit(0)

        expected_sha = None
        if os.path.exists(sha_path):
            try:
                with open(sha_path, "r", encoding="utf-8") as sf:
                    expected_sha = sf.read().strip()
            except Exception:
                pass

        restored_bytes = None
        restore_source = None

        candidate_str = strip_patch(current_str)
        candidate_bytes = candidate_str.encode("utf-8")
        if not is_patched(candidate_bytes):
            cand_sha = sha256_bytes(candidate_bytes)
            if expected_sha is None or cand_sha == expected_sha:
                restored_bytes = candidate_bytes
                restore_source = "marker_strip"

        if restored_bytes is None and os.path.exists(orig_path):
            with open(orig_path, "rb") as of:
                orig_cand = of.read()
            if not is_patched(orig_cand):
                orig_sha = sha256_bytes(orig_cand)
                if expected_sha is None or orig_sha == expected_sha:
                    restored_bytes = orig_cand
                    restore_source = "orig_backup"

        git_dir = os.path.join(internal_dir, ".git")
        if restored_bytes is None and os.path.isdir(git_dir):
            res = run_git("show", "vendor-original:Web_API.py")
            if res.returncode == 0 and res.stdout and not is_patched(res.stdout):
                git_bytes = res.stdout.encode("utf-8")
                git_sha = sha256_bytes(git_bytes)
                if expected_sha is None or git_sha == expected_sha:
                    restored_bytes = git_bytes
                    restore_source = "git:vendor-original"

        if restored_bytes is None:
            print("RESULT_JSON:" + json.dumps({{
                "ok": False,
                "error": "Failed to safely restore: no verified original backup matched expected SHA-256.",
            }}))
            raise SystemExit(0)

        atomic_write(web_api_path, restored_bytes, 0o644)
        if os.path.exists(ext_path):
            try:
                os.remove(ext_path)
            except OSError:
                pass

        if os.path.isdir(git_dir):
            run_git("add", "-f", "Web_API.py")
            run_git("rm", "--ignore-unmatch", "-f", "redline_ha_ext.py")
            run_git("commit", "-m", f"Restore vendor Web_API.py via {{restore_source}}")

        restarted = False
        restart_msg = None
        if restart_rts1:
            ok, restart_msg = do_restart()
            restarted = ok

        restored_sha = sha256_bytes(restored_bytes)
        print("RESULT_JSON:" + json.dumps({{
            "ok": True,
            "patched": False,
            "action": "uninstall",
            "restore_source": restore_source,
            "restored_from": restore_source,
            "restored_sha256": restored_sha,
            "matches_factory_sha": restored_sha == factory_sha,
            "restarted": restarted,
            "restart_message": restart_msg,
        }}))
finally:
    if lock_fd is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except Exception:
            pass
"""
    b64_script = base64.b64encode(inner_script.encode("utf-8")).decode("ascii")
    return f"python3 -c \"import base64; exec(base64.b64decode('{b64_script}').decode('utf-8'))\""


def _run_ssh_command_sync(
    host: str,
    port: int,
    username: str,
    password: str,
    remote_cmd: str,
    timeout: float = 25.0,
) -> str:
    """Execute an SSH command using pty + system ssh binary as a universal fallback."""
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        raise RuntimeError("System 'ssh' binary not found and asyncssh unavailable")

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(
            ssh_bin,
            [
                ssh_bin,
                "-p",
                str(port),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=10",
                f"{username}@{host}",
                remote_cmd,
            ],
        )

    output = b""
    password_sent = False
    import time as _time

    end_time = _time.monotonic() + timeout
    while _time.monotonic() < end_time:
        r, _, _ = select.select([fd], [], [], 0.25)
        if r:
            try:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                output += chunk
                if not password_sent and b"password:" in output.lower():
                    os.write(fd, (password + "\n").encode("utf-8"))
                    password_sent = True
            except OSError:
                break
        else:
            try:
                finished_pid, _ = os.waitpid(pid, os.WNOHANG)
                if finished_pid == pid:
                    break
            except ChildProcessError:
                break

    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass

    return output.decode("utf-8", errors="replace")


async def async_exec_ssh(
    host: str,
    port: int,
    username: str,
    password: str,
    remote_cmd: str,
    timeout: float = 25.0,
) -> str:
    """Execute a command over SSH using asyncssh (preferred) or pty+ssh fallback."""
    try:
        import asyncssh  # type: ignore[import-not-found]

        async with asyncio.timeout(timeout):
            async with asyncssh.connect(
                host,
                port=port,
                username=username,
                password=password,
                known_hosts=None,
            ) as conn:
                result = await conn.run(remote_cmd, check=False)
                stdout = result.stdout or ""
                stderr = result.stderr or ""
                return f"{stdout}\n{stderr}"
    except ImportError:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            _run_ssh_command_sync,
            host,
            port,
            username,
            password,
            remote_cmd,
            timeout,
        )


def _extract_result_json(raw_output: str) -> dict[str, Any]:
    """Extract the RESULT_JSON:{...} line from SSH command output."""
    for line in reversed(raw_output.splitlines()):
        if "RESULT_JSON:" in line:
            idx = line.index("RESULT_JSON:") + len("RESULT_JSON:")
            return json.loads(line[idx:].strip())
    raise RuntimeError(f"Did not receive RESULT_JSON from controller SSH output: {raw_output[-500:]}")


async def async_install_controller_patch(
    host: str,
    port: int = 22,
    username: str = "rtcnc",
    password: str = "rtcnc",
    restart_rts1: bool = True,
) -> dict[str, Any]:
    """Install or update the Redline HA FastAPI extension on the controller via SSH."""
    cmd = _build_remote_patch_python_script("install", restart_rts1=restart_rts1)
    out = await async_exec_ssh(host, port, username, password, cmd)
    res = _extract_result_json(out)
    _LOGGER.info("Installed Redline CNC controller patch on %s: %s", host, res)
    return res


async def async_uninstall_controller_patch(
    host: str,
    port: int = 22,
    username: str = "rtcnc",
    password: str = "rtcnc",
    restart_rts1: bool = True,
) -> dict[str, Any]:
    """Uninstall the Redline HA FastAPI extension and restore vendor Web_API.py via SSH."""
    cmd = _build_remote_patch_python_script("uninstall", restart_rts1=restart_rts1)
    out = await async_exec_ssh(host, port, username, password, cmd)
    res = _extract_result_json(out)
    _LOGGER.info("Uninstalled Redline CNC controller patch on %s: %s", host, res)
    return res


async def async_restart_controller_rts1(
    host: str,
    port: int = 22,
    username: str = "rtcnc",
    password: str = "rtcnc",
    force: bool = False,
) -> dict[str, Any]:
    """Safely restart the RTS1 controller process via SSH."""
    cmd = _build_remote_patch_python_script("restart", restart_rts1=True, force_restart=force)
    out = await async_exec_ssh(host, port, username, password, cmd)
    res = _extract_result_json(out)
    _LOGGER.info("Restarted Redline CNC RTS1 on %s: %s", host, res)
    return res


async_install_patch_ssh = async_install_controller_patch
async_uninstall_patch_ssh = async_uninstall_controller_patch
