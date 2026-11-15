"""Client for the r7sd API over its Unix socket."""

from __future__ import annotations

import http.client
import json
import socket

from .daemon import default_socket


class DaemonUnavailable(Exception):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("r7sd", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class Client:
    def __init__(self, socket_path: str | None = None, timeout: float = 30.0):
        self.path = socket_path or default_socket()
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        conn = _UnixConnection(self.path, self.timeout)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if payload else {}
            try:
                conn.request(method, path, body=payload, headers=headers)
            except (FileNotFoundError, ConnectionRefusedError) as e:
                raise DaemonUnavailable(
                    f"r7sd is not running (no daemon at {self.path}). "
                    "Start it with `systemctl --user start r7sd` or run `r7sd`."
                ) from e
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, (json.loads(data) if data else {})
        finally:
            conn.close()

    def get(self, path: str) -> tuple[int, dict]:
        return self.request("GET", path)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        return self.request("POST", path, body)
