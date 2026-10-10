import io
import json
import os
import uuid
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler

import pytest

from dotline import config as config_module
from dotline.cli import main
from dotline.client import RETRY_PAUSE, Client, ClientError
from dotline.config import config_home, initialize, load_config, parse_config


def test_init_idempotent_prints_only_path_and_protects_files(mailbox, capsys):
    config, store = mailbox
    old_token = config.token()
    old_config = (config.home / "config.toml").read_bytes()
    assert main(["init"]) == 0
    captured = capsys.readouterr()
    assert captured.out == str(config.home / "token") + "\n"
    assert captured.err == "" and old_token not in captured.out
    assert config.token() == old_token
    assert (config.home / "config.toml").read_bytes() == old_config
    if os.name != "nt":
        for path in (config.home, store.box, store.claims):
            assert path.stat().st_mode & 0o777 == 0o700
        for path in (config.home / "token", config.home / "config.toml", store.inbox, store.replies):
            assert path.stat().st_mode & 0o777 == 0o600


def test_init_uses_required_token_generator(tmp_path, monkeypatch):
    called = []

    def generate(count):
        called.append(count)
        return "test-only-generated-bearer"

    monkeypatch.setattr("dotline.config.secrets.token_urlsafe", generate)
    initialize(tmp_path / "new")
    assert called == [36]


