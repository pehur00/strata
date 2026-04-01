from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

# ── Config paths ───────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".strata"
CONFIG_FILE = CONFIG_DIR / "config.yaml"

# ── GitHub Copilot ─────────────────────────────────────────────────────────────

COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
COPILOT_CHAT_URL = "https://api.githubcopilot.com/chat/completions"
COPILOT_MODELS_URL = "https://api.githubcopilot.com/models"
COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
COPILOT_GITHUB_TOKEN_FILE = CONFIG_DIR / "copilot_github_token"
COPILOT_DEFAULT_MODEL = "gpt-4o"
COPILOT_HEADERS = {
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.0",
    "Copilot-Integration-Id": "vscode-chat",
    "User-Agent": "strata-cli/0.2.0",
    "Accept": "application/json",
}

# ── Claude / Anthropic ─────────────────────────────────────────────────────────

CLAUDE_DEFAULT_MODEL = "claude-opus-4-5"
CLAUDE_CREDS_FILE = Path.home() / ".claude" / ".credentials.json"
CLAUDE_CONFIG_FILE = Path.home() / ".claude" / "config.json"

# ── Codex ──────────────────────────────────────────────────────────────────────

CODEX_DEFAULT_MODEL = "gpt-4o"
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"

# ── Disabled-by-policy providers (kept for discoverability) ───────────────────

GITHUB_MODELS_DEFAULT_MODEL = "gpt-4o-mini"
OPENAI_DEFAULT_MODEL = "gpt-4o"
OLLAMA_DEFAULT_MODEL = "llama3.1"
OLLAMA_DEFAULT_HOST = "http://localhost:11434"


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return {}
    return {}


def save_config(updates: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_config()
    existing.update(updates)
    CONFIG_FILE.write_text(yaml.dump(existing, sort_keys=False), encoding="utf-8")


def run_silent(*cmd: str, timeout: int = 5) -> str | None:
    """Run command and return stripped stdout; None on failure."""
    try:
        res = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        return res.stdout.strip() if res.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def get_github_token(config: dict[str, Any] | None = None) -> str | None:
    cfg = config or load_config()
    if tok := cfg.get("github_token"):
        return tok
    for env in ("GITHUB_TOKEN", "GH_TOKEN"):
        if tok := os.environ.get(env):
            return tok
    return run_silent("gh", "auth", "token")


def github_token_source(config: dict[str, Any]) -> str:
    if config.get("github_token"):
        return "strata config"
    if os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        return "env var"
    return "gh CLI"


def get_claude_oauth_token() -> str | None:
    if CLAUDE_CREDS_FILE.exists():
        try:
            creds = json.loads(CLAUDE_CREDS_FILE.read_text(encoding="utf-8"))
            if tok := creds.get("claudeAiOauthToken"):
                return tok
        except Exception:
            return None
    return None


def has_claude_oauth() -> bool:
    return bool(get_claude_oauth_token())


def get_codex_oauth_token() -> str | None:
    if not CODEX_AUTH_FILE.exists():
        return None
    try:
        auth = json.loads(CODEX_AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if tok := auth.get("token") or auth.get("apiKey"):
        return tok
    tokens = auth.get("tokens")
    if isinstance(tokens, dict):
        if tok := tokens.get("access_token"):
            return tok
    return None


def has_codex_oauth() -> bool:
    return bool(get_codex_oauth_token())

