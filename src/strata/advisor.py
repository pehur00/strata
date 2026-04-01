from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from .agent import AgentError, ArchitectureAgent
from .models import ArchitectureWorkspace
from .scoring import ScoreResult, TodoItem, score_workspace
from .workspace import WORKSPACE_DIR, find_workspace_root

_DOMAINS: list[dict[str, Any]] = [
    {
        "id": "enterprise",
        "name": "Enterprise Architect",
        "focus": "business capability boundaries, ownership, and portfolio coherence",
        "evidence_keys": ["capabilities", "applications", "standards"],
    },
    {
        "id": "data",
        "name": "Data Architect",
        "focus": "data ownership, contracts, lifecycle, and domain exchange quality",
        "evidence_keys": ["data_domains", "data_products", "data_flows"],
    },
    {
        "id": "solution",
        "name": "Solution Architect",
        "focus": "solution decomposition, tradeoff traceability, and implementation fit",
        "evidence_keys": ["solutions", "solution_components", "adrs"],
    },
    {
        "id": "governance_interop",
        "name": "Governance/Interoperability Architect",
        "focus": "governance guardrails, interoperability quality, and operational resilience",
        "evidence_keys": ["standards", "data_flows", "adrs", "applications"],
    },
]

_RUBRIC_DIMENSIONS: list[dict[str, str]] = [
    {"key": "modularity_and_boundaries", "label": "Modularity and Boundaries"},
    {"key": "contract_quality", "label": "Contract Quality"},
    {"key": "data_design_and_ownership", "label": "Data Design and Ownership"},
    {"key": "operability_and_nfrs", "label": "Operability and NFRs"},
    {
        "key": "governance_and_decision_traceability",
        "label": "Governance and Decision Traceability",
    },
    {"key": "delivery_enablement", "label": "Delivery Enablement"},
]

_DIMENSION_ANCHORS = {
    0: "absent",
    1: "ad hoc",
    2: "partial/inconsistent",
    3: "baseline defined and used",
    4: "consistently applied and measured",
    5: "optimized with feedback loops",
}

_DIMENSION_WEIGHT_PROFILE = {
    "modularity_and_boundaries": 1.0,
    "contract_quality": 1.0,
    "data_design_and_ownership": 1.0,
    "operability_and_nfrs": 1.0,
    "governance_and_decision_traceability": 1.0,
    "delivery_enablement": 1.0,
}

_GAP_TYPE_TO_DOCS: dict[str, list[dict[str, str]]] = {
    "boundary_ambiguity": [
        {
            "doc_type": "ADR",
            "title": "Service and Domain Boundary Decision",
            "draft_template_ref": "adr-boundary-decision",
            "expected_benefit": "interop_gain",
        }
    ],
    "integration_contract_drift": [
        {
            "doc_type": "Interface Contract Spec",
            "title": "Interface Contract Baseline",
            "draft_template_ref": "interface-contract-spec",
            "expected_benefit": "interop_gain",
        },
        {
            "doc_type": "ADR",
            "title": "Contract Versioning Strategy",
            "draft_template_ref": "adr-contract-versioning",
            "expected_benefit": "risk_reduction",
        },
    ],
    "major_architecture_tradeoff": [
        {
            "doc_type": "ADR",
            "title": "Technology and Approach Decision",
            "draft_template_ref": "adr-technology-decision",
            "expected_benefit": "delivery_speed",
        }
    ],
    "capability_design_gap": [
        {
            "doc_type": "HLD",
            "title": "Capability Solution High-Level Design",
            "draft_template_ref": "hld-capability-solution",
            "expected_benefit": "delivery_speed",
        }
    ],
    "data_ownership_lifecycle": [
        {
            "doc_type": "Data Model & Ownership Spec",
            "title": "Data Ownership and Lifecycle Specification",
            "draft_template_ref": "data-ownership-spec",
            "expected_benefit": "risk_reduction",
        },
        {
            "doc_type": "ADR",
            "title": "Data Source-of-Truth Decision",
            "draft_template_ref": "adr-source-of-truth",
            "expected_benefit": "interop_gain",
        },
    ],
    "nfr_slo_uncertainty": [
        {
            "doc_type": "NFR/SLO Specification",
            "title": "Operational SLO and NFR Baseline",
            "draft_template_ref": "nfr-slo-spec",
            "expected_benefit": "risk_reduction",
        },
        {
            "doc_type": "Runbook",
            "title": "Operational Runbook",
            "draft_template_ref": "runbook-template",
            "expected_benefit": "risk_reduction",
        },
    ],
    "governance_gap": [
        {
            "doc_type": "Architecture Governance Policy",
            "title": "Architecture Governance Policy",
            "draft_template_ref": "governance-policy",
            "expected_benefit": "risk_reduction",
        },
        {
            "doc_type": "Decision Log",
            "title": "Decision Traceability Log",
            "draft_template_ref": "decision-log",
            "expected_benefit": "delivery_speed",
        },
    ],
}

_MODEL_KEY_BY_PROVIDER = {
    "copilot": "copilot_model",
    "claude": "claude_model",
    "codex": "openai_model",
    "github": "github_model",
    "openai": "openai_model",
    "ollama": "ollama_model",
}

_PRIORITY_ORDER = {"high": 1, "medium": 2, "low": 3}


