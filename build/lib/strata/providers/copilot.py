from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any, Callable

from ..ai_config import (
    COPILOT_CHAT_URL,
    COPILOT_CLIENT_ID,
    COPILOT_DEFAULT_MODEL,
    COPILOT_GITHUB_TOKEN_FILE,
    COPILOT_HEADERS,
    COPILOT_MODELS_URL,
    COPILOT_TOKEN_URL,
    github_token_source,
    get_github_token,
)
from ..ai_errors import AgentError
from .base import ProviderAdapter


def _httpx():
    try:
        return importlib.import_module("httpx")
    except ImportError as exc:
        raise AgentError("httpx is required for AI features. Install it: pip install httpx") from exc


def _token_candidates(config: dict[str, Any]) -> tuple[str | None, str, bool]:
    """Return (token, source_label, from_copilot_file)."""
    if COPILOT_GITHUB_TOKEN_FILE.exists():
        tok = COPILOT_GITHUB_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok, "~/.strata/copilot_github_token", True
    tok = get_github_token(config)
    return tok, github_token_source(config), False


def do_copilot_device_flow(
    log_fn: Callable[[str], None] | None = None,
) -> str:
    """Authenticate against the Copilot OAuth app and persist token."""
    httpx = _httpx()

    with httpx.Client(timeout=15) as client:
        resp = client.post(
            "https://github.com/login/device/code",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"client_id": COPILOT_CLIENT_ID, "scope": "read:user"},
        )
    resp.raise_for_status()
    data = resp.json()
    device_code = data["device_code"]
    user_code = data["user_code"]
    verification_uri = data["verification_uri"]
    interval = data.get("interval", 5) + 1

    rich_msg = (
        f"\n  Go to: [link={verification_uri}]{verification_uri}[/link]\n"
        f"  Enter code: [bold cyan]{user_code}[/bold cyan]\n"
        "  [dim]Waiting for you to authorise…[/dim]\n"
    )
    if log_fn:
        log_fn(rich_msg)
    else:
        import sys

        sys.stderr.write(
            f"\n  Go to: {verification_uri}\n"
            f"  Enter code: {user_code}\n"
            "  Waiting for authorisation...\n"
        )
        sys.stderr.flush()

    deadline = time.time() + data.get("expires_in", 900)
    while time.time() < deadline:
        time.sleep(interval)
        with httpx.Client(timeout=15) as client:
            poll = client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={
                    "client_id": COPILOT_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
        poll_data = poll.json()
        if "access_token" in poll_data:
            token = poll_data["access_token"]
            COPILOT_GITHUB_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            COPILOT_GITHUB_TOKEN_FILE.write_text(token, encoding="utf-8")
            COPILOT_GITHUB_TOKEN_FILE.chmod(0o600)
            return token

        err = poll_data.get("error", "")
        if err not in ("authorization_pending", "slow_down"):
            raise AgentError(f"Copilot device flow failed: {err}")
        if err == "slow_down":
            interval += 5
    raise AgentError("Copilot device flow timed out — please try again.")


def fetch_copilot_models(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Fetch live Copilot chat models with model_picker_enabled=true."""
    cfg = config or {}
    token = get_github_token(cfg)
    if not token:
        return []
    httpx = _httpx()
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                COPILOT_MODELS_URL,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except Exception:
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json().get("data", [])
    except Exception:
        return []
    return [
        m for m in data
        if m.get("model_picker_enabled")
        and m.get("capabilities", {}).get("type") == "chat"
    ]


class CopilotProvider(ProviderAdapter):
    provider_id = "copilot"
    model_config_key = "copilot_model"
    default_model = COPILOT_DEFAULT_MODEL

    def auth_remediation(self) -> str:
        return "Switch with /model copilot to start GitHub Copilot device authentication."

    def supports_interactive_auth(self) -> bool:
        return True

    def run_interactive_auth(
        self,
        config: dict[str, Any],
        log_fn: Callable[[str], None] | None = None,
    ) -> tuple[bool, str]:
        del config
        do_copilot_device_flow(log_fn=log_fn)
        return True, "Copilot OAuth device flow completed."

    def _exchange_session_token(
        self,
        github_token: str,
    ) -> tuple[int, str]:
        """Return (status_code, token_or_error_text)."""
        httpx = _httpx()
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(
                    COPILOT_TOKEN_URL,
                    headers={
                        "Authorization": f"token {github_token}",
                        "x-github-api-version": "2025-04-01",
                        **COPILOT_HEADERS,
                    },
                )
        except Exception as exc:
            raise AgentError(f"Network error during Copilot token exchange: {exc}") from exc

        if resp.status_code == 200:
            return 200, resp.json().get("token", "")
        return resp.status_code, resp.text[:300]

    def _build_status_message(self, status_code: int, detail: str) -> str:
        if status_code == 401:
            return (
                "GitHub token rejected (401).\n"
                "Fix: switch provider with /model copilot to re-authenticate."
            )
        if status_code == 403:
            return (
                "Copilot access denied (403). Check your subscription: "
                "https://github.com/settings/copilot"
            )
        if status_code == 404:
            return (
                "Copilot token endpoint returned 404.\n"
                "The stored token was not issued by the Copilot OAuth app.\n"
                "Fix: switch provider with /model copilot to re-authenticate."
            )
        return f"Copilot token exchange failed ({status_code}): {detail}"

    def availability(self, config: dict[str, Any]) -> tuple[bool, str]:
        tok, src, from_copilot_file = _token_candidates(config)
        if not tok:
            return False, "Not authenticated. Use /model copilot to start OAuth device flow."

        status_code, payload = self._exchange_session_token(tok)
        if status_code != 200:
            if from_copilot_file and status_code in (401, 404):
                try:
                    COPILOT_GITHUB_TOKEN_FILE.unlink()
                except OSError:
                    pass
            return False, self._build_status_message(status_code, payload)

        model = config.get(self.model_config_key, self.default_model)
        return True, f"GitHub Copilot ({model}) — OAuth via {src}"

    def _session_token_or_error(self, config: dict[str, Any]) -> str:
        tok, _, from_copilot_file = _token_candidates(config)
        if not tok:
            raise AgentError(
                "No GitHub token for Copilot.\n"
                "Use /model copilot to start OAuth device flow."
            )
        status_code, payload = self._exchange_session_token(tok)
        if status_code != 200:
            if from_copilot_file and status_code in (401, 404):
                try:
                    COPILOT_GITHUB_TOKEN_FILE.unlink()
                except OSError:
                    pass
            raise AgentError(self._build_status_message(status_code, payload))
        return payload

    def list_models(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        return fetch_copilot_models(config)

    def validate_model(self, config: dict[str, Any], model: str) -> tuple[bool, str]:
        models = self.list_models(config)
        if not models:
            return False, (
                "Unable to fetch live Copilot model list. "
                "Switch to Copilot with /model copilot and try again."
            )
        ids = {m.get("id", "") for m in models}
        if model in ids:
            return True, "Model is available in GitHub Copilot."
        preview = ", ".join(sorted(i for i in ids if i)[:6])
        return False, (
            f"Unknown Copilot model '{model}'. "
            f"Use /model to view live models. Examples: {preview}"
        )

    def chat(self, config: dict[str, Any], messages: list[dict[str, str]]) -> str:
        session_token = self._session_token_or_error(config)
        model = config.get(self.model_config_key, self.default_model)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
        }
        if model.startswith("gpt-"):
            payload["response_format"] = {"type": "json_object"}

        httpx = _httpx()
        try:
            with httpx.Client(timeout=90) as client:
                resp = client.post(
                    COPILOT_CHAT_URL,
                    headers={
                        "Authorization": f"Bearer {session_token}",
                        "Content-Type": "application/json",
                        **COPILOT_HEADERS,
                    },
                    json=payload,
                )
        except Exception as exc:
            raise AgentError(f"Network error calling Copilot chat API: {exc}") from exc

        if resp.status_code != 200:
            raise AgentError(f"Copilot API error {resp.status_code}: {resp.text[:400]}")
        return resp.json()["choices"][0]["message"]["content"]
