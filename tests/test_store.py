import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from dotline.store import CLAIM_SECONDS, ClaimConflict, Follower


def test_claim_exclusivity_and_reclaim_by_same_session(mailbox):
    _, store = mailbox
    message = store.send("Review this")
    store.claim(message["id"], "one")
    store.claim(message["id"], "one")
    with pytest.raises(ClaimConflict):
        store.claim(message["id"], "two")
    assert store.pending()[0]["claimed_by"] == "one"


def test_claim_lapses_after_30_minutes(mailbox, monkeypatch):
    _, store = mailbox
    store.send("Long task")
    now = time.time()
    monkeypatch.setattr("dotline.store.time.time", lambda: now)
    store.claim(1, "one")
    now += CLAIM_SECONDS - 1
    with pytest.raises(ClaimConflict):
        store.claim(1, "two")
    now += 1
    assert store.pending()[0]["claimed_by"] is None
    store.claim(1, "two")
    assert store.pending()[0]["claimed_by"] == "two"


@pytest.mark.parametrize("operation", ["claim", "reply", "reply_by", "pending"])
@pytest.mark.parametrize(
    "damaged",
    [
        b'{"by": "original", "at": NaN}',
        b'{"by": "original", "at": Infinity}',
        b'{"by": "original", "at": -Infinity}',
        b'{"by": "original", "at": 1e400}',
        ('{"by": "original", "at": ' + "9" * 400 + "}").encode(),
        b'{"by": "original", "at": -1}',
        b'{"by": "original", "at": true}',
        b'{"by": "original", "at": false}',
        b'{"by": "original", "at": "2000000000"}',
        b'{"by": "original", "at": null}',
        b'{"by": "original", "at": []}',
        b'{"by": "original", "at": {}}',
        b'{"by": "original"}',
        b'{"at": 2000000000}',
        b'{"at": 0}',
        b'{"by": null, "at": 2000000000}',
        b'{"by": false, "at": 2000000000}',
        b'{"by": 7, "at": 2000000000}',
        b'{"by": [], "at": 2000000000}',
        b'{"by": {}, "at": 2000000000}',
        b'{"by": "", "at": 2000000000}',
        b'{"by": "", "at": 0}',
        ('{"by": "' + "x" * 201 + '", "at": 2000000000}').encode(),
        ('{"by": "' + "x" * 201 + '", "at": 0}').encode(),
        b"{}",
        b"[]",
        b'["original", 2000000000]',
        b'"original"',
        b"42",
        b"true",
        b"null",
        b'{"by": "original",',
        b'{"by": "\xff", "at": 2000000000}',
        b"[" * 20000 + b"0" + b"]" * 20000,
    ],
    ids=[
        "nan", "infinity", "negative_infinity", "overflow_float", "huge_integer",
        "negative_time", "true_time", "false_time", "string_time", "null_time",
        "list_time", "object_time", "missing_time", "missing_holder",
        "expired_missing_holder", "null_holder", "bool_holder", "number_holder",
        "list_holder", "object_holder", "empty_holder", "expired_empty_holder",
        "long_holder", "expired_long_holder", "empty_object", "empty_list", "list",
        "string", "number", "bool", "null", "torn_json", "invalid_utf8", "deep_json",
    ],
)
def test_corrupt_claim_is_refused_without_mutating_mailbox(
    mailbox, monkeypatch, operation, damaged,
):
    _, store = mailbox
    monkeypatch.setattr("dotline.store.time.time", lambda: 2000000000)
    store.send("Unanswered question")
    claim_path = store.claims / "1"
    claim_path.write_bytes(damaged)
    inbox_before = store.inbox.read_bytes()
    replies_before = store.replies.read_bytes()

    with pytest.raises(ValueError) as caught:
        if operation == "claim":
            store.claim(1, "original")
        elif operation == "reply":
            store.reply(1, "Answer")
        elif operation == "reply_by":
            store.reply(1, "Answer", session="original")
        else:
            store.pending()

    assert type(caught.value) is ValueError
    assert str(caught.value) == "claim file is invalid"
    assert claim_path.read_bytes() == damaged
    assert store.inbox.read_bytes() == inbox_before
    assert store.replies.read_bytes() == replies_before


