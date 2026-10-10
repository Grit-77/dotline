import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        "        body = pathlib.Path(arg[1:]).read_bytes()\n"
        "        pathlib.Path(os.environ['BODY_REPORT']).write_bytes(body)\n"
        "        with open(os.environ['BODY_LOG'], 'ab') as log:\n"
        "            log.write(body + b'\\n')\n"
        "        # FAKE_CURL_EXIT lists one exit code per POST, in order: 7 = no connection, 22 = HTTP error.\n"
        "        codes = [code for code in os.environ.get('FAKE_CURL_EXIT', '').split(',') if code]\n"
        "        calls = len(open(os.environ['BODY_LOG'], 'rb').read().splitlines())\n"
        "        if calls <= len(codes) and int(codes[calls - 1]):\n"
        "            sys.exit(int(codes[calls - 1]))\n"
        "print(os.environ['FAKE_RESPONSE'])\n",
        encoding="utf-8",
    )
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ARGUMENT_REPORT", str(tmp_path / "args.json"))
    monkeypatch.setenv("BODY_REPORT", str(tmp_path / "body.json"))
    monkeypatch.setenv("BODY_LOG", str(tmp_path / "bodies.log"))
    monkeypatch.setenv("FAKE_RESPONSE", '{"id":7,"ts":"now"}')
    monkeypatch.delenv("FAKE_CURL_EXIT", raising=False)
    return tmp_path


def posted_bodies(directory):
    return [json.loads(line) for line in (directory / "bodies.log").read_text(encoding="utf-8").splitlines()]


def shell(*args):
    return subprocess.run(["bash", str(CLIENTS / "dotline.sh"), *args], capture_output=True, text=True, encoding="utf-8", timeout=5)


def test_shell_sends_utf8_and_keeps_bearer_out_of_process_arguments(fake_curl, mailbox):
    text = 'Unicode 世界 👋, "quotes", backslash \\ and\nnewlines'
    result = shell("send", text, "--topic", "review")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "7"
    body = json.loads((fake_curl / "body.json").read_text(encoding="utf-8"))
    assert body.pop("client_id")
    assert body == {"text": text, "topic": "review"}
    arguments = json.loads((fake_curl / "args.json").read_text())
    assert mailbox[0].token() not in " ".join(arguments)
    assert mailbox[0].token() not in result.stdout + result.stderr
    header_path = next(arg[1:] for arg in arguments if arg.startswith("@") and "header" in arg)
    assert not Path(header_path).exists(), "private temporary header must be removed"


UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def test_shell_send_uses_a_fresh_uuid4_client_id_every_time(fake_curl):
    ids = []
    for _ in range(2):
        assert shell("send", "hello").returncode == 0
        ids.append(json.loads((fake_curl / "body.json").read_text(encoding="utf-8"))["client_id"])
    assert all(UUID4.fullmatch(value) for value in ids)
    assert ids[0] != ids[1]


def test_shell_retries_once_with_the_same_client_id_after_a_network_error(fake_curl, monkeypatch):
    monkeypatch.setenv("FAKE_CURL_EXIT", "7")
    result = shell("send", "hello")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "7\n"
    first, second = posted_bodies(fake_curl)
    assert first == second and UUID4.fullmatch(first["client_id"])


def test_shell_retries_only_once_and_names_the_client_id_to_resend_with(fake_curl, monkeypatch):
    monkeypatch.setenv("FAKE_CURL_EXIT", "7,7,7")
    result = shell("send", "hello")
    assert result.returncode == 1
    bodies = posted_bodies(fake_curl)
    assert len(bodies) == 2
    assert bodies[0]["client_id"] in result.stderr


def test_shell_does_not_retry_an_http_error_status(fake_curl, monkeypatch):
    monkeypatch.setenv("FAKE_CURL_EXIT", "22")
    result = shell("send", "hello")
    assert result.returncode == 1
    assert len(posted_bodies(fake_curl)) == 1


def test_shell_prints_already_delivered_for_a_duplicate_answer(fake_curl, monkeypatch):
    monkeypatch.setenv("FAKE_RESPONSE", '{"id": 7, "ts": "now", "duplicate": true}')
    result = shell("send", "hello", "--client-id", "resend-me")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "already delivered: message 7\n"
    assert posted_bodies(fake_curl)[0]["client_id"] == "resend-me"


def test_shell_refuses_an_overlong_client_id_without_sending(fake_curl):
    result = shell("send", "hello", "--client-id", "x" * 65)
    assert result.returncode == 1
    assert not (fake_curl / "bodies.log").exists()


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


