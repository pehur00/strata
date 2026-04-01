"""AI agent orchestration for Strata.

Active providers are adapter-based and OAuth/CLI-backed:
  - copilot (GitHub Copilot OAuth + token exchange)
  - claude  (Claude Code CLI OAuth)
  - codex   (Codex CLI OAuth)

Providers kept for discoverability but disabled by policy:
  - github, openai, ollama
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from .ai_config import (
    CLAUDE_CONFIG_FILE,
    CLAUDE_CREDS_FILE,
    CODEX_AUTH_FILE,
    CONFIG_DIR,
    CONFIG_FILE,
    COPILOT_CHAT_URL,
    COPILOT_CLIENT_ID,
    COPILOT_DEFAULT_MODEL,
    COPILOT_GITHUB_TOKEN_FILE,
    COPILOT_HEADERS as _COPILOT_HEADERS,
    COPILOT_TOKEN_URL,
    GITHUB_MODELS_DEFAULT_MODEL,
    OLLAMA_DEFAULT_HOST,
    OLLAMA_DEFAULT_MODEL,
    OPENAI_DEFAULT_MODEL,
    get_github_token as _cfg_get_github_token,
    github_token_source as _cfg_github_token_source,
    load_config as _cfg_load_config,
    save_config as _cfg_save_config,
)
from .ai_errors import AgentError
from .models import ArchitectureWorkspace
from .providers import AUTO_PROVIDER_ORDER, get_provider, list_provider_ids
from .providers.copilot import (
    do_copilot_device_flow as _copilot_device_flow_impl,
    fetch_copilot_models as _fetch_copilot_models_impl,
)

# ── Config paths ───────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".strata"
CONFIG_FILE = CONFIG_DIR / "config.yaml"

# ── GitHub Copilot ─────────────────────────────────────────────────────────────

COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
COPILOT_CHAT_URL = "https://api.githubcopilot.com/chat/completions"
COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
COPILOT_GITHUB_TOKEN_FILE = CONFIG_DIR / "copilot_github_token"
COPILOT_DEFAULT_MODEL = "gpt-4o"
# Mimic VS Code so the endpoint accepts the request
_COPILOT_HEADERS = {
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.0",
    "Copilot-Integration-Id": "vscode-chat",
    "User-Agent": "strata-cli/0.1.0",
    "Accept": "application/json",
}

# ── Claude / Anthropic ─────────────────────────────────────────────────────────

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_DEFAULT_MODEL = "claude-opus-4-5"
# Claude Code stores OAuth credentials written by `claude auth login`
CLAUDE_CREDS_FILE = Path.home() / ".claude" / ".credentials.json"
CLAUDE_CONFIG_FILE = Path.home() / ".claude" / "config.json"

# ── Codex / OpenAI ─────────────────────────────────────────────────────────────

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_DEFAULT_MODEL = "gpt-4o"
# Token written by `codex auth login` (OpenAI Codex CLI)
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"

# ── GitHub Models (legacy / fallback) ─────────────────────────────────────────

GITHUB_MODELS_URL = "https://models.inference.ai.azure.com/chat/completions"
GITHUB_MODELS_DEFAULT_MODEL = "gpt-4o-mini"

# ── Ollama ─────────────────────────────────────────────────────────────────────

OLLAMA_DEFAULT_HOST = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "llama3.1"

# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert enterprise architect. Extract structured architecture information \
from the provided document and return it as a single JSON object — no markdown fences, \
no explanation, just the JSON.

The JSON must match this schema exactly (all fields optional unless noted):

{
  "manifest": {                          // required
    "name": "string",                    // required
    "description": "string",
    "cloud_provider": "aws|azure|gcp|multi-cloud|on-premise|hybrid",
    "environment": "dev|staging|production"
  },
  "enterprise": {
    "capabilities": [
      {
        "id": "kebab-slug",              // derived from name
        "name": "string",
        "domain": "string",
        "level": "strategic|core|supporting",
        "owner": "string",
        "description": "string"
      }
    ],
    "applications": [
      {
        "id": "kebab-slug",
        "name": "string",
        "hosting": "kubernetes|serverless|vm|managed-service|saas",
        "criticality": "low|medium|high|critical",
        "owner_team": "string",
        "status": "active|retiring|planned|decommissioned",
        "technology_stack": ["string"],
        "description": "string"
      }
    ],
    "standards": [
      {
        "id": "kebab-slug",
        "name": "string",
        "category": "string",
        "status": "adopt|trial|assess|hold",
        "rationale": "string"
      }
    ]
  },
  "data": {
    "domains": [
      {
        "id": "kebab-slug",
        "name": "string",
        "owner_team": "string",
        "storage_pattern": "warehouse|lakehouse|operational|streaming|mixed",
        "description": "string"
      }
    ],
    "products": [
      {
        "id": "kebab-slug",
        "name": "string",
        "domain_id": "kebab-slug matching a domain id",
        "output_port": "api|files|streaming|sql|graphql",
        "sla_tier": "bronze|silver|gold|platinum",
        "owner_team": "string"
      }
    ],
    "flows": [
      {
        "id": "kebab-slug",
        "name": "string",
        "source_domain": "kebab-slug",
        "target_domain": "kebab-slug",
        "mechanism": "streaming|batch|api|cdc|file-transfer",
        "classification": "public|internal|confidential|restricted"
      }
    ]
  },
  "solutions": [
    {
      "id": "kebab-slug",
      "name": "string",
      "description": "string",
      "pattern": "microservices|event-driven|api-gateway|layered|serverless|modular-monolith|data-mesh",
      "deployment_target": "aws|azure|gcp|multi-cloud|on-premise|hybrid",
      "status": "draft|review|approved|implemented|deprecated",
      "components": [
        {
          "id": "kebab-slug",
          "name": "string",
          "type": "service|gateway|database|queue|cache|cdn|identity|storage|external",
          "technology": "string",
          "hosting": "kubernetes|serverless|managed-service|saas|external"
        }
      ]
    }
  ]
}

Rules:
- Slugs must be kebab-case (lowercase letters, digits, hyphens only)
- Infer missing enum values from context; use the default when truly unknown
- domain_id in data products and source/target in flows MUST match slug of a domain
- Use "multi-cloud" when cloud provider cannot be determined
- Return ONLY the raw JSON object — absolutely no surrounding markdown"""