@pytest.mark.parametrize("holder", ["legacy", "x" * 200, "\u00e7\U0001f600", " "])
@pytest.mark.parametrize("at", [2000000000, 1999999999.5, 2000000001.0])
def test_valid_legacy_claim_keeps_holder_and_same_session_does_not_renew(
    mailbox, monkeypatch, holder, at,
):
    _, store = mailbox
    monkeypatch.setattr("dotline.store.time.time", lambda: 2000000000)
    store.send("Question")
    claim_path = store.claims / "1"
    stored = json.dumps({"by": holder, "at": at, "legacy": True}).encode()
    claim_path.write_bytes(stored)

    store.claim(1, holder)
    assert claim_path.read_bytes() == stored
    assert store.pending()[0]["claimed_by"] == holder
    with pytest.raises(ClaimConflict):
        store.claim(1, "another session")
    assert store.reply(1, "Answer", session=holder)["to"] == 1


@pytest.mark.parametrize("at", [0, 0.0, 1999998200])
def test_valid_expired_legacy_claim_can_be_replaced(mailbox, monkeypatch, at):
    _, store = mailbox
    monkeypatch.setattr("dotline.store.time.time", lambda: 2000000000)
    store.send("Question")
    (store.claims / "1").write_text(json.dumps({"by": "legacy", "at": at}), encoding="utf-8")

    assert store.pending()[0]["claimed_by"] is None
    store.claim(1, "new session")
    assert store.pending()[0]["claimed_by"] == "new session"
    assert store.reply(1, "Answer", session="new session")["to"] == 1


@pytest.mark.parametrize("expired", [False, True])
def test_claim_partial_write_failure_preserves_target_and_retry_succeeds(
    mailbox, monkeypatch, expired,
):
    _, store = mailbox
    monkeypatch.setattr("dotline.store.time.time", lambda: 2000000000)
    store.send("Question")
    target = store.claims / "1"
    old_claim = b'{"by": "old session", "at": 0}'
    if expired:
        target.write_bytes(old_claim)
    unrelated = store.claims / ".unrelated.tmp"
    unrelated.write_bytes(b"another attempt's file")
    expected_names = {unrelated.name, "1"} if expired else {unrelated.name}
    original_fdopen = os.fdopen
    failed = False

    class PartialWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, data):
            nonlocal failed
            if not failed and data.startswith(b'{"by":'):
                failed = True
                if os.name != "nt":
                    assert os.fstat(self.stream.fileno()).st_mode & 0o777 == 0o600
                self.stream.write(data[:10])
                self.stream.flush()
                raise OSError("interrupted claim write")
            return self.stream.write(data)

    monkeypatch.setattr("dotline.store.os.fdopen", lambda *args, **kwargs: PartialWrite(
        original_fdopen(*args, **kwargs),
    ))
    with pytest.raises(OSError, match="interrupted claim write"):
        store.claim(1, "new session")

    assert {path.name for path in store.claims.iterdir()} == expected_names
    if expired:
        assert target.read_bytes() == old_claim
    else:
        assert not target.exists()
    assert store.pending()[0]["claimed_by"] is None
    store.claim(1, "new session")
    assert store.pending()[0]["claimed_by"] == "new session"
    assert {path.name for path in store.claims.iterdir()} == {unrelated.name, "1"}
    assert unrelated.read_bytes() == b"another attempt's file"


