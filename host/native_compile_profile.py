from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CommandError, capability_unavailable
from .validation import ID_RE, SHA256_RE


PROFILE_SCHEMA = "relay.liveloop.native-compile-profiles"
PROFILE_DIGEST_SCHEMA = "relay.liveloop.native-compile-profile/2"
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ASSEMBLY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_PROFILE_BYTES = 4 * 1024 * 1024


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} fields do not match the native compile profile contract")
    return value


def _text(value: Any, label: str, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(char in value for char in "\r\n\0")
    ):
        raise ValueError(f"{label} must be bounded canonical text")
    value.encode("utf-8", errors="strict")
    return value


def _string_list(value: Any, label: str, *, maximum: int = 4096, item_maximum: int = 1024) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not isinstance(item, str) or not item or len(item) > item_maximum for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{label} must be a unique bounded string array")
    for item in value:
        item.encode("utf-8", errors="strict")
    return tuple(value)


def _ordinal_key(value: str) -> bytes:
    # .NET StringComparer.Ordinal compares UTF-16 code units.
    return value.encode("utf-16-be", errors="strict")


@dataclass(frozen=True, slots=True)
class NativeCompileAssemblyProfile:
    name: str
    module_id: str
    dependencies: tuple[str, ...]
    baseline_path: str
    baseline_sha256: str


@dataclass(frozen=True, slots=True)
class NativeCompileProfile:
    profile_id: str
    target: str
    project_root: Path
    unity_root: Path
    unity_version: str
    build_target: str
    build_target_group: str
    configuration: str
    development_build: bool
    subtarget: int
    extra_scripting_defines: tuple[str, ...]
    defines: tuple[str, ...]
    references: tuple[str, ...]
    source_inputs: tuple[str, ...]
    module_id: str
    dependency_closure: tuple[str, ...]
    initial_module_generation: int
    entry_assembly_name: str
    assemblies: tuple[NativeCompileAssemblyProfile, ...]
    timeout_seconds: float

    def context_assemblies(self) -> list[dict[str, Any]]:
        return [
            {
                "name": item.name,
                "moduleId": item.module_id,
                "dependencies": list(item.dependencies),
                "baselinePath": item.baseline_path,
                "baselineSha256": item.baseline_sha256,
            }
            for item in self.assemblies
        ]