def powershell(*args, executable=None):
    executable = executable or shutil.which("pwsh") or shutil.which("powershell")
    if os.name != "nt" or executable is None:
        pytest.skip("the PowerShell client is run only where Windows PowerShell or pwsh is installed")
    command = [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(CLIENTS / "dotline.ps1"), *args]
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_powershell_sends_a_fresh_uuid4_client_id_and_reports_a_duplicate(api):
    _, _, store = api
    assert powershell("send", "first").stdout.strip() == "1"
    assert powershell("send", "second").stdout.strip() == "2"
    ids = [message["client_id"] for message in store.messages_after()]
    assert all(UUID4.fullmatch(value) for value in ids) and ids[0] != ids[1]
    assert powershell("send", "third", "-ClientId", "resend-me").stdout.strip() == "3"
    again = powershell("send", "third", "-ClientId", "resend-me")
    assert again.returncode == 0, again.stderr
    assert again.stdout.strip() == "already delivered: message 3"
    assert len(store.messages_after()) == 3


@pytest.fixture
def dropping_server(monkeypatch):
    """Reads the first POST and closes the connection without answering; then behaves."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.server.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if len(self.server.bodies) == 1:
                self.connection.close()
                return
            data = b'{"id": 5, "ts": "now", "duplicate": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM, errno.EAFNOSUPPORT}:
            pytest.skip("Sandbox/platform forbids loopback sockets")
        raise
    server.daemon_threads = True
    server.bodies = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("DOTLINE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_powershell_retries_once_with_the_same_client_id_after_a_dropped_connection(mailbox, dropping_server):
    result = powershell("send", "hello")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "already delivered: message 5"
    first, second = dropping_server.bodies
    assert first == second and UUID4.fullmatch(first["client_id"])


@pytest.fixture
def invalid_response_server(monkeypatch, request):
    status, body, truncated = request.param

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.server.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body) + (20 if truncated else 0)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.bodies = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("DOTLINE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def real_shell_send(mailbox, monkeypatch):
    if os.name == "nt":
        bash = Path("C:/Program Files/Git/bin/bash.exe")
        if not bash.exists():
            pytest.skip("Git Bash is unavailable")
        executable = str(bash)
    else:
        executable = shutil.which("bash")
        if executable is None:
            pytest.skip("bash is unavailable")
    monkeypatch.setenv("DOTLINE_TOKEN_FILE", mailbox[0].token_path.as_posix())
    return subprocess.run(
        [executable, (CLIENTS / "dotline.sh").as_posix(), "send", "private-request-text"],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


@pytest.mark.parametrize("invalid_response_server", [
    (500, b"private-response-text", False),
    (200, b'{"id":7', False),
    (200, b'{"unexpected":7}', False),
    (200, b'{"id":true}', False),
    (200, b'{"id":', True),
    (200, b'{"id":7,"ts":"now"}', True),
    (200, b'[{"id":7,"ts":"now"}]', False),
    (200, b'{"id":7,"duplicate":null}', False),
    (200, b'{"id":7,"ts":"literal\nnewline"}', False),
    (200, b'{"\\u0000id":7}', False),
], indirect=True)
@pytest.mark.parametrize("client_kind", ["python", "powershell", "powershell51", "shell"])
def test_failed_send_reports_recovery_id_for_unusable_http_answers(
    mailbox, invalid_response_server, client_kind, monkeypatch, capsys
):
    if client_kind == "python":
        from dotline.cli import main

        exit_code = main(["send", "private-request-text"])
        captured = capsys.readouterr()
        stdout, stderr = captured.out, captured.err
    elif client_kind in ("powershell", "powershell51"):
        executable = None
        if client_kind == "powershell51":
            executable = shutil.which("powershell")
            if executable is None:
                pytest.skip("Windows PowerShell 5.1 is unavailable")
        result = powershell("send", "private-request-text", executable=executable)
        exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
    else:
        result = real_shell_send(mailbox, monkeypatch)
        exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
    assert exit_code == 1
    assert stdout == ""
    bodies = invalid_response_server.bodies
    assert bodies and bodies[0]["client_id"] in stderr
    assert all(body["client_id"] == bodies[0]["client_id"] for body in bodies)
    assert len(bodies) <= 2
    assert "private-request-text" not in stderr
    assert "private-response-text" not in stderr
    assert mailbox[0].token() not in stderr


@pytest.mark.parametrize("invalid_response_server,expected", [
    ((201, b'{"id":7,"ts":"escaped\\nnewline"}', False), "7\n"),
    ((200, b' {"ts":"now", "duplicate":true, "id":7}\r\n', False),
     "already delivered: message 7\n"),
], indirect=["invalid_response_server"])
def test_shell_accepts_complete_send_response(mailbox, invalid_response_server, monkeypatch, expected):
    result = real_shell_send(mailbox, monkeypatch)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    assert len(invalid_response_server.bodies) == 1