def test_claim_failed_replace_preserves_expired_claim_and_retry_succeeds(mailbox, monkeypatch):
    _, store = mailbox
    monkeypatch.setattr("dotline.store.time.time", lambda: 2000000000)
    store.send("Question")
    target = store.claims / "1"
    old_claim = b'{"by": "old session", "at": 0}'
    target.write_bytes(old_claim)
    original_replace = os.replace
    failed = False

    def fail_once(source, destination):
        nonlocal failed
        assert destination == target
        assert source.parent == store.claims
        assert json.loads(source.read_bytes()) == {"by": "new session", "at": 2000000000}
        if os.name != "nt":
            assert source.stat().st_mode & 0o777 == 0o600
        if not failed:
            failed = True
            raise OSError("claim replacement failed")
        return original_replace(source, destination)

    monkeypatch.setattr("dotline.store.os.replace", fail_once)
    with pytest.raises(OSError, match="claim replacement failed"):
        store.claim(1, "new session")

    assert target.read_bytes() == old_claim
    assert {path.name for path in store.claims.iterdir()} == {"1"}
    store.claim(1, "new session")
    assert store.pending()[0]["claimed_by"] == "new session"
    assert {path.name for path in store.claims.iterdir()} == {"1"}


def test_claim_fsync_failure_preserves_expired_claim_and_retry_succeeds(mailbox, monkeypatch):
    _, store = mailbox
    monkeypatch.setattr("dotline.store.time.time", lambda: 2000000000)
    store.send("Question")
    target = store.claims / "1"
    old_claim = b'{"by": "old session", "at": 0}'
    target.write_bytes(old_claim)
    original_fsync = os.fsync
    failed = False

    def fail_once(fd):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("claim synchronization failed")
        return original_fsync(fd)

    monkeypatch.setattr("dotline.store.os.fsync", fail_once)
    with pytest.raises(OSError, match="claim synchronization failed"):
        store.claim(1, "new session")

    assert target.read_bytes() == old_claim
    assert {path.name for path in store.claims.iterdir()} == {"1"}
    store.claim(1, "new session")
    assert store.pending()[0]["claimed_by"] == "new session"
    assert {path.name for path in store.claims.iterdir()} == {"1"}


def test_answered_cannot_be_claimed_or_replied_again(mailbox):
    _, store = mailbox
    store.send("Question")
    store.claim(1, "one")
    store.reply(1, "Answer")
    with pytest.raises(ClaimConflict):
        store.claim(1, "one")
    with pytest.raises(ClaimConflict):
        store.reply(1, "Another answer")


def test_pending_excludes_answered_messages(mailbox):
    _, store = mailbox
    store.send("Answered")
    store.send("Waiting")
    store.claim(2, "session")
    store.reply(1, "Done")
    assert [(item["id"], item["claimed_by"]) for item in store.pending()] == [(2, "session")]


def test_reply_ids_are_sequential_per_file(mailbox):
    _, store = mailbox
    store.send("First")
    store.send("Second")
    assert store.reply(2, "Second first")["id"] == 1
    assert store.reply(1, "First second")["id"] == 2


def test_reply_by_requires_current_claim(mailbox):
    _, store = mailbox
    store.send("Question")
    store.claim(1, "one")
    with pytest.raises(ClaimConflict):
        store.reply(1, "Answer", session="two")
    assert store.reply(1, "Answer", session="one")["to"] == 1


@pytest.mark.parametrize("message_id", [0, -1, True, 9])
def test_invalid_or_missing_id_is_refused(mailbox, message_id):
    with pytest.raises(ValueError):
        mailbox[1].claim(message_id, "session")


def test_concurrent_process_claim_uses_exclusive_file(mailbox):
    config, store = mailbox
    store.send("One owner")
    commands = [
        [sys.executable, "-m", "dotline", "claim", "1", "--by", str(index)]
        for index in range(4)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda command: subprocess.run(command, capture_output=True, timeout=10), commands))
    assert sorted(result.returncode for result in results) == [0, 3, 3, 3]
    assert len(list(store.claims.iterdir())) == 1
    if os.name != "nt":
        assert (store.claims / "1").stat().st_mode & 0o777 == 0o600
        assert store.claims.stat().st_mode & 0o777 == 0o700
    assert config.token() not in b"".join(result.stdout + result.stderr for result in results).decode()


