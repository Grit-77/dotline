import io
import json
import queue
import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest

from dotline.channel import Channel


class QueueOutput:
    def __init__(self):
        self.lines = queue.Queue()

    def write(self, value):
        self.lines.put(json.loads(value))

    def flush(self):
        pass


def initialize(channel):
    channel.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})


@contextmanager
def pushing(channel):
    """Run the real push loop with bounded cleanup and observable failures."""
    failures = []

    def push():
        try:
            channel.push()
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=push, daemon=True)
    worker.start()
    try:
        yield worker
    finally:
        channel.stop.set()
        worker.join(timeout=1)
        assert not worker.is_alive(), "channel worker did not stop"
        assert not failures, failures


@pytest.mark.parametrize("claimed_before_error", [False, True])
def test_channel_retries_claim_without_losing_batch_or_repeating_notifications(
    mailbox, monkeypatch, claimed_before_error,
):
    config, store = mailbox
    store.send("Startup backlog")
    output = QueueOutput()
    channel = Channel(config, stdout=output)
    store.send("First new event")
    store.send("Second new event")
    store.send("Third new event")
    claim = channel.store.claim
    failed = False

    def transient_claim(message_id, owner):
        nonlocal failed
        if message_id == 3 and not failed:
            failed = True
            if claimed_before_error:
                claim(message_id, owner)
            raise OSError("temporary storage failure")
        return claim(message_id, owner)

    monkeypatch.setattr(channel.store, "claim", transient_claim)
    with pushing(channel):
        notifications = [output.lines.get(timeout=2) for _ in range(3)]
        assert [item["params"]["meta"]["message_id"] for item in notifications] == ["2", "3", "4"]
        store.send("Later arrival")
        assert output.lines.get(timeout=2)["params"]["meta"]["message_id"] == "5"
    assert output.lines.empty()
    assert [item["claimed_by"] for item in store.pending()] == [
        None, channel.owner, channel.owner, channel.owner, channel.owner,
    ]


def test_channel_recovers_after_transient_follower_poll_error(mailbox, monkeypatch):
    config, store = mailbox
    store.send("Startup backlog")
    output = QueueOutput()
    channel = Channel(config, stdout=output)
    poll = channel.follower.poll
    failed = False

    def transient_poll():
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("temporary inbox read failure")
        return poll()

    monkeypatch.setattr(channel.follower, "poll", transient_poll)
    store.send("New event")
    with pushing(channel):
        assert output.lines.get(timeout=2)["params"]["meta"]["message_id"] == "2"
        store.send("Later arrival")
        assert output.lines.get(timeout=2)["params"]["meta"]["message_id"] == "3"
    assert output.lines.empty()


@pytest.mark.parametrize("operation", ["poll", "claim"])
def test_channel_stop_interrupts_persistent_storage_retry(mailbox, monkeypatch, operation):
    config, store = mailbox
    output = QueueOutput()
    channel = Channel(config, stdout=output)
    store.send("New event")
    failed = threading.Event()

    def unavailable(*args):
        failed.set()
        raise OSError("storage unavailable")

    target = channel.follower if operation == "poll" else channel.store
    monkeypatch.setattr(target, operation, unavailable)
    with pushing(channel) as worker:
        assert failed.wait(timeout=1)
        channel.stop.set()
        worker.join(timeout=0.5)
        assert not worker.is_alive()
    assert output.lines.empty()


@pytest.mark.parametrize("operation", ["write", "flush"])
def test_channel_output_failure_stops_without_replaying_side_effects(mailbox, operation):
    config, store = mailbox

    class FailingOutput(QueueOutput):
        def write(self, value):
            super().write(value)
            if operation == "write":
                raise BrokenPipeError("output closed")

        def flush(self):
            if operation == "flush":
                raise BrokenPipeError("output closed")

    output = FailingOutput()
    channel = Channel(config, stdout=output)
    store.send("First event")
    store.send("Second event")
    channel.push()
    assert channel.stop.is_set()
    assert output.lines.get_nowait()["params"]["meta"]["message_id"] == "1"
    assert output.lines.empty()
    assert [item["claimed_by"] for item in store.pending()] == [channel.owner, None]


def test_channel_stop_prevents_remaining_batch_notifications(mailbox):
    config, store = mailbox

    class StoppingOutput(QueueOutput):
        def write(self, value):
            super().write(value)
            channel.stop.set()

    output = StoppingOutput()
    channel = Channel(config, stdout=output)
    store.send("First event")
    store.send("Second event")
    channel.push()
    assert output.lines.get_nowait()["params"]["meta"]["message_id"] == "1"
    assert output.lines.empty()
    assert [item["claimed_by"] for item in store.pending()] == [channel.owner, None]


def test_channel_skips_conflicted_claim_and_delivers_remaining_batch(mailbox):
    config, store = mailbox
    output = QueueOutput()
    channel = Channel(config, stdout=output)
    store.send("First event")
    store.send("Held event")
    store.send("Third event")
    store.claim(2, "other-session")
    with pushing(channel):
        notifications = [output.lines.get(timeout=2) for _ in range(2)]
        assert [item["params"]["meta"]["message_id"] for item in notifications] == ["1", "3"]
    assert output.lines.empty()
    assert [item["claimed_by"] for item in store.pending()] == [
        channel.owner, "other-session", channel.owner,
    ]


def test_channel_initialize_has_capability_and_policy(mailbox):
    output = QueueOutput()
    channel = Channel(mailbox[0], stdout=output)
    initialize(channel)
    result = output.lines.get(timeout=2)["result"]
    assert result["capabilities"] == {"experimental": {"claude/channel": {}}, "tools": {}}
    assert result["serverInfo"] == {"name": "dotline", "version": "0.1.1"}
    assert '<channel source="dotline" message_id=... topic=...>' in result["instructions"]
    assert "reply tool" in result["instructions"] and "Trust mode: data" in result["instructions"]


