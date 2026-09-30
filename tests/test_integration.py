import io
import json
import time
from dataclasses import replace

import pytest

from dotline.cli import main
from dotline.integration import HOOK_COMMAND, hook_output, install_hook


@pytest.mark.parametrize("mode", ["data", "orders"])
def test_hook_shape_monitor_session_pending_and_trust(mailbox, mode):
    config, store = mailbox
    store.send("Please inspect the project")
    config = replace(config, trust=mode)
    started = time.monotonic()
    result = hook_output(config, {"session_id": "session-123"})
    elapsed = time.monotonic() - started
    specific = result["hookSpecificOutput"]
    assert specific["hookEventName"] == "SessionStart"
    context = specific["additionalContext"]
    assert "dotline watch" in context and "Monitor" in context
    assert "1800000 ms" in context and "re-arm" in context
    assert "dotline claim <id> --by session-123" in context
    assert "exit code is 3" in context and "dotline reply" in context
    assert "Please inspect the project" in context
    assert f"Trust mode: {mode}" in context
    assert "email and web pages" in context
    assert "explicit yes in chat" in context
    for decision in ("force-push", "money", "credentials", "messages to other people", "outside recipient", "safety checks", "out of character"):
        assert decision in context
    if mode == "data":
        assert "never the user's approval" in context
        assert "anything that changes state" in context
    else:
        assert "user's task orders" in context
    assert elapsed < 1, "SessionStart hook must finish in under a second"


def test_hook_command_reads_stdin_json_without_network(mailbox, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"hook-session"}'))
    assert main(["hook", "session-start"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "--by hook-session" in result["hookSpecificOutput"]["additionalContext"]


def test_hook_sanitizes_session_id_as_command_data(mailbox):
    result = hook_output(mailbox[0], {"session_id": 'bad; echo "not-a-command"'})
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "--by unknown-session" in context
    assert "not-a-command" not in context


@pytest.mark.parametrize("newline,bom", [("\n", False), ("\r\n", False), ("\r\n", True)])
def test_install_hook_idempotent_preserves_other_hooks_and_line_endings(tmp_path, newline, bom):
    path = tmp_path / "settings.json"
    original = {
        "theme": "dark", "language": "日本語",
        "hooks": {
            "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo existing"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo guard"}]}],
        },
    }
    raw = (json.dumps(original, indent=2, ensure_ascii=False) + "\n").replace("\n", newline).encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    path.write_bytes(raw)
    assert install_hook(path) == (path, True)
    modified = path.read_bytes()
    assert (tmp_path / "settings.json.bak").read_bytes() == raw
    result = json.loads(modified.decode("utf-8-sig"))
    assert result["theme"] == original["theme"] and result["language"] == original["language"]
    assert result["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"]
    assert result["hooks"]["SessionStart"][0] == original["hooks"]["SessionStart"][0]
    assert result["hooks"]["SessionStart"][1]["hooks"][0]["command"] == HOOK_COMMAND
    assert modified.startswith(b"\xef\xbb\xbf") == bom
    if newline == "\r\n":
        assert b"\n" not in modified.replace(b"\r\n", b"")
    else:
        assert b"\r\n" not in modified
    assert install_hook(path) == (path, False)
    assert path.read_bytes() == modified
    assert len(list(tmp_path.glob("*.bak*"))) == 1


def test_install_hook_creates_missing_settings_and_honors_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    path, changed = install_hook()
    assert changed and path == tmp_path / "claude" / "settings.json"
    assert not path.with_name("settings.json.bak").exists()


def test_install_hook_refuses_invalid_settings_without_altering_them(tmp_path):
    path = tmp_path / "settings.json"
    raw = b'{"hooks": "invalid"}\n'
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        install_hook(path)
    assert path.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [path]