class NativeCompileProfileRegistry:
    """Trusted, server-owned mapping from task target to a complete compile/baseline profile."""

    def __init__(self, profiles: list[NativeCompileProfile]) -> None:
        if not profiles:
            raise ValueError("At least one native compile profile is required")
        self._by_target: dict[str, NativeCompileProfile] = {}
        self._by_id: dict[str, NativeCompileProfile] = {}
        for profile in profiles:
            if profile.target in self._by_target or profile.profile_id in self._by_id:
                raise ValueError("Native compile profile target and profileId values must be unique")
            self._by_target[profile.target] = profile
            self._by_id[profile.profile_id] = profile

    @classmethod
    def load(cls, path: str | Path) -> NativeCompileProfileRegistry:
        candidate = Path(path).expanduser().resolve(strict=True)
        if not candidate.is_file() or candidate.stat().st_size > MAX_PROFILE_BYTES:
            raise ValueError("Native compile profile file must be a regular file no larger than 4 MiB")
        raw = candidate.read_bytes()
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Native compile profile file must be unique-field UTF-8 JSON") from exc
        return cls.from_value(value)

    @classmethod
    def from_value(cls, value: Any) -> NativeCompileProfileRegistry:
        document = _object(value, {"schema", "version", "profiles"}, "profile document")
        if document["schema"] != PROFILE_SCHEMA or type(document["version"]) is not int or document["version"] != 2:
            raise ValueError("Native compile profile schema/version is unsupported")
        raw_profiles = document["profiles"]
        if not isinstance(raw_profiles, list) or not 1 <= len(raw_profiles) <= 256:
            raise ValueError("profiles must contain 1..256 entries")
        return cls([cls._parse_profile(raw, index) for index, raw in enumerate(raw_profiles)])

    @staticmethod
    def _parse_profile(raw: Any, index: int) -> NativeCompileProfile:
        keys = {
            "profileId", "target", "projectRoot", "unityRoot", "unityVersion", "buildTarget", "buildTargetGroup",
            "configuration", "developmentBuild", "subtarget", "extraScriptingDefines", "defines",
            "references", "sourceInputs", "moduleId", "dependencyClosure", "initialModuleGeneration",
            "entryAssemblyName", "assemblies", "timeoutSeconds",
        }
        value = _object(raw, keys, f"profiles[{index}]")
        profile_id = _text(value["profileId"], "profileId", 128)
        target = _text(value["target"], "target", 2000)
        if not PROFILE_ID_RE.fullmatch(profile_id):
            raise ValueError("profileId is not a safe identifier")
        root_text = _text(value["projectRoot"], "projectRoot", 4096)
        root = Path(root_text).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("projectRoot must be an existing directory")
        unity_root_text = _text(value["unityRoot"], "unityRoot", 4096)
        unity_root = Path(unity_root_text).expanduser().resolve(strict=True)
        if not unity_root.is_dir():
            raise ValueError("unityRoot must be an existing directory")
        if root == unity_root or root in unity_root.parents or unity_root in root.parents:
            raise ValueError("projectRoot and unityRoot must be distinct, non-nested roots")
        unity_version = _text(value["unityVersion"], "unityVersion", 128)
        build_target = _text(value["buildTarget"], "buildTarget", 128)
        build_target_group = _text(value["buildTargetGroup"], "buildTargetGroup", 128)
        configuration = _text(value["configuration"], "configuration", 128)
        development_build = value["developmentBuild"]
        if configuration not in {"Debug", "Release"}:
            raise ValueError("configuration must be exactly Debug or Release")
        if type(development_build) is not bool or development_build != (configuration == "Debug"):
            raise ValueError("developmentBuild must exactly match the Debug/Release configuration")
        subtarget = value["subtarget"]
        if type(subtarget) is not int or not 0 <= subtarget <= 65535:
            raise ValueError("subtarget must be a bounded non-negative integer")
        extra_scripting_defines = _string_list(
            value["extraScriptingDefines"], "extraScriptingDefines", maximum=1024, item_maximum=512
        )
        defines = _string_list(value["defines"], "defines", maximum=1024, item_maximum=512)
        references = _string_list(value["references"], "references", maximum=4096)
        source_inputs = _string_list(value["sourceInputs"], "sourceInputs", maximum=4096)
        if not source_inputs:
            raise ValueError("sourceInputs must not be empty")
        module_id = _text(value["moduleId"], "moduleId", 128)
        if not ID_RE.fullmatch(module_id):
            raise ValueError("moduleId is not a safe identifier")
        dependency_closure = _string_list(value["dependencyClosure"], "dependencyClosure", maximum=128, item_maximum=256)
        if not dependency_closure or module_id not in dependency_closure or any(not ID_RE.fullmatch(item) for item in dependency_closure):
            raise ValueError("dependencyClosure must uniquely include the configured moduleId")
        initial_generation = value["initialModuleGeneration"]
        if type(initial_generation) is not int or initial_generation < 0:
            raise ValueError("initialModuleGeneration must be a non-negative integer")
        entry_name = _text(value["entryAssemblyName"], "entryAssemblyName", 128)
        if not ASSEMBLY_NAME_RE.fullmatch(entry_name):
            raise ValueError("entryAssemblyName is not a safe assembly identifier")
        raw_assemblies = value["assemblies"]
        if not isinstance(raw_assemblies, list) or not 1 <= len(raw_assemblies) <= 63:
            raise ValueError("assemblies must contain 1..63 entries so the complete DLL/PDB closure fits the Host contract")
        assemblies: list[NativeCompileAssemblyProfile] = []
        names: set[str] = set()
        for assembly_index, raw_assembly in enumerate(raw_assemblies):
            assembly = _object(
                raw_assembly,
                {"name", "moduleId", "dependencies", "baselinePath", "baselineSha256"},
                f"profiles[{index}].assemblies[{assembly_index}]",
            )
            name = _text(assembly["name"], "assembly.name", 128)
            assembly_module = _text(assembly["moduleId"], "assembly.moduleId", 128)
            dependencies = _string_list(assembly["dependencies"], "assembly.dependencies", maximum=128, item_maximum=128)
            baseline_relative = _text(assembly["baselinePath"], "assembly.baselinePath", 2048)
            baseline_hash = assembly["baselineSha256"]
            if not ASSEMBLY_NAME_RE.fullmatch(name) or assembly_module not in dependency_closure:
                raise ValueError("Assembly name/moduleId does not belong to the profile closure")
            if name in names:
                raise ValueError("Assembly names must be unique")
            names.add(name)
            if name in dependencies:
                raise ValueError("An assembly cannot depend on itself")
            if not isinstance(baseline_hash, str) or not SHA256_RE.fullmatch(baseline_hash):
                raise ValueError("baselineSha256 must be a lowercase SHA-256 value")
            relative_path = Path(baseline_relative)
            if relative_path.is_absolute() or any(part in {"..", ""} for part in relative_path.parts):
                raise ValueError("baselinePath must remain relative to projectRoot")
            baseline_lexical = Path(os.path.abspath(root / relative_path))
            _reject_reparse_path(root, baseline_lexical)
            baseline = baseline_lexical.resolve(strict=True)
            try:
                baseline.relative_to(root)
            except ValueError as exc:
                raise ValueError("baselinePath escaped projectRoot") from exc
            if not baseline.is_file():
                raise ValueError("baselinePath must refer to a regular file")
            assemblies.append(
                NativeCompileAssemblyProfile(
                    name=name,
                    module_id=assembly_module,
                    dependencies=dependencies,
                    baseline_path=relative_path.as_posix(),
                    baseline_sha256=baseline_hash,
                )
            )
        if entry_name not in names:
            raise ValueError("entryAssemblyName must be in the compiled assembly set")
        if any(not set(item.dependencies).issubset(names) for item in assemblies):
            raise ValueError("Assembly dependencies must be covered by the compiled assembly closure")
        timeout = value["timeoutSeconds"]
        if type(timeout) not in {int, float} or not 0.1 <= float(timeout) <= 3600:
            raise ValueError("timeoutSeconds must be between 0.1 and 3600")
        return NativeCompileProfile(
            profile_id=profile_id,
            target=target,
            project_root=root,
            unity_root=unity_root,
            unity_version=unity_version,
            build_target=build_target,
            build_target_group=build_target_group,
            configuration=configuration,
            development_build=development_build,
            subtarget=subtarget,
            extra_scripting_defines=extra_scripting_defines,
            defines=defines,
            references=references,
            source_inputs=source_inputs,
            module_id=module_id,
            dependency_closure=dependency_closure,
            initial_module_generation=initial_generation,
            entry_assembly_name=entry_name,
            assemblies=tuple(assemblies),
            timeout_seconds=float(timeout),
        )

    def for_task(self, task: dict[str, Any]) -> NativeCompileProfile:
        target = task.get("target")
        if not isinstance(target, str):
            raise capability_unavailable("native_compile", "Task target has no server-configured compile profile.")
        profile = self._by_target.get(target)
        if profile is None:
            raise capability_unavailable("native_compile", "Task target has no server-configured compile profile.")
        return profile


