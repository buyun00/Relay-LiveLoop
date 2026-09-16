from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from typing import Any, Callable

from .errors import CommandError

PROTOCOL_VERSION = 1
MAX_COMMAND_BYTES = 256 * 1024
MAX_ARGUMENT_DEPTH = 12
MAX_COLLECTION_ITEMS = 256
MAX_STRING_LENGTH = 16 * 1024

OPERATIONS = frozenset(
    {
        "status",
        "task.open",
        "task.update",
        "task.show",
        "observe",
        "source.locate",
        "source.edit",
        "component.preview",
        "component.revert",
        "prepare",
        "task.approve",
        "iterate",
        "verify",
        "input.click",
        "input.text",
        "report",
        "baseline.build",
        "baseline.import",
        "player.start",
        "player.attach",
        "player.stop",
        "job.status",
        "job.cancel",
    }
)

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _fail(message: str) -> None:
    raise CommandError("CONTRACT_MISMATCH", message, stage="validation")


def _expect_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{name} must be an object.")
    return value


def _expect_exact_keys(
    value: dict[str, Any], name: str, required: set[str], optional: set[str] | None = None
) -> None:
    optional = optional or set()
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        _fail(f"{name} is missing: {', '.join(sorted(missing))}.")
    if unknown:
        _fail(f"{name} has unknown fields: {', '.join(sorted(unknown))}.")


def _string(value: Any, name: str, *, minimum: int = 1, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        _fail(f"{name} must be a string with length {minimum}..{maximum}.")
    return value


def _nullable_string(value: Any, name: str, *, maximum: int = 2048) -> str | None:
    if value is None:
        return None
    return _string(value, name, maximum=maximum)


def _identifier(value: Any, name: str) -> str:
    text = _string(value, name, maximum=128)
    if not ID_RE.fullmatch(text):
        _fail(f"{name} contains unsupported characters.")
    return text


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        _fail(f"{name} must be a boolean.")
    return value


def _bounded_integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"{name} must be an integer in the range {minimum}..{maximum}.")
    return value


def _string_list(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 64,
    item_maximum: int = 1024,
) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        _fail(f"{name} must be an array with {minimum}..{maximum} items.")
    return [
        _string(item, f"{name}[{index}]", maximum=item_maximum)
        for index, item in enumerate(value)
    ]


def _json_value(value: Any, name: str, depth: int = 0) -> Any:
    if depth > MAX_ARGUMENT_DEPTH:
        _fail(f"{name} exceeds the maximum nesting depth.")
    if value is None or type(value) in (bool, int, float):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            _fail(f"{name} contains a non-finite number.")
        return value
    if isinstance(value, str):
        return _string(value, name, minimum=0, maximum=MAX_STRING_LENGTH)
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            _fail(f"{name} contains too many items.")
        return [_json_value(item, f"{name}[{index}]", depth + 1) for index, item in enumerate(value)]
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            _fail(f"{name} contains too many fields.")
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            key = _string(key, f"{name} key", maximum=128)
            normalized[key] = _json_value(item, f"{name}.{key}", depth + 1)
        return normalized
    _fail(f"{name} contains a value that cannot be represented as JSON.")


def _allowed_impact(value: Any, name: str = "allowedImpact") -> dict[str, Any]:
    obj = _expect_dict(value, name)
    required = {"hotfix", "rebuildViews", "reloadModules", "restartPlayer", "buildBaseline"}
    _expect_exact_keys(obj, name, required)
    return {
        "hotfix": _boolean(obj["hotfix"], f"{name}.hotfix"),
        "rebuildViews": _string_list(obj["rebuildViews"], f"{name}.rebuildViews", maximum=64, item_maximum=256),
        "reloadModules": _string_list(obj["reloadModules"], f"{name}.reloadModules", maximum=64, item_maximum=256),
        "restartPlayer": _boolean(obj["restartPlayer"], f"{name}.restartPlayer"),
        "buildBaseline": _boolean(obj["buildBaseline"], f"{name}.buildBaseline"),
    }


