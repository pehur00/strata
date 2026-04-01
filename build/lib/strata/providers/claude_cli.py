from __future__ import annotations

import shutil
import subprocess
from typing import Any

from ..ai_config import CLAUDE_DEFAULT_MODEL, has_claude_oauth
from ..ai_errors import AgentError
from .base import ProviderAdapter

_KNOWN_CLAUDE_MODELS = {
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-opus-4.1",
    "claude-opus-4.6",
    "claude-sonnet-4.6",
    "opus",
    "sonnet",
}


def _build_prompt(messages: list[dict[str, str]]) -> str:
    system = next((m["content"] for m in messages if m.get("role") == "system"), "")
    convo = [m for m in messages if m.get("role") != "system"]

    if len(convo) == 1:
        user = convo[0]["content"]
        return f"<system>\n{system}\n</system>\n\n{user}" if system else user

    parts = [f"<system>\n{system}\n</system>"] if system else []
    for msg in convo:
        role = "Human" if msg.get("role") == "user" else "Assistant"
        parts.append(f"\n{role}: {msg.get('content', '')}")
    return "\n".join(parts)


class ClaudeCliProvider(ProviderAdapter):
    provider_id = "claude"
    model_config_key = "claude_model"
    default_model = CLAUDE_DEFAULT_MODEL

    def auth_remediation(self) -> str:
        return "Run: claude auth login"

    def availability(self, config: dict[str, Any]) -> tuple[bool, str]:
        if not shutil.which("claude"):
            return (
                False,
                "Claude CLI not found. Install Claude Code: https://claude.ai/code",
            )
        if not has_claude_oauth():
            return False, "Claude CLI found but not authenticated. Run: claude auth login"
        model = config.get(self.model_config_key, self.default_model)
        return True, f"Claude CLI ({model}) — OAuth via Claude Code"

    def list_models(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"id": m, "name": m, "vendor": "Anthropic"} for m in sorted(_KNOWN_CLAUDE_MODELS)]

    def _probe_model(self, model: str) -> tuple[bool, str]:
        if not shutil.which("claude"):
            return False, "Claude CLI is not installed."
        try:
            res = subprocess.run(
                ["claude", "-p", "--model", model, "Reply exactly with: OK"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return False, "Claude model probe timed out."
        except OSError as exc:
            return False, f"Claude model probe failed: {exc}"
        if res.returncode == 0:
            return True, "Model probe succeeded."
        stderr = (res.stderr or "").strip().splitlines()
        detail = stderr[0] if stderr else "unknown model/probe failure"
        return False, f"Model probe failed: {detail}"

    def validate_model(self, config: dict[str, Any], model: str) -> tuple[bool, str]:
        if model in _KNOWN_CLAUDE_MODELS:
            return True, "Model is in known Claude model allowlist."
        return self._probe_model(model)

    def chat(self, config: dict[str, Any], messages: list[dict[str, str]]) -> str:
        if not shutil.which("claude"):
            raise AgentError("Claude CLI not found. Install Claude Code: https://claude.ai/code")
        model = config.get(self.model_config_key, self.default_model)
        prompt = _build_prompt(messages)
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", model, prompt],
                capture_output=True,
                text=True,
                timeout=240,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentError("Claude CLI timed out (240s).") from exc
        except OSError as exc:
            raise AgentError(f"Claude CLI execution failed: {exc}") from exc
        if result.returncode != 0:
            raise AgentError(f"Claude CLI error {result.returncode}: {(result.stderr or '')[:300]}")
        return (result.stdout or "").strip()