def _reject_reparse_path(root: Path, candidate: Path) -> None:
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode) or getattr(
        root_info, "st_file_attributes", 0
    ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise ValueError("Trusted source root is missing, non-directory, or reparse-backed")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Source input escaped projectRoot") from exc
    current = root
    for part in relative.parts:
        current = current / part
        info = current.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or attributes & reparse:
            raise ValueError("Source inputs cannot traverse reparse points")


def native_input_snapshot(profile: NativeCompileProfile) -> str:
    """Compute the same length-prefixed digest as the Unity EditorInputSnapshot implementation."""
    digest = hashlib.sha256()

    def add_bytes(value: bytes) -> None:
        digest.update(value)

    def add_length(value: int) -> None:
        add_bytes(struct.pack("<i", value))

    def add_long(value: int) -> None:
        add_bytes(struct.pack("<q", value))

    def add_string(value: str) -> None:
        encoded = value.encode("utf-8", errors="strict")
        add_length(len(encoded))
        add_bytes(encoded)

    add_string(profile.build_target)
    add_string(profile.configuration)
    for values in (profile.defines, profile.references):
        ordered = sorted(values, key=_ordinal_key)
        add_length(len(ordered))
        for item in ordered:
            add_string(item)

    root = profile.project_root.resolve(strict=True)
    normalized: list[tuple[str, Path]] = []
    seen_relative: set[str] = set()
    for source in profile.source_inputs:
        raw = Path(source)
        if raw.is_absolute():
            raise ValueError("sourceInputs must be project-relative")
        candidate = Path(os.path.abspath(root / raw))
        _reject_reparse_path(root, candidate)
        if not candidate.is_file():
            raise FileNotFoundError(f"Source input is missing: {source}")
        relative = candidate.relative_to(root).as_posix()
        if relative in seen_relative:
            raise ValueError("sourceInputs resolve to duplicate project files")
        seen_relative.add(relative)
        normalized.append((relative, candidate))
    normalized.sort(key=lambda item: _ordinal_key(item[0]))
    add_length(len(normalized))
    for relative, candidate in normalized:
        add_string(relative)
        size = candidate.stat().st_size
        add_long(size)
        with candidate.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                add_bytes(chunk)
    return "sha256:" + digest.hexdigest()


def native_compile_profile_digest(profile: NativeCompileProfile) -> str:
    """Bind every normalized compile, routing, closure, and baseline field in a server profile."""
    document = {
        "schema": PROFILE_DIGEST_SCHEMA,
        "version": 2,
        "profileId": profile.profile_id,
        "target": profile.target,
        "projectRoot": profile.project_root.as_posix(),
        "unityRoot": profile.unity_root.as_posix(),
        "unityVersion": profile.unity_version,
        "buildTarget": profile.build_target,
        "buildTargetGroup": profile.build_target_group,
        "configuration": profile.configuration,
        "developmentBuild": profile.development_build,
        "subtarget": profile.subtarget,
        "extraScriptingDefines": list(profile.extra_scripting_defines),
        "defines": list(profile.defines),
        "references": list(profile.references),
        "sourceInputs": list(profile.source_inputs),
        "moduleId": profile.module_id,
        "dependencyClosure": list(profile.dependency_closure),
        "initialModuleGeneration": profile.initial_module_generation,
        "entryAssemblyName": profile.entry_assembly_name,
        "assemblies": [
            {
                "name": item.name,
                "moduleId": item.module_id,
                "dependencies": list(item.dependencies),
                "baselinePath": item.baseline_path,
                "baselineSha256": item.baseline_sha256,
            }
            for item in profile.assemblies
        ],
        "timeoutSeconds": profile.timeout_seconds,
    }
    canonical = json.dumps(
        document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def verify_profile_baselines(profile: NativeCompileProfile) -> None:
    for assembly in profile.assemblies:
        lexical = Path(os.path.abspath(profile.project_root / assembly.baseline_path))
        try:
            _reject_reparse_path(profile.project_root, lexical)
        except (OSError, ValueError) as exc:
            raise CommandError("AUTH_REQUIRED", "Configured baseline path crosses a reparse point or escaped the project root.", stage="prepare", runtime_changed=False, recoverable=False) from exc
        path = lexical.resolve(strict=True)
        try:
            path.relative_to(profile.project_root)
        except ValueError as exc:
            raise CommandError("AUTH_REQUIRED", "Configured baseline escaped the project root.", stage="prepare", runtime_changed=False, recoverable=False) from exc
        if not path.is_file():
            raise CommandError("INPUT_CHANGED", "Configured baseline assembly is missing.", stage="prepare", runtime_changed=False, recoverable=False)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != assembly.baseline_sha256:
            raise CommandError("INPUT_CHANGED", "Configured immutable baseline hash no longer matches.", stage="prepare", runtime_changed=False, recoverable=False)