def _task_open(args: dict[str, Any]) -> dict[str, Any]:
    _expect_exact_keys(
        args,
        "arguments",
        {"goal", "sessionId", "target", "allowedImpact", "acceptance"},
        {"reference"},
    )
    return {
        "goal": _string(args["goal"], "arguments.goal", maximum=4000),
        "sessionId": _identifier(args["sessionId"], "arguments.sessionId"),
        "target": _string(args["target"], "arguments.target", maximum=2000),
        "allowedImpact": _allowed_impact(args["allowedImpact"]),
        "acceptance": _string_list(args["acceptance"], "arguments.acceptance", minimum=1, maximum=32, item_maximum=2000),
        "reference": _nullable_string(args.get("reference"), "arguments.reference", maximum=4000),
    }


def _task_update(args: dict[str, Any]) -> dict[str, Any]:
    _expect_exact_keys(args, "arguments", {"updates"}, {"taskId"})
    updates = _expect_dict(args["updates"], "arguments.updates")
    allowed = {"goal", "target", "reference", "allowedImpact", "acceptance"}
    if not updates or not updates.keys() <= allowed:
        _fail("arguments.updates must contain one or more supported task fields.")
    result: dict[str, Any] = {}
    for key, value in updates.items():
        if key == "goal":
            result[key] = _string(value, "arguments.updates.goal", maximum=4000)
        elif key == "target":
            result[key] = _string(value, "arguments.updates.target", maximum=2000)
        elif key == "reference":
            result[key] = _nullable_string(value, "arguments.updates.reference", maximum=4000)
        elif key == "allowedImpact":
            result[key] = _allowed_impact(value, "arguments.updates.allowedImpact")
        elif key == "acceptance":
            result[key] = _string_list(value, "arguments.updates.acceptance", minimum=1, maximum=32, item_maximum=2000)
    normalized = {"updates": result}
    if "taskId" in args:
        normalized["taskId"] = _identifier(args["taskId"], "arguments.taskId")
    return normalized


def _task_reference(args: dict[str, Any]) -> dict[str, Any]:
    _expect_exact_keys(args, "arguments", set(), {"taskId"})
    return {"taskId": _identifier(args["taskId"], "arguments.taskId")} if "taskId" in args else {}


def _target(args: dict[str, Any]) -> dict[str, Any]:
    _expect_exact_keys(args, "arguments", {"target"}, {"taskId"})
    result = {"target": _string(args["target"], "arguments.target", maximum=2000)}
    if "taskId" in args:
        result["taskId"] = _identifier(args["taskId"], "arguments.taskId")
    return result


def _source_edit(args: dict[str, Any]) -> dict[str, Any]:
    _expect_exact_keys(args, "arguments", {"edits", "expectedSourceHash"}, {"taskId"})
    source_hash = _string(args["expectedSourceHash"], "arguments.expectedSourceHash", minimum=64, maximum=64)
    if not SHA256_RE.fullmatch(source_hash):
        _fail("arguments.expectedSourceHash must be a lowercase SHA-256 value.")
    edits = args["edits"]
    if not isinstance(edits, list) or not 1 <= len(edits) <= 128:
        _fail("arguments.edits must contain 1..128 edits.")
    normalized_edits = []
    for index, raw in enumerate(edits):
        edit = _expect_dict(raw, f"arguments.edits[{index}]")
        _expect_exact_keys(edit, f"arguments.edits[{index}]", {"sourceId", "property", "expectedValue", "newValue"})
        normalized_edits.append(
            {
                "sourceId": _identifier(edit["sourceId"], f"arguments.edits[{index}].sourceId"),
                "property": _string(edit["property"], f"arguments.edits[{index}].property", maximum=512),
                "expectedValue": _json_value(edit["expectedValue"], f"arguments.edits[{index}].expectedValue"),
                "newValue": _json_value(edit["newValue"], f"arguments.edits[{index}].newValue"),
            }
        )
    result: dict[str, Any] = {"edits": normalized_edits, "expectedSourceHash": source_hash}
    if "taskId" in args:
        result["taskId"] = _identifier(args["taskId"], "arguments.taskId")
    return result