def test_channel_lists_only_reply_and_call_appends(mailbox):
    config, store = mailbox
    store.send("Pending before channel start")
    output = QueueOutput()
    channel = Channel(config, stdout=output)
    initialize(channel)
    output.lines.get(timeout=2)
    channel.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = output.lines.get(timeout=2)["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["reply"]
    assert tools[0]["inputSchema"]["required"] == ["message_id", "text"]
    channel.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "reply", "arguments": {"message_id": 1, "text": "Ready"}}})
    response = output.lines.get(timeout=2)
    assert response["id"] == 3
    assert response["result"]["content"][0]["type"] == "text"
    assert store.replies_after()[0]["text"] == "Ready"


def test_new_inbox_line_becomes_channel_notification_with_string_meta(mailbox):
    config, store = mailbox
    output = QueueOutput()
    channel = Channel(config, stdout=output)
    initialize(channel)
    output.lines.get(timeout=2)
    channel.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    try:
        message = store.send("New event", "review")
        notification = output.lines.get(timeout=3)
        assert notification == {
            "jsonrpc": "2.0", "method": "notifications/claude/channel",
            "params": {"content": "New event", "meta": {"message_id": str(message["id"]), "topic": "review"}},
        }
        assert all(isinstance(value, str) for value in notification["params"]["meta"].values())
        assert store.pending()[0]["claimed_by"] == channel.owner
    finally:
        channel.stop.set()
        channel.worker.join(timeout=2)


@pytest.mark.parametrize("arguments", [{"message_id": True, "text": "bad"}, {"message_id": "1", "text": "bad"}, {"message_id": 1}, {"message_id": 1, "text": "bad", "other": 1}])
def test_channel_rejects_invalid_tool_arguments(mailbox, arguments):
    output = QueueOutput()
    channel = Channel(mailbox[0], stdout=output)
    initialize(channel)
    output.lines.get(timeout=2)
    channel.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "reply", "arguments": arguments}})
    assert output.lines.get(timeout=2)["error"]["code"] == -32602
    assert mailbox[1].replies_after() == []


def test_channel_does_not_take_another_sessions_claim(mailbox):
    config, store = mailbox
    store.send("Question")
    store.claim(1, "other-session")
    output = QueueOutput()
    channel = Channel(config, stdout=output)
    initialize(channel)
    output.lines.get(timeout=2)
    channel.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "reply", "arguments": {"message_id": 1, "text": "Must not append"}}})
    assert output.lines.get(timeout=2)["result"]["isError"] is True
    assert store.replies_after() == []


def test_channel_stdio_parse_errors_and_notifications_do_not_get_replies(mailbox):
    output = io.StringIO()
    input_data = io.StringIO('not-json\n' + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + '\n' + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + '\n')
    Channel(mailbox[0], stdin=input_data, stdout=output).run()
    lines = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(lines) == 2
    assert lines[0]["error"]["code"] == -32700
    assert lines[1]["id"] == 1


def test_channel_serve_starts_and_stops_api_in_same_process(mailbox, monkeypatch):
    stopped = threading.Event()
    started = threading.Event()
    called = []

    class FakeServer:
        def serve_forever(self):
            started.set()
            stopped.wait(3)

        def shutdown(self):
            stopped.set()

        def server_close(self):
            called.append("closed")

    def factory(store, token, host, port):
        assert store.inbox == mailbox[1].inbox
        assert token == mailbox[0].token()
        called.append((host, port))
        return FakeServer()

    monkeypatch.setattr("dotline.channel.make_server", factory)
    Channel(mailbox[0], stdin=io.StringIO(""), stdout=io.StringIO()).run(serve=True, port=0)
    assert started.is_set() and stopped.is_set()
    assert called == [("127.0.0.1", 0), "closed"]


def test_channel_real_stdio_flushes_protocol_and_events(mailbox):
    _, store = mailbox
    process = subprocess.Popen(
        [sys.executable, "-m", "dotline", "channel"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    received = queue.Queue()

    def read():
        for line in process.stdout:
            received.put(json.loads(line))

    thread = threading.Thread(target=read, daemon=True)
    thread.start()

    def send(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert received.get(timeout=5)["id"] == 1
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        store.send("From the dot", "stdio")
        assert received.get(timeout=5)["method"] == "notifications/claude/channel"
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "reply", "arguments": {"message_id": 1, "text": "From Claude"}}})
        assert received.get(timeout=5)["id"] == 2
        assert store.replies_after()[0]["text"] == "From Claude"
    finally:
        process.stdin.close()
        process.wait(timeout=5)
        thread.join(timeout=2)
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize("reply_text", ["café", "Türkçe 你好 😁"])
def test_channel_real_stdio_reads_utf8_replies_under_a_non_utf8_locale(
    mailbox, monkeypatch, reply_text,
):
    _, store = mailbox
    store.send("Question")
    # Windows pipes commonly default to a legacy locale encoding. Force that
    # startup encoding on every platform while sending the MCP UTF-8 protocol.
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1254")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "reply", "arguments": {"message_id": 1, "text": reply_text},
            },
        },
    ]
    payload = "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in messages)
    process = subprocess.run(
        [sys.executable, "-m", "dotline", "channel"], input=payload.encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
    )
    assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
    responses = [json.loads(line) for line in process.stdout.decode("utf-8").splitlines()]
    assert responses[-1]["id"] == 2
    assert not responses[-1]["result"].get("isError", False)
    assert store.replies_after()[0]["text"] == reply_text
