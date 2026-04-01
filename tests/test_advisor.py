from __future__ import annotations

import json
from pathlib import Path

from strata.advisor import advisory_to_todo_items, load_latest_advisory, run_advisory_cycle
from strata.models import ArchitectureWorkspace, WorkspaceManifest
from strata.scoring import _ODA_DIM_QUESTIONS
from strata.tui import StrataApp


def _workspace(name: str = "AdvisorTest") -> ArchitectureWorkspace:
    return ArchitectureWorkspace(manifest=WorkspaceManifest(name=name))


def _patch_fake_agent(monkeypatch, ask_payload=None, available: bool = True) -> None:
    def _fake_init(self, provider: str = "auto"):
        self._provider = provider
        self._config = {"copilot_model": "gpt-4o"}

    monkeypatch.setattr("strata.advisor.ArchitectureAgent.__init__", _fake_init)
    monkeypatch.setattr("strata.advisor.ArchitectureAgent._effective_provider", lambda self: "copilot")
    monkeypatch.setattr(
        "strata.advisor.ArchitectureAgent.check_available",
        lambda self: (available, "ok" if available else "provider unavailable"),
    )

    if ask_payload is None:
        ask_payload = '{"domains":{}}'

    if callable(ask_payload):
        monkeypatch.setattr("strata.advisor.ArchitectureAgent.ask", lambda self, p: ask_payload(p))
    else:
        monkeypatch.setattr("strata.advisor.ArchitectureAgent.ask", lambda self, _prompt: ask_payload)


def _orchestrator_payload(*, duplicate_gaps: bool = False) -> str:
    gaps = [
        {
            "id": "g1",
            "gap": "Integration contracts are inconsistent across domains.",
            "gap_type": "integration_contract_drift",
            "severity": "high",
            "dimension": "contract_quality",
        }
    ]
    if duplicate_gaps:
        gaps.append(dict(gaps[0]))

    decisions = [
        {
            "id": "d1",
            "decision": "Adopt a consistent contract versioning strategy.",
            "priority": "high",
            "dimension": "contract_quality",
            "status": "open",
            "gap_type": "integration_contract_drift",
            "inputs_needed": ["current APIs", "target versioning policy"],
            "expected_benefit": "Improves interoperability and reduces integration failures.",
        }
    ]

    payload = {
        "domains": {
            "enterprise": {
                "domain_summary": "Enterprise needs stronger contract standardization.",
                "maturity_gaps": list(gaps),
                "decisions_needed": list(decisions),
                "cross_domain_dependencies": ["enterprise -> data (api)"],
                "interop_risks": ["critical: contracts drift between domains"],
            },
            "data": {
                "domain_summary": "Data exchanges need explicit contracts.",
                "maturity_gaps": list(gaps),
                "decisions_needed": list(decisions),
                "cross_domain_dependencies": ["data -> solution (event)"],
                "interop_risks": ["critical: event schemas are not versioned"],
            },
            "solution": {
                "domain_summary": "Solution integration choices are not fully documented.",
                "maturity_gaps": list(gaps),
                "decisions_needed": list(decisions),
                "cross_domain_dependencies": ["solution -> data (api)"],
                "interop_risks": ["high: interface assumptions are implicit"],
            },
            "governance_interop": {
                "domain_summary": "Governance and interoperability controls need hardening.",
                "maturity_gaps": list(gaps),
                "decisions_needed": list(decisions),
                "cross_domain_dependencies": ["governance -> enterprise (policy)"],
                "interop_risks": ["critical: no approval gate for interface changes"],
            },
        }
    }
    return json.dumps(payload)


def test_run_advisory_cycle_degraded_persists_files(monkeypatch, tmp_path: Path):
    _patch_fake_agent(monkeypatch, ask_payload='{"domains":{}}', available=False)

    run = run_advisory_cycle(_workspace(), profile="oda", provider="auto", root=tmp_path)
    assert run["meta"]["degraded"] is True

    synthesis = run["panel"]["synthesis"]
    assert len(synthesis["domain_scores"]) == 4
    assert synthesis["recommended_docs"]

    advice_dir = tmp_path / "architecture" / "advice"
    assert (advice_dir / "latest.yaml").exists()
    assert (advice_dir / "latest.md").exists()
    assert (advice_dir / "decision-backlog.md").exists()
    assert any((advice_dir / "runs").glob("*.yaml"))
    assert any((advice_dir / "runs").glob("*.md"))