# ── Entity schemas for AI-assisted field extraction ────────────────────────────

_ENTITY_SCHEMAS: dict[str, dict] = {
    "capability": {
        "description": "A business capability in the enterprise architecture layer.",
        "fields": {
            "name": "string — the capability name (e.g. 'Order Management')",
            "domain": "string — the business domain (e.g. 'Commerce', 'HR', 'Finance')",
            "level": "enum: strategic | core | supporting",
            "owner": "string — owning team or person (may be empty)",
            "description": "string — short description (may be empty)",
        },
    },
    "application": {
        "description": "An application in the portfolio.",
        "fields": {
            "name": "string — the application name",
            "hosting": "enum: kubernetes | serverless | vm | managed-service | saas",
            "criticality": "enum: low | medium | high | critical",
            "owner": "string — owning team",
            "status": "enum: active | retiring | planned | decommissioned",
            "description": "string — short description (may be empty)",
        },
    },
    "standard": {
        "description": "A technology standard on the tech radar.",
        "fields": {
            "name": "string — technology or tool name (e.g. 'Kafka', 'Terraform')",
            "category": "string — e.g. messaging | database | observability | security | infrastructure",
            "status": "enum: adopt | trial | assess | hold",
            "rationale": "string — rationale for this ring placement",
        },
    },
    "domain": {
        "description": "A data domain in the data architecture.",
        "fields": {
            "name": "string — domain name",
            "owner": "string — owning team",
            "storage_pattern": "enum: warehouse | lakehouse | operational | streaming | mixed",
            "description": "string — short description (may be empty)",
        },
    },
    "product": {
        "description": "A data product within a data domain.",
        "fields": {
            "name": "string — data product name",
            "domain_id": "string — MUST match an existing domain ID slug from the workspace context",
            "output_port": "enum: api | files | streaming | sql | graphql",
            "sla_tier": "enum: bronze | silver | gold | platinum",
            "owner": "string — owning team",
        },
    },
    "flow": {
        "description": "A data flow between two data domains.",
        "fields": {
            "name": "string — flow name",
            "source_domain": "string — MUST match an existing domain ID slug from context",
            "target_domain": "string — MUST match an existing domain ID slug from context",
            "mechanism": "enum: streaming | batch | api | cdc | file-transfer",
            "classification": "enum: public | internal | confidential | restricted",
        },
    },
    "solution": {
        "description": "A solution design in the solution architecture layer.",
        "fields": {
            "name": "string — solution name",
            "description": "string — what this solution does",
            "pattern": "enum: microservices | event-driven | api-gateway | layered | serverless | modular-monolith | data-mesh",
            "deployment_target": "enum: aws | azure | gcp | multi-cloud | on-premise | hybrid",
        },
    },
}

_ENTITY_TYPES = " | ".join(_ENTITY_SCHEMAS)

_FIELD_EXTRACTION_SYSTEM = """You are an architecture assistant for Strata — a multi-domain architecture CLI.
Extract structured field values from a natural language description for the specified entity type.

Rules:
- Return ONLY a raw JSON object. No markdown fences, no explanation.
- Use ONLY the listed enum values. Pick the closest match when ambiguous.
- Leave optional string fields as "" if not mentioned.
- Cross-reference IDs (domain_id, source_domain, target_domain) MUST come from the workspace context.
- If a required ID is not in the context, slugify the closest matching name (lowercase, hyphens)."""

_CLASSIFY_SYSTEM = f"""You are an architecture assistant for Strata — a multi-domain architecture CLI.
Classify a natural language instruction and extract structured field values.

Supported entity types: {_ENTITY_TYPES}

Return ONLY a raw JSON object with exactly these three keys:
  "entity"  — one of the entity types above
  "action"  — always "add"
  "fields"  — a dict of extracted field values matching the entity schema

Rules:
- No markdown, no explanation — raw JSON only.
- Use only the valid enum values for each entity type.
- Cross-reference IDs must match IDs provided in the workspace context if given."""

