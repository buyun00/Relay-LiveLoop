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
from host.editor_transport import EditorJobTransport
from host.native_compile_profile import NativeCompileProfileRegistry
from host.runtime_session import load_runtime_session
from host.runtime_transport import LoopbackRuntimeHostTransport
from host.runtime_transport_provider import RuntimeTransportProvider
from host.runtime_update_provider import MAX_RUNTIME_FRAME_BYTES, create_native_update_providers
from host.coordinator_binding import bind_shared_coordinator
from host.player_process_composition import create_player_process_provider, load_player_process_config
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
    serve.add_argument("--runtime-session-file", help="Protected JSON handoff for one authenticated Development Player session.")
    serve.add_argument("--machine-config", help="Machine config containing the playerProcess composition paths.")
    serve.add_argument("--native-compile-profiles", help="Server-owned source/baseline profile registry required with --runtime-session-file.")
    serve.add_argument("--editor-job-root", help="Durable incoming/processing/results root for the existing Editor worker transport.")
    serve.add_argument("--editor-artifact-root", help="Editor compile artifact directory contained by one configured --artifact-root.")
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

    shutdown = subparsers.add_parser("shutdown", help="Request authenticated graceful Host shutdown while preserving Player.")
    shutdown.add_argument("--request-id", default=None)
    shutdown.add_argument("--wait-for-active-jobs", action="store_true")
    shutdown.add_argument("--url", default=os.environ.get(URL_ENV, "http://127.0.0.1:18760"))
    shutdown.add_argument("--timeout", type=float, default=30.0)
    shutdown.add_argument("--json", action="store_true", help="Emit compact JSON. Output is always JSON safe.")
    _common_security(shutdown)
    return parser


def build_command_service(
    ledger: Ledger,
    artifacts: ArtifactStore,
    *,
    providers=None,
    preparation_provider=None,
    runtime_provider=None,
    player_process_provider=None,
) -> CommandService:
    """Construct the shared service and attach its single optional coordinator owner."""
    provider_registry = providers
    if player_process_provider is not None:
        if provider_registry is None:
            from host.providers import ProviderRegistry

            provider_registry = ProviderRegistry()
        provider_registry.register(
            "player_process",
            player_process_provider.provider_id,
            player_process_provider,
            verified=player_process_provider.is_verified,
        )
    service = CommandService(ledger, artifacts, providers=provider_registry)
    def coordinator_availability_probe(capability: str, provider: Any) -> bool:
        probe = getattr(provider, "probe_capability", None)
        if callable(probe):
            return bool(probe(capability))
        return bool(getattr(provider, "is_verified", False))

    binding = bind_shared_coordinator(
        service,
        preparation_provider,
        runtime_provider,
        availability_probe=coordinator_availability_probe,
    )
    service.coordinator_binding = binding
    return service


def _serve(args: argparse.Namespace) -> int:
    token = _token_from_args(args)
    roots = []
    for value in args.artifact_root:
        root = Path(value).resolve()
        root.mkdir(parents=True, exist_ok=True)
        roots.append(root)
    machine_config = load_player_process_config(args.machine_config) if args.machine_config else None
    runtime_session_file = args.runtime_session_file
    if machine_config is not None:
        configured_session_file = machine_config.runtime_session_file
        if runtime_session_file is None:
            runtime_session_file = str(configured_session_file)
        elif Path(runtime_session_file).expanduser().resolve(strict=False) != configured_session_file:
            raise ValueError("--runtime-session-file must match playerProcess.runtimeSessionFile.")
    profile_path = getattr(args, "native_compile_profiles", None)
    editor_job_path = getattr(args, "editor_job_root", None)
    editor_artifact_path = getattr(args, "editor_artifact_root", None)
    configured_compile_values = (profile_path, editor_job_path, editor_artifact_path)
    if runtime_session_file and any(not value for value in configured_compile_values):
        raise ValueError("--runtime-session-file requires --native-compile-profiles, --editor-job-root, and --editor-artifact-root; manual manifest preparation is disabled for Player-bound Host service.")
    if not runtime_session_file and any(configured_compile_values):
        raise ValueError("Native compile preparation requires --runtime-session-file and its authenticated Player session.")
    compile_profiles = None
    editor_transport = None
    if runtime_session_file:
        compile_profiles = NativeCompileProfileRegistry.load(profile_path)
        editor_job_root = Path(editor_job_path).expanduser().resolve(strict=False)
        editor_artifact_root = Path(editor_artifact_path).expanduser().resolve(strict=False)
        if not any(editor_artifact_root == root or root in editor_artifact_root.parents for root in roots):
            raise ValueError("--editor-artifact-root must be equal to or contained by a configured --artifact-root.")
        editor_transport = EditorJobTransport(editor_job_root, editor_artifact_root)
    ledger = Ledger(args.database)
    artifacts = ArtifactStore(ledger, roots)
    runtime_transport = None
    providers = None
    preparation_provider = None
    runtime_provider = None
    if runtime_session_file:
        session = load_runtime_session(runtime_session_file)
        runtime_transport = LoopbackRuntimeHostTransport(
            shared_secret=session.shared_secret,
            expected_session_id=session.session_id,
            expected_launch_id=session.launch_id,
            expected_runtime_revision=session.runtime_revision,
            protocol_version=session.protocol_version,
            listen_address=session.host_address,
            port=session.port,
            maximum_frame_bytes=MAX_RUNTIME_FRAME_BYTES,
        )
        from host.providers import ProviderRegistry
        providers = ProviderRegistry()
        runtime_transport.start()
        player = RuntimeTransportProvider(runtime_transport, {"observe"})
        providers.register("observation", "development-player-observation", player, verified=True)
        verification = RuntimeTransportProvider(runtime_transport, {"verify", "input.click", "input.text"})
        providers.register("verification", "development-player-verification", verification, verified=True)
        preparation_provider, runtime_provider = create_native_update_providers(
            runtime_transport,
            artifacts,
            session,
            ledger=ledger,
            profiles=compile_profiles,
            editor_transport=editor_transport,
        )
    player_process_provider = None
    if machine_config is not None:
        player_process_provider = create_player_process_provider(
            config=machine_config,
            ledger=ledger,
            artifacts=artifacts,
            runtime_transport=runtime_transport,
        )
    service = build_command_service(
        ledger,
        artifacts,
        providers=providers,
        preparation_provider=preparation_provider,
        runtime_provider=runtime_provider,
        player_process_provider=player_process_provider,
    )
    server = create_http_server(service, token, args.host, args.port)
    try:
        if service.coordinator is not None:
            service.coordinator.recover_pending()
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        if runtime_transport is not None:
            runtime_transport.close()
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


def _shutdown(args: argparse.Namespace) -> int:
    token = _token_from_args(args)
    client = RelayHTTPClient(args.url, token, timeout_seconds=args.timeout)
    request_id = args.request_id or f"request_{uuid4().hex}"
    try:
        response = client.shutdown(request_id, wait_for_active_jobs=args.wait_for_active_jobs)
    except RelayHTTPError as exc:
        response = exc.response or {
            "requestId": request_id,
            "status": "failed",
            "runtimeChanged": False,
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
    print(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":") if args.json else None, indent=None if args.json else 2))
    return 0 if response.get("status") == "accepted" else 2


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] not in {"serve", "command", "shutdown", "-h", "--help"}:
        raw_args.insert(0, "command")
    parser = build_parser()
    args = parser.parse_args(raw_args)
    try:
        if args.mode == "serve":
            return _serve(args)
        if args.mode == "shutdown":
            return _shutdown(args)
        return _send(args)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    sys.exit(main())
