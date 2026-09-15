from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .errors import CommandError, capability_unavailable


class CommandProvider(Protocol):
    capability: str

    def execute(self, operation: str, command: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CapabilityState:
    capability: str
    available: bool
    provider_id: str | None
    reason: str | None
    verified: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "available": self.available,
            "providerId": self.provider_id,
            "reason": self.reason,
            "verified": self.verified,
        }


class ProviderRegistry:
    """Neutral provider lookup. Registration never implies native verification."""

    def __init__(self) -> None:
        self._providers: dict[str, tuple[str, CommandProvider, bool]] = {}
        self._known: dict[str, str] = {
            "observation": "No observation provider is configured.",
            "source": "No source provider is configured.",
            "runtime_component": "No runtime component provider is configured.",
            "preparation": "No compile or asset preparation provider is configured.",
            "runtime_apply": "No runtime transport or update provider is configured.",
            "verification": "No evidence provider is configured.",
            "baseline": "No baseline provider is configured.",
            "player_process": "No player process provider is configured.",
        }

    def register(self, capability: str, provider_id: str, provider: CommandProvider, *, verified: bool = False) -> None:
        if capability not in self._known:
            raise ValueError(f"Unknown capability: {capability}")
        if capability in self._providers:
            raise ValueError(f"Capability already registered: {capability}")
        self._providers[capability] = (provider_id, provider, verified)

    def require(self, capability: str, *, require_verified: bool = True) -> CommandProvider:
        registration = self._providers.get(capability)
        if registration is None:
            raise capability_unavailable(capability, self._known.get(capability))
        provider_id, provider, verified = registration
        effective_verified = bool(getattr(provider, "is_verified", verified))
        if require_verified and not effective_verified:
            raise capability_unavailable(capability, f"Provider {provider_id} is registered but has no verified capability evidence.")
        return provider

    def states(self) -> list[dict[str, Any]]:
        states = []
        for capability, reason in sorted(self._known.items()):
            registration = self._providers.get(capability)
            if registration is None:
                state = CapabilityState(capability, False, None, reason, False)
            else:
                provider_id, provider, verified = registration
                effective_verified = bool(getattr(provider, "is_verified", verified))
                state = CapabilityState(
                    capability,
                    effective_verified,
                    provider_id,
                    None if effective_verified else "Provider is registered but has no authenticated capability evidence.",
                    effective_verified,
                )
            states.append(state.as_dict())
        return states

    def execute(self, capability: str, operation: str, command: dict[str, Any]) -> dict[str, Any]:
        provider = self.require(capability)
        try:
            result = provider.execute(operation, command)
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(
                "STATE_UNKNOWN",
                f"Provider {capability} failed without a contract result ({type(exc).__name__}).",
                stage=capability,
                runtime_changed=None,
                recoverable=False,
            ) from exc
        if not isinstance(result, dict):
            raise CommandError("CONTRACT_MISMATCH", f"Provider {capability} returned a non-object result.", stage=capability)
        return result