def test_home_override_xdg_and_windows(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTLINE_HOME", str(tmp_path / "override"))
    assert config_home() == tmp_path / "override"
    monkeypatch.delenv("DOTLINE_HOME")
    monkeypatch.setattr(config_module, "os", SimpleNamespace(name="posix", environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")}))
    assert config_home() == tmp_path / "xdg" / "dotline"
    monkeypatch.setattr(config_module, "os", SimpleNamespace(name="nt", environ={"APPDATA": str(tmp_path / "appdata")}))
    assert config_home() == tmp_path / "appdata" / "dotline"


def test_config_supports_both_trust_modes_and_scalar_comments(mailbox):
    config, _ = mailbox
    (config.home / "config.toml").write_text("url = 'https://mailbox.example' # URL\nport = 8791\ntrust = \"orders\"\n", encoding="utf-8")
    changed = load_config(config.home)
    assert changed.trust == "orders" and changed.port == 8791
    assert changed.client_url == "https://mailbox.example"
    with pytest.raises(ValueError):
        parse_config('trust = "data"\ntrust = "orders"')


def test_client_url_and_token_file_overrides(mailbox, tmp_path, monkeypatch):
    config, _ = mailbox
    token_path = tmp_path / "client-token"
    token_path.write_text("test-only-override", encoding="utf-8")
    monkeypatch.setenv("DOTLINE_URL", "https://mailbox.example/")
    monkeypatch.setenv("DOTLINE_TOKEN_FILE", str(token_path))
    assert config.client_url == "https://mailbox.example"
    assert config.token() == "test-only-override"


@pytest.mark.parametrize("url", ["http://public.example", "https://user:secret@example.test", "https://example.test?token=anything", "https://example.test/path", "file:///tmp/test", "https://example.test/#fragment"])
def test_client_refuses_insecure_or_credential_bearing_urls(mailbox, monkeypatch, url):
    monkeypatch.setenv("DOTLINE_URL", url)
    with pytest.raises(ValueError):
        _ = mailbox[0].client_url


def test_token_show_requires_flag_and_warns(mailbox, capsys):
    config, _ = mailbox
    with pytest.raises(SystemExit) as error:
        main(["token"])
    assert error.value.code == 2
    assert config.token() not in capsys.readouterr().err
    assert main(["token", "--show"]) == 0
    captured = capsys.readouterr()
    assert "Warning" in captured.err
    assert captured.out.strip() == config.token()


def test_token_never_in_normal_cli_outputs(mailbox, capsys, monkeypatch, tmp_path):
    config, store = mailbox
    store.send("Question")
    for args in (["init"], ["pending"], ["pending", "--json"], ["claim", "1", "--by", "session"], ["reply", "1", "Answer"], ["claim", "1", "--by", "session"]):
        main(args)
        captured = capsys.readouterr()
        assert config.token() not in captured.out + captured.err
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"session"}'))
    assert main(["hook", "session-start"]) == 0
    captured = capsys.readouterr()
    assert config.token() not in captured.out + captured.err
    assert main(["install-hook", "--settings", str(tmp_path / "settings.json")]) == 0
    captured = capsys.readouterr()
    assert config.token() not in captured.out + captured.err


def test_send_file_reply_file_and_client_command_outputs(api, tmp_path, capsys):
    _, config, store = api
    path = tmp_path / "text.txt"
    path.write_text("Multiline\nUnicode: こんにちは", encoding="utf-8")
    assert main(["send", "--file", str(path), "--topic", "files"]) == 0
    assert capsys.readouterr().out.strip() == "1"
    assert store.messages_after()[0]["text"] == path.read_text(encoding="utf-8")
    assert main(["reply", "1", "--file", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "1"
    for args in (["wait", "1"], ["replies", "--after", "0"], ["health"]):
        assert main(args) == 0
        captured = capsys.readouterr()
        assert config.token() not in captured.out + captured.err
    assert main(["send", "text", "--file", str(path)]) == 1


def test_claim_cli_returns_three_for_conflict(mailbox, capsys):
    mailbox[1].send("Question")
    assert main(["claim", "1", "--by", "one"]) == 0
    assert main(["claim", "1", "--by", "two"]) == 3
    assert "held by another session" in capsys.readouterr().err


def test_wait_polls_every_twenty_seconds_without_sockets(mailbox, monkeypatch):
    client = Client(mailbox[0])
    now = [0.0]
    slept = []
    responses = [[], [], [{"id": 1, "to": 7, "text": "Finished"}]]
    monkeypatch.setattr(client, "replies", lambda after, **kwargs: responses.pop(0))

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    assert client.wait(7, 1, clock=lambda: now[0], sleep=sleep)["text"] == "Finished"
    assert slept == [20, 20]


def test_wait_timeout_does_not_sleep_past_deadline(mailbox, monkeypatch):
    client = Client(mailbox[0])
    now = [0.0]
    monkeypatch.setattr(client, "replies", lambda after, **kwargs: [])
    with pytest.raises(ClientError, match="timed out"):
        client.wait(1, 0.1, clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert now[0] == 6


@pytest.mark.parametrize("minutes,expected_timeouts", [(0.1, [6]), (1, [15, 15, 14])])
def test_wait_caps_transport_timeout_and_stops_polling_at_deadline(
    mailbox, minutes, expected_timeouts
):
    client = Client(mailbox[0])
    now = [0.0]
    polls = []

    class EmptyOpener:
        def open(self, request, timeout):
            polls.append((request.full_url, timeout))
            now[0] += 3
            return io.BytesIO(b'{"replies":[]}')

    client.opener = EmptyOpener()
    with pytest.raises(ClientError, match="timed out"):
        client.wait(
            7, minutes, clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
    assert [timeout for _, timeout in polls] == expected_timeouts
    assert now[0] == minutes * 60


def test_wait_accepts_an_immediate_matching_reply_with_short_budget(mailbox):
    client = Client(mailbox[0])

    class ReplyOpener:
        def open(self, request, timeout):
            assert 0 < timeout <= 0.06
            return io.BytesIO(b'{"replies":[{"id":1,"to":7,"text":"Finished"}]}')

    client.opener = ReplyOpener()
    assert client.wait(7, 0.001, clock=lambda: 0)["text"] == "Finished"


@pytest.mark.parametrize("minutes", [float("nan"), float("inf"), -float("inf")])
def test_wait_rejects_nonfinite_minutes_before_polling(mailbox, minutes):
    client = Client(mailbox[0])

    class UnexpectedOpener:
        def open(self, request, timeout):
            pytest.fail("invalid wait budget must not poll")

    client.opener = UnexpectedOpener()
    with pytest.raises(ClientError, match="minutes"):
        client.wait(7, minutes)


def test_redirects_are_not_followed_and_errors_never_echo_bearer(mailbox, capsys):
    config, _ = mailbox
    client = Client(config)
    handler = next(handler for handler in client.opener.handlers if isinstance(handler, HTTPRedirectHandler))
    assert handler.redirect_request(None, None, 302, "redirect", {}, "https://other.example") is None

    class FailedOpener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 401, config.token(), {}, None)

    client.opener = FailedOpener()
    with pytest.raises(ClientError) as error:
        client.replies()
    assert config.token() not in str(error.value)
    assert config.token() not in capsys.readouterr().err


def test_missing_token_file_errors_do_not_print_its_content(mailbox, monkeypatch, capsys):
    monkeypatch.setenv("DOTLINE_TOKEN_FILE", str(mailbox[0].home / "missing"))
    assert main(["send", "test"]) == 1
    assert "file or connection unavailable" in capsys.readouterr().err


def test_package_version_and_runtime_dependencies():
    from dotline import __version__

    assert __version__ == "0.1.1"
    project = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dependencies = []' in project
    assert 'requires-python = ">=3.10"' in project


def is_uuid4(value):
    parsed = uuid.UUID(value)
    return parsed.version == 4 and parsed.variant == uuid.RFC_4122 and str(parsed) == value


class ScriptedOpener:
    """Stands in for urllib's opener: every call consumes one prepared outcome."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.bodies = []

    def open(self, request, timeout):
        self.bodies.append(json.loads(request.data))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return io.BytesIO(json.dumps(outcome).encode("utf-8"))


def test_client_retries_once_with_the_same_client_id_after_a_connection_error(mailbox):
    client = Client(mailbox[0])
    client.opener = ScriptedOpener(ConnectionResetError("reset"), {"id": 4, "ts": "then", "duplicate": True})
    pauses = []
    assert client.send("hello", "topic", sleep=pauses.append) == {"id": 4, "ts": "then", "duplicate": True}
    first, second = client.opener.bodies
    assert first == second and first["text"] == "hello"
    assert is_uuid4(first["client_id"])
    assert pauses == [RETRY_PAUSE]


def test_client_retries_only_once_and_then_names_the_client_id_to_resend_with(mailbox):
    client = Client(mailbox[0])
    client.opener = ScriptedOpener(TimeoutError("slow"), ConnectionRefusedError("down"))
    with pytest.raises(ClientError) as error:
        client.send("hello", sleep=lambda seconds: None)
    assert len(client.opener.bodies) == 2
    assert client.opener.bodies[0]["client_id"] in str(error.value)


def test_client_does_not_retry_an_http_error_status(mailbox):
    client = Client(mailbox[0])
    client.opener = ScriptedOpener(HTTPError("http://127.0.0.1", 500, "error", {}, None))
    with pytest.raises(ClientError, match="HTTP 500"):
        client.send("hello", sleep=lambda seconds: None)
    assert len(client.opener.bodies) == 1


def test_client_refuses_an_overlong_client_id_before_sending(mailbox):
    client = Client(mailbox[0])
    client.opener = ScriptedOpener()
    with pytest.raises(ClientError, match="client_id"):
        client.send("hello", client_id="x" * 65)
    assert client.opener.bodies == []


def test_every_cli_send_uses_a_fresh_uuid4_client_id(api, capsys):
    _, _, store = api
    assert main(["send", "first"]) == 0 and main(["send", "second"]) == 0
    assert capsys.readouterr().out.split() == ["1", "2"]
    ids = [message["client_id"] for message in store.messages_after()]
    assert ids[0] != ids[1]
    assert all(is_uuid4(value) for value in ids)


def test_cli_send_with_a_used_client_id_prints_already_delivered(api, capsys):
    _, _, store = api
    assert main(["send", "hello", "--client-id", "resend-me"]) == 0
    assert capsys.readouterr().out == "1\n"
    assert main(["send", "hello", "--client-id", "resend-me"]) == 0
    assert capsys.readouterr().out == "already delivered: message 1\n"
    assert len(store.messages_after()) == 1


def test_client_retries_a_truncated_response_with_the_same_client_id(mailbox):
    client = Client(mailbox[0])
    client.opener = ScriptedOpener(
        IncompleteRead(b'{"id":', 20), {"id": 4, "ts": "then", "duplicate": True}
    )
    assert client.send("hello", sleep=lambda seconds: None)["id"] == 4
    assert client.opener.bodies[0] == client.opener.bodies[1]


@pytest.mark.parametrize("outcome", [
    HTTPError("http://127.0.0.1", 500, "private-response-text", {}, None),
    {}, {"id": True}, {"id": 0}, {"id": "7"}, [],
    IncompleteRead(b"private-response-text", 20),
])
def test_client_failed_send_preserves_recovery_id_without_private_content(mailbox, outcome):
    client = Client(mailbox[0])
    client.opener = ScriptedOpener(outcome, outcome)
    with pytest.raises(ClientError) as error:
        client.send("private-request-text", sleep=lambda seconds: None)
    assert client.opener.bodies[0]["client_id"] in str(error.value)
    assert "private-request-text" not in str(error.value)
    assert "private-response-text" not in str(error.value)
    assert mailbox[0].token() not in str(error.value)
