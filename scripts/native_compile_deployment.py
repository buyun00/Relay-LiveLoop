"""Local deployment validation and portable reconstruction of server compile profiles."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host.native_compile_profile import (NativeCompileProfileRegistry, _reject_duplicate_pairs,
    _reject_reparse_path, native_compile_profile_digest, verify_profile_baselines)
from host.native_compile_roots import _canonical_file, _canonical_relative


def read_json(path: Path) -> dict:
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Deployment document exceeds 4 MiB")
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_reject_duplicate_pairs)


def file_fact(path: Path) -> dict:
    before = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Compile deployment input changed while being read")
    return {"length": after.st_size, "sha256": digest}


def check_fact(path: Path, expected: dict) -> None:
    if set(expected) != {"length", "sha256"} or file_fact(path) != expected:
        raise ValueError("Portable compiler input does not match its exported hash/length")


def canonical_reference(root: Path, relative: str, *, external: bool) -> Path:
    # Unity's installed reference list can use different Windows casing. Bind the
    # real regular file before projecting its path; trust-root input lists stay exact.
    relative = _canonical_relative(relative)
    lexical = root / relative
    _reject_reparse_path(root, lexical)
    resolved = lexical.resolve(strict=True)
    return _canonical_file(root, resolved.relative_to(root).as_posix(), _reject_reparse_path, external=external)


def load_configured(config: dict):
    native = config.get("nativeCompile")
    if native is None:
        return None
    if not isinstance(native, dict) or set(native) != {"profilesFile", "editorJobRoot", "editorArtifactRoot"}:
        raise ValueError("nativeCompile requires exact profilesFile/editorJobRoot/editorArtifactRoot fields")
    profile_path = Path(native["profilesFile"]).resolve(strict=True)
    document = read_json(profile_path)
    registry = NativeCompileProfileRegistry.from_value(document)
    project = Path(config["gameProjectRoot"]).resolve(strict=True)
    unity = Path(config["unityExecutable"]).resolve(strict=True)
    profiles = list(registry._by_id.values())
    for profile in profiles:
        if profile.project_root != project or not unity.is_relative_to(profile.unity_root):
            raise ValueError("Compile profile project/Unity roots differ from this machine configuration")
        verify_profile_baselines(profile)
    return document, profiles


def inspect(config: dict) -> dict:
    configured = load_configured(config)
    if configured is None:
        return {"configured": False, "nativeRuntimeVerified": False}
    document, profiles = configured
    return {"configured": True, "registryVersion": document["version"],
            "profiles": [{"profileId": p.profile_id, "profileDigest": native_compile_profile_digest(p),
                          "contextVersion": 4 if p.profile_version == 3 else 3,
                          "receiptVersion": 2 if p.profile_version == 3 else 1,
                          "trustedSourceRootCount": len(p.trusted_source_roots),
                          "trustedSourceInputCount": sum(len(r.inputs) for r in p.trusted_source_roots)}
                         for p in profiles], "nativeRuntimeVerified": False}


def export_plan(config: dict) -> dict:
    configured = load_configured(config)
    if configured is None:
        return {"configured": False}
    document, parsed = configured
    profiles = []
    sources = {}
    for raw, profile in zip(document["profiles"], parsed):
        value = copy.deepcopy(raw)
        del value["projectRoot"], value["unityRoot"]
        roots = {"project": profile.project_root, "unity": profile.unity_root,
                 **{"source:" + r.root_id: r.path for r in profile.trusted_source_roots}}
        references = []
        for reference in raw["references"]:
            path = Path(reference)
            scope = "relative"
            relative = reference
            if path.is_absolute():
                scope = next((name for name, root in roots.items() if path.is_relative_to(root)), None)
                if scope is None:
                    raise ValueError("Compile reference is outside the portable profile roots")
                relative = path.relative_to(roots[scope]).as_posix()
            actual_root = profile.project_root if scope == "relative" else roots[scope]
            actual = canonical_reference(actual_root, relative, external=scope.startswith("source:"))
            relative = actual.relative_to(actual_root).as_posix()
            references.append({"scope": scope, "path": relative, **file_fact(actual)})
        value["references"] = references
        value["projectInputFacts"] = [{"path": relative, **file_fact(
            _canonical_file(profile.project_root, relative, _reject_reparse_path, external=False))}
            for relative in profile.source_inputs]
        if profile.profile_version == 3:
            value["trustedSourceRoots"] = []
            for root in profile.trusted_source_roots:
                destination = "compiler-sources/" + root.root_id
                value["trustedSourceRoots"].append({"rootId": root.root_id, "path": destination, "inputs": list(root.inputs)})
                for relative in root.inputs:
                    actual = _canonical_file(root.path, relative, _reject_reparse_path, external=True)
                    key = destination + "/" + relative
                    entry = {"sourcePath": str(actual), "relativePath": key, **file_fact(actual)}
                    if key in sources and sources[key] != entry:
                        raise ValueError("Portable trusted source identities disagree across profiles")
                    sources[key] = entry
        profiles.append(value)
    return {"configured": True, "portable": {"schema": "relay.liveloop.native-compile-portable", "version": 1,
            "registryVersion": document["version"], "profiles": profiles}, "sources": list(sources.values())}


def reconstruct(portable: dict, config: dict, destination: Path) -> dict:
    if set(portable) != {"schema", "version", "registryVersion", "profiles"} or portable["schema"] != "relay.liveloop.native-compile-portable" or portable["version"] != 1:
        raise ValueError("Portable native compile contract is unsupported")
    project = Path(config["gameProjectRoot"]).resolve(strict=True)
    unity_exe = Path(config["unityExecutable"]).resolve(strict=True)
    # Installed Unity executable belongs to <installation>/Editor/Unity.exe.
    if unity_exe.parent.name != "Editor" or unity_exe.name.lower() != "unity.exe":
        raise ValueError("Portable Native profile requires an explicit installed Editor/Unity.exe")
    unity = unity_exe.parent.parent
    data = Path(config["dataRoot"]).resolve(strict=True)
    document = {"schema": "relay.liveloop.native-compile-profiles", "version": portable["registryVersion"], "profiles": []}
    for raw in portable["profiles"]:
        value = copy.deepcopy(raw)
        value["projectRoot"] = project.as_posix()
        value["unityRoot"] = unity.as_posix()
        roots = {"project": project, "unity": unity}
        for root in value.get("trustedSourceRoots", []):
            relative = _canonical_relative(root["path"])
            if relative != "compiler-sources/" + root["rootId"]:
                raise ValueError("Portable source root is outside the dedicated compiler-sources directory")
            root["path"] = (data / relative).as_posix()
            roots["source:" + root["rootId"]] = Path(root["path"])
        facts = value.pop("projectInputFacts")
        if not isinstance(facts, list) or [x["path"] for x in facts] != value["sourceInputs"]:
            raise ValueError("Portable project input facts differ from the exact profile input list")
        for item in facts:
            check_fact(_canonical_file(project, item["path"], _reject_reparse_path, external=False),
                       {k: item[k] for k in ("length", "sha256")})
        references = []
        for item in value["references"]:
            if set(item) != {"scope", "path", "length", "sha256"}:
                raise ValueError("Portable reference fields are invalid")
            scope = item["scope"]
            if scope != "relative" and scope not in roots:
                raise ValueError("Portable reference root is not configured")
            actual = canonical_reference(project if scope == "relative" else roots[scope], item["path"],
                                         external=scope.startswith("source:"))
            check_fact(actual, {k: item[k] for k in ("length", "sha256")})
            references.append(actual.relative_to(project).as_posix() if scope == "relative" else actual.as_posix())
        value["references"] = references
        document["profiles"].append(value)
    registry = NativeCompileProfileRegistry.from_value(document)
    for profile in registry._by_id.values():
        verify_profile_baselines(profile)
    destination = destination.absolute()
    if not destination.is_relative_to(data) or destination.exists():
        raise ValueError("Reconstructed profile destination must be fresh and under new dataRoot")
    _reject_reparse_path(Path(data.anchor), data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse_path(data, destination.parent)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    updated = dict(config, nativeCompile={"profilesFile": str(destination),
        "editorJobRoot": str(data / "editor-jobs"), "editorArtifactRoot": str(data / "artifacts/native-compile")})
    return {"nativeCompile": updated["nativeCompile"], "validation": inspect(updated),
            "runtimeSessionImported": False, "secondMachineValidated": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("inspect", "export", "reconstruct"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--portable", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    try:
        config = read_json(args.config)
        if args.mode == "inspect":
            result = inspect(config)
        elif args.mode == "export":
            result = export_plan(config)
        else:
            result = reconstruct(read_json(args.portable), config, args.destination)
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
        else:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc), "nativeRuntimeVerified": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
