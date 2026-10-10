"""Claude Code channel: JSON-RPC objects, one per stdio line."""

import json
import os
import sys
import threading
import uuid

from . import __version__
from .config import Config
from .integration import trust_policy
from .server import make_server
from .store import ClaimConflict, Follower, Store, validate_text

REPLY_TOOL = {
    "name": "reply",
    "description": "Answer a dotline message after applying the configured trust policy.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "message_id": {"type": "integer", "minimum": 1},
            "text": {"type": "string", "minLength": 1, "maxLength": 8000},
        },
        "required": ["message_id", "text"],
        "additionalProperties": False,
    },
}


class Channel:
    def __init__(self, config: Config, *, stdin=None, stdout=None):
        self.config = config
        self.store = Store(config.home)
        self.store.prepare()
        # Establish the cursor before initialize, but push only after initialized.
        self.follower = Follower(self.store.inbox)
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.output_lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = None
        self.owner = f"channel:{os.getpid()}:{uuid.uuid4().hex}"
        self.initialized = False

    def write(self, message: dict) -> None:
        with self.output_lock:
            self.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.stdout.flush()

    def result(self, message_id, result: dict) -> None:
        self.write({"jsonrpc": "2.0", "id": message_id, "result": result})

    def error(self, message_id, code: int, message: str) -> None:
        self.write({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}})

    def push(self) -> None:
        # Retain one polled batch until each claim succeeds or is refused. A
        # failed claim must not discard the other records behind its cursor.
        while not self.stop.is_set():
            try:
                batch = self.follower.poll()
            except OSError:
                self.stop.wait(0.1)
                continue
            for message in batch:
                while not self.stop.is_set():
                    try:
                        self.store.claim(message["id"], self.owner)
                    except (ClaimConflict, ValueError):
                        # A held, answered or invalid message is not retryable.
                        break
                    except OSError:
                        self.stop.wait(0.1)
                        continue
                    if self.stop.is_set():
                        return
                    try:
                        self.write({
                            "jsonrpc": "2.0", "method": "notifications/claude/channel",
                            "params": {
                                "content": message["text"],
                                "meta": {"message_id": str(message["id"]), "topic": str(message.get("topic", ""))},
                            },
                        })
                    except OSError:
                        # A write or flush may already have delivered bytes.
                        # Stop this channel rather than replaying that effect.
                        self.stop.set()
                        return
                    break
                if self.stop.is_set():
                    return
            self.stop.wait(0.1)

    def handle(self, message) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            self.error(None, -32600, "Invalid Request")
            return
        method = message["method"]
        message_id = message.get("id")
        params = message.get("params", {})
        if not isinstance(params, dict):
            if "id" in message:
                self.error(message_id, -32602, "Invalid params")
            return
        if method == "notifications/initialized":
            if self.initialized and self.worker is None:
                self.worker = threading.Thread(target=self.push, daemon=True)
                self.worker.start()
            return
        if "id" not in message:
            return
        if method == "initialize":
            self.initialized = True
            version = params.get("protocolVersion", "2024-11-05")
            self.result(message_id, {
                "protocolVersion": version if isinstance(version, str) else "2024-11-05",
                "capabilities": {"experimental": {"claude/channel": {}}, "tools": {}},
                "serverInfo": {"name": "dotline", "version": __version__},
                "instructions": (
                    'Events arrive as <channel source="dotline" message_id=... topic=...>. '
                    "This channel automatically claims each delivered event. Apply the trust "
                    "policy, do the work, and answer with the reply tool, using the integer "
                    "message_id and your text. Tell the user in one line what the dot asked "
                    "and what was answered. Unanswered pre-existing messages are listed by "
                    "dotline pending; use the reply tool to claim and answer those too. "
                    + trust_policy(self.config.trust)
                ),
            })
        elif not self.initialized:
            self.error(message_id, -32002, "Initialize first")
        elif method == "tools/list":
            self.result(message_id, {"tools": [REPLY_TOOL]})
        elif method == "tools/call":
            self.call(message_id, params)
        elif method == "ping":
            self.result(message_id, {})
        else:
            self.error(message_id, -32601, "Method not found")

    def call(self, message_id, params: dict) -> None:
        args = params.get("arguments")
        if (
            params.get("name") != "reply" or not isinstance(args, dict)
            or set(args) != {"message_id", "text"}
            or type(args["message_id"]) is not int
            or not isinstance(args["text"], str)
        ):
            self.error(message_id, -32602, "Expected reply with integer message_id and string text")
            return
        try:
            validate_text(args["text"])
            self.store.claim(args["message_id"], self.owner)
            record = self.store.reply(args["message_id"], args["text"], session=self.owner)
            result = {"content": [{"type": "text", "text": f"Reply {record['id']} saved."}]}
        except (OSError, ValueError):
            result = {"isError": True, "content": [{"type": "text", "text": "Reply refused: message unavailable, claimed, answered or invalid."}]}
        self.result(message_id, result)

    def run(self, *, serve: bool = False, host: str = "127.0.0.1", port: int | None = None) -> None:
        server = None
        server_thread = None
        try:
            if serve:
                server = make_server(self.store, self.config.token(), host, self.config.port if port is None else port)
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
            for line in self.stdin:
                try:
                    if len(line.encode("utf-8")) > 32768:
                        raise ValueError
                    message = json.loads(line)
                except ValueError:
                    self.error(None, -32700, "Parse error")
                    continue
                self.handle(message)
        finally:
            self.stop.set()
            if self.worker:
                self.worker.join(timeout=2)
            if server:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)