_SCAN_SYSTEM = """You are an architecture intelligence assistant for Strata.
Scan the provided document and extract ALL architecture artefacts mentioned.

Return a JSON ARRAY of objects. Each object must have exactly two keys:
  "entity" : one of  capability | application | standard | domain | product | flow | solution
  "fields" : an object with field values for that entity type

Entity field descriptions:
- capability  : name, domain, level (strategic|core|supporting), owner, description
- application : name, hosting (kubernetes|serverless|vm|managed-service|saas),
                criticality (low|medium|high|critical), owner,
                status (active|retiring|planned|decommissioned), description
- standard    : name, category, status (adopt|trial|assess|hold), rationale
- domain      : name, owner, storage_pattern (warehouse|lakehouse|operational|streaming|mixed),
                description
- product     : name, domain_id (slug of owning domain), output_port (api|files|streaming|sql|graphql),
                sla_tier (bronze|silver|gold|platinum), owner
- flow        : name, source_domain, target_domain,
                mechanism (streaming|batch|api|cdc|file-transfer),
                classification (public|internal|confidential|restricted)
- solution    : name, description, pattern (microservices|event-driven|api-gateway|layered|
                serverless|modular-monolith|data-mesh),
                deployment_target (aws|azure|gcp|multi-cloud|on-premise|hybrid)

Rules:
- Return [] if nothing architecture-related is present.
- Return ONLY the raw JSON array — no markdown fences, no explanation.
- Extract as many fields as are clearly stated; omit absent ones.
- Slugify any id cross-references (lowercase, hyphens, no spaces).
- Do NOT duplicate artefacts — each distinct thing appears exactly once.
"""

