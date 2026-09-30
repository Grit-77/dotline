"""JSONL mailbox, process locks and exclusive, expiring claims."""

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import private_dir, private_write

CLAIM_SECONDS = 30 * 60
CLIENT_ID_MAX = 64
_thread_lock = threading.RLock()


class ClaimConflict(ValueError):
    """The message is already answered or held by another session."""


def validate_text(text: object) -> str:
    if not isinstance(text, str) or not 1 <= len(text) <= 8000:
        raise ValueError("text must contain 1 to 8000 characters")
    return text


def validate_client_id(client_id: object) -> str:
    if not isinstance(client_id, str) or not 1 <= len(client_id) <= CLIENT_ID_MAX:
        raise ValueError(f"client_id must contain 1 to {CLIENT_ID_MAX} characters")
    return client_id


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, home: Path):
        self.box = home / "box"
        self.inbox = self.box / "inbox.jsonl"
        self.replies = self.box / "replies.jsonl"
        self.claims = self.box / "claims"

    def prepare(self) -> None:
        private_dir(self.box)
        private_dir(self.claims)
        for path in (self.inbox, self.replies, self.box / ".lock"):
            try:
                private_write(path, b"", exclusive=True)
            except FileExistsError:
                if os.name != "nt":
                    path.chmod(0o600)
            except PermissionError:
                # Windows: another process is creating the same file at this moment (sharing violation).
                if not path.exists():
                    raise

    @contextmanager
    def locked(self):
        self.prepare()
        with _thread_lock, (self.box / ".lock").open("r+b") as lock:
            if os.name == "nt":
                import msvcrt

                # Lock byte 0 without reading it first: while another process holds the lock, Windows
                # refuses reads of that byte (PermissionError), and a region past the end of the file
                # can be locked, so the file never needs content.
                lock.seek(0)
                # LK_NBLCK plus retry avoids msvcrt's ten-second lock limit.
                while True:
                    try:
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.02)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read(path: Path) -> list[dict]:
        if not path.exists():
            return []
        records = []
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                # A torn final write must not hide earlier durable messages.
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and type(record.get("id")) is int:
                    records.append(record)
        return records

    @staticmethod
    def _append(path: Path, record: dict) -> None:
        with path.open("ab") as stream:
            # Repair an incomplete tail before adding the next complete record.
            with path.open("rb") as reader:
                reader.seek(0, os.SEEK_END)
                if reader.tell():
                    reader.seek(-1, os.SEEK_END)
                    if reader.read(1) != b"\n":
                        stream.write(b"\n")
            stream.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())

    def messages_after(self, after: int = 0) -> list[dict]:
        with self.locked():
            return [item for item in self._read(self.inbox) if item["id"] > after]

    def replies_after(self, after: int = 0) -> list[dict]:
        with self.locked():
            return [item for item in self._read(self.replies) if item["id"] > after]

    def send(self, text: str, topic: str = "", client_id: str | None = None) -> dict:
        return self.send_once(text, topic, client_id)[0]

    def send_once(self, text: str, topic: str = "", client_id: str | None = None) -> tuple[dict, bool]:
        """Append a message; return (record, duplicate).

        A client_id that is already in the inbox appends nothing and returns the original
        record. The lookup and the append share one lock hold, so concurrent sends of the same
        client_id, even from different processes, still produce exactly one record.
        """
        validate_text(text)
        if not isinstance(topic, str) or len(topic) > 120:
            raise ValueError("topic must be a string of at most 120 characters")
        if client_id is not None:
            validate_client_id(client_id)
        with self.locked():
            records = self._read(self.inbox)
            if client_id is not None:
                for item in records:
                    if item.get("client_id") == client_id:
                        return item, True
            record = {
                "id": max((item["id"] for item in records), default=0) + 1,
                "ts": timestamp(), "text": text, "topic": topic,
            }
            if client_id is not None:
                record["client_id"] = client_id
            self._append(self.inbox, record)
            return record, False

    @staticmethod
    def _valid_id(message_id: int) -> None:
        if type(message_id) is not int or message_id < 1:
            raise ValueError("message id must be a positive integer")

    def _unanswered(self, message_id: int) -> None:
        self._valid_id(message_id)
        if not any(item["id"] == message_id for item in self._read(self.inbox)):
            raise ValueError("message does not exist")
        if any(item.get("to") == message_id for item in self._read(self.replies)):
            raise ClaimConflict("message is already answered")

    def _holder(self, message_id: int) -> dict | None:
        path = self.claims / str(message_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - record["at"] < CLAIM_SECONDS:
                return record
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError):
            # Fail closed on a corrupt claim instead of silently stealing it.
            raise ValueError("claim file is invalid") from None
        return None

    def claim(self, message_id: int, session: str) -> None:
        if not isinstance(session, str) or not 1 <= len(session) <= 200:
            raise ValueError("session must contain 1 to 200 characters")
        with self.locked():
            self._unanswered(message_id)
            holder = self._holder(message_id)
            if holder:
                if holder["by"] != session:
                    raise ClaimConflict("message is held by another session")
                return
            path = self.claims / str(message_id)
            path.unlink(missing_ok=True)
            private_write(
                path, json.dumps({"by": session, "at": time.time()}).encode(), exclusive=True
            )

    def reply(self, message_id: int, text: str, *, session: str | None = None) -> dict:
        validate_text(text)
        with self.locked():
            self._unanswered(message_id)
            holder = self._holder(message_id)
            if session is not None and (holder is None or holder["by"] != session):
                raise ClaimConflict("message is not held by this session")
            records = self._read(self.replies)
            record = {
                "id": max((item["id"] for item in records), default=0) + 1,
                "ts": timestamp(), "to": message_id, "text": text,
            }
            self._append(self.replies, record)
            return record

    def pending(self) -> list[dict]:
        with self.locked():
            answered = {item.get("to") for item in self._read(self.replies)}
            result = []
            for item in self._read(self.inbox):
                if item["id"] not in answered:
                    holder = self._holder(item["id"])
                    result.append({**item, "claimed_by": holder["by"] if holder else None})
            return result


class Follower:
    """Only new complete lines; detect replacement, truncation and rewritten tails."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.identity = None
        self.anchor = b""
        self.partial = b""
        try:
            with path.open("rb") as stream:
                self.offset = stream.seek(0, os.SEEK_END)
                self.identity = self._identity(os.fstat(stream.fileno()))
                self.anchor = self._anchor(stream)
        except FileNotFoundError:
            pass

    @staticmethod
    def _identity(stat):
        return stat.st_dev, stat.st_ino

    def _anchor(self, stream) -> bytes:
        start = max(0, self.offset - 128)
        stream.seek(start)
        return stream.read(self.offset - start)

    def poll(self) -> list[dict]:
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = self._identity(stat)
                if (
                    identity != self.identity
                    or stat.st_size < self.offset
                    or self._anchor(stream) != self.anchor
                ):
                    self.offset = 0
                    self.partial = b""
                self.identity = identity
                stream.seek(self.offset)
                data = stream.read()
                self.offset = stream.tell()
                self.anchor = self._anchor(stream)
        except FileNotFoundError:
            return []
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        records = []
        for line in lines:
            try:
                item = json.loads(line)
                if isinstance(item, dict) and type(item.get("id")) is int:
                    records.append(item)
            except (ValueError, UnicodeDecodeError):
                continue
        return records

    def follow(self, stop: threading.Event, interval: float = 0.1):
        while not stop.is_set():
            yield from self.poll()
            stop.wait(interval)
