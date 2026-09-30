import io
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler

import pytest

from dotline import config as config_module
from dotline.cli import main
from dotline.client import Client, ClientError
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
    monkeypatch.setattr(client, "replies", lambda after: responses.pop(0))

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    assert client.wait(7, 1, clock=lambda: now[0], sleep=sleep)["text"] == "Finished"
    assert slept == [20, 20]


def test_wait_timeout_does_not_sleep_past_deadline(mailbox, monkeypatch):
    client = Client(mailbox[0])
    now = [0.0]
    monkeypatch.setattr(client, "replies", lambda after: [])
    with pytest.raises(ClientError, match="timed out"):
        client.wait(1, 0.1, clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert now[0] == 6


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

    assert __version__ == "0.1.0"
    project = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dependencies = []' in project
    assert 'requires-python = ">=3.10"' in project
