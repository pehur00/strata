from __future__ import annotations

import os
from types import SimpleNamespace

from strata.agent import ArchitectureAgent
from strata.providers.claude_cli import ClaudeCliProvider
from strata.providers.codex_cli import CodexCliProvider
from strata.providers.copilot import CopilotProvider
from strata.providers.disabled import DisabledByPolicyProvider
from strata.tui import StrataApp


def test_copilot_preflight_404_marks_unavailable(monkeypatch):
    provider = CopilotProvider()

    monkeypatch.setattr(
        "strata.providers.copilot._token_candidates",
        lambda _cfg: ("gh-token", "gh CLI", False),
    )
    monkeypatch.setattr(
        CopilotProvider,
        "_exchange_session_token",
        lambda self, tok: (404, "not found"),
    )

    ok, msg = provider.availability({})
    assert ok is False
    assert "404" in msg


def test_copilot_model_validation_is_strict(monkeypatch):
    provider = CopilotProvider()
    monkeypatch.setattr(
        CopilotProvider,
        "list_models",
        lambda self, _cfg: [{"id": "claude-sonnet-4.6"}, {"id": "gpt-5.4"}],
    )

    ok, _ = provider.validate_model({}, "claude-sonnet-4.6")
    assert ok is True

    ok, msg = provider.validate_model({}, "unknown-model")
    assert ok is False
    assert "Unknown Copilot model" in msg


def test_copilot_interactive_auth_hook(monkeypatch):
    provider = CopilotProvider()
    called = {"ran": False}

    monkeypatch.setattr(
        "strata.providers.copilot.do_copilot_device_flow",
        lambda log_fn=None: called.__setitem__("ran", True) or "token",
    )

    ok, msg = provider.run_interactive_auth({}, log_fn=lambda _m: None)
    assert provider.supports_interactive_auth() is True
    assert ok is True
    assert "completed" in msg.lower()
    assert called["ran"] is True


def test_claude_cli_chat_uses_model_flag(monkeypatch):
    provider = ClaudeCliProvider()
    captured: dict[str, list[str]] = {}

    monkeypatch.setattr("strata.providers.claude_cli.shutil.which", lambda _: "/usr/bin/claude")

    def _fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("strata.providers.claude_cli.subprocess.run", _fake_run)

    out = provider.chat(
        {"claude_model": "claude-sonnet-4-5"},
        [{"role": "user", "content": "hello"}],
    )
    assert out == "ok"
    assert "--model" in captured["cmd"]
    assert "claude-sonnet-4-5" in captured["cmd"]


def test_codex_cli_chat_uses_model_flag(monkeypatch):
    provider = CodexCliProvider()
    captured: dict[str, list[str]] = {}

    monkeypatch.setattr("strata.providers.codex_cli.shutil.which", lambda _: "/usr/bin/codex")

    def _fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("strata.providers.codex_cli.subprocess.run", _fake_run)

    out = provider.chat(
        {"openai_model": "gpt-5.3-codex"},
        [{"role": "user", "content": "hello"}],
    )
    assert out == "ok"
    assert "--model" in captured["cmd"]
    assert "gpt-5.3-codex" in captured["cmd"]


def test_codex_availability_detects_vscode_extension_binary(monkeypatch, tmp_path):
    provider = CodexCliProvider()
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_codex.chmod(0o755)

    monkeypatch.setattr("strata.providers.codex_cli.shutil.which", lambda _bin: None)
    monkeypatch.setattr("strata.providers.codex_cli.glob.glob", lambda _pat: [str(fake_codex)])
    monkeypatch.setattr("strata.providers.codex_cli.has_codex_oauth", lambda: True)

    ok, msg = provider.availability({"openai_model": "gpt-5.4"})
    assert ok is True
    assert str(fake_codex) in msg


def test_disabled_provider_is_deterministic():
    provider = DisabledByPolicyProvider("openai", "hint")
    ok, msg = provider.availability({})
    assert ok is False
    assert "disabled by OAuth-only policy" in msg


def test_agent_dispatches_via_registry(monkeypatch):
    class _FakeProvider:
        def availability(self, _cfg):
            return True, "ok"

        def validate_model(self, _cfg, _model):
            return True, "ok"

        def chat(self, _cfg, _messages):
            return '{"message":"ok","actions":[],"tools":[]}'

        def list_models(self, _cfg):
            return []

    monkeypatch.setattr("strata.agent.list_provider_ids", lambda: ("copilot",))
    monkeypatch.setattr("strata.agent.get_provider", lambda _id: _FakeProvider())

    agent = ArchitectureAgent(provider="copilot")
    result = agent.chat([{"role": "user", "content": "hello"}], workspace_context=None)
    assert result["message"] == "ok"


def test_startup_invalid_copilot_triggers_auto_auth(monkeypatch):
    app = StrataApp()
    called = {"auth": False}

    monkeypatch.setattr(app, "_config_get", lambda _k, _d="": "copilot")
    monkeypatch.setattr(app, "_log", lambda _m: None)
    monkeypatch.setattr(
        app,
        "_start_provider_auth",
        lambda provider, previous="auto", model="", verify_after_auth=True: called.__setitem__("auth", provider == "copilot"),
    )
    monkeypatch.setattr("strata.tui.ArchitectureAgent.check_available", lambda self: (False, "bad token"))

    app._check_startup_provider_health()
    assert called["auth"] is True


def test_startup_valid_copilot_does_not_trigger_auto_auth(monkeypatch):
    app = StrataApp()
    called = {"auth": False}

    monkeypatch.setattr(app, "_config_get", lambda _k, _d="": "copilot")
    monkeypatch.setattr(app, "_log", lambda _m: None)
    monkeypatch.setattr(
        app,
        "_start_provider_auth",
        lambda provider, previous="auto", model="", verify_after_auth=True: called.__setitem__("auth", provider == "copilot"),
    )
    monkeypatch.setattr("strata.tui.ArchitectureAgent.check_available", lambda self: (True, "ok"))

    app._check_startup_provider_health()
    assert called["auth"] is False


def test_model_overview_subcommand_routes_to_overview(monkeypatch):
    app = StrataApp()
    called = {"overview": False, "provider": None}

    class _FakeInput:
        def focus(self):
            return None

    monkeypatch.setattr(app, "_show_model_picker", lambda: called.__setitem__("overview", True))
    monkeypatch.setattr(app, "_set_model_provider", lambda p, m="": called.__setitem__("provider", (p, m)))
    monkeypatch.setattr(app, "query_one", lambda *_args, **_kwargs: _FakeInput())

    app._handle_slash("model overview")
    assert called["overview"] is True
    assert called["provider"] is None