def test_watch_only_new_complete_lines_and_truncation(mailbox):
    _, store = mailbox
    store.send("Old")
    follower = Follower(store.inbox)
    assert follower.poll() == []
    new = store.send("New")
    assert follower.poll() == [new]
    assert follower.poll() == []
    store.inbox.write_bytes(b"")
    assert follower.poll() == []
    fresh = store.send("After truncation")
    assert follower.poll() == [fresh]


def test_watch_survives_rewrite_without_observing_empty_file(mailbox):
    _, store = mailbox
    store.send("A" * 100)
    follower = Follower(store.inbox)
    rewritten = {"id": 2, "text": "B" * 200, "topic": "", "ts": "now"}
    store.inbox.write_text(json.dumps(rewritten) + "\n", encoding="utf-8")
    assert follower.poll() == [rewritten]


@pytest.mark.parametrize("change", ["append", "rewrite"])
def test_watch_retry_after_late_read_error_preserves_cursor_and_unread_lines(
    mailbox, monkeypatch, change,
):
    _, store = mailbox
    store.send("Already observed")
    partial = b'{"id": 2, "text": "unfinished"'
    with store.inbox.open("ab") as stream:
        stream.write(partial)
    follower = Follower(store.inbox)
    before = (follower.offset, follower.identity, follower.anchor, follower.partial)
    if change == "append":
        with store.inbox.open("ab") as stream:
            stream.write(b'}\n')
        expected = {"id": 2, "text": "unfinished"}
    else:
        store.inbox.write_bytes(b'{"id": 3, "text": "rewritten"}\n')
        expected = {"id": 3, "text": "rewritten"}

    original_anchor = follower._anchor
    failed = False

    def fail_late_once(stream, offset=None):
        nonlocal failed
        if not failed and stream.tell() == store.inbox.stat().st_size:
            failed = True
            raise OSError("transient anchor read failure")
        if offset is None:
            return original_anchor(stream)
        return original_anchor(stream, offset)

    monkeypatch.setattr(follower, "_anchor", fail_late_once)
    with pytest.raises(OSError, match="transient anchor read failure"):
        follower.poll()
    assert (follower.offset, follower.identity, follower.anchor, follower.partial) == before
    assert follower.poll() == [expected]
    assert follower.poll() == []


def test_watch_buffers_partial_lines_and_file_replacement(mailbox):
    _, store = mailbox
    follower = Follower(store.inbox)
    with store.inbox.open("ab") as stream:
        stream.write(b'{"id": 1, "text": "partial"')
    assert follower.poll() == []
    with store.inbox.open("ab") as stream:
        stream.write(b'}\n')
    assert follower.poll() == [{"id": 1, "text": "partial"}]
    replacement = store.box / "replacement"
    replacement.write_bytes(b'{"id": 2, "text": "replaced"}\n')
    os.replace(replacement, store.inbox)
    assert follower.poll() == [{"id": 2, "text": "replaced"}]


@pytest.mark.parametrize("text", ["in flight", "\U0001f600" * 6000], ids=["short", "multiple_blocks"])
def test_watch_delivers_initial_partial_line_when_completed(mailbox, text):
    _, store = mailbox
    store.send("Old complete message")
    record = {"id": 2, "text": text}
    encoded = json.dumps(record, ensure_ascii=False).encode("utf-8")
    with store.inbox.open("ab") as stream:
        stream.write(encoded[:-1])
    follower = Follower(store.inbox)
    assert follower.poll() == []
    with store.inbox.open("ab") as stream:
        stream.write(encoded[-1:] + b"\n")
    assert follower.poll() == [record]
    assert follower.poll() == []


