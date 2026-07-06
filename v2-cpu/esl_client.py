"""
FreeSWITCH ESL inbound client.

Connects TO FreeSWITCH (port 8021) to control calls — NOT an outbound socket server.
Provides a thin wrapper around the ESL text protocol for sending commands
and receiving events.
"""

import os
import socket
import time
import urllib.parse
import threading
from typing import Dict, Optional, Callable

from logging_config import get_logger

log = get_logger("agent.esl")


class ESLEvent:
    """Parsed ESL event."""

    def __init__(self, headers: Dict[str, str] = None, body: str = ""):
        self.headers = headers or {}
        self.body = body

    def get(self, key: str, default: str = None) -> Optional[str]:
        val = self.headers.get(key, default)
        if val:
            return urllib.parse.unquote(val)
        return val

    @property
    def event_name(self):
        return self.get("Event-Name", "")


class ESLClient:
    """
    ESL inbound connection to FreeSWITCH.

    Usage:
        esl = ESLClient("fs-host", 8021, "FS!Secure2026")
        esl.connect()
        esl.subscribe("CHANNEL_CREATE CHANNEL_HANGUP CUSTOM")
        esl.api("status")
        esl.bgapi("uuid_audio_stream", f"{uuid} start ws://... both 16000")
    """

    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self.sock: Optional[socket.socket] = None
        self.buf = b""
        self.connected = False
        self._lock = threading.Lock()

    def connect(self, timeout: float = 10.0):
        """Connect and authenticate to FreeSWITCH ESL."""
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(30.0)

        # FS sends "Content-Type: auth/request"
        event = self._read_event()
        if not event or "auth/request" not in event.headers.get("Content-Type", ""):
            raise ConnectionError("Expected auth/request from FreeSWITCH")

        # Send password
        self._send(f"auth {self.password}\n\n")
        event = self._read_event()
        if not event:
            raise ConnectionError("No response to auth")
        reply = event.headers.get("Reply-Text", "")
        if "+OK" not in reply:
            raise ConnectionError(f"Auth failed: {reply}")

        self.connected = True
        log.info("Connected to FreeSWITCH ESL at %s:%d", self.host, self.port)

    def subscribe(self, events: str):
        """Subscribe to ESL events. e.g. 'CHANNEL_CREATE CHANNEL_HANGUP CUSTOM'"""
        self._send(f"event plain {events}\n\n")
        return self._read_event()

    def api(self, command: str, arg: str = "") -> str:
        """Execute a blocking API command. Returns the response body."""
        with self._lock:
            cmd = f"api {command}"
            if arg:
                cmd += f" {arg}"
            self._send(cmd + "\n\n")
            event = self._read_event()
            if event:
                return event.body or event.headers.get("Reply-Text", "")
            return ""

    def bgapi(self, command: str, arg: str = "") -> str:
        """Execute a non-blocking API command."""
        with self._lock:
            cmd = f"bgapi {command}"
            if arg:
                cmd += f" {arg}"
            self._send(cmd + "\n\n")
            event = self._read_event()
            if event:
                return event.headers.get("Reply-Text", event.body or "")
            return ""

    def execute(self, uuid: str, app: str, arg: str = "") -> str:
        """Execute a dialplan application on a specific channel."""
        with self._lock:
            msg = (
                f"sendmsg {uuid}\n"
                f"call-command: execute\n"
                f"execute-app-name: {app}\n"
            )
            if arg:
                msg += f"execute-app-arg: {arg}\n"
            msg += "\n"
            self._send(msg)
            event = self._read_event()
            if event:
                return event.headers.get("Reply-Text", "")
            return ""

    def read_events(self, timeout: float = 1.0):
        """Generator that yields ESL events (for the event loop)."""
        if not self.connected:
            return
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            event = self._read_event(timeout=timeout)
            if event:
                yield event
        except Exception:
            log.debug("ESL read_events failed", exc_info=True)
        finally:
            try:
                self.sock.settimeout(old_timeout)
            except OSError:
                log.debug("Failed to restore ESL socket timeout", exc_info=True)

    def close(self):
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                log.debug("Failed to close ESL socket", exc_info=True)

    # ---- low-level ----

    def _send(self, data: str):
        try:
            self.sock.sendall(data.encode("utf-8"))
        except (ConnectionError, OSError):
            self.connected = False

    def _read_event(self, timeout: float = 30.0) -> Optional[ESLEvent]:
        if not self.sock:
            return None
        old = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            # Read headers until blank line
            while b"\n\n" not in self.buf:
                try:
                    data = self.sock.recv(4096)
                    if not data:
                        self.connected = False
                        return None
                    self.buf += data
                except socket.timeout:
                    return None
                except (ConnectionError, OSError):
                    self.connected = False
                    return None

            header_end = self.buf.index(b"\n\n")
            header_block = self.buf[:header_end].decode("utf-8", "replace")
            self.buf = self.buf[header_end + 2:]

            headers: Dict[str, str] = {}
            for line in header_block.split("\n"):
                if ": " in line:
                    k, v = line.split(": ", 1)
                    headers[k.strip()] = v.strip()

            content_length = int(headers.get("Content-Length", 0))
            body = ""
            if content_length > 0:
                while len(self.buf) < content_length:
                    try:
                        data = self.sock.recv(4096)
                        if not data:
                            self.connected = False
                            return None
                        self.buf += data
                    except socket.timeout:
                        break
                    except (ConnectionError, OSError):
                        self.connected = False
                        return None
                body = self.buf[:content_length].decode("utf-8", "replace")
                self.buf = self.buf[content_length:]

            if body and headers.get("Content-Type", "").startswith("text/event-plain"):
                for line in body.splitlines():
                    if ": " in line:
                        k, v = line.split(": ", 1)
                        headers[k.strip()] = v.strip()

            return ESLEvent(headers, body)
        finally:
            try:
                self.sock.settimeout(old)
            except OSError:
                pass