_AGENTIC_SYSTEM = """\
You are Strata, an intelligent enterprise architecture assistant embedded in an \
architecture management CLI.

You help architects manage their workspace through natural conversation. You answer \
questions, provide architecture advice, and propose structured entities to create \
when the user wants to add something.

## Current Workspace State
__WORKSPACE_SNAPSHOT__

## Staged Items Pending Review
__STAGING_SNAPSHOT__

## Supported Entity Types
__ENTITY_SCHEMAS__

## Available Tools
You have these tools you can invoke in addition to creating entities:

  scan_folder — Scan a local folder (or file) for architecture artefacts.
    Use this when the user asks to scan, index, ingest, or analyse documents
    in a folder path. You DO have access to scan local folders — trigger this
    tool and Strata will run the scan for you.

    {"tool": "scan_folder", "path": "<the folder or file path the user specified>"}

  accept_all_staged — Accept ALL currently pending staged items in one operation.
    Use this whenever the user asks to accept all, approve all, or bulk-approve.
    Takes no arguments — it accepts every pending item automatically.

    {"tool": "accept_all_staged"}

  accept_staged — Accept a single specific staged item by ID.
    Use this when the user wants to accept one specific item.
    Only use IDs listed in the Staged Items section above.

    {"tool": "accept_staged", "id": "<stg-xxx id>"}

  reject_staged — Reject a staged item (marks it rejected, keeps it for audit).
    Only use IDs listed in the Staged Items section above.

    {"tool": "reject_staged", "id": "<stg-xxx id>"}

  show_diagram — Render an architecture diagram inline in the TUI.
    Use this when the user asks to see, show, display, or generate a diagram,
    map, or visual of the architecture.

    {"tool": "show_diagram", "type": "capability-map"}   — business capability map
    {"tool": "show_diagram", "type": "data-flow"}        — data domain flow diagram
    {"tool": "show_diagram", "type": "solution", "id": "<solution-id>"}  — one solution

    Valid types: capability-map, data-flow, solution

  add_folder — Add a local folder path to the workspace watch list and persist it.
    Use this when the user asks to add, register, watch, or monitor a folder.
    This ACTUALLY saves the folder — always use this tool, never just say you did it.

    {"tool": "add_folder", "path": "<absolute folder path>"}

  remove_folder — Remove a folder from the workspace watch list.
    Use this when the user asks to remove, unwatch, or stop monitoring a folder.

    {"tool": "remove_folder", "path": "<absolute folder path>"}

  start_watching — Start the live file watcher so the workspace auto-updates on changes.
    Use this when the user asks to start watching, enable live watch, or monitor for changes.
    Takes no arguments.

    {"tool": "start_watching"}

  stop_watching — Stop the live file watcher.
    Use this when the user asks to stop watching or disable live monitoring.

    {"tool": "stop_watching"}

## Configured Watch Folders
__WATCH_FOLDERS__

## Response Format
Always respond with a raw JSON object (no markdown fences, no extra text):
{
  "message": "Your conversational reply — always required, shown directly to the user",
  "actions": [
    {
      "entity": "capability|application|standard|domain|product|flow|solution",
      "fields": {}
    }
  ],
  "tools": [
    {"tool": "scan_folder", "path": "/path/to/scan"},
    {"tool": "accept_all_staged"},
    {"tool": "accept_staged", "id": "stg-001"},
    {"tool": "reject_staged", "id": "stg-002"},
    {"tool": "show_diagram", "type": "capability-map"},
    {"tool": "show_diagram", "type": "solution", "id": "api-platform"},
    {"tool": "add_folder", "path": "/path/to/folder"},
    {"tool": "remove_folder", "path": "/path/to/folder"},
    {"tool": "start_watching"},
    {"tool": "stop_watching"}
  ]
}

## Rules
- "message" is ALWAYS required — be helpful, concise, and conversational
- For questions or advice: answer in "message", leave "actions" and "tools" as []
- For creation requests with enough info: populate "actions" with the entity to create
- For creation requests missing info: ask clarifying questions in "message", leave "actions" as []
- You may propose MULTIPLE actions in one turn (e.g., create a domain AND a product together)
- Cross-reference IDs (domain_id, source_domain, target_domain) MUST use IDs from the workspace state
- If a required parent (e.g. domain for a product) does not exist yet, propose creating it first in the same response
- Never invent IDs — only use IDs listed in the workspace state above
- When a user asks to scan, analyse, or ingest a folder: use the scan_folder tool — you CAN access local folders through it
- When a user asks to add, register, watch, or track a folder: use the add_folder tool — ALWAYS use this tool, never just say you did it
- When a user asks to remove or unwatch a folder: use the remove_folder tool
- When a user asks to start watching or enable live watch: use the start_watching tool
- When a user asks to stop watching: use the stop_watching tool
- When a user asks to both add a folder AND start watching: emit BOTH add_folder and start_watching tools in the same response
- When a user says "accept all", "approve all", "accept all pending", or similar bulk request: use accept_all_staged (single tool call, no ID needed) — you CAN do this
- When a user asks to accept one specific item: use accept_staged with its ID
- When a user asks to reject a staged item: use reject_staged with the item's ID
- When a user asks to see, show, display, draw, or generate a diagram, map, or visual: use show_diagram with the appropriate type
- Return ONLY the raw JSON object — no markdown, no explanation outside the JSON\
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _load_config() -> dict[str, Any]:
    return _cfg_load_config()


def save_config(updates: dict[str, Any]) -> None:
    """Merge *updates* into ~/.strata/config.yaml."""
    _cfg_save_config(updates)


def _run_silent(*cmd: str, timeout: int = 5) -> str | None:
    """Run a command and return stripped stdout, or None on any failure."""
    try:
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


# ── GitHub token helpers ───────────────────────────────────────────────────────

def get_github_token(config: dict[str, Any] | None = None) -> str | None:
    """Return a GitHub token: config → GITHUB_TOKEN → GH_TOKEN → gh CLI."""
    return _cfg_get_github_token(config)


def _github_token_source(config: dict[str, Any]) -> str:
    return _cfg_github_token_source(config)


# ── Claude credential helpers ──────────────────────────────────────────────────

def get_claude_token(config: dict[str, Any] | None = None) -> str | None:
    """Return a Claude/Anthropic API key or OAuth token.

    Resolution order:
      1. ``ANTHROPIC_API_KEY`` environment variable
      2. ``anthropic_api_key`` key in ~/.strata/config.yaml
      3. ``~/.claude/.credentials.json`` → ``claudeAiOauthToken``
         (OAuth token written by Claude Code — works as x-api-key)
      4. ``~/.claude/config.json`` → ``apiKey`` field
    """
    cfg = config or _load_config()
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    if key := cfg.get("anthropic_api_key"):
        return key
    # Claude Code OAuth credentials (token works with Anthropic API as x-api-key)
    if CLAUDE_CREDS_FILE.exists():
        try:
            creds = json.loads(CLAUDE_CREDS_FILE.read_text(encoding="utf-8"))
            if key := creds.get("claudeAiOauthToken"):
                return key
        except Exception:  # noqa: BLE001
            pass
    # Explicit API key in Claude Code config
    if CLAUDE_CONFIG_FILE.exists():
        try:
            cfg_json = json.loads(CLAUDE_CONFIG_FILE.read_text(encoding="utf-8"))
            if key := cfg_json.get("apiKey"):
                return key
        except Exception:  # noqa: BLE001
            pass
    return None


def _claude_token_source(config: dict[str, Any]) -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY"
    if config.get("anthropic_api_key"):
        return "strata config"
    if CLAUDE_CREDS_FILE.exists():
        try:
            creds = json.loads(CLAUDE_CREDS_FILE.read_text(encoding="utf-8"))
            if creds.get("claudeAiOauthToken"):
                return "Claude Code OAuth (~/.claude/.credentials.json)"
        except Exception:  # noqa: BLE001
            pass
    if CLAUDE_CONFIG_FILE.exists():
        try:
            if json.loads(CLAUDE_CONFIG_FILE.read_text()).get("apiKey"):
                return "Claude Code config (~/.claude/config.json)"
        except Exception:  # noqa: BLE001
            pass
    return "unknown"


# ── Codex credential helpers ───────────────────────────────────────────────────

def get_codex_token(config: dict[str, Any] | None = None) -> str | None:
    """Return an OpenAI/Codex API key.

    Resolution order:
      1. ``OPENAI_API_KEY`` environment variable
      2. ``openai_api_key`` in ~/.strata/config.yaml
      3. ``~/.codex/auth.json`` → ``apiKey`` or ``token`` (written by Codex CLI)
    """
    cfg = config or _load_config()
    if key := os.environ.get("OPENAI_API_KEY"):
        return key
    if key := cfg.get("openai_api_key"):
        return key
    if CODEX_AUTH_FILE.exists():
        try:
            auth = json.loads(CODEX_AUTH_FILE.read_text(encoding="utf-8"))
            if key := auth.get("apiKey") or auth.get("token") or auth.get("openai_api_key"):
                return key
            # Codex CLI stores session JWTs under "tokens" dict
            tokens = auth.get("tokens", {})
            if isinstance(tokens, dict):
                if key := tokens.get("access_token"):
                    return key
        except Exception:  # noqa: BLE001
            pass
    return None


# ── Copilot device flow ────────────────────────────────────────────────────────

def _do_copilot_device_flow(
    log_fn: "Callable[[str], None] | None" = None,
) -> str:
    """Run Copilot OAuth device flow and persist token."""
    return _copilot_device_flow_impl(log_fn=log_fn)


def fetch_copilot_models(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Fetch live Copilot models via provider adapter helper."""
    return _fetch_copilot_models_impl(config=config)


