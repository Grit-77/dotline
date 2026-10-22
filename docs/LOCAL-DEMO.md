# Try the mailbox without an agent

Use this local manual demo to try send → claim → reply → readback before
connecting a ChatGPT dot or Claude Code. The second terminal plays the consumer.
No agent session, model API key or public tunnel is needed for this protocol demo.

This is a **synthetic local exchange**, not a native agent integration test.
The recorded rehearsal used Windows, Python 3.12 and source revision
`71b3c8f9e32585ef2d48fb7ecb14fc3a7084e3ec`. Python 3.10+ and Git must already
be installed. Installation may download build tooling.

## Terminal A: install and start an isolated mailbox

Use a new empty directory and keep both terminals in that directory. The commands
below use PowerShell. Choose another unused loopback port if 18790 is occupied.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install git+https://github.com/Grit-77/dotline@71b3c8f9e32585ef2d48fb7ecb14fc3a7084e3ec
$env:DOTLINE_HOME = Join-Path $PWD 'mailbox'
$env:DOTLINE_URL = 'http://127.0.0.1:18790'
Remove-Item Env:DOTLINE_TOKEN_FILE -ErrorAction SilentlyContinue
.\.venv\Scripts\dotline.exe init
.\.venv\Scripts\dotline.exe serve --host 127.0.0.1 --port 18790
```

The separate virtual environment and `DOTLINE_HOME` keep this demo's runtime and
mailbox away from an existing dotline setup. `init` prints the token file path;
do not print or share its contents. The server stays in the foreground and binds
to loopback. It does not expose your computer to the public internet.

## Terminal B: send, claim and answer

Open another PowerShell terminal in the same demo directory. If you selected a
different port, use that same value here.

```powershell
$env:DOTLINE_HOME = Join-Path $PWD 'mailbox'
$env:DOTLINE_URL = 'http://127.0.0.1:18790'
Remove-Item Env:DOTLINE_TOKEN_FILE -ErrorAction SilentlyContinue
.\.venv\Scripts\dotline.exe health
.\.venv\Scripts\dotline.exe send 'Synthetic local demo request' --topic demo
.\.venv\Scripts\dotline.exe pending --json
# Use the ID printed by send in place of 1:
.\.venv\Scripts\dotline.exe claim 1 --by manual-demo
.\.venv\Scripts\dotline.exe reply 1 --by manual-demo 'Synthetic local demo reply'
.\.venv\Scripts\dotline.exe wait 1 --minutes 0.1
.\.venv\Scripts\dotline.exe replies
```

Check that `wait` returns the reply you wrote and that `replies` associates it
with the sent message. Health alone does not test authenticated mailbox access.
Reusing this mailbox can produce IDs other than 1; use the actual ID throughout.

## Stop and decide what to try next

Stop the server with Ctrl+C in Terminal A and close the demo terminals. The
virtual environment and mailbox remain in the demo directory. Mailbox data and
the bearer are plaintext: keep that directory private and remove it when you
no longer need the synthetic data.

The local rehearsal also exercised file input, a repeated `client_id`, a refused
conflicting claim and an unanswered wait. The same 90 selected CLI, HTTP and
integration tests passed in researcher and manager runs on the pinned source;
these are repeated runs of 90 tests, not 180 distinct tests. No real messages,
owner settings or external agent sessions were used.

Those observations do not establish that a dot or Claude follows its trust
instructions. To connect your actual session, return to the
[Claude-side setup](../README.md#claude-side) and
[dot connection choices](../README.md#talking-from-a-dot). Native integration
and remote HTTPS need their own checks. Retain the
[trust and security limits](../README.md#trust-and-security).

If you get stuck, report the source revision, operating system, command stage
and redacted error in an [issue](https://github.com/Grit-77/dotline/issues).
Do not include bearer values or private message contents.
