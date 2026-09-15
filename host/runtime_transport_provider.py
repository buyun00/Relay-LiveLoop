from __future__ import annotations

from typing import Any, Iterable


class RuntimeTransportProvider:
    """Allowlisted adapter from the shared command service to one Player session."""

    def __init__(self, transport: Any, operations: Iterable[str]) -> None:
        self._transport = transport
        self._operations = frozenset(operations)

    @property
    def is_verified(self) -> bool:
        return bool(self._transport.authenticated)

    def execute(self, operation: str, command: dict[str, Any]) -> dict[str, Any]:
        if operation not in self._operations:
            raise ValueError(f"Runtime operation {operation} is not supported by this provider.")
        return self._transport.execute(operation, command)