def test_orchestration_dedupes_duplicate_subagent_outputs(monkeypatch, tmp_path: Path):
    _patch_fake_agent(monkeypatch, ask_payload=_orchestrator_payload(duplicate_gaps=True), available=True)
    monkeypatch.setattr("strata.advisor._needs_deep_dive", lambda *_a, **_k: False)

    run = run_advisory_cycle(_workspace("OrchestrationTest"), profile="oda", provider="auto", root=tmp_path)
    synthesis = run["panel"]["synthesis"]

    assert len(synthesis["decisions_needed"]) == 1
    assert len(run["panel"]["domains"]) == 4
    assert len(run["panel"]["subagents"]) == 4

    assert synthesis["recommended_docs"]
    first_doc = synthesis["recommended_docs"][0]
    for key in [
        "doc_type",
        "title",
        "domain",
        "reason",
        "inputs_needed",
        "expected_benefit",
        "priority",
        "draft_template_ref",
        "gap_fingerprint",
    ]:
        assert key in first_doc


def test_load_latest_and_map_to_todo_items(monkeypatch, tmp_path: Path):
    _patch_fake_agent(monkeypatch, ask_payload=_orchestrator_payload(), available=True)
    monkeypatch.setattr("strata.advisor._needs_deep_dive", lambda *_a, **_k: False)

    run_advisory_cycle(_workspace(), profile="oda", provider="auto", root=tmp_path)

    latest = load_latest_advisory(tmp_path)
    assert latest is not None
    items = advisory_to_todo_items(latest)
    assert len(items) >= 1
    assert items[0].doc_type
    assert items[0].subject.endswith(".md")


def test_confidence_gate_triggers_deep_dive(monkeypatch, tmp_path: Path):
    calls = {"count": 0}

    def _ask(prompt: str) -> str:
        calls["count"] += 1
        if "Perform a deep-dive advisory refinement" in prompt:
            payload = {
                "domain_summary": "Deep dive completed.",
                "maturity_gaps": [
                    {
                        "id": "g1",
                        "gap": "Critical interoperability issue remains unresolved.",
                        "gap_type": "integration_contract_drift",
                        "severity": "critical",
                        "dimension": "contract_quality",
                    }
                ],
                "decisions_needed": [
                    {
                        "id": "d1",
                        "decision": "Define contract governance board and SLA for change control.",
                        "priority": "high",
                        "dimension": "governance_and_decision_traceability",
                        "status": "open",
                        "gap_type": "governance_gap",
                    }
                ],
                "cross_domain_dependencies": ["enterprise -> data (api)"],
                "interop_risks": ["critical: unresolved interface ownership"],
            }
            return json.dumps(payload)
        return _orchestrator_payload()

    _patch_fake_agent(monkeypatch, ask_payload=_ask, available=True)

    run = run_advisory_cycle(_workspace("DeepDiveTest"), profile="oda", provider="auto", root=tmp_path)
    assert calls["count"] >= 2
    assert run["panel"]["domains"][0]["domain_summary"]


def test_progress_callback_emits_phase_sequence(monkeypatch, tmp_path: Path):
    _patch_fake_agent(monkeypatch, ask_payload=_orchestrator_payload(), available=True)
    monkeypatch.setattr("strata.advisor._needs_deep_dive", lambda *_a, **_k: False)

    events: list[dict] = []
    run = run_advisory_cycle(
        _workspace("ProgressTest"),
        profile="oda",
        provider="auto",
        root=tmp_path,
        progress_cb=lambda e: events.append(e),
    )

    assert run["meta"]["run_id"]
    phases = [str(e.get("phase", "")) for e in events]
    assert "preflight" in phases
    assert "orchestrator" in phases
    assert "synthesis" in phases
    assert "persist" in phases
    assert "complete" in phases
    assert any(p.startswith("domain:enterprise:normalise") for p in phases)


