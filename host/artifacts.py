from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, BinaryIO

from .errors import CommandError
from .ledger import Ledger
from .validation import ID_RE, SHA256_RE

HASH_CHUNK_SIZE = 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$")


def sha256_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(HASH_CHUNK_SIZE):
        size += len(chunk)
        if size > MAX_ARTIFACT_BYTES:
            raise CommandError(
                "CONTRACT_MISMATCH",
                f"Artifact exceeds {MAX_ARTIFACT_BYTES} bytes.",
                stage="artifact",
            )
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest(), size


def sha256_file(path: Path) -> tuple[str, int]:
    with path.open("rb") as stream:
        return sha256_stream(stream)


class ArtifactStore:
    """Registers immutable file evidence from explicitly scoped roots."""

    def __init__(self, ledger: Ledger, allowed_roots: list[str | Path]):
        if not allowed_roots:
            raise ValueError("At least one artifact root is required.")
        self.ledger = ledger
        self.allowed_roots = tuple(Path(root).resolve(strict=True) for root in allowed_roots)
        for root in self.allowed_roots:
            if not root.is_dir():
                raise ValueError(f"Artifact root is not a directory: {root}")

    def _resolve_scoped(self, candidate: str | Path) -> Path:
        try:
            path = Path(candidate).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CommandError("CONTRACT_MISMATCH", "Artifact path does not resolve to an existing file.", stage="artifact") from exc
        if not path.is_file():
            raise CommandError("CONTRACT_MISMATCH", "Artifact path is not a regular file.", stage="artifact")
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise CommandError("AUTH_REQUIRED", "Artifact path is outside the configured artifact roots.", stage="artifact")
        return path

    def register(
        self,
        path: str | Path,
        *,
        kind: str,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        media_type: str | None = None,
        task_id: str | None = None,
        job_id: str | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        scoped = self._resolve_scoped(path)
        if not ID_RE.fullmatch(kind):
            raise CommandError("CONTRACT_MISMATCH", "Artifact kind contains unsupported characters.", stage="artifact")
        digest, size = sha256_file(scoped)
        if expected_sha256 is not None:
            if not SHA256_RE.fullmatch(expected_sha256):
                raise CommandError("CONTRACT_MISMATCH", "expected_sha256 must be a lowercase SHA-256 value.", stage="artifact")
            if digest != expected_sha256:
                raise CommandError("INPUT_CHANGED", "Artifact hash differs from the expected immutable input.", stage="artifact")
        if expected_size is not None:
            if type(expected_size) is not int or expected_size < 0:
                raise CommandError("CONTRACT_MISMATCH", "expected_size must be a non-negative integer.", stage="artifact")
            if size != expected_size:
                raise CommandError("INPUT_CHANGED", "Artifact size differs from the expected immutable input.", stage="artifact", recoverable=False)
        effective_media_type = media_type or mimetypes.guess_type(scoped.name)[0] or "application/octet-stream"
        if not isinstance(effective_media_type, str) or not MEDIA_TYPE_RE.fullmatch(effective_media_type):
            raise CommandError("CONTRACT_MISMATCH", "Artifact media_type must be a bounded type/subtype token.", stage="artifact")
        record = self.ledger.register_artifact(
            {
                "taskId": task_id,
                "jobId": job_id,
                "planId": plan_id,
                "absolutePath": str(scoped),
                "sha256": digest,
                "sizeBytes": size,
                "kind": kind,
                "mediaType": effective_media_type,
                "originalName": scoped.name,
            }
        )
        return self.public_metadata(record)

    @staticmethod
    def public_metadata(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "artifactId": record["artifactId"],
            "kind": record["kind"],
            "sha256": record["sha256"],
            "mediaType": record["mediaType"],
            "size": record["sizeBytes"],
        }

    def open_verified(self, artifact_id: str) -> tuple[dict[str, Any], BinaryIO]:
        if not ID_RE.fullmatch(artifact_id):
            raise CommandError("CONTRACT_MISMATCH", "artifactId contains unsupported characters.", stage="artifact")
        record = self.ledger.get_artifact(artifact_id)
        path = self._resolve_scoped(record["absolutePath"])
        stream = path.open("rb")
        try:
            digest, size = sha256_stream(stream)
        except Exception:
            stream.close()
            raise
        if digest != record["sha256"] or size != record["sizeBytes"]:
            stream.close()
            raise CommandError(
                "INPUT_CHANGED",
                "Registered artifact bytes changed after registration.",
                stage="artifact",
                recoverable=False,
            )
        return self.public_metadata(record), stream
