from __future__ import annotations

import hashlib
import json
import re
from typing import Any


CHANGE_ANALYSIS_SCHEMA = "relay.liveloop.change-analysis"
ROUTE_SELECTION_SCHEMA = "relay.liveloop.auto-route-selection"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DISPOSITIONS = frozenset({"BODY_ONLY", "STRUCTURE", "UNCHANGED", "UNKNOWN"})
_RESOURCE_DISPOSITIONS = frozenset({"CHANGED", "UNCHANGED", "UNKNOWN"})


class RouteSelectionError(ValueError):
    """A classification or route cannot be trusted or executed safely."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _is_snapshot(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and _SHA256_RE.fullmatch(value[7:]) is not None


def classify_native_assemblies(profile: Any, assemblies: Any) -> tuple[str, bool]:
    """Classify verified compiler analysis without accepting operator-selected routes."""
    if not isinstance(assemblies, list) or not assemblies:
        raise RouteSelectionError("Native assembly analysis is absent or incomplete.")
    changed: list[dict[str, Any]] = []
    for item in assemblies:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"]
            or not isinstance(item.get("moduleId"), str)
            or not item["moduleId"]
            or type(item.get("structureEqual")) is not bool
            or not isinstance(item.get("changedMethods"), list)
        ):
            raise RouteSelectionError("Native assembly analysis is malformed; route is unknown.")
        if any(
            not isinstance(method, dict)
            or set(method) != {"typeName", "signature"}
            or not isinstance(method.get("typeName"), str)
            or not method["typeName"]
            or not isinstance(method.get("signature"), str)
            or not method["signature"]
            for method in item["changedMethods"]
        ):
            raise RouteSelectionError("Native changed-method analysis is malformed; route is unknown.")
        if not item["structureEqual"] or item["changedMethods"]:
            changed.append(item)
    if not changed:
        return "UNCHANGED", False
    if any(not item["structureEqual"] for item in changed):
        return "STRUCTURE", False
    hotfix_shape_verified = (
        len(changed) == 1
        and changed[0]["moduleId"] == getattr(profile, "module_id", None)
        and bool(changed[0]["changedMethods"])
    )
    return "BODY_ONLY", hotfix_shape_verified


def make_code_analysis(
    profile: Any,
    assemblies: list[dict[str, Any]],
    *,
    input_snapshot: str,
    profile_digest: str,
    provider_id: str,
) -> dict[str, Any]:
    if not _is_snapshot(input_snapshot) or not _is_snapshot(profile_digest) or not isinstance(provider_id, str) or not provider_id:
        raise RouteSelectionError("Code change analysis lacks a valid input/profile/provider binding.")
    disposition, hotfix_shape_verified = classify_native_assemblies(profile, assemblies)
    material = {
        "schema": CHANGE_ANALYSIS_SCHEMA,
        "version": 1,
        "component": "code",
        "disposition": disposition,
        "inputSnapshot": input_snapshot,
        "profileDigest": profile_digest,
        "providerId": provider_id,
        "assemblies": assemblies,
    }
    return {
        "schema": CHANGE_ANALYSIS_SCHEMA,
        "version": 1,
        "component": "code",
        "disposition": disposition,
        "inputSnapshot": input_snapshot,
        "profileDigest": profile_digest,
        "providerId": provider_id,
        "evidenceSha256": _sha256(material),
        "hotfixShapeVerified": hotfix_shape_verified,
    }


def validate_component_analysis(
    value: Any,
    *,
    component: str,
    provider_id: str,
    input_snapshot: str,
    profile_digest: str,
) -> dict[str, Any]:
    common = {
        "schema", "version", "component", "disposition", "inputSnapshot", "profileDigest", "providerId", "evidenceSha256",
    }
    expected = common | ({"hotfixShapeVerified"} if component == "code" else set())
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema") != CHANGE_ANALYSIS_SCHEMA
        or type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("component") != component
        or not isinstance(provider_id, str)
        or not provider_id
        or value.get("providerId") != provider_id
        or value.get("inputSnapshot") != input_snapshot
        or value.get("profileDigest") != profile_digest
        or not _is_snapshot(value.get("inputSnapshot"))
        or not _is_snapshot(value.get("profileDigest"))
        or not isinstance(value.get("evidenceSha256"), str)
        or not _SHA256_RE.fullmatch(value["evidenceSha256"])
    ):
        raise RouteSelectionError(f"{component} change analysis is malformed or not bound to current inputs.")
    allowed = _DISPOSITIONS if component == "code" else _RESOURCE_DISPOSITIONS
    if value.get("disposition") not in allowed:
        raise RouteSelectionError(f"{component} change classification is unknown.")
    if component == "code" and type(value.get("hotfixShapeVerified")) is not bool:
        raise RouteSelectionError("Code Hotfix compatibility classification is malformed.")
    return dict(value)


def select_route(code: dict[str, Any], resource: dict[str, Any]) -> dict[str, Any]:
    """Select a route solely from verified component analyses; UNKNOWN always fails closed."""
    try:
        code = validate_component_analysis(
            code,
            component="code",
            provider_id=code.get("providerId") if isinstance(code, dict) else None,
            input_snapshot=code.get("inputSnapshot") if isinstance(code, dict) else None,
            profile_digest=code.get("profileDigest") if isinstance(code, dict) else None,
        )
        resource = validate_component_analysis(
            resource,
            component="resource",
            provider_id=resource.get("providerId") if isinstance(resource, dict) else None,
            input_snapshot=resource.get("inputSnapshot") if isinstance(resource, dict) else None,
            profile_digest=resource.get("profileDigest") if isinstance(resource, dict) else None,
        )
    except RouteSelectionError as exc:
        raise RouteSelectionError("A required component classification is missing, malformed, or unknown.") from exc
    code_disposition = code["disposition"]
    resource_disposition = resource["disposition"]
    if code_disposition not in _DISPOSITIONS or resource_disposition not in _RESOURCE_DISPOSITIONS:
        raise RouteSelectionError("A required component classification is missing or unknown.")
    if "UNKNOWN" in {code_disposition, resource_disposition}:
        raise RouteSelectionError("A component classification is unknown; automatic routing is refused.")
    if code_disposition == "BODY_ONLY" and code.get("hotfixShapeVerified") is not True:
        raise RouteSelectionError("Body-only changes do not fit the verified single-assembly Hotfix contract.")
    if code_disposition == "UNCHANGED" and resource_disposition == "UNCHANGED":
        route = "NO_CHANGES"
    elif code_disposition == "UNCHANGED":
        route = "RESOURCE_ONLY"
    elif code_disposition == "BODY_ONLY" and resource_disposition == "UNCHANGED":
        route = "HOTFIX"
    elif code_disposition == "BODY_ONLY":
        route = "HOTFIX_AND_ASSET_RELOAD"
    elif resource_disposition == "UNCHANGED":
        route = "MODULE_RELOAD"
    else:
        route = "MODULE_AND_ASSET_RELOAD"
    value = {
        "schema": ROUTE_SELECTION_SCHEMA,
        "version": 1,
        "route": route,
        "codeAnalysis": dict(code),
        "resourceAnalysis": dict(resource),
    }
    value["selectionSha256"] = _sha256(value)
    return value


def verify_route_selection(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "version", "route", "codeAnalysis", "resourceAnalysis", "selectionSha256"}
        or value.get("schema") != ROUTE_SELECTION_SCHEMA
        or type(value.get("version")) is not int
        or value["version"] != 1
        or not isinstance(value.get("selectionSha256"), str)
        or not _SHA256_RE.fullmatch(value["selectionSha256"])
    ):
        raise RouteSelectionError("Automatic route-selection evidence is malformed.")
    body = {key: value[key] for key in ("schema", "version", "route", "codeAnalysis", "resourceAnalysis")}
    if _sha256(body) != value["selectionSha256"]:
        raise RouteSelectionError("Automatic route-selection evidence hash differs.")
    expected = select_route(value["codeAnalysis"], value["resourceAnalysis"])
    if expected != value:
        raise RouteSelectionError("Automatic route-selection evidence does not match component analyses.")
    return dict(value)