def test_degraded_mode_emits_progress_and_completion(monkeypatch, tmp_path: Path):
    _patch_fake_agent(monkeypatch, ask_payload='{"domains":{}}', available=False)

    events: list[dict] = []
    run = run_advisory_cycle(
        _workspace("DegradedProgressTest"),
        profile="oda",
        provider="auto",
        root=tmp_path,
        progress_cb=lambda e: events.append(e),
    )

    assert run["meta"]["degraded"] is True
    phases = [str(e.get("phase", "")) for e in events]
    assert "preflight" in phases
    assert "synthesis" in phases
    assert "persist" in phases
    done = [e for e in events if str(e.get("phase")) == "complete"]
    assert done
    assert str(done[-1].get("state")) == "degraded"


def test_start_advisor_scheduler_uses_manifest_interval(monkeypatch):
    app = StrataApp()
    ws = _workspace("SchedTest")
    ws = ws.model_copy(
        update={
            "manifest": ws.manifest.model_copy(
                update={"advisor_enabled": True, "advisor_interval_minutes": 5}
            )
        }
    )
    app._workspace = ws

    called: dict[str, object] = {}

    class _Timer:
        def stop(self):
            return None

    def _fake_set_interval(seconds, callback, name=None):
        called["seconds"] = seconds
        called["name"] = name
        called["callback"] = callback
        return _Timer()

    monkeypatch.setattr(app, "set_interval", _fake_set_interval)
    monkeypatch.setattr(app, "_log", lambda _m: None)

    app._start_advisor_scheduler()
    assert called["seconds"] == 300
    assert called["name"] == "auto-advisor"

    called.clear()
    ws_disabled = ws.model_copy(
        update={
            "manifest": ws.manifest.model_copy(
                update={"advisor_enabled": False, "advisor_interval_minutes": 0}
            )
        }
    )
    app._workspace = ws_disabled
    app._start_advisor_scheduler()
    assert called == {}


def test_improve_ai_opens_latest_advisory(monkeypatch):
    app = StrataApp()
    app._workspace = _workspace("UiTest")
    advisory = {
        "meta": {"profile": "oda", "workspace_name": "UiTest"},
        "panel": {
            "synthesis": {
                "domain_scores": [
                    {
                        "domain": "enterprise",
                        "weighted_score": 2.4,
                        "confidence": 0.8,
                        "attention_level": "high",
                        "dimensions": [],
                    }
                ],
                "recommended_docs": [
                    {
                        "doc_type": "HLD",
                        "title": "Solution Capability HLD",
                        "domain": "solution",
                        "reason": "Need end-to-end capability design.",
                        "inputs_needed": ["capability scope"],
                        "expected_benefit": "delivery_speed",
                        "priority": "medium",
                        "draft_template_ref": "hld-capability-solution",
                        "gap_fingerprint": "abc123",
                        "subject": "solution-capability-hld.md",
                        "action": "Define interoperability target architecture",
                    }
                ],
            }
        },
    }
    called = {"rendered": False}

    monkeypatch.setattr("strata.tui.load_latest_advisory", lambda: advisory)
    monkeypatch.setattr(
        app,
        "_render_advisory_overview",
        lambda _a, _t: called.__setitem__("rendered", True),
    )

    app._start_improve_ai_workflow()
    assert app._wf_active is True
    assert len(app._wf_todo) == 1
    assert called["rendered"] is True


def test_advisor_interval_command_updates_manifest_and_scheduler(monkeypatch):
    app = StrataApp()
    ws = _workspace("CmdTest")
    app._workspace = ws

    calls = {"minutes": None, "scheduler": False}
    monkeypatch.setattr("strata.tui.set_advisor_interval", lambda m: calls.__setitem__("minutes", m))
    monkeypatch.setattr("strata.tui.load_workspace", lambda: ws)
    monkeypatch.setattr(app, "_start_advisor_scheduler", lambda: calls.__setitem__("scheduler", True))
    monkeypatch.setattr(app, "_show_dashboard", lambda: None)
    monkeypatch.setattr(app, "_log_strata", lambda _m: None)

    app._handle_advisor_slash("interval 15")
    assert calls["minutes"] == 15
    assert calls["scheduler"] is True