def _preview(args: dict[str, Any]) -> dict[str, Any]:
    _expect_exact_keys(args, "arguments", {"target", "changes", "expected"}, {"taskId"})
    changes = _expect_dict(args["changes"], "arguments.changes")
    expected = _expect_dict(args["expected"], "arguments.expected")
    if not changes or changes.keys() != expected.keys():
        _fail("arguments.changes and arguments.expected must contain the same non-empty property set.")
    result = {
        "target": _string(args["target"], "arguments.target", maximum=2000),
        "changes": _json_value(changes, "arguments.changes"),
        "expected": _json_value(expected, "arguments.expected"),
    }
    if "taskId" in args:
        result["taskId"] = _identifier(args["taskId"], "arguments.taskId")
    return result


def _single_id(args: dict[str, Any], key: str, *, optional_task: bool = False) -> dict[str, Any]:
    optional = {"taskId"} if optional_task else set()
    _expect_exact_keys(args, "arguments", {key}, optional)
    result = {key: _identifier(args[key], f"arguments.{key}")}
    if optional_task and "taskId" in args:
        result["taskId"] = _identifier(args["taskId"], "arguments.taskId")
    return result


def _prepare(args: dict[str, Any]) -> dict[str, Any]:
    _expect_exact_keys(args, "arguments", set(), {"taskId", "inputSnapshot", "expectedRuntimeRevision"})
    result: dict[str, Any] = {}
    for key in ("taskId", "inputSnapshot", "expectedRuntimeRevision"):
        if key in args:
            result[key] = _identifier(args[key], f"arguments.{key}")
    return result


def _approve(args: dict[str, Any]) -> dict[str, Any]:
    _expect_exact_keys(args, "arguments", {"planId", "userConfirmationRef", "approvedImpact"}, {"taskId"})
    result = {
        "planId": _identifier(args["planId"], "arguments.planId"),
        "userConfirmationRef": _string(args["userConfirmationRef"], "arguments.userConfirmationRef", maximum=1000),
        "approvedImpact": _allowed_impact(args["approvedImpact"], "arguments.approvedImpact"),
    }
    if "taskId" in args:
        result["taskId"] = _identifier(args["taskId"], "arguments.taskId")
    return result


def _iterate(args: dict[str, Any]) -> dict[str, Any]:
    _expect_exact_keys(args, "arguments", set(), {"taskId", "planId"})
    return {key: _identifier(value, f"arguments.{key}") for key, value in args.items()}


