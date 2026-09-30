import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

CLIENTS = Path(__file__).parents[1] / "clients"


@pytest.fixture
def fake_curl(mailbox, tmp_path, monkeypatch):
    if os.name == "nt" or not shutil.which("bash"):
        pytest.skip("bash script execution requires a POSIX bash environment")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    program = bin_dir / "curl"
    program.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, stat, sys\n"
        "args = sys.argv[1:]\n"
        "headers = [arg[1:] for arg in args if arg.startswith('@') and 'header' in arg]\n"
        "for filename in headers:\n"
        "    assert stat.S_IMODE(pathlib.Path(filename).stat().st_mode) == 0o600\n"
        "    assert pathlib.Path(filename).read_text().startswith('Authorization: Bearer ')\n"
        "pathlib.Path(os.environ['ARGUMENT_REPORT']).write_text(json.dumps(args))\n"
        "for arg in args:\n"
        "    if arg.startswith('@') and arg.endswith('/body'):\n"
        "        pathlib.Path(os.environ['BODY_REPORT']).write_bytes(pathlib.Path(arg[1:]).read_bytes())\n"
        "print(os.environ['FAKE_RESPONSE'])\n",
        encoding="utf-8",
    )
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ARGUMENT_REPORT", str(tmp_path / "args.json"))
    monkeypatch.setenv("BODY_REPORT", str(tmp_path / "body.json"))
    monkeypatch.setenv("FAKE_RESPONSE", '{"id":7,"ts":"now"}')
    return tmp_path


def shell(*args):
    return subprocess.run(["bash", str(CLIENTS / "dotline.sh"), *args], capture_output=True, text=True, encoding="utf-8", timeout=5)


def test_shell_sends_utf8_and_keeps_bearer_out_of_process_arguments(fake_curl, mailbox):
    text = 'Unicode 世界 👋, "quotes", backslash \\ and\nnewlines'
    result = shell("send", text, "--topic", "review")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "7"
    body = json.loads((fake_curl / "body.json").read_text(encoding="utf-8"))
    assert body == {"text": text, "topic": "review"}
    arguments = json.loads((fake_curl / "args.json").read_text())
    assert mailbox[0].token() not in " ".join(arguments)
    assert mailbox[0].token() not in result.stdout + result.stderr
    header_path = next(arg[1:] for arg in arguments if arg.startswith("@") and "header" in arg)
    assert not Path(header_path).exists(), "private temporary header must be removed"


def test_shell_file_preserves_trailing_newlines(fake_curl):
    path = fake_curl / "message.txt"
    path.write_text("A file\n\n", encoding="utf-8")
    result = shell("send", "--file", str(path))
    assert result.returncode == 0, result.stderr
    assert json.loads((fake_curl / "body.json").read_text())["text"] == "A file\n\n"


def test_shell_wait_selects_correct_reply_and_decodes_text(fake_curl, monkeypatch):
    text = 'Correct } { "to": 99\n你好 \\ end'
    monkeypatch.setenv("FAKE_RESPONSE", json.dumps({"replies": [
        {"id": 1, "ts": "now", "to": 99, "text": "Other"},
        {"id": 2, "ts": "now", "to": 7, "text": text},
    ]}, ensure_ascii=False))
    result = shell("wait", "7")
    assert result.returncode == 0, result.stderr
    assert result.stdout == text + "\n"


@pytest.mark.parametrize("command", ["health", "replies"])
def test_shell_health_and_replies(fake_curl, monkeypatch, command):
    monkeypatch.setenv("FAKE_RESPONSE", '{"ok":true}' if command == "health" else '{"replies":[]}')
    result = shell(command)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ({"ok": True} if command == "health" else {"replies": []})
    arguments = json.loads((fake_curl / "args.json").read_text())
    assert ("--header" in arguments) == (command == "replies")


def test_shell_refuses_plaintext_remote_url(fake_curl, monkeypatch):
    monkeypatch.setenv("DOTLINE_URL", "http://outside.example")
    result = shell("health")
    assert result.returncode == 1
    assert "remote URLs must use HTTPS" in result.stderr


def test_powershell_is_utf8_bom_and_uses_private_file_utf8_and_tls():
    raw = (CLIENTS / "dotline.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert "::Tls12" in text and "[Console]::OutputEncoding" in text
    assert "ReadAllText($script:tokenFile" in text
    assert "$utf8.GetBytes($Body)" in text
    assert "$request.AllowAutoRedirect = $false" in text
    assert "Write-Output $secret" not in text


def test_powershell_script_parses_when_interpreter_is_available():
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell interpreter is unavailable; BOM/transport contract still tested")
    filename = str(CLIENTS / "dotline.ps1").replace("'", "''")
    command = f"$t=$null; $e=$null; [System.Management.Automation.Language.Parser]::ParseFile('{filename}', [ref]$t, [ref]$e) > $null; if ($e.Count) {{ exit 1 }}"
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, timeout=15)
    assert result.returncode == 0


def test_shell_script_is_executable_on_posix():
    if os.name != "nt":
        assert stat.S_IMODE((CLIENTS / "dotline.sh").stat().st_mode) & stat.S_IXUSR
