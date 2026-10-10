# Contributing

Small, focused pull requests are welcome. Describe the user-visible behavior
and add a test that would fail without your change. Keep runtime dependencies
empty and support Python 3.10+, Linux, macOS and Windows. Do not commit tokens,
mailbox data or company logos. See SECURITY.md for private vulnerability reports.

From the project root:

```sh
uv run --with pytest --with-editable . pytest -q
uvx ruff check .
```

The editable install runs the working tree's source, including uncommitted
changes, instead of a cached wheel from an earlier build.

HTTP tests bind only to loopback with an ephemeral port. They skip with an
explicit reason if the environment forbids sockets; local mailbox, hook and
channel tests must still run. CI runs ruff and pytest on all three operating
systems with Python 3.10 and 3.13. The workflow takes effect when this directory
is the root of its public repository.

Use English for code and documentation. Keep security-sensitive errors generic
and logs free of request content. Preserve other Claude Code hooks when changing
the installer. Preview channel changes need contract tests and clear limitations
in the README. Contributions are licensed under Apache-2.0.