def _verify(args: dict[str, Any]) -> dict[str, Any]:
    optional = {
        "taskId",
        "checks",
        "requirePocoSnapshot",
        "captureId",
        "nonce",
        "requireFreshFrame",
        "expectedViewportGeneration",
        "expectedOwnerGeneration",
        "targetId",
        "frameArtifactId",
        "minimumFrameExclusive",
        "maximumWidth",
        "maximumHeight",
        "requireViewport",
        "minimumWidth",
        "minimumHeight",
        "requireSafeArea",
    }
    _expect_exact_keys(args, "arguments", {"checkSetId"}, optional)
    result: dict[str, Any] = {"checkSetId": _identifier(args["checkSetId"], "arguments.checkSetId")}
    if "taskId" in args:
        result["taskId"] = _identifier(args["taskId"], "arguments.taskId")
    if "checks" in args:
        checks = args["checks"]
        if not isinstance(checks, list) or not 1 <= len(checks) <= 64:
            _fail("arguments.checks must contain 1..64 check objects.")
        result["checks"] = [_json_value(_expect_dict(item, f"arguments.checks[{i}]"), f"arguments.checks[{i}]") for i, item in enumerate(checks)]

    require_poco = _boolean(args["requirePocoSnapshot"], "arguments.requirePocoSnapshot") if "requirePocoSnapshot" in args else False
    if require_poco:
        if "captureId" not in args or "nonce" not in args:
            _fail("arguments.requirePocoSnapshot requires captureId and nonce.")
        result["requirePocoSnapshot"] = True
        result["captureId"] = _identifier(args["captureId"], "arguments.captureId")
        result["nonce"] = _string(args["nonce"], "arguments.nonce", minimum=16, maximum=256)
    elif "captureId" in args or "nonce" in args:
        _fail("arguments.captureId and arguments.nonce require requirePocoSnapshot=true.")
    elif "requirePocoSnapshot" in args:
        result["requirePocoSnapshot"] = False

    require_fresh = _boolean(args["requireFreshFrame"], "arguments.requireFreshFrame") if "requireFreshFrame" in args else False
    fresh_fields = {
        "frameArtifactId",
        "minimumFrameExclusive",
        "maximumWidth",
        "maximumHeight",
    }
    if require_fresh:
        required_fresh = {
            "expectedViewportGeneration",
            "expectedOwnerGeneration",
            "targetId",
            *fresh_fields,
        }
        missing = required_fresh - args.keys()
        if missing:
            _fail(f"arguments.requireFreshFrame is missing: {', '.join(sorted(missing))}.")
        result["requireFreshFrame"] = True
        result["expectedViewportGeneration"] = _bounded_integer(
            args["expectedViewportGeneration"],
            "arguments.expectedViewportGeneration",
            minimum=1,
            maximum=2**63 - 1,
        )
        result["expectedOwnerGeneration"] = _bounded_integer(
            args["expectedOwnerGeneration"],
            "arguments.expectedOwnerGeneration",
            minimum=0,
            maximum=2**63 - 1,
        )
        result["targetId"] = _identifier(args["targetId"], "arguments.targetId")
        result["frameArtifactId"] = _identifier(args["frameArtifactId"], "arguments.frameArtifactId")
        result["minimumFrameExclusive"] = _bounded_integer(
            args["minimumFrameExclusive"],
            "arguments.minimumFrameExclusive",
            minimum=0,
            maximum=2**63 - 1,
        )
        result["maximumWidth"] = _bounded_integer(
            args["maximumWidth"], "arguments.maximumWidth", minimum=1, maximum=8192
        )
        result["maximumHeight"] = _bounded_integer(
            args["maximumHeight"], "arguments.maximumHeight", minimum=1, maximum=8192
        )
    else:
        if fresh_fields & args.keys():
            _fail("fresh-frame bounds require requireFreshFrame=true.")
        if "requireFreshFrame" in args:
            result["requireFreshFrame"] = False
        for key in ("expectedViewportGeneration", "expectedOwnerGeneration"):
            if key in args:
                result[key] = _bounded_integer(
                    args[key],
                    f"arguments.{key}",
                    minimum=0,
                    maximum=2**63 - 1,
                )
        if "targetId" in args:
            result["targetId"] = _identifier(args["targetId"], "arguments.targetId")

    require_viewport = _boolean(args["requireViewport"], "arguments.requireViewport") if "requireViewport" in args else False
    viewport_fields = {"minimumWidth", "minimumHeight", "requireSafeArea"}
    if require_viewport:
        result["requireViewport"] = True
        result["minimumWidth"] = _bounded_integer(
            args.get("minimumWidth", 1), "arguments.minimumWidth", minimum=1, maximum=8192
        )
        result["minimumHeight"] = _bounded_integer(
            args.get("minimumHeight", 1), "arguments.minimumHeight", minimum=1, maximum=8192
        )
        result["requireSafeArea"] = _boolean(args.get("requireSafeArea", False), "arguments.requireSafeArea")
    elif viewport_fields & args.keys():
        _fail("viewport bounds require requireViewport=true.")
    elif "requireViewport" in args:
        result["requireViewport"] = False
    return result


def _input_mutation(args: dict[str, Any], operation: str) -> dict[str, Any]:
    required = {"targetId", "expectedOwnerGeneration", "expectedFrame", "expectedViewportGeneration"}
    optional = {"taskId"}
    if operation == "input.click":
        required |= {"screenX", "screenY"}
    else:
        required.add("text")
    _expect_exact_keys(args, "arguments", required, optional)
    result = {"targetId": _identifier(args["targetId"], "arguments.targetId")}
    for key in ("expectedOwnerGeneration", "expectedFrame", "expectedViewportGeneration"):
        if type(args[key]) is not int:
            _fail(f"arguments.{key} must be an integer.")
        result[key] = args[key]
    if operation == "input.click":
        for key in ("screenX", "screenY"):
            if type(args[key]) not in (int, float) or not math.isfinite(args[key]):
                _fail(f"arguments.{key} must be a finite number.")
            result[key] = args[key]
    else:
        result["text"] = _string(args["text"], "arguments.text", maximum=4096)
    if "taskId" in args:
        result["taskId"] = _identifier(args["taskId"], "arguments.taskId")
    return result


