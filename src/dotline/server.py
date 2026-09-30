"""Bounded, authenticated HTTP API. Logs contain fixed route names only."""

import hmac
import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .store import Store, validate_client_id

LOG = logging.getLogger("dotline.http")
ROUTES = {"/v1/health", "/v1/messages", "/v1/replies"}
BODY_LIMIT = 16384


class RateLimit:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.recent = deque()
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            now = self.clock()
            while self.recent and self.recent[0] <= now - 60:
                self.recent.popleft()
            if len(self.recent) >= 30:
                return False
            self.recent.append(now)
            return True


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store: Store, token: str):
        self.store = store
        self.token = token
        self.limiter = RateLimit()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "dotline"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_request(self, code="-", size="-"):
        try:
            path = urlsplit(getattr(self, "path", "")).path
        except ValueError:
            path = "unknown"
        route = path if path in ROUTES else "unknown"
        command = getattr(self, "command", None)
        method = command if command in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"} else "OTHER"
        LOG.info("%s %s %s", method, route, code)

    def log_message(self, format, *args):
        # BaseHTTPServer includes raw request lines in error logs. Suppress them.
        pass

    def _respond(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        if status == 429:
            self.send_header("Retry-After", "60")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        # Parser errors are JSON too, and never echo the offending input.
        self._respond(code, {"error": "invalid HTTP request"})

    def _handle(self):
        if not self.server.limiter.allow():
            self._respond(429, {"error": "rate limit exceeded"})
            return
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self._respond(400, {"error": "invalid path"})
            return
        if parsed.path != "/v1/health":
            headers = self.headers.get_all("Authorization", [])
            provided = headers[0] if len(headers) == 1 else ""
            expected = "Bearer " + self.server.token
            if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
                self._respond(401, {"error": "authentication required"})
                return
        if parsed.path not in ROUTES:
            self._respond(404, {"error": "unknown path"})
            return
        allowed = {"GET", "POST"} if parsed.path == "/v1/messages" else {"GET"}
        if self.command not in allowed:
            self._respond(405, {"error": "method not allowed"})
            return
        if parsed.path == "/v1/health":
            self._respond(200, {"ok": True})
            return
        if self.command == "POST":
            self._post()
            return
        try:
            query = parse_qs(parsed.query, keep_blank_values=True)
            values = query.get("after", ["0"])
            if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
                raise ValueError
            after = int(values[0])
            if parsed.path == "/v1/messages":
                payload = {"messages": self.server.store.messages_after(after)}
            else:
                payload = {"replies": self.server.store.replies_after(after)}
        except ValueError:
            self._respond(400, {"error": "after must be a nonnegative integer"})
            return
        self._respond(200, payload)

    def _post(self):
        lengths = self.headers.get_all("Content-Length", [])
        try:
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                raise ValueError
            length = int(lengths[0])
            if not 1 <= length <= BODY_LIMIT:
                raise ValueError
        except ValueError:
            self._respond(413, {"error": "body must contain 1 to 16384 bytes"})
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._respond(400, {"error": "transfer encoding is not supported"})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
            client_id = validate_client_id(body["client_id"]) if "client_id" in body else None
            record, duplicate = self.server.store.send_once(body.get("text"), body.get("topic", ""), client_id)
        except (ValueError, UnicodeDecodeError):
            self._respond(
                400,
                {"error": "expected text of 1 to 8000 characters, topic of at most 120 "
                          "and an optional client_id of 1 to 64 characters"},
            )
            return
        except TimeoutError:
            self._respond(408, {"error": "body read timed out"})
            return
        if duplicate:
            # The same client_id was already stored: nothing was created. Answer with the original.
            self._respond(200, {"id": record["id"], "ts": record["ts"], "duplicate": True})
            return
        self._respond(201, {"id": record["id"], "ts": record["ts"]})

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)

    def _dispatch(self):
        try:
            self._handle()
        except (OSError, ValueError):
            # Do not disclose local paths, submitted text or headers in errors.
            try:
                self._respond(500, {"error": "mailbox unavailable"})
            except OSError:
                pass


def make_server(store: Store, token: str, host: str = "127.0.0.1", port: int = 8790) -> ApiServer:
    store.prepare()
    return ApiServer((host, port), store, token)
