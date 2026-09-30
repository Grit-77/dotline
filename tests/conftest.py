import errno
import threading

import pytest

from dotline.config import initialize, load_config
from dotline.server import make_server
from dotline.store import Store


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
    home = tmp_path / "dotline"
    monkeypatch.setenv("DOTLINE_HOME", str(home))
    monkeypatch.delenv("DOTLINE_URL", raising=False)
    monkeypatch.delenv("DOTLINE_TOKEN_FILE", raising=False)
    initialize(home)
    return load_config(home), Store(home)


@pytest.fixture
def api(mailbox, monkeypatch):
    config, store = mailbox
    try:
        server = make_server(store, config.token(), "127.0.0.1", 0)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM, errno.EAFNOSUPPORT}:
            pytest.skip("Sandbox/platform forbids loopback sockets; HTTP tests require 127.0.0.1:0")
        raise
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("DOTLINE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield server, config, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
