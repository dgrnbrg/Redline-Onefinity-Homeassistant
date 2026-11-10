"""Controller WebSocket client for RTS-X managing gcode/workflow/, TTL pruning, and chunked upload."""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import logging
import os
import socket
import struct
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024  # 64 KB per RTS-X protocol


class SimpleWebSocketClient:
    """Minimal RFC 6455 WebSocket client using asyncio.open_connection."""

    def __init__(self, host: str, port: int = 8080, timeout: float = 5.0) -> None:
        if "://" in host:
            host = host.split("://", 1)[1]
        if "/" in host:
            host = host.split("/", 1)[0]
        if ":" in host:
            parts = host.split(":")
            host = parts[0]
            try:
                port = int(parts[1])
            except ValueError:
                pass
        self.host = host
        self.port = port
        self.timeout = timeout
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.connected = False
        self.last_error: str | None = None

    async def _attempt_handshake(self, host: str, port: int) -> tuple[bool, str | None, bytes]:
        """Attempt socket connection and WebSocket HTTP 101 Upgrade handshake."""
        reader = None
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=self.timeout,
            )

            # Generate Sec-WebSocket-Key
            raw_key = os.urandom(16)
            ws_key = base64.b64encode(raw_key).decode("ascii")

            handshake = (
                f"GET / HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {ws_key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            )

            writer.write(handshake.encode("ascii"))
            await writer.drain()

            # Read response header
            header_data = b""
            while b"\r\n\r\n" not in header_data:
                chunk = await asyncio.wait_for(reader.read(1024), timeout=self.timeout)
                if not chunk:
                    break
                header_data += chunk

            if b"101 Switching Protocols" in header_data:
                self.reader = reader
                self.writer = writer
                self.connected = True
                return True, None, header_data

            # Handshake was rejected (e.g. 200 OK from HTTP server)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return False, "Handshake rejected (no 101 status)", header_data
        except asyncio.TimeoutError:
            if writer:
                try:
                    writer.close()
                except Exception:
                    pass
            return False, f"Connection to {host}:{port} timed out after {self.timeout}s", b""
        except ConnectionRefusedError:
            return False, f"Connection to {host}:{port} refused (controller service offline or port blocked)", b""
        except socket.gaierror as err:
            return False, f"Hostname '{host}' could not be resolved: {err}", b""
        except OSError as err:
            return False, f"Network OS error connecting to {host}:{port}: {err}", b""
        except Exception as err:
            return False, f"Failed to connect to {host}:{port}: {err}", b""

    async def connect(self) -> bool:
        """Perform HTTP Upgrade handshake to establish WebSocket connection, with automatic fallback from HTTP port 80 to 8080."""
        self.last_error = None
        ok, err, header_data = await self._attempt_handshake(self.host, self.port)
        if ok:
            return True

        # Check if the target was the HTTP web UI (port 80/8000 or uvicorn / flutter HTML returned)
        is_http_ui = (
            self.port in (80, 8000)
            or b"uvicorn" in header_data.lower()
            or b"flutter" in header_data.lower()
            or b"200 OK" in header_data
            or b"text/html" in header_data.lower()
        )

        if is_http_ui and self.port != 8080:
            _LOGGER.info(
                "Port %d responded with HTTP web content (Uvicorn/Flutter). "
                "Attempting automatic fallback to RTS-X WebSocket server on port 8080...",
                self.port,
            )
            fb_ok, fb_err, _ = await self._attempt_handshake(self.host, 8080)
            if fb_ok:
                _LOGGER.info(
                    "Successfully connected to RTS-X WebSocket server on port 8080 "
                    "(auto-recovered from HTTP port %d)!",
                    self.port,
                )
                self.port = 8080
                return True
            else:
                self.last_error = (
                    f"Handshake rejected on port {self.port}: Port {self.port} is the RTS-X HTTP web UI (Uvicorn/Flutter), not the WebSocket server. "
                    f"Automatic fallback to RTS-X WebSocket port 8080 failed ({fb_err}). "
                    f"Please verify RTS-X WebSocket server is running on port 8080."
                )
                _LOGGER.warning(self.last_error)
                return False

        # Extract only the HTTP status line instead of dumping thousands of bytes of HTML
        if header_data:
            first_line = header_data.split(b"\r\n")[0].decode("latin-1", "ignore").strip()
            self.last_error = f"Handshake rejected: {first_line or 'Non-WebSocket response'}"
        else:
            self.last_error = err or f"Could not connect to {self.host}:{self.port}"

        _LOGGER.warning(self.last_error)
        return False

    async def send_json(self, payload: dict[str, Any]) -> bool:
        """Send a JSON text frame with client masking."""
        if not self.connected or not self.writer:
            return False
        try:
            raw_data = json.dumps(payload).encode("utf-8")
            frame = self._build_frame(raw_data, opcode=0x1)  # 0x1 = text
            self.writer.write(frame)
            await self.writer.drain()
            return True
        except Exception as err:
            _LOGGER.debug("Error sending WS frame: %s", err)
            self.connected = False
            return False

    async def read_json(self, timeout: float = 3.0) -> dict[str, Any] | None:
        """Read next incoming text frame and decode JSON."""
        if not self.connected or not self.reader:
            return None
        try:
            raw = await asyncio.wait_for(self._read_frame(), timeout=timeout)
            if raw is None:
                return None
            return json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as err:
            _LOGGER.debug("Error reading WS message: %s", err)
            return None

    async def _read_frame(self) -> bytes | None:
        if not self.reader:
            return None
        head = await self.reader.readexactly(2)
        b1, b2 = head[0], head[1]
        opcode = b1 & 0x0F
        is_masked = bool(b2 & 0x80)
        payload_len = b2 & 0x7F

        if payload_len == 126:
            ext = await self.reader.readexactly(2)
            payload_len = struct.unpack("!H", ext)[0]
        elif payload_len == 127:
            ext = await self.reader.readexactly(8)
            payload_len = struct.unpack("!Q", ext)[0]

        mask_key = None
        if is_masked:
            mask_key = await self.reader.readexactly(4)

        payload = await self.reader.readexactly(payload_len)
        if is_masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        if opcode == 0x8:  # Close frame
            self.connected = False
            return None
        if opcode == 0x9:  # Ping frame
            pong = self._build_frame(payload, opcode=0xA)
            if self.writer:
                self.writer.write(pong)
                await self.writer.drain()
            return await self._read_frame()

        return payload

    def _build_frame(self, data: bytes, opcode: int = 0x1) -> bytes:
        length = len(data)
        b1 = 0x80 | (opcode & 0x0F)  # FIN=1
        mask_key = os.urandom(4)

        if length <= 125:
            header = bytes([b1, 0x80 | length])
        elif length <= 65535:
            header = bytes([b1, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([b1, 0x80 | 127]) + struct.pack("!Q", length)

        masked_data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        return header + mask_key + masked_data

    async def close(self) -> None:
        self.connected = False
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        self.writer = None
        self.reader = None


class ControllerWorkflowManager:
    """High-level client managing gcode/workflow/, TTL pruning, and chunked uploads."""

    def __init__(self, host: str = "192.168.1.150", port: int = 8080) -> None:
        if "://" in host:
            host = host.split("://", 1)[1]
        if "/" in host:
            host = host.split("/", 1)[0]
        if ":" in host:
            parts = host.split(":")
            host = parts[0]
            try:
                port = int(parts[1])
            except ValueError:
                pass
        self.host = host
        self.port = port
        self.workflow_folder = "gcode/workflow"

    async def test_connection(self) -> bool:
        """Verify WebSocket connectivity to the controller."""
        client = SimpleWebSocketClient(self.host, self.port, timeout=2.5)
        ok = await client.connect()
        if client.port != self.port:
            self.port = client.port
        await client.close()
        return ok

    async def test_connection_detailed(self) -> dict[str, Any]:
        """Test connection and return latency and error details."""
        client = SimpleWebSocketClient(self.host, self.port, timeout=2.5)
        t0 = time.perf_counter()
        ok = await client.connect()
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        err = client.last_error if not ok else None
        if client.port != self.port:
            self.port = client.port
        await client.close()
        return {
            "ok": ok,
            "host": self.host,
            "port": self.port,
            "latency_ms": latency_ms if ok else None,
            "error": err,
        }

    async def execute_sync(
        self,
        files: dict[str, str],
        workflow_key: str,
        ttl_hours: float = 24.0,
        subfolder: str = "gcode/workflow",
    ) -> dict[str, Any]:
        """Execute a complete, atomic sync transaction on a SINGLE WebSocket connection."""
        logs: list[str] = []

        def log(msg: str) -> None:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            entry = f"[{ts}] {msg}"
            logs.append(entry)
            _LOGGER.info(entry)

        log(f"Initiating sync to controller at ws://{self.host}:{self.port} (target: {subfolder})")
        client = SimpleWebSocketClient(self.host, self.port, timeout=3.5)

        t_start = time.perf_counter()
        if not await client.connect():
            err_msg = client.last_error or f"Could not connect to controller at {self.host}:{self.port}"
            log(f"Connection failed: {err_msg}")
            return {
                "ok": False,
                "workflow_key": workflow_key,
                "uploaded": [],
                "replaced_earlier": [],
                "pruned_stale": [],
                "error": err_msg,
                "logs": logs,
                "duration_sec": round(time.perf_counter() - t_start, 2),
            }

        # If client auto-switched to port 8080 (due to port 80 HTTP UI fallback):
        if client.port != self.port:
            log(f"Auto-switched from port {self.port} to RTS-X WebSocket port {client.port}")
            self.port = client.port

        uploaded: list[str] = []
        replaced: list[str] = []
        pruned: list[str] = []

        try:
            log(f"Connected to controller WebSocket at {self.host}:{self.port}")

            # 1. Ensure target folder exists
            log(f"Ensuring destination folder '{subfolder}' exists...")
            await client.send_json({"command": "createFolder", "folderPath": subfolder})
            await asyncio.sleep(0.05)

            # 2. Query file list once
            log("Querying file list from controller...")
            await client.send_json({"command": "refreshFileList"})
            existing_files: list[dict[str, Any]] = []
            for _ in range(5):
                msg = await client.read_json(timeout=1.5)
                if not msg:
                    break
                if msg.get("name") == "gcode" and "children" in msg:
                    sub_name = subfolder.split("/")[-1] if "/" in subfolder else subfolder
                    for ch in msg.get("children", []):
                        if ch.get("name") == sub_name and "children" in ch:
                            existing_files = ch.get("children", [])
                            break
                    break

            log(f"Found {len(existing_files)} existing file(s) in '{subfolder}'")

            # 3. Prune stale files (TTL)
            if ttl_hours > 0:
                now = datetime.datetime.now()
                for f in existing_files:
                    fname = f.get("name", "")
                    mod_str = f.get("modified", "")
                    if fname and mod_str:
                        try:
                            mod_time = datetime.datetime.strptime(mod_str, "%Y-%m-%d %H:%M:%S")
                            age_hrs = (now - mod_time).total_seconds() / 3600.0
                            if age_hrs > ttl_hours:
                                log(f"Pruning stale file: {fname} (age: {age_hrs:.1f}h > {ttl_hours}h)")
                                await client.send_json({"command": "deleteFile", "filePath": f"{subfolder}/{fname}"})
                                pruned.append(fname)
                                await asyncio.sleep(0.02)
                        except Exception:
                            pass

            # 4. Delete earlier versions of this workflow
            if workflow_key:
                prefix = f"{workflow_key}_"
                for f in existing_files:
                    fname = f.get("name", "")
                    if fname and (fname.startswith(prefix) or fname == f"{workflow_key}.ngc"):
                        log(f"Replacing earlier version: {fname}")
                        await client.send_json({"command": "deleteFile", "filePath": f"{subfolder}/{fname}"})
                        replaced.append(fname)
                        await asyncio.sleep(0.02)

            # 5. Upload newly generated file(s)
            for fname, content in files.items():
                dest_path = f"{subfolder}/{fname}"
                raw_bytes = content.encode("utf-8")
                total_size = len(raw_bytes)
                checksum = hashlib.md5(raw_bytes).hexdigest()
                log(f"Uploading '{fname}' ({total_size} bytes, {total_size // CHUNK_SIZE + 1} chunks)...")

                await client.send_json(
                    {
                        "command": "startUpload",
                        "fileName": dest_path,
                        "totalSize": total_size,
                        "checksum": checksum,
                    }
                )
                await asyncio.sleep(0.05)

                offset = 0
                while offset < total_size:
                    chunk = raw_bytes[offset : offset + CHUNK_SIZE]
                    chunk_b64 = base64.b64encode(chunk).decode("ascii")
                    await client.send_json(
                        {
                            "command": "uploadChunk",
                            "fileName": dest_path,
                            "chunkData": chunk_b64,
                            "offset": offset,
                        }
                    )
                    offset += len(chunk)
                    await asyncio.sleep(0.002)

                await client.send_json(
                    {
                        "command": "finishUpload",
                        "filePath": dest_path,
                    }
                )
                await asyncio.sleep(0.05)
                uploaded.append(fname)
                log(f"Successfully uploaded '{fname}'")

            log(f"Sync complete: {len(uploaded)} file(s) uploaded in {time.perf_counter() - t_start:.2f}s")
            return {
                "ok": len(uploaded) > 0 or len(files) == 0,
                "workflow_key": workflow_key,
                "uploaded": uploaded,
                "replaced_earlier": replaced,
                "pruned_stale": pruned,
                "logs": logs,
                "duration_sec": round(time.perf_counter() - t_start, 2),
            }
        except Exception as err:
            err_str = f"Sync interrupted by error: {err}"
            log(err_str)
            return {
                "ok": False,
                "workflow_key": workflow_key,
                "uploaded": uploaded,
                "replaced_earlier": replaced,
                "pruned_stale": pruned,
                "error": err_str,
                "logs": logs,
                "duration_sec": round(time.perf_counter() - t_start, 2),
            }
        finally:
            await client.close()

    async def ensure_folder(self, folder_path: str = "gcode/workflow") -> bool:
        """Create folder on controller if it does not already exist."""
        client = SimpleWebSocketClient(self.host, self.port)
        if not await client.connect():
            return False
        try:
            await client.send_json({"command": "createFolder", "folderPath": folder_path})
            await asyncio.sleep(0.1)
            return True
        finally:
            await client.close()

    async def get_workflow_files(self) -> list[dict[str, Any]]:
        """Retrieve list of files inside gcode/workflow/ on controller."""
        client = SimpleWebSocketClient(self.host, self.port)
        if not await client.connect():
            return []

        try:
            await client.send_json({"command": "refreshFileList"})
            # Look for file tree response
            for _ in range(5):
                msg = await client.read_json(timeout=2.0)
                if not msg:
                    break
                if msg.get("name") == "gcode" and "children" in msg:
                    # Search for workflow folder in children
                    for ch in msg.get("children", []):
                        if ch.get("name") == "workflow" and "children" in ch:
                            return ch.get("children", [])
            return []
        finally:
            await client.close()

    async def delete_file(self, file_path: str) -> bool:
        """Delete a file on the controller."""
        client = SimpleWebSocketClient(self.host, self.port)
        if not await client.connect():
            return False
        try:
            await client.send_json({"command": "deleteFile", "filePath": file_path})
            await asyncio.sleep(0.05)
            return True
        finally:
            await client.close()

    async def prune_stale_jobs(self, ttl_hours: float = 24.0) -> list[str]:
        """Automatically delete any files in gcode/workflow/ older than ttl_hours."""
        if ttl_hours <= 0:
            return []

        files = await self.get_workflow_files()
        now = datetime.datetime.now()
        deleted_files: list[str] = []

        for f in files:
            fname = f.get("name", "")
            mod_str = f.get("modified", "")
            if not fname or not mod_str:
                continue

            try:
                mod_time = datetime.datetime.strptime(mod_str, "%Y-%m-%d %H:%M:%S")
                age_seconds = (now - mod_time).total_seconds()
                if age_seconds > ttl_hours * 3600:
                    target_path = f"{self.workflow_folder}/{fname}"
                    _LOGGER.info("Pruning stale workflow file (age: %.1f hrs): %s", age_seconds / 3600, target_path)
                    if await self.delete_file(target_path):
                        deleted_files.append(fname)
            except Exception:
                continue

        return deleted_files

    async def delete_earlier_workflow_versions(self, workflow_key: str) -> list[str]:
        """Delete any existing files belonging to the same logical workflow key."""
        if not workflow_key:
            return []

        files = await self.get_workflow_files()
        deleted: list[str] = []
        prefix = f"{workflow_key}_"

        for f in files:
            fname = f.get("name", "")
            if fname.startswith(prefix) or fname == f"{workflow_key}.ngc":
                target_path = f"{self.workflow_folder}/{fname}"
                _LOGGER.info("Replacing earlier workflow iteration: %s", target_path)
                if await self.delete_file(target_path):
                    deleted.append(fname)

        return deleted

    async def upload_gcode(self, filename: str, content: str) -> bool:
        """Upload a G-code file in 64 KB Base64 chunks matching RTS-X WebSocket protocol."""
        client = SimpleWebSocketClient(self.host, self.port)
        if not await client.connect():
            return False

        try:
            dest_path = f"{self.workflow_folder}/{filename}"
            raw_bytes = content.encode("utf-8")
            total_size = len(raw_bytes)
            checksum = hashlib.md5(raw_bytes).hexdigest()

            # 1. startUpload
            await client.send_json(
                {
                    "command": "startUpload",
                    "fileName": dest_path,
                    "totalSize": total_size,
                    "checksum": checksum,
                }
            )
            await asyncio.sleep(0.05)

            # 2. uploadChunk
            offset = 0
            while offset < total_size:
                chunk = raw_bytes[offset : offset + CHUNK_SIZE]
                chunk_b64 = base64.b64encode(chunk).decode("ascii")
                await client.send_json(
                    {
                        "command": "uploadChunk",
                        "fileName": dest_path,
                        "chunkData": chunk_b64,
                        "offset": offset,
                    }
                )
                offset += len(chunk)
                await asyncio.sleep(0.002)  # 2ms pacing

            # 3. finishUpload
            await client.send_json(
                {
                    "command": "finishUpload",
                    "filePath": dest_path,
                }
            )
            await asyncio.sleep(0.1)
            return True
        finally:
            await client.close()
