from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from api.http_server import create_http_server
from clients.http_client import RelayHTTPClient, RelayHTTPError
from host.artifacts import ArtifactStore
from host.ledger import Ledger
from host.service import CommandService
from host.validation import OPERATIONS

TOKEN_ENV = "RELAY_LIVELOOP_TOKEN"
URL_ENV = "RELAY_LIVELOOP_URL"


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object.")
    return value


def _read_object(path: str) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.is_file():
        raise ValueError("--file must refer to an existing regular file.")
    if candidate.stat().st_size > 256 * 1024:
        raise ValueError("--file exceeds 262144 bytes.")
    return _json_object(candidate.read_text(encoding="utf-8"), "--file")


def _token_from_args(args: argparse.Namespace) -> str:
    token: str | None = None
    if args.token_file:
        candidate = Path(args.token_file)
        if not candidate.is_file() or candidate.stat().st_size > 4096:
            raise ValueError("--token-file must refer to a token file no larger than 4096 bytes.")
        token = candidate.read_text(encoding="utf-8").strip()
    else:
        token = os.environ.get(TOKEN_ENV)
    if not token:
        raise ValueError(f"Set {TOKEN_ENV} or provide --token-file.")
    if len(token) > 4096:
        raise ValueError("Bearer token is too long.")
    return token


def _common_security(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--token-file", help=f"Read the local bearer token from a file instead of {TOKEN_ENV}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relay-liveloop")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    serve = subparsers.add_parser("serve", help="Run the local Relay LiveLoop HTTP host.")
    serve.add_argument("--database", required=True)
    serve.add_argument("--artifact-root", action="append", required=True)
    serve.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost", "::1"))
    serve.add_argument("--port", type=int, default=18760)
    _common_security(serve)

    command = subparsers.add_parser("command", help="Send one protocol command to the local host.")
    command.add_argument("operation", choices=sorted(OPERATIONS))
    payload = command.add_mutually_exclusive_group()
    payload.add_argument("--file", help="Read the operation arguments object from a UTF-8 JSON file.")
    payload.add_argument("--arguments", help="Use an inline JSON object as operation arguments.")
    command.add_argument("--task", help="Set taskId in both the command envelope and operation arguments.")
    command.add_argument("--request-id", default=None)
    command.add_argument("--context", default="{}", help="JSON object containing protocol context fields.")
    command.add_argument("--url", default=os.environ.get(URL_ENV, "http://127.0.0.1:18760"))
    command.add_argument("--timeout", type=float, default=30.0)
    command.add_argument("--json", action="store_true", help="Emit compact JSON. Output is always JSON safe.")
    _common_security(command)
    return parser


def _serve(args: argparse.Namespace) -> int:
    token = _token_from_args(args)
    roots = []
    for value in args.artifact_root:
        root = Path(value).resolve()
        root.mkdir(parents=True, exist_ok=True)
        roots.append(root)
    ledger = Ledger(args.database)
    service = CommandService(ledger, ArtifactStore(ledger, roots))
    server = create_http_server(service, token, args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ledger.close()
    return 0


def _send(args: argparse.Namespace) -> int:
    token = _token_from_args(args)
    arguments = _read_object(args.file) if args.file else _json_object(args.arguments or "{}", "--arguments")
    context = _json_object(args.context, "--context")
    envelope: dict[str, Any] = {
        "protocolVersion": 1,
        "requestId": args.request_id or f"request_{uuid4().hex}",
        "operation": args.operation,
        "arguments": arguments,
    }
    if args.task:
        envelope["taskId"] = args.task
        existing = arguments.get("taskId")
        if existing is not None and existing != args.task:
            raise ValueError("--task conflicts with arguments.taskId.")
        arguments["taskId"] = args.task
    if context:
        envelope["context"] = context
    client = RelayHTTPClient(args.url, token, timeout_seconds=args.timeout)
    try:
        response = client.command(envelope)
    except RelayHTTPError as exc:
        response = exc.response or {
            "requestId": envelope["requestId"],
            "status": "failed",
            "runtimeChanged": None,
            "facts": {
                "sourceSaved": None,
                "runtimeMatched": None,
                "checksPassed": None,
                "visualReviewed": None,
                "freshVerified": None,
            },
            "error": {
                "code": "INTERNAL_ERROR",
                "stage": "http_client",
                "message": str(exc),
                "recoverable": True,
                "details": {},
            },
            "artifacts": [],
        }
    if args.json:
        print(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    else:
        print(json.dumps(response, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if response.get("status") in {"accepted", "completed", "approval_required"} else 2


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] not in {"serve", "command", "-h", "--help"}:
        raw_args.insert(0, "command")
    parser = build_parser()
    args = parser.parse_args(raw_args)
    try:
        return _serve(args) if args.mode == "serve" else _send(args)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    sys.exit(main())
