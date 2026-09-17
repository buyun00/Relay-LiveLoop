from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import CommandError
from .native_compile_profile import (
    NativeCompileProfile,
    _reject_reparse_path,
    _ordinal_key,
    native_compile_profile_digest,
)
from .validation import SHA256_RE


RECEIPT_SCHEMA = "relay.liveloop.native-compile-input-receipt"
RECEIPT_VERSION = 1
COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY = "ENUMERATED_ONLY_NOT_PROVEN_COMPLETE"
REQUIRED_COMPILER_INPUT_LIMITATIONS = frozenset({
    "ACTUAL_COMPILER_PROCESS_IDENTITY_NOT_PROVEN",
    "ADDITIONAL_COMPILER_ARGUMENTS_MAY_REFERENCE_UNENUMERATED_FILES_NOT_PROVEN",
    "ANALYZER_OR_SOURCE_GENERATOR_TRANSITIVE_FILE_READS_NOT_PROVEN",
})
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_INPUTS = 100000
MAX_RECEIPT_ASSEMBLIES = 4096
MAX_RECEIPT_OUTPUTS = 4096
HYBRIDCLR_PACKAGE_RELATIVE = "Packages/com.code-philosophy.hybridclr"
ROSYLN_RELATIVE = "Editor/Data/MonoBleedingEdge/lib/mono/msbuild/Current/bin/Roslyn"
BUILDPIPELINE_RELATIVE = "Editor/Data/Tools/BuildPipeline"
TOOLCHAIN_REQUIRED_RELATIVE = (
    "Editor/Unity.exe",
    "Editor/Data/Managed/UnityEngine/UnityEditor.CoreModule.dll",
    "Editor/Data/Managed/UnityEngine/UnityEngine.CoreModule.dll",
    "Editor/Data/Managed/UnityEngine/UnityEditor.dll",
    "Editor/Data/MonoBleedingEdge/bin/mono.exe",
    "Editor/Data/MonoBleedingEdge/lib/mono/net_4_x-win32/Facades/netstandard.dll",
    f"{ROSYLN_RELATIVE}/csc.exe",
    f"{ROSYLN_RELATIVE}/csc.exe.config",
    f"{ROSYLN_RELATIVE}/csc.rsp",
    f"{ROSYLN_RELATIVE}/Microsoft.Build.Tasks.CodeAnalysis.dll",
    f"{ROSYLN_RELATIVE}/Microsoft.CodeAnalysis.dll",
    f"{ROSYLN_RELATIVE}/Microsoft.CodeAnalysis.CSharp.dll",
    f"{ROSYLN_RELATIVE}/System.Collections.Immutable.dll",
    f"{ROSYLN_RELATIVE}/System.Memory.dll",
    f"{ROSYLN_RELATIVE}/System.Reflection.Metadata.dll",
    f"{ROSYLN_RELATIVE}/System.Runtime.CompilerServices.Unsafe.dll",
    f"{ROSYLN_RELATIVE}/System.Threading.Tasks.Extensions.dll",
)
CONFIGURATION_ROLES = frozenset({
    "project-settings",
    "package-manifest",
    "assembly-definition",
    "hybridclr-package",
})
ASSEMBLY_GRAPH_FIELDS = {
    "name", "sourceFiles", "allReferences", "compilerOptions", "defines", "flags",
}
COMPILER_OPTIONS_FIELDS = {
    "additionalCompilerArguments", "allowUnsafeCode", "analyzerConfigPath", "apiCompatibilityLevel",
    "codeOptimization", "languageVersion", "responseFiles",
    "roslynAdditionalFilePaths", "roslynAnalyzerDllPaths", "roslynAnalyzerRulesetPath",
}


def _fail(code: str, message: str) -> None:
    raise CommandError(
        code,
        message,
        stage="compile_input_receipt",
        runtime_changed=False,
        recoverable=False,
    )


def _bounded_text(value: Any, label: str, maximum: int = 4096, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > maximum
        or any(character in value for character in "\r\n\0")
    ):
        raise ValueError(f"{label} is not bounded canonical text")
    value.encode("utf-8", errors="strict")
    return value


