"""Small, private configuration with no runtime dependencies."""

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def private_write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        if os.name != "nt":
            os.fchmod(stream.fileno(), 0o600)
        stream.write(data)


def config_home() -> Path:
    override = os.environ.get("DOTLINE_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "dotline"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "dotline"


def parse_config(text: str) -> dict:
    # This intentionally supports only dotline's three scalar TOML settings.
    # JSON-style double-quoted strings are a subset of TOML basic strings.
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(url|port|trust)\s*=\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[0-9]+)\s*(?:#.*)?", line)
        if not match or match[1] in result:
            raise ValueError("config.toml must contain only url, port and trust scalar settings")
        value = match[2]
        result[match[1]] = value[1:-1] if value.startswith("'") else json.loads(value)
    return result


@dataclass(frozen=True)
class Config:
    home: Path
    url: str = "http://127.0.0.1:8790"
    port: int = 8790
    trust: str = "data"

    @property
    def token_path(self) -> Path:
        return Path(os.environ.get("DOTLINE_TOKEN_FILE", str(self.home / "token"))).expanduser()

    def token(self) -> str:
        value = self.token_path.read_text(encoding="utf-8").strip()
        if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise ValueError("token file must contain a nonempty printable ASCII token")
        return value

    @property
    def client_url(self) -> str:
        value = os.environ.get("DOTLINE_URL", self.url).rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("URL must be an HTTP(S) origin without credentials, path or query")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("remote URLs must use HTTPS")
        return value


def load_config(home: Path | None = None) -> Config:
    home = home or config_home()
    path = home / "config.toml"
    values = parse_config(path.read_text(encoding="utf-8")) if path.exists() else {}
    config = Config(home, **values)
    if type(config.port) is not int or not 1 <= config.port <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")
    if config.trust not in ("data", "orders"):
        raise ValueError("trust must be data or orders")
    if not isinstance(config.url, str):
        raise ValueError("url must be a string")
    return config


def initialize(home: Path | None = None) -> Path:
    home = home or config_home()
    private_dir(home)
    from .store import Store

    Store(home).prepare()
    try:
        private_write(home / "token", (secrets.token_urlsafe(36) + "\n").encode(), exclusive=True)
    except FileExistsError:
        if os.name != "nt":
            (home / "token").chmod(0o600)
    try:
        private_write(
            home / "config.toml",
            b'url = "http://127.0.0.1:8790"\nport = 8790\ntrust = "data"\n',
            exclusive=True,
        )
    except FileExistsError:
        pass
    return home / "token"
