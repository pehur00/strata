from __future__ import annotations

from .base import ProviderAdapter
from .claude_cli import ClaudeCliProvider
from .codex_cli import CodexCliProvider
from .copilot import CopilotProvider
from .disabled import DisabledByPolicyProvider

OAUTH_ONLY_POLICY_ENABLED = True

AUTO_PROVIDER_ORDER = ("claude", "copilot", "codex")
ALL_PROVIDER_ORDER = ("copilot", "claude", "github", "codex", "openai", "ollama")
POLICY_DISABLED_PROVIDERS = {"github", "openai", "ollama"}

_PROVIDERS: dict[str, ProviderAdapter] = {
    "copilot": CopilotProvider(),
    "claude": ClaudeCliProvider(),
    "codex": CodexCliProvider(),
    "github": DisabledByPolicyProvider(
        "github",
        "GitHub Models remains visible for discoverability only.",
    ),
    "openai": DisabledByPolicyProvider(
        "openai",
        "Direct OpenAI API usage is disabled in this mode.",
    ),
    "ollama": DisabledByPolicyProvider(
        "ollama",
        "Local provider is disabled while OAuth-only policy is active.",
    ),
}


def list_provider_ids() -> tuple[str, ...]:
    return ALL_PROVIDER_ORDER


def get_provider(provider_id: str) -> ProviderAdapter:
    return _PROVIDERS[provider_id]


def is_policy_disabled(provider_id: str) -> bool:
    return provider_id in POLICY_DISABLED_PROVIDERS