def _string_array(value: Any, label: str, *, maximum: int = 100000, allow_empty: bool = True) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or len(value) > maximum
        or any(not isinstance(item, str) or not item or len(item) > 4096 for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{label} is not a unique bounded string array")
    for item in value:
        item.encode("utf-8", errors="strict")
    return list(value)


def validate_compiler_input_limitations(value: Any) -> list[str]:
    limitations = _string_array(value, "compiler input limitations", maximum=32, allow_empty=False)
    if not REQUIRED_COMPILER_INPUT_LIMITATIONS.issubset(limitations):
        raise ValueError("Compiler input limitations omit a required non-hermetic boundary")
    return limitations


def validate_compiler_input_coverage(value: Any) -> list[str]:
    if not isinstance(value, dict) or value.get("compilerInputCoverage") != COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY:
        raise ValueError("Compiler inputs must be classified as enumerated-only, not proven complete")
    return validate_compiler_input_limitations(value.get("compilerInputLimitations"))


def _is_reparse(info: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse)


def _walk_regular_files(directory: Path) -> list[Path]:
    root = Path(os.path.abspath(directory))
    if not root.is_dir() or _is_reparse(root.lstat()):
        raise ValueError("Receipt inventory root is missing or reparse-backed")
    result: list[Path] = []
    pending = [root]
    while pending:
        current = pending.pop()
        for entry in os.scandir(current):
            candidate = Path(entry.path)
            info = candidate.lstat()
            if _is_reparse(info):
                raise ValueError("Receipt inventory cannot traverse reparse points")
            if stat.S_ISDIR(info.st_mode):
                pending.append(candidate)
            elif stat.S_ISREG(info.st_mode):
                result.append(candidate)
            else:
                raise ValueError("Receipt inventory contains a non-regular filesystem entry")
    return result


def _relative_under(root: Path, path: Path) -> str:
    lexical = Path(os.path.abspath(path))
    _reject_reparse_path(root, lexical)
    resolved = lexical.resolve(strict=True)
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("Receipt file escaped its trusted root") from exc
    _safe_relative(relative)
    return relative


def _safe_relative(value: Any) -> str:
    relative = _bounded_text(value, "relative path", 4096)
    if "\\" in relative or ":" in relative:
        raise ValueError("Receipt paths must use project-relative forward-slash paths")
    candidate = PurePosixPath(relative)
    if relative == "." or candidate.is_absolute() or candidate.as_posix() != relative or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("Receipt path is rooted or traverses outside its root")
    return candidate.as_posix()


def _add_config_path(entries: dict[str, set[str]], root: Path, candidate: Path, role: str) -> None:
    relative = _relative_under(root, candidate)
    entries.setdefault(relative, set()).add(role)


def expected_project_configuration_inputs(profile: NativeCompileProfile) -> dict[str, set[str]]:
    """Return the live project configuration closure independently derived by the Host."""
    project = profile.project_root
    entries: dict[str, set[str]] = {}

    project_settings = project / "ProjectSettings"
    for path in _walk_regular_files(project_settings):
        _add_config_path(entries, project, path, "project-settings")

    for relative in ("Packages/manifest.json", "Packages/packages-lock.json"):
        path = project / PurePosixPath(relative)
        if not path.is_file():
            raise ValueError(f"Required Unity package resolution file is missing: {relative}")
        _add_config_path(entries, project, path, "package-manifest")

    for directory_name in ("Assets", "Packages", "Library/PackageCache"):
        directory = project / directory_name
        if not directory.is_dir() and directory_name == "Library/PackageCache":
            continue
        for path in _walk_regular_files(directory):
            if path.suffix.lower() not in {".asmdef", ".asmref"}:
                continue
            _add_config_path(entries, project, path, "assembly-definition")
            meta = Path(str(path) + ".meta")
            if not meta.is_file():
                raise ValueError("Assembly definition/reference is missing its Unity .meta file")
            _add_config_path(entries, project, meta, "assembly-definition")

    hybridclr_root = project / PurePosixPath(HYBRIDCLR_PACKAGE_RELATIVE)
    for path in _walk_regular_files(hybridclr_root):
        _add_config_path(entries, project, path, "hybridclr-package")

    return entries


def expected_unity_toolchain_inputs(profile: NativeCompileProfile) -> dict[str, set[str]]:
    unity = profile.unity_root
    paths = set(TOOLCHAIN_REQUIRED_RELATIVE)
    for directory_relative in (ROSYLN_RELATIVE, BUILDPIPELINE_RELATIVE):
        directory = unity / PurePosixPath(directory_relative)
        for path in _walk_regular_files(directory):
            paths.add(_relative_under(unity, path))
    result: dict[str, set[str]] = {}
    for relative in paths:
        candidate = unity / PurePosixPath(relative)
        if not candidate.is_file():
            raise ValueError(f"Required Unity toolchain file is missing: {relative}")
        normalized = _relative_under(unity, candidate)
        result.setdefault(normalized, set()).add("unity-toolchain")
    return result


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _add_string(digest: Any, value: str) -> None:
    encoded = value.encode("utf-8", errors="strict")
    digest.update(struct.pack("<i", len(encoded)))
    digest.update(encoded)


def native_compile_input_set_digest(inputs: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    ordered = sorted(inputs, key=lambda item: (_ordinal_key(item["scope"]), _ordinal_key(item["path"])))
    for item in ordered:
        _add_string(digest, item["scope"])
        _add_string(digest, item["path"])
        roles = sorted(item["roles"], key=_ordinal_key)
        digest.update(struct.pack("<i", len(roles)))
        for role in roles:
            _add_string(digest, role)
        _add_string(digest, item["sha256"])
        digest.update(struct.pack("<q", item["size"]))
    return digest.hexdigest()


def _absolute_receipt_key(profile: NativeCompileProfile, raw: Any) -> tuple[str, str]:
    absolute = _bounded_text(raw, "compiler input path", 8192)
    candidate = Path(absolute)
    if not candidate.is_absolute():
        raise ValueError("Compiler graph paths must be absolute")
    lexical = Path(os.path.abspath(candidate))
    for scope, root in (("project", profile.project_root), ("unity", profile.unity_root)):
        try:
            relative = _relative_under(root, lexical)
            return scope, relative
        except (OSError, RuntimeError, ValueError):
            continue
    raise ValueError("Compiler graph path is outside server-owned project and Unity roots")


def _require_recorded_role(
    profile: NativeCompileProfile,
    inputs: dict[tuple[str, str], dict[str, Any]],
    absolute_path: Any,
    role: str,
) -> None:
    key = _absolute_receipt_key(profile, absolute_path)
    record = inputs.get(key)
    if record is None or role not in record["roles"]:
        raise ValueError("Compiler graph input is absent from the hashed receipt closure")


def _validate_compiler_options(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != COMPILER_OPTIONS_FIELDS:
        raise ValueError(f"{label} fields differ from the Unity compiler-options contract")
    arguments = value["additionalCompilerArguments"]
    if (
        not isinstance(arguments, list)
        or len(arguments) > 4096
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 8192
            or any(character in item for character in "\r\n\0")
            for item in arguments
        )
    ):
        raise ValueError(f"{label}.additionalCompilerArguments is not a bounded argument array")
    for argument in arguments:
        argument.encode("utf-8", errors="strict")
    if type(value["allowUnsafeCode"]) is not bool:
        raise ValueError(f"{label} boolean compiler options are invalid")
    for field in ("apiCompatibilityLevel", "codeOptimization", "languageVersion"):
        _bounded_text(value[field], f"{label}.{field}", 512)
    for field in ("analyzerConfigPath", "roslynAnalyzerRulesetPath"):
        if value[field] not in (None, ""):
            _bounded_text(value[field], f"{label}.{field}", 8192)
    for field in ("responseFiles", "roslynAdditionalFilePaths", "roslynAnalyzerDllPaths"):
        _string_array(value[field], f"{label}.{field}", maximum=4096)
    return value


def validate_native_compile_input_receipt(
    value: Any,
    profile: NativeCompileProfile,
    *,
    job_id: str,
    receipt_path: str | Path,
    expected_output_files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a sealed Editor receipt and rehash all recorded inputs/outputs now."""
    try:
        required = {
            "schema", "version", "profileDigest", "jobId", "inputSnapshot", "projectRoot", "unityRoot", "unityVersion",
            "compileApi", "compileResultStatus", "typeDbPresent", "compilerProcessIdentityStatus", "limitations", "target", "graphMatch",
            "assemblies", "compileResultAssemblies", "inputs", "inputSetSha256Before", "inputSetSha256After", "inputSetMatches",
            "outputs",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("Receipt fields do not match the frozen compile-input contract")
        if value["schema"] != RECEIPT_SCHEMA or type(value["version"]) is not int or value["version"] != RECEIPT_VERSION:
            raise ValueError("Receipt schema/version is unsupported")
        if value["jobId"] != job_id or not isinstance(value["profileDigest"], str):
            raise ValueError("Receipt job/profile identity is malformed")
        _bounded_text(value["inputSnapshot"], "inputSnapshot", 71)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value["inputSnapshot"]):
            raise ValueError("Receipt source snapshot is malformed")
        _bounded_text(value["profileDigest"], "profileDigest", 71)
        if value["profileDigest"] != native_compile_profile_digest(profile):
            raise ValueError("Receipt profile digest differs from the current server-owned profile")
        if value["projectRoot"] != profile.project_root.as_posix() or value["unityRoot"] != profile.unity_root.as_posix():
            raise ValueError("Receipt roots differ from the server-owned profile")
        if value["unityVersion"] != profile.unity_version:
            raise ValueError("Receipt Unity version differs from the server-owned profile")
        _bounded_text(value["unityVersion"], "unityVersion", 128)
        if value["compileApi"] != "UnityEditor.Build.Player.PlayerBuildInterface.CompilePlayerScripts":
            raise ValueError("Receipt does not identify the supported Unity compile API")
        if value["compileResultStatus"] != "SUCCESS" or type(value["typeDbPresent"]) is not bool or value["typeDbPresent"] is not True:
            raise ValueError("Receipt does not bind a successful compile result and non-null type database")
        if value["compilerProcessIdentityStatus"] != "NOT_PROVEN":
            raise ValueError("Receipt must preserve the known unverified compiler-process boundary")
        validate_compiler_input_limitations(value["limitations"])
        if type(value["graphMatch"]) is not bool or value["graphMatch"] is not True:
            raise ValueError("Unity Player graph did not match the exact compile result graph")
        if type(value["inputSetMatches"]) is not bool or value["inputSetMatches"] is not True:
            raise ValueError("Receipt before/after input snapshots do not match")

        target = value["target"]
        target_keys = {
            "requested", "activeBefore", "activeAfter", "group", "subtarget", "options",
            "developmentBuild", "extraScriptingDefines", "extraScriptingDefinesWasNull",
        }
        if not isinstance(target, dict) or set(target) != target_keys:
            raise ValueError("Receipt compile settings fields are invalid")
        expected_options = "DevelopmentBuild" if profile.development_build else "None"
        if (
            target["requested"] != profile.build_target
            or target["activeBefore"] != profile.build_target
            or target["activeAfter"] != profile.build_target
            or target["group"] != profile.build_target_group
            or type(target["subtarget"]) is not int
            or target["subtarget"] != profile.subtarget
            or target["options"] != expected_options
            or type(target["developmentBuild"]) is not bool
            or target["developmentBuild"] != profile.development_build
            or target["extraScriptingDefines"] != list(profile.extra_scripting_defines)
            or type(target["extraScriptingDefinesWasNull"]) is not bool
            or target["extraScriptingDefinesWasNull"] != (not profile.extra_scripting_defines)
        ):
            raise ValueError("Receipt settings differ from the server-owned target profile")

        raw_inputs = value["inputs"]
        if not isinstance(raw_inputs, list) or not 1 <= len(raw_inputs) <= MAX_RECEIPT_INPUTS:
            raise ValueError("Receipt input set is empty or exceeds its bounded contract")
        inputs_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for index, raw in enumerate(raw_inputs):
            if not isinstance(raw, dict) or set(raw) != {"scope", "path", "roles", "sha256", "size"}:
                raise ValueError(f"Receipt inputs[{index}] fields are invalid")
            scope = raw["scope"]
            if scope not in {"project", "unity"}:
                raise ValueError(f"Receipt inputs[{index}] scope is invalid")
            relative = _safe_relative(raw["path"])
            roles = _string_array(raw["roles"], f"Receipt inputs[{index}].roles", maximum=4096, allow_empty=False)
            for role in roles:
                _bounded_text(role, "input role", 512)
            digest = raw["sha256"]
            size = raw["size"]
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest) or type(size) is not int or size < 0:
                raise ValueError(f"Receipt inputs[{index}] digest/size is invalid")
            key = (scope, relative)
            if key in inputs_by_key:
                raise ValueError("Receipt repeats an input path")
            inputs_by_key[key] = {"scope": scope, "path": relative, "roles": roles, "sha256": digest, "size": size}

        before = value["inputSetSha256Before"]
        after = value["inputSetSha256After"]
        if not isinstance(before, str) or not SHA256_RE.fullmatch(before) or not isinstance(after, str) or not SHA256_RE.fullmatch(after):
            raise ValueError("Receipt before/after input digests are malformed")
        computed_set_digest = native_compile_input_set_digest(raw_inputs)
        if before != computed_set_digest or after != computed_set_digest:
            raise ValueError("Receipt before/after input digest does not bind the listed inputs")

        expected_config = expected_project_configuration_inputs(profile)
        recorded_config: set[tuple[str, str, str]] = set()
        for (scope, relative), raw in inputs_by_key.items():
            if scope == "project":
                for role in raw["roles"]:
                    if role in CONFIGURATION_ROLES:
                        recorded_config.add((relative, role, scope))
        expected_config_roles = {
            (relative, role, "project")
            for relative, roles in expected_config.items()
            for role in roles
        }
        if recorded_config != expected_config_roles:
            raise ValueError("Receipt project/package/HybridCLR configuration closure is incomplete or stale")

        expected_toolchain = expected_unity_toolchain_inputs(profile)
        recorded_toolchain = {
            (relative, "unity")
            for (scope, relative), raw in inputs_by_key.items()
            if scope == "unity" and "unity-toolchain" in raw["roles"]
        }
        expected_toolchain_set = {(relative, "unity") for relative in expected_toolchain}
        if recorded_toolchain != expected_toolchain_set:
            raise ValueError("Receipt Unity toolchain file set is incomplete or stale")

        for (scope, relative), raw in inputs_by_key.items():
            root = profile.project_root if scope == "project" else profile.unity_root
            candidate = Path(os.path.abspath(root / PurePosixPath(relative)))
            _reject_reparse_path(root, candidate)
            resolved = candidate.resolve(strict=True)
            if not resolved.is_file():
                raise ValueError("Receipt input is not a regular file")
            actual_hash, actual_size = _hash_file(resolved)
            if actual_hash != raw["sha256"] or actual_size != raw["size"]:
                _fail("INPUT_CHANGED", "A compiler input, project setting, HybridCLR file, or toolchain file changed after receipt capture.")

        assemblies = value["assemblies"]
        if not isinstance(assemblies, list) or not 1 <= len(assemblies) <= MAX_RECEIPT_ASSEMBLIES:
            raise ValueError("Receipt compile-result assembly graph is empty or too large")
        names: set[str] = set()
        for index, assembly in enumerate(assemblies):
            if not isinstance(assembly, dict) or set(assembly) != ASSEMBLY_GRAPH_FIELDS:
                raise ValueError(f"Receipt assemblies[{index}] fields are invalid")
            name = _bounded_text(assembly["name"], f"Receipt assemblies[{index}].name", 256)
            if name in names:
                raise ValueError("Receipt assembly graph repeats an assembly name")
            names.add(name)
            source_files = _string_array(assembly["sourceFiles"], f"{name}.sourceFiles", allow_empty=False)
            references = _string_array(assembly["allReferences"], f"{name}.allReferences", allow_empty=True)
            defines = _string_array(assembly["defines"], f"{name}.defines", maximum=4096)
            _bounded_text(assembly["flags"], f"{name}.flags", 512)
            options = _validate_compiler_options(assembly["compilerOptions"], f"{name}.compilerOptions")
            for source in source_files:
                _require_recorded_role(profile, inputs_by_key, source, f"assembly-source:{name}")
            for reference in references:
                _require_recorded_role(profile, inputs_by_key, reference, f"assembly-reference:{name}")
            path_fields = (
                ("analyzerConfigPath", "compiler-analyzer-config"),
                ("roslynAnalyzerRulesetPath", "compiler-analyzer-ruleset"),
            )
            for field, role_prefix in path_fields:
                if options[field] not in (None, ""):
                    _require_recorded_role(profile, inputs_by_key, options[field], f"{role_prefix}:{name}")
            for field, role_prefix in (
                ("responseFiles", "compiler-response"),
                ("roslynAdditionalFilePaths", "compiler-additional-file"),
                ("roslynAnalyzerDllPaths", "compiler-analyzer-dll"),
            ):
                for path in options[field]:
                    _require_recorded_role(profile, inputs_by_key, path, f"{role_prefix}:{name}")

        receipt_parent = Path(receipt_path).resolve(strict=True).parent
        if not receipt_parent.is_dir() or _is_reparse(receipt_parent.lstat()):
            raise ValueError("Receipt output root is missing or reparse-backed")
        raw_compile_results = value["compileResultAssemblies"]
        if not isinstance(raw_compile_results, list) or not 1 <= len(raw_compile_results) <= MAX_RECEIPT_ASSEMBLIES:
            raise ValueError("CompilePlayerScripts returned no assembly paths or exceeded its bound")
        compile_result_names: set[str] = set()
        staging_names: set[str] = set()
        for index, raw_path in enumerate(raw_compile_results):
            output_path = Path(_bounded_text(raw_path, f"compileResultAssemblies[{index}]", 8192))
            if not output_path.is_absolute():
                raise ValueError("CompilePlayerScripts assembly paths must be absolute")
            output_parent = output_path.parent
            if output_parent.parent != receipt_parent or not re.fullmatch(r"compile-[0-9a-f]{32}", output_parent.name):
                raise ValueError("CompilePlayerScripts assembly path is outside its fresh SDK staging directory")
            if output_path.suffix.casefold() != ".dll":
                raise ValueError("CompilePlayerScripts returned a non-DLL assembly path")
            if output_path.stem not in names:
                raise ValueError("CompilePlayerScripts returned an assembly outside the active Player graph")
            output_name = output_path.name.casefold()
            if output_name in compile_result_names:
                raise ValueError("CompilePlayerScripts repeated an assembly output path")
            compile_result_names.add(output_name)
            staging_names.add(output_parent.name)
        if len(staging_names) != 1:
            raise ValueError("CompilePlayerScripts assembly paths do not share one invocation staging directory")

        outputs = value["outputs"]
        if not isinstance(outputs, list) or not 1 <= len(outputs) <= MAX_RECEIPT_OUTPUTS:
            raise ValueError("Receipt output set is empty or too large")
        output_by_name: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(outputs):
            if not isinstance(raw, dict) or set(raw) != {"relativePath", "sha256", "size"}:
                raise ValueError(f"Receipt outputs[{index}] fields are invalid")
            relative = _safe_relative(raw["relativePath"])
            if "/" in relative:
                raise ValueError("SDK-copied compile outputs must be top-level files")
            output_key = relative.casefold()
            if output_key in output_by_name:
                raise ValueError("Receipt repeats a copied compile output")
            digest = raw["sha256"]
            size = raw["size"]
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest) or type(size) is not int or size < 0:
                raise ValueError("Receipt output digest/size is invalid")
            output = receipt_parent / relative
            try:
                output_info = output.lstat()
            except OSError as exc:
                raise ValueError("SDK-copied compile output is missing") from exc
            if output.parent != receipt_parent or not stat.S_ISREG(output_info.st_mode) or _is_reparse(output_info):
                raise ValueError("SDK-copied compile output is missing or escaped its receipt directory")
            actual_hash, actual_size = _hash_file(output)
            if actual_hash != digest or actual_size != size:
                _fail("INPUT_CHANGED", "A compile output changed after its receipt was sealed.")
            output_by_name[output_key] = raw

        if not compile_result_names.issubset(output_by_name):
            raise ValueError("CompilePlayerScripts returned assembly paths absent from the copied output set")

        for expected in expected_output_files or []:
            if not isinstance(expected, dict) or set(expected) != {"assemblyName", "extension", "sha256", "size"}:
                raise ValueError("Analysis output binding fields are invalid")
            relative = _bounded_text(expected["assemblyName"], "output assembly name", 256) + _bounded_text(expected["extension"], "output extension", 8)
            raw = output_by_name.get(relative.casefold())
            if (
                raw is None
                or raw["sha256"] != expected["sha256"]
                or raw["size"] != expected["size"]
            ):
                raise ValueError("Analysis output is not byte-identical to the SDK compile receipt")
            if expected["extension"].casefold() == ".dll" and relative.casefold() not in compile_result_names:
                raise ValueError("Analysis DLL is absent from the CompilePlayerScripts result paths")

        return value
    except CommandError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, UnicodeError, OverflowError, struct.error) as exc:
        _fail("CONTRACT_MISMATCH", f"Compile input receipt is malformed or cannot be verified ({type(exc).__name__}).")
