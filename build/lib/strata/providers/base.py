from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable
from typing import Any


class ProviderAdapter(ABC):
    """Provider adapter contract for OAuth/CLI-backed chat providers."""

    provider_id: str
    model_config_key: str | None = None
    default_model: str | None = None

    @abstractmethod
    def availability(self, config: dict[str, Any]) -> tuple[bool, str]:
        """Return provider availability and human-readable detail."""

    @abstractmethod
    def chat(self, config: dict[str, Any], messages: list[dict[str, str]]) -> str:
        """Execute a chat completion call and return raw model output."""

    def list_models(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        """Return discoverable models for this provider."""
        return []

    def validate_model(self, config: dict[str, Any], model: str) -> tuple[bool, str]:
        """Validate a model selection for this provider."""
        return True, "Model accepted."

    def auth_remediation(self) -> str:
        """User-facing remediation when auth is missing/invalid."""
        return "Re-authenticate this provider."

    def supports_interactive_auth(self) -> bool:
        """Whether this provider can run an interactive auth flow in-app."""
        return False

    def run_interactive_auth(
        self,
        config: dict[str, Any],
        log_fn: Callable[[str], None] | None = None,
    ) -> tuple[bool, str]:
        """Attempt provider auth flow; return (success, detail)."""
        del config, log_fn
        return False, self.auth_remediation()