# ── Main agent class ───────────────────────────────────────────────────────────

class ArchitectureAgent:
    """Extract architecture elements from freeform text using an AI model.

    Example::

        agent = ArchitectureAgent()
        workspace = agent.extract_from_text(markdown_text, "My Org")
    """

    def __init__(self, provider: str = "auto") -> None:
        self._config = _load_config()
        self._provider = provider if provider != "auto" else self._config.get("provider", "auto")

    # ── Availability checks ────────────────────────────────────────────────────

    def check_available(self) -> tuple[bool, str]:
        """Return (is_available, human-readable description)."""
        p = self._effective_provider()
        return self._check_provider(p)

    def check_all(self) -> list[tuple[str, bool, str]]:
        """Return availability for every known provider as [(name, ok, msg), ...]."""
        return [
            (name, *self._check_provider(name))  # type: ignore[misc]
            for name in list_provider_ids()
        ]

    def _check_provider(self, p: str) -> tuple[bool, str]:
        if p not in list_provider_ids():
            return False, f"Unknown provider '{p}'."
        provider = get_provider(p)
        return provider.availability(self._config)

    def _effective_provider(self) -> str:
        """Resolve 'auto' → first available provider."""
        if self._provider != "auto":
            return self._provider
        configured = self._config.get("provider", "auto")
        if configured != "auto" and configured in list_provider_ids():
            ok, _ = self._check_provider(configured)
            if ok:
                return configured
        for name in AUTO_PROVIDER_ORDER:
            ok, _ = self._check_provider(name)
            if ok:
                return name
        if configured != "auto" and configured in list_provider_ids():
            return configured
        return "copilot"

    def list_models(self, provider: str) -> list[dict[str, Any]]:
        """Return discoverable models for *provider*."""
        return get_provider(provider).list_models(self._config)

    def validate_model(self, provider: str, model: str) -> tuple[bool, str]:
        """Validate a model id for a provider."""
        return get_provider(provider).validate_model(self._config, model)

    def verify_provider_selection(self, provider: str, model: str = "") -> tuple[bool, str]:
        """Transaction preflight for /model provider switching."""
        if provider not in list_provider_ids() and provider != "auto":
            return False, f"Unknown provider '{provider}'."
        if model and provider != "auto":
            ok, msg = self.validate_model(provider, model)
            if not ok:
                return False, f"Model validation failed: {msg}"
        ok, msg = ArchitectureAgent(provider=provider).check_available()
        if not ok:
            return False, msg
        return True, msg

    # ── httpx import helper ────────────────────────────────────────────────────

    @staticmethod
    def _httpx():
        try:
            import httpx  # noqa: PLC0415
            return httpx
        except ImportError as exc:
            raise AgentError(
                "httpx is required for AI features. Install it: pip install httpx"
            ) from exc

    # ── GitHub Copilot ─────────────────────────────────────────────────────────

    def _get_copilot_token(self) -> str:
        """Exchange a Copilot-app GitHub token for a Copilot session token."""
        # Prefer the dedicated Copilot OAuth token (issued by Copilot's own app)
        if COPILOT_GITHUB_TOKEN_FILE.exists():
            github_token = COPILOT_GITHUB_TOKEN_FILE.read_text(encoding="utf-8").strip()
        else:
            github_token = get_github_token(self._config)

        if not github_token:
            raise AgentError(
                "No GitHub token for Copilot.\n"
                "Use /model copilot in the TUI to authenticate."
            )

        httpx = self._httpx()
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(
                    COPILOT_TOKEN_URL,
                    headers={
                        "Authorization": f"token {github_token}",
                        "x-github-api-version": "2025-04-01",
                        **_COPILOT_HEADERS,
                    },
                )
        except Exception as exc:
            raise AgentError(f"Network error during Copilot token exchange: {exc}") from exc
        if resp.status_code == 401:
            raise AgentError(
                "GitHub token rejected (401).\n"
                "Re-authenticate: switch with /model copilot in the TUI."
            )
        if resp.status_code == 403:
            raise AgentError(
                "Copilot access denied (403). Check your subscription: "
                "https://github.com/settings/copilot"
            )
        if resp.status_code == 404:
            if COPILOT_GITHUB_TOKEN_FILE.exists():
                COPILOT_GITHUB_TOKEN_FILE.unlink()
            raise AgentError(
                "Copilot token endpoint returned 404.\n"
                "The stored token was not issued by the Copilot OAuth app.\n"
                "Fix: switch with /model copilot in the TUI to re-authenticate."
            )
        if resp.status_code != 200:
            raise AgentError(
                f"Copilot token exchange failed ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json()["token"]

    def _call_copilot(self, messages: list[dict[str, str]]) -> str:
        copilot_token = self._get_copilot_token()
        httpx = self._httpx()
        model = self._config.get("copilot_model", COPILOT_DEFAULT_MODEL)
        # response_format=json_object is only supported by GPT models
        use_json_mode = model.startswith("gpt-")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            with httpx.Client(timeout=90) as client:
                resp = client.post(
                    COPILOT_CHAT_URL,
                    headers={
                        "Authorization": f"Bearer {copilot_token}",
                        "Content-Type": "application/json",
                        **_COPILOT_HEADERS,
                    },
                    json=payload,
                )
        except Exception as exc:
            raise AgentError(f"Network error calling Copilot chat API: {exc}") from exc
        if resp.status_code != 200:
            raise AgentError(f"Copilot API error {resp.status_code}: {resp.text[:400]}")
        return resp.json()["choices"][0]["message"]["content"]

    # ── Claude / Anthropic ─────────────────────────────────────────────────────

    def _call_claude_api(self, messages: list[dict[str, str]], api_key: str) -> str:
        """Call Anthropic Messages API with a key or Claude Code OAuth token."""
        httpx = self._httpx()
        model = self._config.get("claude_model", ANTHROPIC_DEFAULT_MODEL)

        # Anthropic API uses a separate top-level "system" field
        system_content = next(
            (m["content"] for m in messages if m["role"] == "system"), None
        )
        user_messages = [m for m in messages if m["role"] != "system"]

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": 8096,
            "messages": user_messages,
        }
        if system_content:
            payload["system"] = system_content

        try:
            with httpx.Client(timeout=120) as client:
                resp = client.post(
                    ANTHROPIC_API_URL,
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": ANTHROPIC_API_VERSION,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except Exception as exc:
            raise AgentError(f"Network error calling Anthropic API: {exc}") from exc

        if resp.status_code == 401:
            raise AgentError(
                "Anthropic API key rejected (401).\n"
                "If using Claude Code OAuth, try: claude auth login"
            )
        if resp.status_code != 200:
            raise AgentError(f"Anthropic API error {resp.status_code}: {resp.text[:400]}")

        return resp.json()["content"][0]["text"]

    def _call_claude_subprocess(self, messages: list[dict[str, str]]) -> str:
        """Call Claude Code CLI as a subprocess — uses your Claude subscription directly."""
        if not shutil.which("claude"):
            raise AgentError(
                "'claude' CLI not found. Install Claude Code: https://claude.ai/code\n"
                "Or set ANTHROPIC_API_KEY to use the API directly."
            )
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        convo = [m for m in messages if m["role"] != "system"]
        # Format conversation — multi-turn as Human/Assistant dialogue
        if len(convo) == 1:
            user = convo[0]["content"]
            prompt = f"<system>\n{system}\n</system>\n\n{user}" if system else user
        else:
            parts = [f"<system>\n{system}\n</system>"] if system else []
            for m in convo:
                role = "Human" if m["role"] == "user" else "Assistant"
                parts.append(f"\n{role}: {m['content']}")
            prompt = "\n".join(parts)
        try:
            result = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentError("Claude CLI timed out (180s).") from exc
        if result.returncode != 0:
            raise AgentError(
                f"Claude CLI exited {result.returncode}: {result.stderr[:300]}"
            )
        return result.stdout.strip()

    def _call_claude(self, messages: list[dict[str, str]]) -> str:
        tok = get_claude_token(self._config)
        if tok:
            return self._call_claude_api(messages, tok)
        return self._call_claude_subprocess(messages)

    # ── GitHub Models (legacy) ─────────────────────────────────────────────────

    def _call_github_models(self, messages: list[dict[str, str]]) -> str:
        token = get_github_token(self._config)
        if not token:
            raise AgentError(
                "No GitHub token. Run 'gh auth login' or set GITHUB_TOKEN."
            )
        httpx = self._httpx()
        model = self._config.get("github_model", GITHUB_MODELS_DEFAULT_MODEL)
        try:
            with httpx.Client(timeout=90) as client:
                resp = client.post(
                    GITHUB_MODELS_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "model": model,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "temperature": 0,
                    },
                )
        except Exception as exc:
            raise AgentError(f"Network error calling GitHub Models: {exc}") from exc

        if resp.status_code == 401:
            raise AgentError("GitHub token rejected (401).")
        if resp.status_code != 200:
            raise AgentError(f"GitHub Models error {resp.status_code}: {resp.text[:400]}")
        return resp.json()["choices"][0]["message"]["content"]

    # ── Codex / OpenAI ─────────────────────────────────────────────────────────

    def _call_codex_subprocess(self, messages: list[dict[str, str]]) -> str:
        """Call the Codex CLI as a subprocess — uses your OpenAI subscription."""
        if not shutil.which("codex"):
            raise AgentError(
                "'codex' CLI not found. Install it: https://github.com/openai/codex\n"
                "Or set OPENAI_API_KEY to use the API directly."
            )
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        try:
            result = subprocess.run(
                ["codex", "-q", "--full-auto", user],
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentError("Codex CLI timed out (180s).") from exc
        if result.returncode != 0:
            raise AgentError(f"Codex CLI error: {result.stderr[:300]}")
        return result.stdout.strip()

    def _call_openai(self, messages: list[dict[str, str]]) -> str:
        api_key = get_codex_token(self._config)
        if not api_key:
            raise AgentError("No OpenAI API key. Set OPENAI_API_KEY or run 'strata ai configure'.")
        httpx = self._httpx()
        model = self._config.get("openai_model", OPENAI_DEFAULT_MODEL)
        try:
            with httpx.Client(timeout=90) as client:
                resp = client.post(
                    OPENAI_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "temperature": 0,
                    },
                )
        except Exception as exc:
            raise AgentError(f"Network error calling OpenAI: {exc}") from exc

        if resp.status_code != 200:
            raise AgentError(f"OpenAI API error {resp.status_code}: {resp.text[:400]}")
        return resp.json()["choices"][0]["message"]["content"]

    def _call_codex(self, messages: list[dict[str, str]]) -> str:
        tok = get_codex_token(self._config)
        if tok:
            return self._call_openai(messages)
        return self._call_codex_subprocess(messages)

    # ── Ollama ─────────────────────────────────────────────────────────────────

    def _call_ollama(self, messages: list[dict[str, str]]) -> str:
        httpx = self._httpx()
        host = self._config.get("ollama_host", os.environ.get("OLLAMA_HOST", OLLAMA_DEFAULT_HOST))
        model = self._config.get("ollama_model", OLLAMA_DEFAULT_MODEL)
        try:
            with httpx.Client(timeout=180) as client:
                resp = client.post(
                    f"{host}/api/chat",
                    json={"model": model, "messages": messages, "stream": False, "format": "json"},
                )
        except Exception as exc:
            raise AgentError(f"Network error calling Ollama at {host}: {exc}") from exc
        if resp.status_code != 200:
            raise AgentError(f"Ollama error {resp.status_code}: {resp.text[:400]}")
        return resp.json()["message"]["content"]

    # ── Dispatch ───────────────────────────────────────────────────────────────

    def _call(self, messages: list[dict[str, str]]) -> str:
        provider_id = self._effective_provider()
        if provider_id not in list_provider_ids():
            raise AgentError(f"Unknown provider '{provider_id}'. Run 'strata ai configure'.")
        provider = get_provider(provider_id)
        return provider.chat(self._config, messages)

    # ── Public API ─────────────────────────────────────────────────────────────

    def ask(self, prompt: str, system: str = "") -> str:
        """Lightweight direct AI call — no agentic pipeline overhead.

        Unlike ``chat()``, this does NOT inject the full agent system prompt,
        tool list, or workspace snapshot.  Use it for structured JSON generation
        or any call where you control the full prompt yourself.

        Parameters
        ----------
        prompt:
            The user-turn content to send.
        system:
            Optional system message.  If empty, no system role is added.

        Returns
        -------
        str — raw response with leading/trailing markdown code-fences removed.
        """
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        raw = self._call(messages)
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())
        return raw

    def extract_from_text(
        self,
        text: str,
        workspace_name: str = "Extracted Architecture",
    ) -> ArchitectureWorkspace:
        """Send *text* to the AI and return a populated ArchitectureWorkspace."""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Extract all architecture information from the document below "
                    f"and return a JSON object.\n\n{text}"
                ),
            },
        ]
        raw = self._call(messages)

        # Strip accidental markdown fences
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())

        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            snippet = raw[:300] + ("…" if len(raw) > 300 else "")
            raise AgentError(
                f"AI returned invalid JSON: {exc}\nRaw output: {snippet}"
            ) from exc

        if "manifest" not in data:
            data["manifest"] = {"name": workspace_name}
        elif not data["manifest"].get("name"):
            data["manifest"]["name"] = workspace_name

        try:
            return ArchitectureWorkspace.model_validate(data)
        except Exception as exc:
            raise AgentError(
                f"AI JSON did not match workspace schema: {exc}\n"
                f"Raw (first 400 chars): {raw[:400]}"
            ) from exc

    def extract_entity_fields(
        self,
        entity_type: str,
        prompt: str,
        workspace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Extract structured entity fields from a natural language *prompt*.

        *workspace_context* should contain lists of existing IDs so the AI can
        resolve cross-references (``domain_ids``, ``capability_ids``, etc.).
        """
        schema = _ENTITY_SCHEMAS.get(entity_type)
        if not schema:
            valid = ", ".join(_ENTITY_SCHEMAS)
            raise AgentError(f"Unknown entity type '{entity_type}'. Valid: {valid}")
        ctx_block = (
            f"\n\nWorkspace context:\n{json.dumps(workspace_context, indent=2)}"
            if workspace_context
            else ""
        )
        user_msg = (
            f"Entity type: {entity_type}\n"
            f"Description: {schema['description']}\n\n"
            f"Fields to extract:\n{json.dumps(schema['fields'], indent=2)}"
            f"{ctx_block}\n\n"
            f"Natural language input:\n{prompt}"
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _FIELD_EXTRACTION_SYSTEM},
            {"role": "user", "content": user_msg},
        ]
        raw = self._call(messages)
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentError(
                f"AI returned invalid JSON: {exc}\nRaw: {raw[:300]}"
            ) from exc

    def classify_and_extract(
        self,
        prompt: str,
        workspace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Classify intent and extract fields from a free-form natural language command.

        Returns a dict with keys: ``entity``, ``action``, ``fields``.
        """
        schema_block = json.dumps(
            {k: v["fields"] for k, v in _ENTITY_SCHEMAS.items()}, indent=2
        )
        ctx_block = (
            f"\n\nWorkspace context:\n{json.dumps(workspace_context, indent=2)}"
            if workspace_context
            else ""
        )
        user_msg = (
            f"Entity field schemas:\n{schema_block}"
            f"{ctx_block}\n\n"
            f"Instruction:\n{prompt}"
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _CLASSIFY_SYSTEM},
            {"role": "user", "content": user_msg},
        ]
        raw = self._call(messages)
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())
        try:
            result: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentError(
                f"AI returned invalid JSON: {exc}\nRaw: {raw[:300]}"
            ) from exc
        if "entity" not in result or "fields" not in result:
            raise AgentError(
                f"AI response missing required keys. Got: {list(result)}"
            )
        return result
    def scan_document(
        self,
        text: str,
        source_name: str = "",
        workspace_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Scan *text* for architecture artefacts.

        Returns a list of ``{"entity": ..., "fields": {...}}`` dicts ready for staging.
        *workspace_context* (with existing IDs) helps the AI avoid proposing duplicates.
        """
        ctx_block = (
            f"\n\nExisting workspace IDs for deduplication:\n{json.dumps(workspace_context, indent=2)}"
            if workspace_context
            else ""
        )
        user_msg = (
            f"Source: {source_name or 'document'}"
            f"{ctx_block}\n\n"
            f"Document:\n{text[:12000]}"
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _SCAN_SYSTEM},
            {"role": "user", "content": user_msg},
        ]
        raw = self._call(messages)
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())
        try:
            result = json.loads(raw)
            if not isinstance(result, list):
                return []
            return [
                item for item in result
                if isinstance(item, dict) and "entity" in item and "fields" in item
            ]
        except json.JSONDecodeError:
            return []

    def chat(
        self,
        messages: list[dict[str, str]],
        workspace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Agentic conversational interface — responds naturally and proposes entity actions.

        Parameters
        ----------
        messages:
            Conversation history as ``[{"role": "user"|"assistant", "content": "..."}]``.
            The last message should be the current user input (no system message).
        workspace_context:
            Current workspace state with full entity listings, used to build the system
            prompt snapshot so the LLM knows what already exists.

        Returns
        -------
        dict with:
            ``message``  — conversational reply to display to the user
            ``actions``  — list of ``{"entity": str, "fields": dict}`` to propose
            ``tools``    — list of ``{"tool": "scan_folder", "path": str}`` to execute
        """
        # ── Build workspace snapshot ───────────────────────────────────────────
        lines: list[str] = []
        watch_folders_lines: list[str] = []
        if workspace_context:
            ws_name = workspace_context.get("workspace_name", "Unknown")
            lines.append(f"Workspace: {ws_name}\n")
            for section, items in workspace_context.get("entities", {}).items():
                if items:
                    lines.append(f"{section.capitalize()} ({len(items)}):")
                    for item in items[:25]:  # cap to keep prompt manageable
                        lines.append("  - " + ", ".join(
                            f"{k}={v}" for k, v in item.items() if v
                        ))
                else:
                    lines.append(f"{section.capitalize()}: (none)")
            # Watch folders
            folders = workspace_context.get("watch_folders", [])
            if folders:
                watch_folders_lines = [f"  - {f}" for f in folders]
            else:
                watch_folders_lines = ["  (none configured — user can add via /folders or 'strata workspace add-folder')"]
        else:
            lines = ["(no workspace loaded)"]
            watch_folders_lines = ["  (no workspace)"]

        workspace_snapshot = "\n".join(lines)
        watch_folders_str = "\n".join(watch_folders_lines)

        # ── Build staging snapshot ─────────────────────────────────────────────
        staging_lines: list[str] = []
        if workspace_context:
            pending = workspace_context.get("staging", [])
            if pending:
                staging_lines.append(f"Pending items ({len(pending)}):")
                for s in pending[:50]:  # cap to keep prompt manageable
                    staging_lines.append(
                        f"  - id={s['id']}, entity={s['entity']}, "
                        f"name={s.get('name', '')}, source={s.get('source', '')}"
                    )
            else:
                staging_lines.append("  (no pending items)")
        else:
            staging_lines.append("  (no workspace)")
        staging_snapshot = "\n".join(staging_lines)

        # ── Build entity schema summary ────────────────────────────────────────
        schema_lines: list[str] = []
        for etype, schema in _ENTITY_SCHEMAS.items():
            schema_lines.append(f"\n{etype} — {schema['description']}")
            for fname, fdesc in schema["fields"].items():
                schema_lines.append(f"  {fname}: {fdesc}")
        entity_schemas = "\n".join(schema_lines)

        system = (
            _AGENTIC_SYSTEM
            .replace("__WORKSPACE_SNAPSHOT__", workspace_snapshot)
            .replace("__ENTITY_SCHEMAS__", entity_schemas)
            .replace("__WATCH_FOLDERS__", watch_folders_str)
            .replace("__STAGING_SNAPSHOT__", staging_snapshot)
        )

        all_messages = [{"role": "system", "content": system}] + messages
        raw = self._call(all_messages)
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())

        try:
            result: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            # Graceful fallback — treat raw text as the conversational message
            return {"message": raw.strip(), "actions": [], "tools": []}

        if not isinstance(result, dict):
            return {"message": str(result), "actions": [], "tools": []}

        return {
            "message": result.get("message", ""),
            "actions": [
                a for a in result.get("actions", [])
                if isinstance(a, dict) and "entity" in a and "fields" in a
            ],
            "tools": [
                t for t in result.get("tools", [])
                if isinstance(t, dict) and t.get("tool") == "scan_folder" and "path" in t
            ],
        }
