from __future__ import annotations

import glob
import os
import shutil
import subprocess
from typing import Any

from ..ai_config import CODEX_DEFAULT_MODEL, has_codex_oauth
from ..ai_errors import AgentError
from .base import ProviderAdapter

_KNOWN_CODEX_MODELS = {
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5.2",
    "gpt-5",
    "gpt-4o",
    "o3",
}


def _resolve_codex_executable() -> str | None:
    """Resolve the Codex CLI binary from PATH or common local install locations."""
    env_bin = os.environ.get("CODEX_BIN", "").strip()
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin

    if path_bin := shutil.which("codex"):
        return path_bin

    home = os.path.expanduser("~")
    candidates = sorted(
        glob.glob(
            os.path.join(
                home,
                ".vscode",
                "extensions",
                "openai.chatgpt-*",
                "bin",
                "*",
                "codex",
            )
        )
    )
    for cand in reversed(candidates):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


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


class CodexCliProvider(ProviderAdapter):
    provider_id = "codex"
    model_config_key = "openai_model"
    default_model = CODEX_DEFAULT_MODEL

    def auth_remediation(self) -> str:
        return "Run: codex login"

    def availability(self, config: dict[str, Any]) -> tuple[bool, str]:
        codex_bin = _resolve_codex_executable()
        if not codex_bin:
            return (
                False,
                "Codex CLI not found. Install Codex CLI: https://github.com/openai/codex",
            )
        if not has_codex_oauth():
            return False, "Codex CLI found but not authenticated. Run: codex login"
        model = config.get(self.model_config_key, self.default_model)
        return True, f"Codex CLI ({model}) — OAuth via local Codex login ({codex_bin})"

    def list_models(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"id": m, "name": m, "vendor": "OpenAI"} for m in sorted(_KNOWN_CODEX_MODELS)]

    def _probe_model(self, model: str) -> tuple[bool, str]:
        codex_bin = _resolve_codex_executable()
        if not codex_bin:
            return False, "Codex CLI is not installed."
        try:
            res = subprocess.run(
                [
                    codex_bin,
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--model",
                    model,
                    "Reply exactly with: OK",
                ],
                capture_output=True,
                text=True,
                timeout=75,
            )
        except subprocess.TimeoutExpired:
            return False, "Codex model probe timed out."
        except OSError as exc:
            return False, f"Codex model probe failed: {exc}"
        if res.returncode == 0:
            return True, "Model probe succeeded."
        stderr = (res.stderr or "").strip().splitlines()
        detail = stderr[0] if stderr else "unknown model/probe failure"
        return False, f"Model probe failed: {detail}"

    def validate_model(self, config: dict[str, Any], model: str) -> tuple[bool, str]:
        if model in _KNOWN_CODEX_MODELS:
            return True, "Model is in known Codex model allowlist."
        return self._probe_model(model)

    def chat(self, config: dict[str, Any], messages: list[dict[str, str]]) -> str:
        codex_bin = _resolve_codex_executable()
        if not codex_bin:
            raise AgentError("Codex CLI not found. Install Codex CLI: https://github.com/openai/codex")
        model = config.get(self.model_config_key, self.default_model)
        prompt = _build_prompt(messages)
        try:
            res = subprocess.run(
                [
                    codex_bin,
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--model",
                    model,
                    prompt,
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentError("Codex CLI timed out (300s).") from exc
        except OSError as exc:
            raise AgentError(f"Codex CLI execution failed: {exc}") from exc
        if res.returncode != 0:
            raise AgentError(f"Codex CLI error {res.returncode}: {(res.stderr or '')[:300]}")
        return (res.stdout or "").strip()