def _baseline_build(args: dict[str, Any]) -> dict[str, Any]:
    _expect_exact_keys(args, "arguments", {"sourceSnapshot", "profileId", "authorizationRef"})
    return {key: _identifier(args[key], f"arguments.{key}") for key in ("sourceSnapshot", "profileId", "authorizationRef")}


def _player_start(args: dict[str, Any]) -> dict[str, Any]:
    _expect_exact_keys(args, "arguments", {"baselineId", "authorizationRef"})
    return {key: _identifier(args[key], f"arguments.{key}") for key in ("baselineId", "authorizationRef")}


def _player_stop(args: dict[str, Any]) -> dict[str, Any]:
    _expect_exact_keys(args, "arguments", {"sessionId", "authorizationRef"})
    return {key: _identifier(args[key], f"arguments.{key}") for key in ("sessionId", "authorizationRef")}


ARGUMENT_VALIDATORS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "status": lambda args: (_expect_exact_keys(args, "arguments", set()) or {}),
    "task.open": _task_open,
    "task.update": _task_update,
    "task.show": _task_reference,
    "observe": _task_reference,
    "source.locate": _target,
    "source.edit": _source_edit,
    "component.preview": _preview,
    "component.revert": lambda args: _single_id(args, "overlayId", optional_task=True),
    "prepare": _prepare,
    "task.approve": _approve,
    "iterate": _iterate,
    "verify": _verify,
    "input.click": lambda args: _input_mutation(args, "input.click"),
    "input.text": lambda args: _input_mutation(args, "input.text"),
    "report": _task_reference,
    "baseline.build": _baseline_build,
    "baseline.import": lambda args: _single_id(args, "artifactId"),
    "player.start": _player_start,
    "player.attach": lambda args: _single_id(args, "sessionId"),
    "player.stop": _player_stop,
    "job.status": lambda args: _single_id(args, "jobId"),
    "job.cancel": lambda args: _single_id(args, "jobId"),
}


def validate_command(raw: Any) -> dict[str, Any]:
    command = _expect_dict(raw, "command")
    try:
        encoded = json.dumps(command, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail(f"command is not valid JSON data: {exc}.")
    if len(encoded) > MAX_COMMAND_BYTES:
        _fail(f"command exceeds {MAX_COMMAND_BYTES} bytes.")
    _expect_exact_keys(
        command,
        "command",
        {"protocolVersion", "requestId", "operation", "arguments"},
        {"taskId", "context"},
    )
    if type(command["protocolVersion"]) is not int or command["protocolVersion"] != PROTOCOL_VERSION:
        _fail(f"protocolVersion must equal {PROTOCOL_VERSION}.")
    request_id = _identifier(command["requestId"], "requestId")
    operation = _string(command["operation"], "operation", maximum=64)
    if operation not in OPERATIONS:
        _fail("operation is not supported by protocol version 1.")
    task_id = command.get("taskId")
    if task_id is not None:
        task_id = _identifier(task_id, "taskId")
    context = command.get("context", {})
    context = _expect_dict(context, "context")
    _expect_exact_keys(
        context,
        "context",
        set(),
        {"projectId", "workspaceId", "sessionId", "expectedRuntimeRevision", "expectedLaunchId"},
    )
    context = {key: _identifier(value, f"context.{key}") for key, value in context.items()}
    arguments = ARGUMENT_VALIDATORS[operation](_expect_dict(command["arguments"], "arguments"))
    argument_task_id = arguments.pop("taskId", None)
    if task_id and argument_task_id and task_id != argument_task_id:
        _fail("taskId and arguments.taskId must match when both are supplied.")
    task_id = task_id or argument_task_id
    if operation in {"task.update", "task.show", "observe", "source.locate", "source.edit", "component.preview", "component.revert", "prepare", "task.approve", "iterate", "verify", "input.click", "input.text", "report"} and not task_id:
        _fail(f"{operation} requires taskId.")
    normalized = {
        "protocolVersion": PROTOCOL_VERSION,
        "requestId": request_id,
        "operation": operation,
        "taskId": task_id,
        "context": context,
        "arguments": arguments,
    }
    return deepcopy(normalized)


def canonical_command_hash(command: dict[str, Any]) -> str:
    import hashlib

    encoded = json.dumps(command, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
