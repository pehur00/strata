from __future__ import annotations

from typing import Any

from ..ai_errors import AgentError
from .base import ProviderAdapter


class DisabledByPolicyProvider(ProviderAdapter):
    """Provider placeholder kept visible but disabled by OAuth-only policy."""

    def __init__(self, provider_id: str, hint: str) -> None:
        self.provider_id = provider_id
        self._hint = hint

    def _msg(self) -> str:
        return (
            f"{self.provider_id} is disabled by OAuth-only policy. "
            f"Use one of: copilot, claude, codex. {self._hint}"
        )

    def auth_remediation(self) -> str:
        return "Use /model copilot, /model claude, or /model codex."

    def availability(self, config: dict[str, Any]) -> tuple[bool, str]:
        return False, self._msg()

    def validate_model(self, config: dict[str, Any], model: str) -> tuple[bool, str]:
        return False, self._msg()

    def chat(self, config: dict[str, Any], messages: list[dict[str, str]]) -> str:
        raise AgentError(self._msg())

