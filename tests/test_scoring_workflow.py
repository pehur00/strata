from __future__ import annotations

from strata.models import ArchitectureWorkspace, WorkspaceManifest
from strata.scoring import (
    RoadmapPhase,
    build_fallback_doc_recommendations,
    build_workflow_steps,
    score_workspace,
)


def test_build_workflow_steps_uses_governance_question_template() -> None:
    ws = ArchitectureWorkspace(manifest=WorkspaceManifest(name="WorkflowTest"))
    result = score_workspace(ws, profile="default")
    steps = build_workflow_steps(result)

    by_key = {s.phase.dimension_key: s for s in steps}
    assert "governance_coverage" in by_key
    assert "Tech Radar" in by_key["governance_coverage"].question


def test_fallback_doc_recommendations_handle_adr_numbers_and_collisions() -> None:
    phase = RoadmapPhase(
        phase="1 — Quick Win",
        horizon="< 2 weeks",
        dimension_key="solution_completeness",
        dimension_label="Solution Completeness",
        current_score=1.4,
        score_delta="+0.8–1.5",
        priority="🔴 High",
        priority_level=1,
        action="No ADRs — architecture decisions should be recorded",
    )

    docs = build_fallback_doc_recommendations(
        phase,
        next_adr_number=9,
        existing_documents=[
            "ADR-009-solution-architecture-baseline.md",
            "hld-solution-completeness-target-architecture.md",
        ],
    )

    assert len(docs) == 2
    assert docs[0].doc_type == "ADR"
    assert docs[0].filename.startswith("ADR-010-")
    assert docs[1].doc_type == "HLD"
    assert docs[1].filename == "hld-solution-completeness-target-architecture-v2.md"


def test_fallback_doc_recommendations_for_unknown_dimension() -> None:
    phase = RoadmapPhase(
        phase="2 — Short-term",
        horizon="1–3 months",
        dimension_key="custom_dimension",
        dimension_label="Custom Dimension",
        current_score=2.3,
        score_delta="+0.5–1.0",
        priority="🟡 Medium",
        priority_level=2,
        action="Custom finding",
    )

    docs = build_fallback_doc_recommendations(phase, next_adr_number=3)

    assert len(docs) == 2
    assert docs[0].doc_type == "ADR"
    assert docs[0].filename.startswith("ADR-003-")
    assert docs[1].doc_type == "HLD"
