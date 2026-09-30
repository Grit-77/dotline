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