def test_watch_cli_flushes_without_waiting_for_exit(mailbox):
    _, store = mailbox
    process = subprocess.Popen(
        [sys.executable, "-m", "dotline", "watch"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    lines = []
    ready = threading.Event()

    def reader():
        lines.append(process.stdout.readline())
        ready.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        # Repeated new appends avoid depending on subprocess startup timing.
        deadline = time.monotonic() + 5
        while not ready.wait(0.1) and time.monotonic() < deadline:
            store.send("Flushed event")
        assert ready.is_set(), "watch must flush a complete line while still running"
        assert json.loads(lines[0])["text"] == "Flushed event"
        assert process.poll() is None
    finally:
        process.terminate()
        process.communicate(timeout=5)
        thread.join(timeout=2)


def test_torn_tail_does_not_join_next_record(mailbox):
    _, store = mailbox
    store.send("First")
    with store.inbox.open("ab") as stream:
        stream.write(b'{"id":2,"text":')
    assert store.send("Next")["id"] == 2
    assert [message["text"] for message in store.messages_after()] == ["First", "Next"]


@pytest.mark.parametrize(
    "tail", [b'{"id":2,"text":"\xf0\x9f', b'{"id":2,"text":"\xff"}\n'],
    ids=["torn_character", "complete_invalid_record"],
)
def test_invalid_utf8_inbox_record_preserves_messages_and_safe_retry(mailbox, tail):
    _, store = mailbox
    first = store.send("First", client_id="first-send")
    with store.inbox.open("ab") as stream:
        stream.write(tail)
    assert store.messages_after() == [first]
    damaged_bytes = store.inbox.read_bytes()
    assert store.send_once("Retry", client_id="first-send") == (first, True)
    assert store.inbox.read_bytes() == damaged_bytes
    next_record, duplicate = store.send_once("Next", client_id="next-send")
    assert not duplicate
    assert next_record["id"] == 2
    assert store.send_once("Retry next", client_id="next-send") == (next_record, True)
    assert store.messages_after() == [first, next_record]
    assert store.messages_after(1) == [next_record]


@pytest.mark.parametrize(
    "tail", [b'{"id":2,"to":2,"text":"\xf0\x9f', b'{"id":2,"to":2,"text":"\xff"}\n'],
    ids=["torn_character", "complete_invalid_record"],
)
def test_invalid_utf8_reply_record_preserves_answers_and_allows_next_reply(mailbox, tail):
    _, store = mailbox
    store.send("First question")
    store.send("Second question")
    first = store.reply(1, "First answer")
    with store.replies.open("ab") as stream:
        stream.write(tail)
    assert store.replies_after() == [first]
    assert [message["id"] for message in store.pending()] == [2]
    with pytest.raises(ClaimConflict):
        store.reply(1, "Duplicate answer")
    next_record = store.reply(2, "Second answer")
    assert next_record["id"] == 2
    assert store.replies_after() == [first, next_record]
    assert store.replies_after(1) == [next_record]
    assert store.pending() == []


SEND_AT = """
import sys, time
from pathlib import Path
from dotline.store import Store

home, client_id, start = sys.argv[1], sys.argv[2], float(sys.argv[3])
while time.time() < start:
    time.sleep(0.001)
record, duplicate = Store(Path(home)).send_once("one send", "", client_id)
print(record["id"], duplicate)
"""


def test_concurrent_processes_with_the_same_client_id_leave_one_record(mailbox):
    config, store = mailbox
    start = time.time() + 2
    commands = [
        [sys.executable, "-c", SEND_AT, str(config.home), "same-send", str(start)] for _ in range(4)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda command: subprocess.run(command, capture_output=True, text=True, timeout=30), commands))
    assert [result.returncode for result in results] == [0, 0, 0, 0], [result.stderr for result in results]
    # Every process sees the same message; exactly one of them created it.
    assert sorted(result.stdout.strip() for result in results) == ["1 False", "1 True", "1 True", "1 True"]
    assert [message["client_id"] for message in store.messages_after()] == ["same-send"]
    assert len(store.inbox.read_text(encoding="utf-8").splitlines()) == 1
