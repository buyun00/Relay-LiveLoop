"""Closed, server-owned external compiler input roots; no request-controlled trust."""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

ROOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class TrustedCompileSourceRoot:
    root_id: str
    path: Path
    inputs: tuple[str, ...]


def _ordinal(value: str) -> bytes:
    return value.encode("utf-16-be", errors="strict")


def _canonical_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(c in value for c in "\\:\r\n\0"):
        raise ValueError("Trusted compiler input must be a bounded forward-slash relative path")
    parsed = PurePosixPath(value)
    value.encode("utf-8", errors="strict")
    if value == "." or parsed.is_absolute() or parsed.as_posix() != value or any(p in {".", "..", ""} for p in parsed.parts):
        raise ValueError("Trusted compiler input contains a path alias or traversal")
    return value


def _canonical_absolute(value: Any) -> Path:
    if not isinstance(value, str) or not value or len(value) > 8192 or any(c in value for c in "\r\n\0"):
        raise ValueError("Trusted compiler path must be bounded absolute text")
    value.encode("utf-8", errors="strict")
    path = Path(value)
    forward = value.replace("\\", "/") if os.name == "nt" else value
    if not path.is_absolute() or path.as_posix() != forward:
        raise ValueError("Trusted compiler path is not canonical absolute text")
    lexical = Path(os.path.abspath(path))
    if lexical.as_posix() != path.as_posix():
        raise ValueError("Trusted compiler path contains a dot-segment alias")
    return lexical


def _canonical_file(root: Path, relative: str, reject_reparse: Callable[[Path, Path], None], *, external: bool) -> Path:
    relative = _canonical_relative(relative)
    lexical = root / PurePosixPath(relative)
    reject_reparse(root, lexical)
    resolved = lexical.resolve(strict=True)
    if resolved.as_posix() != lexical.as_posix() or not resolved.is_file():
        raise ValueError("Compiler input is missing, non-regular, or has a canonical path alias")
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or (external and info.st_nlink > 1):
        raise ValueError("External compiler input cannot use a hard-link alias")
    return resolved


def parse_trusted_source_roots(raw: Any, builtins: tuple[Path, Path], reject_reparse: Callable[[Path, Path], None]) -> tuple[TrustedCompileSourceRoot, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
        raise ValueError("trustedSourceRoots must contain 1..32 explicit roots")
    roots: list[TrustedCompileSourceRoot] = []
    ids: set[str] = set()
    owned = list(builtins)
    count = 0
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"rootId", "path", "inputs"}:
            raise ValueError("Trusted source root fields differ from the server configuration contract")
        identity = item["rootId"]
        if not isinstance(identity, str) or not ROOT_ID_RE.fullmatch(identity) or identity.casefold() in ids | {"project", "unity"}:
            raise ValueError("Trusted source root identity is invalid or duplicated")
        root = _canonical_absolute(item["path"])
        reject_reparse(Path(root.anchor), root)  # also rejects ancestor junction/symlink aliases
        if root.resolve(strict=True).as_posix() != root.as_posix() or not root.is_dir() or root == Path(root.anchor):
            raise ValueError("Trusted source root must be an exact existing canonical directory")
        if any(root == other or root in other.parents or other in root.parents for other in owned):
            raise ValueError("Trusted source roots must be distinct and non-overlapping")
        inputs = item["inputs"]
        if not isinstance(inputs, list) or not inputs or len(inputs) > 100000:
            raise ValueError("Trusted source root requires an exact non-empty input file list")
        normalized = tuple(sorted((_canonical_relative(p) for p in inputs), key=_ordinal))
        if len({p.casefold() for p in normalized}) != len(normalized):
            raise ValueError("Trusted source root inputs have duplicate path identities")
        for relative in normalized:
            _canonical_file(root, relative, reject_reparse, external=True)
        count += len(normalized)
        if count > 100000:
            raise ValueError("Trusted external input set exceeds its bound")
        roots.append(TrustedCompileSourceRoot(identity, root, normalized))
        ids.add(identity.casefold())
        owned.append(root)
    return tuple(sorted(roots, key=lambda root: _ordinal(root.root_id)))


def source_roots_contract(roots: tuple[TrustedCompileSourceRoot, ...]) -> list[dict[str, Any]]:
    return [{"rootId": root.root_id, "path": root.path.as_posix(), "inputs": list(root.inputs)} for root in roots]


def root_mapping(profile: Any) -> dict[str, Path]:
    return {"project": profile.project_root, "unity": profile.unity_root,
            **{"source:" + root.root_id: root.path for root in profile.trusted_source_roots}}


def verified_input_path(profile: Any, scope: str, relative: str, reject_reparse: Callable[[Path, Path], None]) -> Path:
    mapping = root_mapping(profile)
    if scope not in mapping:
        raise ValueError("Compiler input root identity is not server-configured")
    if scope.startswith("source:"):
        configured = next(root for root in profile.trusted_source_roots if scope == "source:" + root.root_id)
        if relative not in configured.inputs:
            raise ValueError("External compiler input is outside the explicit server-owned file set")
    return _canonical_file(mapping[scope], relative, reject_reparse, external=scope.startswith("source:"))


def absolute_input_key(profile: Any, raw: Any, reject_reparse: Callable[[Path, Path], None]) -> tuple[str, str]:
    candidate = _canonical_absolute(raw)
    for scope, root in root_mapping(profile).items():
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        verified_input_path(profile, scope, relative, reject_reparse)
        return scope, relative
    raise ValueError("Compiler input is outside all exact server-owned roots")