def _emit_progress(
    progress_cb: Callable[[dict[str, Any]], None] | None,
    run_id: str,
    phase: str,
    state: str = "running",
    message: str = "",
    domain: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit one advisory progress event, swallowing callback failures."""
    if progress_cb is None:
        return
    event: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "phase": phase,
        "state": state,
        "message": message,
    }
    if domain:
        event["domain"] = domain
    if extra:
        event.update(extra)
    try:
        progress_cb(event)
    except Exception:
        # Progress telemetry must never break advisory execution.
        return


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _clean_json(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```$", "", cleaned)
    return cleaned.strip()


def _clamp(value: float, lo: float = 0.0, hi: float = 5.0) -> float:
    return max(lo, min(hi, value))


def _workspace_fingerprint(workspace: ArchitectureWorkspace) -> str:
    payload = json.dumps(
        workspace.model_dump(exclude_none=True),
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _advice_paths(root: Path | None = None) -> tuple[Path, Path, Path]:
    ws_root = root or find_workspace_root() or Path.cwd()
    advice_dir = ws_root / WORKSPACE_DIR / "advice"
    runs_dir = advice_dir / "runs"
    latest_yaml = advice_dir / "latest.yaml"
    return advice_dir, runs_dir, latest_yaml


def _score_snapshot(result: ScoreResult) -> dict[str, Any]:
    return {
        "overall": result.overall,
        "level": result.level,
        "profile": result.profile_name,
        "dimensions": [
            {
                "key": d.key,
                "label": d.label,
                "score": d.score,
                "weight": d.weight,
                "findings": list(d.findings),
            }
            for d in result.dimensions
        ],
    }


def _normalise_priority(value: Any) -> str:
    if isinstance(value, int):
        return "high" if value <= 1 else "medium" if value == 2 else "low"
    txt = str(value or "").strip().lower()
    if txt in {"high", "medium", "low"}:
        return txt
    return "medium"


def _priority_from_score(priority_score: float) -> str:
    if priority_score >= 7:
        return "high"
    if priority_score >= 4:
        return "medium"
    return "low"


def _attention_level(score: float) -> str:
    if score < 2.0:
        return "critical"
    if score < 3.0:
        return "high"
    if score < 4.0:
        return "medium"
    return "low"


def _domain_title(domain_id: str) -> str:
    return {
        "enterprise": "Enterprise",
        "data": "Data",
        "solution": "Solution",
        "governance_interop": "Governance and Interoperability",
    }.get(domain_id, domain_id.replace("_", " ").title())


def _artifact_counts(workspace: ArchitectureWorkspace) -> dict[str, int]:
    solution_components = sum(len(s.components) for s in workspace.solutions)
    adrs = sum(len(s.adrs) for s in workspace.solutions)
    return {
        "capabilities": len(workspace.enterprise.capabilities),
        "applications": len(workspace.enterprise.applications),
        "standards": len(workspace.enterprise.standards),
        "data_domains": len(workspace.data.domains),
        "data_products": len(workspace.data.products),
        "data_flows": len(workspace.data.flows),
        "solutions": len(workspace.solutions),
        "solution_components": solution_components,
        "adrs": adrs,
    }


def _legacy_scores(result: ScoreResult) -> dict[str, float]:
    defaults = {
        "capability_coverage": 0.0,
        "application_health": 0.0,
        "data_maturity": 0.0,
        "solution_completeness": 0.0,
        "operational_readiness": 0.0,
        "governance_coverage": 0.0,
    }
    for dim in result.dimensions:
        defaults[dim.key] = dim.score
    return defaults


def _mean(a: float, b: float) -> float:
    return (a + b) / 2.0


def _compute_domain_score(
    domain: dict[str, Any],
    result: ScoreResult,
    workspace: ArchitectureWorkspace,
) -> dict[str, Any]:
    legacy = _legacy_scores(result)
    counts = _artifact_counts(workspace)

    base = {
        "modularity_and_boundaries": _mean(
            legacy["capability_coverage"], legacy["solution_completeness"]
        ),
        "contract_quality": _mean(legacy["governance_coverage"], legacy["data_maturity"]),
        "data_design_and_ownership": legacy["data_maturity"],
        "operability_and_nfrs": _mean(
            legacy["operational_readiness"], legacy["application_health"]
        ),
        "governance_and_decision_traceability": legacy["governance_coverage"],
        "delivery_enablement": _mean(
            legacy["capability_coverage"], legacy["application_health"]
        ),
    }

    domain_id = domain["id"]
    if domain_id == "enterprise":
        base["modularity_and_boundaries"] += 0.35 if counts["capabilities"] >= 6 else -0.45
        base["delivery_enablement"] += 0.30 if counts["applications"] >= 5 else -0.35
        base["governance_and_decision_traceability"] += 0.25 if counts["standards"] >= 2 else -0.45
    elif domain_id == "data":
        base["data_design_and_ownership"] += 0.40 if counts["data_products"] >= 3 else -0.70
        base["contract_quality"] += 0.30 if counts["data_flows"] >= 1 else -0.60
        base["delivery_enablement"] += 0.20 if counts["data_domains"] >= 2 else -0.40
    elif domain_id == "solution":
        base["modularity_and_boundaries"] += 0.40 if counts["solutions"] >= 2 else -0.60
        base["governance_and_decision_traceability"] += 0.35 if counts["adrs"] >= 3 else -0.55
        base["operability_and_nfrs"] += 0.20 if counts["solution_components"] >= 5 else -0.45
    elif domain_id == "governance_interop":
        base["governance_and_decision_traceability"] += 0.45 if counts["standards"] >= 3 else -0.70
        base["contract_quality"] += 0.40 if counts["data_flows"] >= 2 else -0.50
        base["operability_and_nfrs"] += 0.25 if counts["applications"] >= 4 else -0.35

    score_rows: list[dict[str, Any]] = []
    weighted_sum = 0.0
    total_weight = 0.0
    for dim in _RUBRIC_DIMENSIONS:
        key = dim["key"]
        weight = _DIMENSION_WEIGHT_PROFILE[key]
        raw_score = _clamp(round(base[key], 2))
        anchor_key = int(max(0, min(5, round(raw_score))))
        score_rows.append(
            {
                "key": key,
                "label": dim["label"],
                "score": raw_score,
                "weight": weight,
                "anchor": _DIMENSION_ANCHORS[anchor_key],
            }
        )
        weighted_sum += raw_score * weight
        total_weight += weight

    weighted_score = round(weighted_sum / (total_weight or 1.0), 2)

    evidence_keys = domain["evidence_keys"]
    evidence = [counts.get(k, 0) for k in evidence_keys]
    non_zero = sum(1 for c in evidence if c > 0)
    density = sum(min(c, 5) for c in evidence) / (5 * len(evidence) or 1)
    confidence = round(_clamp(0.45 + 0.35 * density + 0.2 * (non_zero / (len(evidence) or 1)), 0.35, 0.95), 2)

    return {
        "domain": domain_id,
        "role_name": domain["name"],
        "focus": domain["focus"],
        "dimensions": score_rows,
        "weighted_score": weighted_score,
        "confidence": confidence,
        "attention_level": _attention_level(weighted_score),
    }


def _infer_gap_type(text: str, default: str = "capability_design_gap") -> str:
    t = text.lower()
    if any(x in t for x in ("boundary", "bounded context", "overlap")):
        return "boundary_ambiguity"
    if any(x in t for x in ("contract", "api", "event", "version", "integration")):
        return "integration_contract_drift"
    if any(x in t for x in ("tradeoff", "platform choice", "technology choice", "option")):
        return "major_architecture_tradeoff"
    if any(x in t for x in ("ownership", "lifecycle", "source of truth", "data quality")):
        return "data_ownership_lifecycle"
    if any(x in t for x in ("slo", "sla", "runbook", "operability", "observability", "nfr")):
        return "nfr_slo_uncertainty"
    if any(x in t for x in ("governance", "policy", "approval", "compliance", "traceability")):
        return "governance_gap"
    if any(x in t for x in ("capability", "design", "coverage", "decomposition")):
        return "capability_design_gap"
    return default


def _gap_default_dimension(gap_type: str) -> str:
    mapping = {
        "boundary_ambiguity": "modularity_and_boundaries",
        "integration_contract_drift": "contract_quality",
        "major_architecture_tradeoff": "modularity_and_boundaries",
        "capability_design_gap": "delivery_enablement",
        "data_ownership_lifecycle": "data_design_and_ownership",
        "nfr_slo_uncertainty": "operability_and_nfrs",
        "governance_gap": "governance_and_decision_traceability",
    }
    return mapping.get(gap_type, "delivery_enablement")


def _normalise_dimension(value: Any, fallback: str = "delivery_enablement") -> str:
    key = str(value or "").strip().lower()
    keys = {d["key"] for d in _RUBRIC_DIMENSIONS}
    if key in keys:
        return key
    legacy_map = {
        "capability_coverage": "modularity_and_boundaries",
        "application_health": "delivery_enablement",
        "data_maturity": "data_design_and_ownership",
        "solution_completeness": "modularity_and_boundaries",
        "operational_readiness": "operability_and_nfrs",
        "governance_coverage": "governance_and_decision_traceability",
    }
    if key in legacy_map:
        return legacy_map[key]
    return fallback


def _fallback_domain_payload(
    domain: dict[str, Any],
    score: dict[str, Any],
    result: ScoreResult,
    workspace: ArchitectureWorkspace,
) -> dict[str, Any]:
    sorted_dims = sorted(score["dimensions"], key=lambda d: d["score"])
    top_gaps = sorted_dims[:3]
    legacy_findings: list[str] = []
    for d in result.dimensions:
        legacy_findings.extend(d.findings[:1])

    maturity_gaps: list[dict[str, Any]] = []
    decisions_needed: list[dict[str, Any]] = []
    for idx, d in enumerate(top_gaps, start=1):
        gap_type = _infer_gap_type(d["label"], default="capability_design_gap")
        fallback_finding = legacy_findings[idx - 1] if idx - 1 < len(legacy_findings) else ""
        gap_text = (
            f"{d['label']} is at {d['score']:.2f}/5; {fallback_finding}"
            if fallback_finding
            else f"{d['label']} is at {d['score']:.2f}/5 and needs explicit architecture decisions."
        )
        maturity_gaps.append(
            {
                "id": f"{domain['id']}-g{idx}",
                "gap": gap_text,
                "gap_type": gap_type,
                "severity": "high" if d["score"] < 2.5 else "medium",
                "dimension": d["key"],
            }
        )
        decisions_needed.append(
            {
                "id": f"{domain['id']}-d{idx}",
                "decision": f"Decide the target-state improvement for {d['label']} in { _domain_title(domain['id']) }.",
                "priority": "high" if d["score"] < 2.5 else "medium",
                "dimension": d["key"],
                "status": "open",
                "gap_type": gap_type,
                "inputs_needed": [
                    "current-state constraints",
                    "target-state objective",
                    "owner and timeline",
                ],
                "expected_benefit": "Improves architecture consistency and delivery predictability.",
            }
        )

    deps = [
        f"{flow.source_domain} -> {flow.target_domain} ({flow.mechanism})"
        for flow in workspace.data.flows[:8]
    ]
    interop_risks = []
    if score["weighted_score"] < 3.0:
        interop_risks.append("critical: Cross-domain interoperability assumptions are under-specified.")
    if not deps:
        interop_risks.append("No explicit cross-domain flow map detected.")

    return {
        "role_id": domain["id"],
        "role_name": domain["name"],
        "domain": domain["id"],
        "domain_summary": (
            f"{domain['name']} task pack generated deterministically. "
            f"Weighted score {score['weighted_score']:.2f}/5 ({score['attention_level']})."
        ),
        "maturity_gaps": maturity_gaps,
        "decisions_needed": decisions_needed,
        "cross_domain_dependencies": deps[:6],
        "interop_risks": interop_risks,
    }


def _normalise_gap_item(item: Any, fallback_id: str) -> dict[str, Any] | None:
    if isinstance(item, dict):
        text = str(item.get("gap") or item.get("text") or "").strip()
        if not text:
            return None
        gap_type = _infer_gap_type(
            str(item.get("gap_type") or text),
            default=str(item.get("gap_type") or "capability_design_gap"),
        )
        return {
            "id": str(item.get("id") or fallback_id),
            "gap": text,
            "gap_type": gap_type,
            "severity": str(item.get("severity") or "medium").lower(),
            "dimension": _normalise_dimension(item.get("dimension"), _gap_default_dimension(gap_type)),
        }
    text = str(item).strip()
    if not text:
        return None
    gap_type = _infer_gap_type(text)
    return {
        "id": fallback_id,
        "gap": text,
        "gap_type": gap_type,
        "severity": "medium",
        "dimension": _gap_default_dimension(gap_type),
    }


def _normalise_decision_item(item: Any, fallback_id: str, fallback_gap_type: str) -> dict[str, Any] | None:
    if isinstance(item, dict):
        decision = str(item.get("decision") or item.get("text") or "").strip()
        if not decision:
            return None
        gap_type = _infer_gap_type(
            str(item.get("gap_type") or decision),
            default=str(item.get("gap_type") or fallback_gap_type),
        )
        inputs = item.get("inputs_needed") or []
        if isinstance(inputs, str):
            inputs = [inputs]
        inputs = [str(x).strip() for x in inputs if str(x).strip()]
        return {
            "id": str(item.get("id") or fallback_id),
            "decision": decision,
            "priority": _normalise_priority(item.get("priority")),
            "dimension": _normalise_dimension(item.get("dimension"), _gap_default_dimension(gap_type)),
            "status": str(item.get("status") or "open"),
            "gap_type": gap_type,
            "inputs_needed": inputs,
            "expected_benefit": str(item.get("expected_benefit") or "").strip(),
        }

    decision = str(item).strip()
    if not decision:
        return None
    gap_type = _infer_gap_type(decision, default=fallback_gap_type)
    return {
        "id": fallback_id,
        "decision": decision,
        "priority": "medium",
        "dimension": _gap_default_dimension(gap_type),
        "status": "open",
        "gap_type": gap_type,
        "inputs_needed": [],
        "expected_benefit": "",
    }


def _normalise_domain_payload(
    domain: dict[str, Any],
    data: dict[str, Any] | None,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(data, dict):
        return fallback

    domain_summary = str(data.get("domain_summary") or fallback["domain_summary"]).strip()

    raw_gaps = data.get("maturity_gaps") or []
    maturity_gaps: list[dict[str, Any]] = []
    for idx, gap in enumerate(raw_gaps, start=1):
        normalised = _normalise_gap_item(gap, fallback_id=f"{domain['id']}-g{idx}")
        if normalised:
            maturity_gaps.append(normalised)
    if not maturity_gaps:
        maturity_gaps = fallback["maturity_gaps"]

    raw_decisions = data.get("decisions_needed") or []
    default_gap_type = maturity_gaps[0]["gap_type"] if maturity_gaps else "capability_design_gap"
    decisions_needed: list[dict[str, Any]] = []
    for idx, decision in enumerate(raw_decisions, start=1):
        normalised = _normalise_decision_item(
            decision,
            fallback_id=f"{domain['id']}-d{idx}",
            fallback_gap_type=default_gap_type,
        )
        if normalised:
            decisions_needed.append(normalised)
    if not decisions_needed:
        decisions_needed = fallback["decisions_needed"]

    cross_domain_dependencies = [
        str(x).strip()
        for x in (data.get("cross_domain_dependencies") or [])
        if str(x).strip()
    ] or fallback["cross_domain_dependencies"]

    interop_risks = [
        str(x).strip()
        for x in (data.get("interop_risks") or [])
        if str(x).strip()
    ] or fallback["interop_risks"]

    return {
        "role_id": domain["id"],
        "role_name": domain["name"],
        "domain": domain["id"],
        "domain_summary": domain_summary,
        "maturity_gaps": maturity_gaps,
        "decisions_needed": decisions_needed,
        "cross_domain_dependencies": cross_domain_dependencies,
        "interop_risks": interop_risks,
    }


def _orchestrator_context(
    workspace: ArchitectureWorkspace,
    result: ScoreResult,
    domain_scores: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    counts = _artifact_counts(workspace)
    domains = []
    for domain in _DOMAINS:
        ds = domain_scores[domain["id"]]
        domains.append(
            {
                "domain": domain["id"],
                "role": domain["name"],
                "focus": domain["focus"],
                "weighted_score": ds["weighted_score"],
                "attention_level": ds["attention_level"],
                "confidence": ds["confidence"],
                "dimensions": ds["dimensions"],
            }
        )

    return {
        "workspace": workspace.manifest.name,
        "profile": result.profile_name,
        "counts": counts,
        "domains": domains,
    }


def _ask_orchestrator(
    agent: ArchitectureAgent,
    workspace: ArchitectureWorkspace,
    result: ScoreResult,
    domain_scores: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    context = _orchestrator_context(workspace, result, domain_scores)
    prompt = (
        "You are a principal architecture advisor orchestrator.\n"
        "Generate four domain task-pack outputs for enterprise, data, solution, and governance_interop.\n"
        "Use concept-level architecture language only. Do not use TM Forum identifiers or catalog names.\n\n"
        f"Context:\n{json.dumps(context, indent=2)}\n\n"
        "Return ONLY a JSON object with shape:\n"
        "{\n"
        '  "domains": {\n'
        '    "enterprise": {\n'
        '      "domain_summary": "...",\n'
        '      "maturity_gaps": [{"id":"...","gap":"...","gap_type":"boundary_ambiguity|integration_contract_drift|major_architecture_tradeoff|capability_design_gap|data_ownership_lifecycle|nfr_slo_uncertainty|governance_gap","severity":"critical|high|medium|low","dimension":"<rubric-dimension>"}],\n'
        '      "decisions_needed": [{"id":"...","decision":"...","priority":"high|medium|low","dimension":"<rubric-dimension>","status":"open|resolved","gap_type":"...","inputs_needed":["..."],"expected_benefit":"..."}],\n'
        '      "cross_domain_dependencies": ["..."],\n'
        '      "interop_risks": ["..."]\n'
        "    }, ...\n"
        "  }\n"
        "}\n"
        "Include 2-5 gaps and 2-6 decisions per domain."
    )
    raw = agent.ask(prompt)
    parsed = json.loads(_clean_json(raw))
    return parsed if isinstance(parsed, dict) else {}


def _has_unresolved_critical_risk(payload: dict[str, Any]) -> bool:
    for risk in payload.get("interop_risks", []):
        text = str(risk).lower()
        if "critical" in text and all(x not in text for x in ("resolved", "mitigated", "closed")):
            return True
    return False


def _needs_deep_dive(payload: dict[str, Any], domain_score: dict[str, Any]) -> bool:
    return (
        float(domain_score.get("weighted_score", 0.0)) < 3.0
        or float(domain_score.get("confidence", 0.0)) < 0.65
        or _has_unresolved_critical_risk(payload)
    )


def _ask_deep_dive(
    domain: dict[str, Any],
    agent: ArchitectureAgent,
    payload: dict[str, Any],
    domain_score: dict[str, Any],
) -> dict[str, Any]:
    prompt = (
        "Perform a deep-dive advisory refinement for one architecture domain.\n"
        f"Domain: {domain['id']} ({domain['name']})\n"
        "Use concept-level architecture language only.\n"
        "Focus on unresolved critical interoperability risks, missing decisions, and documentation clarity.\n"
        "Return ONLY JSON with keys: domain_summary, maturity_gaps, decisions_needed, cross_domain_dependencies, interop_risks.\n\n"
        f"Current domain score:\n{json.dumps(domain_score, indent=2)}\n\n"
        f"Current advisory payload:\n{json.dumps(payload, indent=2)}\n"
    )
    raw = agent.ask(prompt)
    parsed = json.loads(_clean_json(raw))
    if not isinstance(parsed, dict):
        raise ValueError("invalid deep-dive response")
    return parsed


def _merge_domain_payload(base: dict[str, Any], deep: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    summary = str(deep.get("domain_summary") or "").strip()
    if summary:
        merged["domain_summary"] = summary

    def _merge_list_dict(key: str, id_field: str, text_field: str) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {}
        for item in base.get(key, []):
            if not isinstance(item, dict):
                continue
            marker = str(item.get(id_field) or item.get(text_field) or "").strip().lower()
            if marker:
                by_key[marker] = dict(item)
        for item in deep.get(key, []):
            if not isinstance(item, dict):
                continue
            marker = str(item.get(id_field) or item.get(text_field) or "").strip().lower()
            if marker:
                by_key[marker] = dict(item)
        return list(by_key.values())

    def _merge_list_text(key: str) -> list[str]:
        seen: list[str] = []
        for seq in (base.get(key, []), deep.get(key, [])):
            for item in seq:
                txt = str(item).strip()
                if txt and txt not in seen:
                    seen.append(txt)
        return seen

    merged["maturity_gaps"] = _merge_list_dict("maturity_gaps", "id", "gap") or base.get("maturity_gaps", [])
    merged["decisions_needed"] = _merge_list_dict("decisions_needed", "id", "decision") or base.get("decisions_needed", [])
    merged["cross_domain_dependencies"] = _merge_list_text("cross_domain_dependencies")
    merged["interop_risks"] = _merge_list_text("interop_risks")
    return merged


def _infer_impact_from_dependencies(payload: dict[str, Any], text: str = "") -> int:
    deps = payload.get("cross_domain_dependencies") or []
    breadth = 0
    for dep in deps:
        dep_txt = str(dep)
        if "->" in dep_txt:
            parts = [p.strip() for p in dep_txt.split("->", 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                breadth += 1
        elif dep_txt:
            breadth += 1

    impact = 1
    if breadth >= 2:
        impact = 2
    if breadth >= 4:
        impact = 3

    lower = text.lower()
    if any(k in lower for k in ("cross-domain", "interoperability", "integration")):
        impact = min(3, impact + 1)
    return impact


def _priority_score(domain_score: float, impact: int, confidence: float) -> float:
    confidence_factor = max(0.7, confidence)
    return round((5 - domain_score) * impact * confidence_factor, 2)


def _subject_from_title(title: str) -> str:
    base = _slug(title) or "architecture-doc"
    if not base.endswith(".md"):
        base = f"{base}.md"
    return base


def _gap_fingerprint(domain: str, gap_type: str, text: str) -> str:
    payload = f"{domain}|{gap_type}|{text.strip().lower()}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


def _derive_recommended_docs(
    domain_outputs: dict[str, dict[str, Any]],
    domain_scores: dict[str, dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    decision_index: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        key = str(decision.get("decision", "")).strip().lower()
        if key:
            decision_index[key] = decision

    docs: dict[tuple[str, str, str], dict[str, Any]] = {}

    for domain_id, payload in domain_outputs.items():
        for gap in payload.get("maturity_gaps", []):
            if not isinstance(gap, dict):
                continue
            gap_text = str(gap.get("gap") or "").strip()
            if not gap_text:
                continue
            gap_type = str(gap.get("gap_type") or _infer_gap_type(gap_text))
            templates = _GAP_TYPE_TO_DOCS.get(gap_type) or _GAP_TYPE_TO_DOCS["capability_design_gap"]
            fingerprint = _gap_fingerprint(domain_id, gap_type, gap_text)

            linked_decision = None
            for d in payload.get("decisions_needed", []):
                if not isinstance(d, dict):
                    continue
                if str(d.get("gap_type") or "") == gap_type:
                    linked_decision = decision_index.get(str(d.get("decision", "")).strip().lower(), d)
                    break

            impact = _infer_impact_from_dependencies(payload, gap_text)
            ds = domain_scores.get(domain_id, {})
            p_score = _priority_score(
                float(ds.get("weighted_score", 0.0)),
                impact,
                float(ds.get("confidence", 0.0)),
            )
            priority = (
                str(linked_decision.get("priority"))
                if isinstance(linked_decision, dict) and linked_decision.get("priority")
                else _priority_from_score(p_score)
            )
            reason = gap_text
            if isinstance(linked_decision, dict) and linked_decision.get("decision"):
                reason = f"{gap_text} Decision needed: {linked_decision.get('decision')}"

            inputs_needed = []
            if isinstance(linked_decision, dict):
                inputs = linked_decision.get("inputs_needed") or []
                if isinstance(inputs, str):
                    inputs = [inputs]
                inputs_needed = [str(x).strip() for x in inputs if str(x).strip()]
            if not inputs_needed:
                inputs_needed = ["current state", "target state", "owner", "timeline"]

            expected_benefit = (
                str(linked_decision.get("expected_benefit") or "").strip()
                if isinstance(linked_decision, dict)
                else ""
            )

            for template in templates:
                title = f"{_domain_title(domain_id)} — {template['title']}"
                key = (template["doc_type"], domain_id, fingerprint)
                candidate = {
                    "doc_type": template["doc_type"],
                    "title": title,
                    "domain": domain_id,
                    "reason": reason,
                    "inputs_needed": inputs_needed,
                    "expected_benefit": expected_benefit or template["expected_benefit"],
                    "priority": _normalise_priority(priority),
                    "priority_score": p_score,
                    "draft_template_ref": template["draft_template_ref"],
                    "gap_fingerprint": fingerprint,
                    "subject": _subject_from_title(title),
                    "action": reason,
                    "dimension": str(gap.get("dimension") or _gap_default_dimension(gap_type)),
                    "score_impact": str(expected_benefit or template["expected_benefit"]),
                    "status": "open",
                }
                existing = docs.get(key)
                if not existing or candidate["priority_score"] > float(existing.get("priority_score", 0.0)):
                    docs[key] = candidate

    ordered = sorted(
        docs.values(),
        key=lambda d: (-float(d.get("priority_score", 0.0)), _PRIORITY_ORDER.get(str(d.get("priority", "medium")), 2), str(d.get("title", ""))),
    )
    return ordered[:40]


def _synthesize(
    domain_outputs: dict[str, dict[str, Any]],
    domain_scores: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    gaps: list[dict[str, Any]] = []
    deps: list[str] = []
    interop: list[str] = []
    summaries: list[str] = []

    decisions_by_text: dict[str, dict[str, Any]] = {}

    for domain_id, payload in domain_outputs.items():
        summaries.append(f"{payload.get('role_name', domain_id)}: {payload.get('domain_summary', '')}")

        for gap in payload.get("maturity_gaps", []):
            if isinstance(gap, dict):
                gaps.append({**gap, "domain": domain_id})

        for dep in payload.get("cross_domain_dependencies", []):
            text = str(dep).strip()
            if text and text not in deps:
                deps.append(text)

        for risk in payload.get("interop_risks", []):
            text = str(risk).strip()
            if text and text not in interop:
                interop.append(text)

        ds = domain_scores.get(domain_id, {})
        for decision in payload.get("decisions_needed", []):
            if not isinstance(decision, dict):
                continue
            text = str(decision.get("decision") or "").strip()
            if not text:
                continue
            impact = _infer_impact_from_dependencies(payload, text)
            p_score = _priority_score(
                float(ds.get("weighted_score", 0.0)),
                impact,
                float(ds.get("confidence", 0.0)),
            )
            normalized = {
                **decision,
                "domain": domain_id,
                "impact": impact,
                "priority_score": p_score,
                "priority": _normalise_priority(decision.get("priority") or _priority_from_score(p_score)),
                "status": str(decision.get("status") or "open"),
            }
            key = text.lower()
            existing = decisions_by_text.get(key)
            if not existing or float(normalized["priority_score"]) > float(existing.get("priority_score", 0.0)):
                decisions_by_text[key] = normalized

    decisions = sorted(
        decisions_by_text.values(),
        key=lambda d: (-float(d.get("priority_score", 0.0)), _PRIORITY_ORDER.get(str(d.get("priority", "medium")), 2), str(d.get("decision", ""))),
    )

    recommended_docs = _derive_recommended_docs(domain_outputs, domain_scores, decisions)

    critical_domains = [
        d for d in domain_scores.values() if str(d.get("attention_level", "")) == "critical"
    ]
    high_domains = [
        d for d in domain_scores.values() if str(d.get("attention_level", "")) in {"critical", "high"}
    ]
    unresolved = [d for d in decisions if str(d.get("status", "open")).lower() != "resolved"]

    domain_summary = (
        "Hybrid orchestrator synthesis completed across four architecture domains. "
        f"Critical domains: {len(critical_domains)}; high-attention domains: {len(high_domains)}; "
        f"open decisions: {len(unresolved)}."
    )

    def _benefit_bucket(gap_type: str) -> str:
        if gap_type in {"nfr_slo_uncertainty", "governance_gap", "data_ownership_lifecycle"}:
            return "risk_reduction"
        if gap_type in {"boundary_ambiguity", "integration_contract_drift"}:
            return "interop_gain"
        return "delivery_speed"

    benefit_scores = {"risk_reduction": 0.0, "delivery_speed": 0.0, "interop_gain": 0.0}
    for d in unresolved:
        bucket = _benefit_bucket(str(d.get("gap_type") or ""))
        benefit_scores[bucket] += float(d.get("priority_score", 0.0))

    organization_benefits = [
        {
            "category": "risk_reduction",
            "score": round(benefit_scores["risk_reduction"], 2),
            "summary": "Reduces operational and governance risk through explicit decisions and controls.",
        },
        {
            "category": "delivery_speed",
            "score": round(benefit_scores["delivery_speed"], 2),
            "summary": "Improves delivery flow via clearer boundaries, ownership, and design intent.",
        },
        {
            "category": "interop_gain",
            "score": round(benefit_scores["interop_gain"], 2),
            "summary": "Improves interoperability via stronger contract and cross-domain dependency clarity.",
        },
    ]

    return {
        "domain_summary": domain_summary,
        "domain_scores": [domain_scores[k] for k in sorted(domain_scores.keys())],
        "maturity_gaps": gaps[:40],
        "decisions_needed": decisions[:40],
        "recommended_docs": recommended_docs,
        "cross_domain_dependencies": deps[:30],
        "interop_risks": interop[:25],
        "organization_benefits": organization_benefits,
        "insight_summary": summaries,
    }


def render_advisory_markdown(run: dict[str, Any]) -> str:
    meta = run.get("meta", {})
    panel = run.get("panel", {})
    synthesis = panel.get("synthesis", {})
    lines: list[str] = []
    lines.append(f"# Hybrid Domain Advisory — {meta.get('workspace_name', 'workspace')}")
    lines.append("")
    lines.append(f"- Run: `{meta.get('run_id', '')}`")
    lines.append(f"- Generated: `{meta.get('generated_at', '')}`")
    lines.append(f"- Profile: `{meta.get('profile', '')}`")
    lines.append(f"- Provider: `{meta.get('provider', '')}` · model `{meta.get('model', '')}`")
    lines.append(f"- Degraded mode: `{'yes' if meta.get('degraded') else 'no'}`")
    lines.append("")
    lines.append("## Synthesis")
    lines.append("")
    lines.append(str(synthesis.get("domain_summary", "")).strip())

    lines.append("")
    lines.append("### Domain Attention")
    lines.append("")
    for ds in synthesis.get("domain_scores", []):
        lines.append(
            f"- **{_domain_title(str(ds.get('domain','')))}**: "
            f"{ds.get('weighted_score', 0):.2f}/5 · "
            f"attention `{ds.get('attention_level', 'n/a')}` · confidence `{ds.get('confidence', 0):.2f}`"
        )

    lines.append("")
    lines.append("### Decisions Needed")
    lines.append("")
    decisions = synthesis.get("decisions_needed", [])
    if not decisions:
        lines.append("- None")
    else:
        for d in decisions[:20]:
            lines.append(
                f"- [{d.get('priority','medium')}] ({d.get('domain','')}) "
                f"{d.get('decision','')} `score={d.get('priority_score', 0)}`"
            )

    lines.append("")
    lines.append("### Recommended Documents")
    lines.append("")
    docs = synthesis.get("recommended_docs", [])
    if not docs:
        lines.append("- None")
    else:
        for d in docs[:30]:
            lines.append(
                f"- [{d.get('priority','medium')}] ({d.get('domain','')}) "
                f"{d.get('doc_type','Doc')}: {d.get('title','')}"
            )

    lines.append("")
    lines.append("### Organization Benefits")
    lines.append("")
    for b in synthesis.get("organization_benefits", []):
        lines.append(
            f"- {b.get('category', 'benefit')}: {b.get('score', 0)} — {b.get('summary', '')}"
        )

    lines.append("")
    lines.append("## Domain Insights")
    lines.append("")
    for sub in panel.get("domains", []):
        lines.append(f"### {sub.get('role_name','Domain')}")
        lines.append("")
        lines.append(sub.get("domain_summary", ""))
        gaps = sub.get("maturity_gaps", [])
        if gaps:
            lines.append("")
            lines.append("Key gaps:")
            for g in gaps[:5]:
                if isinstance(g, dict):
                    lines.append(f"- {g.get('gap','')}")
                else:
                    lines.append(f"- {g}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _load_backlog_machine(backlog_md: Path) -> list[dict[str, Any]]:
    if not backlog_md.exists():
        return []
    text = backlog_md.read_text(encoding="utf-8")
    start = "<!-- strata:decision-backlog:start -->"
    end = "<!-- strata:decision-backlog:end -->"
    if start not in text or end not in text:
        return []
    fragment = text.split(start, 1)[1].split(end, 1)[0]
    yaml_match = re.search(r"```yaml\n(.*?)\n```", fragment, re.DOTALL)
    if not yaml_match:
        return []
    try:
        data = yaml.safe_load(yaml_match.group(1)) or []
    except Exception:
        return []
    return [x for x in data if isinstance(x, dict)]


def _write_backlog(advice_dir: Path, decisions: list[dict[str, Any]]) -> None:
    backlog_md = advice_dir / "decision-backlog.md"
    machine = yaml.dump(decisions, sort_keys=False).strip()
    lines = [
        "# Decision Backlog",
        "",
        "<!-- strata:decision-backlog:start -->",
        "```yaml",
        machine,
        "```",
        "<!-- strata:decision-backlog:end -->",
        "",
        "## Open Decisions",
        "",
    ]
    open_items = [d for d in decisions if str(d.get("status", "open")).lower() != "resolved"]
    if not open_items:
        lines.append("- None")
    else:
        for d in open_items:
            lines.append(f"- [ ] [{d.get('priority','medium')}] {d.get('decision','')}")
    lines.append("")
    lines.append("## Resolved Decisions")
    lines.append("")
    resolved = [d for d in decisions if str(d.get("status", "")).lower() == "resolved"]
    if not resolved:
        lines.append("- None")
    else:
        for d in resolved:
            lines.append(f"- [x] {d.get('decision','')}")
    backlog_md.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _merge_backlog(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in previous:
        key = str(item.get("decision", "")).strip().lower()
        if key:
            merged[key] = dict(item)
    for item in current:
        key = str(item.get("decision", "")).strip().lower()
        if not key:
            continue
        if key in merged and str(merged[key].get("status", "")).lower() == "resolved":
            continue
        merged[key] = dict(item)
    values = list(merged.values())
    values.sort(
        key=lambda d: (
            -float(d.get("priority_score", 0.0)),
            _PRIORITY_ORDER.get(str(d.get("priority", "medium")), 2),
            str(d.get("decision", "")),
        )
    )
    return values


def persist_advisory_run(run: dict[str, Any], root: Path | None = None) -> dict[str, str]:
    advice_dir, runs_dir, latest_yaml = _advice_paths(root)
    advice_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    run_id = str(run.get("meta", {}).get("run_id", "unknown"))
    profile = str(run.get("meta", {}).get("profile", "default"))
    stem = f"{run_id}-{_slug(profile) or 'default'}"
    run_yaml = runs_dir / f"{stem}.yaml"
    run_md = runs_dir / f"{stem}.md"
    latest_md = advice_dir / "latest.md"

    yaml_text = yaml.dump(run, sort_keys=False)
    md_text = render_advisory_markdown(run)
    run_yaml.write_text(yaml_text, encoding="utf-8")
    run_md.write_text(md_text, encoding="utf-8")
    latest_yaml.write_text(yaml_text, encoding="utf-8")
    latest_md.write_text(md_text, encoding="utf-8")

    prev = _load_backlog_machine(advice_dir / "decision-backlog.md")
    current = run.get("panel", {}).get("synthesis", {}).get("decisions_needed", [])
    merged = _merge_backlog(prev, [x for x in current if isinstance(x, dict)])
    _write_backlog(advice_dir, merged)

    return {
        "run_yaml": str(run_yaml),
        "run_md": str(run_md),
        "latest_yaml": str(latest_yaml),
        "latest_md": str(latest_md),
        "decision_backlog": str(advice_dir / "decision-backlog.md"),
    }


def load_latest_advisory(root: Path | None = None) -> dict[str, Any] | None:
    _, _, latest_yaml = _advice_paths(root)
    if not latest_yaml.exists():
        return None
    try:
        data = yaml.safe_load(latest_yaml.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def advisory_to_todo_items(run: dict[str, Any]) -> list[TodoItem]:
    docs = run.get("panel", {}).get("synthesis", {}).get("recommended_docs", [])
    items: list[TodoItem] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        subject = str(doc.get("subject") or _subject_from_title(str(doc.get("title") or ""))).strip()
        action = str(doc.get("action") or doc.get("reason") or "").strip()
        if not subject:
            continue
        prio = _PRIORITY_ORDER.get(str(doc.get("priority", "medium")).lower(), 2)
        items.append(
            TodoItem(
                priority=prio,
                category="add" if str(doc.get("status", "open")).lower() != "resolved" else "improve",
                doc_type=str(doc.get("doc_type", "ADR")),
                subject=subject,
                action=action or f"Document {subject}",
                dimension=str(doc.get("dimension") or doc.get("domain") or ""),
                score_impact=str(doc.get("score_impact") or doc.get("expected_benefit") or ""),
            )
        )
    return items


def run_advisory_cycle(
    workspace: ArchitectureWorkspace,
    profile: str = "oda",
    provider: str = "auto",
    root: Path | None = None,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        result = score_workspace(workspace, profile=profile)
        generated_at = datetime.now(timezone.utc).isoformat()

        domain_scores = {
            domain["id"]: _compute_domain_score(domain, result, workspace)
            for domain in _DOMAINS
        }

        agent = ArchitectureAgent(provider=provider)
        effective_provider = agent._effective_provider()  # noqa: SLF001
        model_key = _MODEL_KEY_BY_PROVIDER.get(effective_provider, "")
        active_model = agent._config.get(model_key, "") if model_key else ""  # noqa: SLF001

        _emit_progress(
            progress_cb,
            run_id,
            "preflight",
            state="running",
            message="Checking provider availability and preparing domain scoring context.",
            extra={
                "provider": effective_provider,
                "model": active_model,
                "profile": profile,
            },
        )

        degraded = False
        provider_message = ""
        ok, provider_message = agent.check_available()
        if not ok:
            degraded = True

        _emit_progress(
            progress_cb,
            run_id,
            "preflight",
            state="degraded" if degraded else "running",
            message=provider_message,
            extra={"provider": effective_provider, "model": active_model},
        )

        domain_outputs: dict[str, dict[str, Any]] = {}
        fallbacks = {
            domain["id"]: _fallback_domain_payload(domain, domain_scores[domain["id"]], result, workspace)
            for domain in _DOMAINS
        }

        if degraded:
            for domain in _DOMAINS:
                domain_outputs[domain["id"]] = fallbacks[domain["id"]]
                _emit_progress(
                    progress_cb,
                    run_id,
                    f"domain:{domain['id']}:normalise",
                    state="degraded",
                    message="Using deterministic fallback payload (provider unavailable).",
                    domain=domain["id"],
                )
        else:
            try:
                _emit_progress(
                    progress_cb,
                    run_id,
                    "orchestrator",
                    state="running",
                    message="Generating domain advisory payloads.",
                )
                orchestrated = _ask_orchestrator(agent, workspace, result, domain_scores)
                domain_payloads = orchestrated.get("domains") if isinstance(orchestrated, dict) else None
                if not isinstance(domain_payloads, dict):
                    raise ValueError("orchestrator missing domains")

                for domain in _DOMAINS:
                    raw_payload = domain_payloads.get(domain["id"])
                    normalised = _normalise_domain_payload(domain, raw_payload, fallbacks[domain["id"]])
                    _emit_progress(
                        progress_cb,
                        run_id,
                        f"domain:{domain['id']}:normalise",
                        state="running",
                        message="Domain payload normalized.",
                        domain=domain["id"],
                    )
                    if _needs_deep_dive(normalised, domain_scores[domain["id"]]):
                        _emit_progress(
                            progress_cb,
                            run_id,
                            f"domain:{domain['id']}:deep_dive",
                            state="running",
                            message="Deep-dive triggered for low score, low confidence, or critical risk.",
                            domain=domain["id"],
                        )
                        try:
                            deep_raw = _ask_deep_dive(domain, agent, normalised, domain_scores[domain["id"]])
                            deep_payload = _normalise_domain_payload(domain, deep_raw, normalised)
                            normalised = _merge_domain_payload(normalised, deep_payload)
                        except (AgentError, json.JSONDecodeError, KeyError, ValueError, TypeError):
                            pass
                    domain_outputs[domain["id"]] = normalised
            except (AgentError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                degraded = True
                _emit_progress(
                    progress_cb,
                    run_id,
                    "orchestrator",
                    state="failed",
                    message=f"Orchestrator failed; falling back to deterministic mode: {exc}",
                )
                for domain in _DOMAINS:
                    domain_outputs[domain["id"]] = fallbacks[domain["id"]]
                    _emit_progress(
                        progress_cb,
                        run_id,
                        f"domain:{domain['id']}:normalise",
                        state="degraded",
                        message="Fallback payload applied after orchestrator failure.",
                        domain=domain["id"],
                    )

        _emit_progress(
            progress_cb,
            run_id,
            "synthesis",
            state="running",
            message="Synthesizing cross-domain decisions and recommendations.",
        )
        synthesis = _synthesize(domain_outputs, domain_scores)
        domains_ordered = [domain_outputs[d["id"]] for d in _DOMAINS]

        run = {
            "meta": {
                "run_id": run_id,
                "generated_at": generated_at,
                "profile": profile,
                "provider": effective_provider,
                "model": active_model,
                "degraded": degraded,
                "provider_status": provider_message,
                "workspace_name": workspace.manifest.name,
                "workspace_fingerprint": _workspace_fingerprint(workspace),
                "domain_count": len(_DOMAINS),
                "subagent_count": len(_DOMAINS),
                "operating_mode": "hybrid_orchestrator",
            },
            "scores": _score_snapshot(result),
            "panel": {
                "domains": domains_ordered,
                "subagents": domains_ordered,
                "synthesis": synthesis,
            },
        }

        _emit_progress(
            progress_cb,
            run_id,
            "persist",
            state="running",
            message="Writing advisory artifacts (runs/latest/backlog).",
        )
        paths = persist_advisory_run(run, root=root)
        run["meta"]["paths"] = paths

        _emit_progress(
            progress_cb,
            run_id,
            "complete",
            state="degraded" if degraded else "ok",
            message="Advisory run complete.",
            extra={"paths": paths},
        )
        return run
    except Exception as exc:
        _emit_progress(
            progress_cb,
            run_id,
            "failed",
            state="failed",
            message=f"Advisory run failed: {exc}",
        )
        raise
