"""Unix-domain socket IPC server — background control surface.

Runs on its own thread inside the GUI process. External processes (or a future
CLI companion) can connect and send one-line JSON commands. Used today only as
a liveness/telemetry endpoint; it never touches GTK directly (handlers must
marshal back to the main loop if they need the UI).
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class IpcServer:
    def __init__(self, socket_path: Path, handler) -> None:
        self._path = Path(socket_path)
        self._handler = handler  # callable(request: dict) -> dict
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass

    def _loop(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(str(self._path))
            self._sock.listen(16)
            self._sock.settimeout(1.0)
        except OSError as exc:
            logger.error("IPC bind failed on %s: %s", self._path, exc)
            return
        logger.info("IPC listening on %s", self._path)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    data = conn.recv(8192)
                    if data:
                        self._dispatch(conn, data)
                except OSError:
                    pass

    def _dispatch(self, conn: socket.socket, data: bytes) -> None:
        try:
            req = json.loads(data.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            req = {"raw": data.decode("utf-8", errors="replace")}
        try:
            resp = self._handler(req) if self._handler else {"ok": True}
        except Exception as exc:  # noqa: BLE001
            logger.exception("IPC handler error")
            resp = {"error": str(exc)}
        try:
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except OSError:
            pass