def test_advisor_progress_subcommand_dispatch(monkeypatch):
    app = StrataApp()
    called = {"progress": False}
    monkeypatch.setattr(app, "_show_advisor_progress", lambda: called.__setitem__("progress", True))
    app._handle_advisor_slash("progress")
    assert called["progress"] is True


def test_advisor_status_renders_domain_cards(monkeypatch):
    app = StrataApp()
    app._workspace = _workspace("AdvisorUi")
    out: list[str] = []
    monkeypatch.setattr(app, "_open_main", lambda _t: None)
    monkeypatch.setattr(app, "_main", lambda text: out.append(text))
    monkeypatch.setattr(
        "strata.tui.ArchitectureAgent.check_available",
        lambda _self: (True, "provider ok"),
    )
    monkeypatch.setattr(
        "strata.tui.load_latest_advisory",
        lambda: {
            "meta": {
                "run_id": "r1",
                "generated_at": "2026-03-31T10:00:00Z",
                "provider": "copilot",
                "model": "claude-sonnet-4-6",
                "degraded": False,
                "paths": {
                    "latest_yaml": "architecture/advice/latest.yaml",
                    "decision_backlog": "architecture/advice/decision-backlog.md",
                },
            },
            "panel": {
                "domains": [
                    {
                        "domain": "enterprise",
                        "cross_domain_dependencies": ["enterprise -> data (api)"],
                        "interop_risks": ["critical: contract drift"],
                    }
                ],
                "synthesis": {
                    "domain_scores": [
                        {
                            "domain": "enterprise",
                            "weighted_score": 2.3,
                            "confidence": 0.7,
                            "attention_level": "high",
                            "dimensions": [
                                {"key": "contract_quality", "score": 1.9},
                                {"key": "delivery_enablement", "score": 2.2},
                            ],
                        }
                    ],
                    "decisions_needed": [
                        {
                            "domain": "enterprise",
                            "decision": "Define contract ownership",
                            "priority": "high",
                            "priority_score": 5.2,
                            "status": "open",
                        }
                    ],
                    "recommended_docs": [
                        {
                            "domain": "enterprise",
                            "doc_type": "ADR",
                            "title": "Enterprise — Contract Versioning Strategy",
                            "priority": "high",
                            "priority_score": 5.1,
                        }
                    ],
                },
            },
        },
    )

    app._show_advisor_status()
    joined = "\n".join(out)
    assert "Domain Agents" in joined
    assert "enterprise" in joined
    assert "Define contract ownership" in joined
    assert "Contract Versioning Strategy" in joined


def test_advisor_progress_view_shows_failed_timeline(monkeypatch):
    app = StrataApp()
    out: list[str] = []
    monkeypatch.setattr(app, "_open_main", lambda _t: None)
    monkeypatch.setattr(app, "_main", lambda text: out.append(text))
    app._advisor_runtime_state = "failed"
    app._advisor_runtime_run_id = "run-1"
    app._advisor_runtime_phase = "failed"
    app._advisor_last_timeline = [
        {
            "ts": "2026-03-31T10:00:00Z",
            "phase": "failed",
            "state": "failed",
            "message": "Advisory run failed: boom",
        }
    ]

    app._show_advisor_progress()
    joined = "\n".join(out)
    assert "state:" in joined
    assert "failed" in joined
    assert "Advisory run failed: boom" in joined


def test_oda_questions_use_concept_only_language():
    combined = " ".join(_ODA_DIM_QUESTIONS.values()).lower()
    forbidden = [
        "tm forum",
        "party management",
        "resource management",
        "component business model",
    ]
    for token in forbidden:
        assert token not in combined
