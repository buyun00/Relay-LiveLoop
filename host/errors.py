from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CommandError(Exception):
    code: str
    message: str
    stage: str = "validation"
    runtime_changed: bool | None = False
    recoverable: bool = True
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "recoverable": self.recoverable,
            "details": self.details or {},
        }


def capability_unavailable(capability: str, reason: str | None = None) -> CommandError:
    detail = reason or "No provider has supplied verified support for this capability."
    return CommandError(
        "CAPABILITY_UNAVAILABLE",
        f"{capability}: {detail}",
        stage="capability",
        runtime_changed=False,
        recoverable=True,
    )
