"""The dotline command line. Only token --show reveals the token."""

import argparse
import json
import logging
import sys
import threading
from pathlib import Path

from . import __version__
from .channel import Channel
from .client import Client
from .config import initialize, load_config
from .integration import hook_output, install_hook
from .server import make_server
from .store import ClaimConflict, Follower, Store


def output(value) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="A direct line between your ChatGPT dot and Claude Code.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create private config and token; print only the token file path")
    token = commands.add_parser("token", help="Explicitly show the secret in your own terminal")
    token.add_argument("--show", action="store_true", required=True)
    for command in ("serve", "channel"):
        sub = commands.add_parser(command, help="Serve HTTP" if command == "serve" else "Run the Claude Code stdio channel")
        sub.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default: 127.0.0.1)")
        sub.add_argument("--port", type=int, help="HTTP port (default: config port, initially 8790)")
        if command == "channel":
            sub.add_argument("--serve", action="store_true", help="Run HTTP alongside the channel in this process")
    commands.add_parser("watch", help="Follow only new inbox JSON lines, flushing each event")
    pending = commands.add_parser("pending", help="List unanswered messages and their holders")
    pending.add_argument("--json", action="store_true")
    claim = commands.add_parser("claim", help="Claim a message for 30 minutes (conflict exits 3)")
    claim.add_argument("id", type=int)
    claim.add_argument("--by", required=True)
    for command in ("reply", "send"):
        sub = commands.add_parser(command, help="Save a local reply" if command == "reply" else "Send an HTTP message")
        if command == "reply":
            sub.add_argument("id", type=int)
            sub.add_argument("--by", help="Optionally require a matching claim holder")
        else:
            sub.add_argument("--topic", default="")
            sub.add_argument("--client-id", help="Reuse the client_id of an earlier send to resend it safely")
        sub.add_argument("text", nargs="*")
        sub.add_argument("--file", type=Path, help="Read UTF-8 text from a file instead of arguments")
    wait = commands.add_parser("wait", help="Poll every 20 seconds for a reply")
    wait.add_argument("id", type=int)
    wait.add_argument("--minutes", type=float, default=10)
    replies = commands.add_parser("replies", help="Read replies over HTTP")
    replies.add_argument("--after", type=int, default=0)
    commands.add_parser("health", help="Check HTTP availability without sending the token")
    hook = commands.add_parser("hook", help="Print Claude Code hook JSON without network access")
    hook.add_argument("event", choices=["session-start"])
    installer = commands.add_parser("install-hook", help="Back up and add the SessionStart hook once")
    installer.add_argument("--settings", type=Path, help="Override the Claude Code settings.json path")
    return root


def message_text(args) -> str:
    if args.file is not None:
        if args.text:
            raise ValueError("use either text arguments or --file")
        return args.file.read_text(encoding="utf-8-sig")
    return " ".join(args.text)


def dispatch(args) -> int:
    if args.command == "init":
        print(initialize())
        return 0
    config = load_config()
    store = Store(config.home)
    client = Client(config)
    if args.command == "token":
        print("Warning: this displays your bearer token. Use only your own private terminal.", file=sys.stderr)
        print(config.token())
    elif args.command == "serve":
        server = make_server(store, config.token(), args.host, config.port if args.port is None else args.port)
        logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    elif args.command == "watch":
        store.prepare()
        follower = Follower(store.inbox)
        for message in follower.follow(threading.Event()):
            output(message)
    elif args.command == "pending":
        pending = store.pending()
        if args.json:
            output(pending)
        else:
            for message in pending:
                # JSON quoting prevents terminal escape injection in message data.
                print(f"{message['id']}\t{json.dumps(message['claimed_by'] or 'unclaimed')}\t{json.dumps(message['text'], ensure_ascii=False)}")
    elif args.command == "claim":
        store.claim(args.id, args.by)
    elif args.command == "reply":
        record = store.reply(args.id, message_text(args), session=args.by)
        print(record["id"])
    elif args.command == "send":
        result = client.send(message_text(args), args.topic, args.client_id)
        if result.get("duplicate") is True:
            print(f"already delivered: message {result['id']}")
        else:
            print(result["id"])
    elif args.command == "wait":
        print(client.wait(args.id, args.minutes)["text"])
    elif args.command == "replies":
        output({"replies": client.replies(args.after)})
    elif args.command == "health":
        output(client.health())
    elif args.command == "hook":
        raw = sys.stdin.read(16385)
        if len(raw) > 16384:
            raise ValueError("hook input exceeds 16384 characters")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("hook input must be a JSON object")
        output(hook_output(config, data))
    elif args.command == "install-hook":
        path, changed = install_hook(args.settings)
        print(f"{'Installed' if changed else 'Already installed'}: {path}")
    elif args.command == "channel":
        logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
        Channel(config).run(serve=args.serve, host=args.host, port=args.port)
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        return dispatch(args)
    except ClaimConflict as error:
        print(f"dotline: {error}", file=sys.stderr)
        return 3
    except json.JSONDecodeError:
        print("dotline: invalid JSON input", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"dotline: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("dotline: file or connection unavailable; check paths, permissions and configuration", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
