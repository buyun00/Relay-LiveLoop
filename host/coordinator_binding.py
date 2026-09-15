from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .coordinator import UpdateCoordinator


AvailabilityProbe = Callable[[str, Any], bool]


@dataclass(frozen=True, slots=True)
class CoordinatorBindingState:
    bound: bool
    preparation_available: bool
    runtime_available: bool
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "bound": self.bound,
            "preparationAvailable": self.preparation_available,
            "runtimeAvailable": self.runtime_available,
            "reason": self.reason,
        }


class SharedCoordinatorBinding:
    """Attach the one existing UpdateCoordinator to one existing CommandService."""

    def __init__(
        self,
        command_service: Any,
        preparation_provider: Any | None = None,
        runtime_provider: Any | None = None,
        *,
        availability_probe: AvailabilityProbe | None = None,
    ) -> None:
        self.command_service = command_service
        self.preparation_provider = preparation_provider
        self.runtime_provider = runtime_provider
        self.availability_probe = availability_probe
        self._state = CoordinatorBindingState(False, False, False, "Binding has not been refreshed.")

    @property
    def state(self) -> CoordinatorBindingState:
        return self._state

    def refresh(self) -> CoordinatorBindingState:
        service = self.command_service
        ledger = getattr(service, "ledger", None)
        artifacts = getattr(service, "artifacts", None)
        result_policy = getattr(service, "result_policy", None)
        evidence = getattr(result_policy, "evidence", None)
        if ledger is None or artifacts is None or evidence is None:
            return self._set(False, False, False, "Shared CommandService owners are absent.")

        existing = getattr(service, "coordinator", None)
        if existing is None:
            if self.preparation_provider is None or self.runtime_provider is None:
                return self._set(False, False, False, "Preparation and runtime providers are both required; no coordinator was constructed.")
            coordinator = UpdateCoordinator(
                ledger,
                artifacts,
                self.preparation_provider,
                self.runtime_provider,
                preparation_verified=False,
                runtime_verified=False,
                evidence_store=evidence,
            )
            service.coordinator = coordinator
        else:
            coordinator = existing
            if self.preparation_provider is not None and coordinator.preparation_provider is not self.preparation_provider:
                return self._set(False, False, False, "A different preparation provider is already owned by CommandService.")
            if self.runtime_provider is not None and coordinator.runtime_provider is not self.runtime_provider:
                return self._set(False, False, False, "A different runtime provider is already owned by CommandService.")
            self.preparation_provider = coordinator.preparation_provider
            self.runtime_provider = coordinator.runtime_provider
            if coordinator.ledger is not ledger or coordinator.artifacts is not artifacts or coordinator.evidence is not evidence:
                return self._set(False, False, False, "Existing coordinator does not use shared CommandService owners.")

        preparation = self._available("preparation", self.preparation_provider)
        runtime = self._available("runtime_apply", self.runtime_provider)
        coordinator.preparation_verified = preparation
        coordinator.runtime_verified = runtime
        return self._set(True, preparation, runtime, None)

    def _available(self, capability: str, provider: Any | None) -> bool:
        if provider is None:
            return False
        if self.availability_probe is not None:
            return bool(self.availability_probe(capability, provider))
        return bool(getattr(provider, "is_verified", False))

    def _set(self, bound: bool, preparation: bool, runtime: bool, reason: str | None) -> CoordinatorBindingState:
        self._state = CoordinatorBindingState(bound, preparation, runtime, reason)
        return self._state


def bind_shared_coordinator(
    command_service: Any,
    preparation_provider: Any | None = None,
    runtime_provider: Any | None = None,
    *,
    availability_probe: AvailabilityProbe | None = None,
) -> SharedCoordinatorBinding:
    binding = SharedCoordinatorBinding(
        command_service,
        preparation_provider,
        runtime_provider,
        availability_probe=availability_probe,
    )
    binding.refresh()
    return binding
