"""Architecture maturity scoring — abstract dimensions with pluggable profiles.

Six abstract scoring dimensions measure an organisation's architecture maturity
on a 0.0–5.0 scale.  **Profiles** (e.g. *telecom*, *cloud-native*, *data-mesh-org*)
layer domain-specific weights and level labels on top of the same dimensions,
making the engine extensible without changing scoring logic.

Usage::

    from strata.scoring import score_workspace, list_profiles

    result = score_workspace(workspace, profile="telecom")
    print(result.overall, result.level)
    for d in result.dimensions:
        print(d.key, d.score, d.findings)
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Protocol, runtime_checkable

from .models import ArchitectureWorkspace, StagedItem


# ── Score data classes ─────────────────────────────────────────────────────────

@dataclass
class DimensionScore:
    """One scored dimension."""
    key: str
    label: str
    score: float              # 0.0 – 5.0
    max_score: float = 5.0
    weight: float = 1.0       # profile-assigned weight
    findings: list[str] = field(default_factory=list)


@dataclass
class ScoreResult:
    """Overall scoring result for a workspace."""
    profile_name: str
    profile_description: str
    dimensions: list[DimensionScore]
    overall: float = 0.0      # weighted average
    level: str = ""           # human-readable maturity level
    level_labels: dict[str, tuple[float, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        total_w = sum(d.weight for d in self.dimensions) or 1
        self.overall = round(
            sum(d.score * d.weight for d in self.dimensions) / total_w, 2
        )
        for label, (lo, hi) in self.level_labels.items():
            if lo <= self.overall < hi:
                self.level = label
                break
        if not self.level and self.level_labels:
            self.level = list(self.level_labels.keys())[-1]


# ── Profile protocol ──────────────────────────────────────────────────────────

@runtime_checkable
class FrameworkProfile(Protocol):
    """Any scoring profile must satisfy this interface."""
    name: str
    description: str

    def score(self, ws: ArchitectureWorkspace, staging: list[StagedItem] | None = None) -> ScoreResult:
        ...


# ── Dimension scorers (pure functions) ─────────────────────────────────────────

def _score_capability_coverage(ws: ArchitectureWorkspace) -> DimensionScore:
    """How well business capabilities are defined and structured."""
    caps = ws.enterprise.capabilities
    findings: list[str] = []
    n = len(caps)

    if n == 0:
        return DimensionScore(
            key="capability_coverage", label="Capability Coverage",
            score=0.0, findings=["No business capabilities defined"],
        )

    score = 0.0

    # Breadth: number of capabilities
    if n >= 20:
        score += 1.5
    elif n >= 10:
        score += 1.0
    elif n >= 3:
        score += 0.5
        findings.append(f"Only {n} capabilities — consider expanding coverage")
    else:
        findings.append(f"Only {n} capabilities — very limited coverage")

    # Depth: domain diversity
    domains = set(c.domain for c in caps)
    if len(domains) >= 5:
        score += 1.0
    elif len(domains) >= 3:
        score += 0.5
        findings.append(f"{len(domains)} domains — consider broader domain coverage")
    else:
        findings.append(f"Only {len(domains)} domain(s) covered")

    # Maturity spread
    mature = sum(1 for c in caps if c.maturity in ("managed", "optimizing"))
    if mature / n >= 0.5:
        score += 1.0
    elif mature / n >= 0.25:
        score += 0.5
        findings.append(f"{mature}/{n} capabilities at managed+ maturity")
    else:
        findings.append(f"Low maturity — only {mature}/{n} at managed+")

    # Level balance (strategic + core + supporting)
    strategic = sum(1 for c in caps if c.level == "strategic")
    core = sum(1 for c in caps if c.level == "core")
    if strategic > 0 and core > 0:
        score += 0.5
    else:
        findings.append("Missing strategic or core-level capabilities")

    # Ownership
    owned = sum(1 for c in caps if c.owner)
    if owned == n:
        score += 1.0
    elif owned / n >= 0.5:
        score += 0.5
        findings.append(f"{n - owned} capabilities without an owner")
    else:
        findings.append(f"Most capabilities lack owners ({n - owned}/{n})")

    return DimensionScore(
        key="capability_coverage", label="Capability Coverage",
        score=min(score, 5.0), findings=findings,
    )


def _score_application_health(ws: ArchitectureWorkspace) -> DimensionScore:
    """Application portfolio health and lifecycle management."""
    apps = ws.enterprise.applications
    findings: list[str] = []
    n = len(apps)

    if n == 0:
        return DimensionScore(
            key="application_health", label="Application Health",
            score=0.0, findings=["No applications registered"],
        )

    score = 0.0

    # Portfolio size
    if n >= 10:
        score += 1.0
    elif n >= 5:
        score += 0.5
    else:
        findings.append(f"Small portfolio ({n} apps)")

    # Active vs retiring
    active = sum(1 for a in apps if a.status == "active")
    retiring = sum(1 for a in apps if a.status == "retiring")
    if active / n >= 0.7:
        score += 1.0
    elif active / n >= 0.5:
        score += 0.5
        findings.append(f"Only {active}/{n} apps are active")
    else:
        findings.append(f"Low active rate: {active}/{n}")
    if retiring > 0:
        score += 0.25  # good — lifecycle is managed
        findings.append(f"{retiring} apps marked for retirement (good hygiene)")

    # Capability linkage
    linked = sum(1 for a in apps if a.capability_ids)
    if linked / n >= 0.7:
        score += 1.0
    elif linked / n >= 0.3:
        score += 0.5
        findings.append(f"{n - linked} apps not linked to any capability")
    else:
        findings.append(f"Most apps lack capability linkage ({n - linked}/{n})")

    # Criticality awareness
    critical = sum(1 for a in apps if a.criticality in ("high", "critical"))
    if critical > 0:
        score += 0.5
        if critical / n > 0.5:
            findings.append(f"High concentration of critical apps ({critical}/{n})")
    else:
        findings.append("No apps flagged as high/critical — review criticality")

    # Hosting diversity / modernisation
    cloud_native = sum(1 for a in apps if a.hosting in ("kubernetes", "serverless"))
    if cloud_native / n >= 0.5:
        score += 0.75
    elif cloud_native / n >= 0.25:
        score += 0.25
        findings.append("Limited cloud-native adoption in hosting")
    else:
        findings.append("Most apps not on modern hosting")

    return DimensionScore(
        key="application_health", label="Application Health",
        score=min(score, 5.0), findings=findings,
    )


def _score_data_maturity(ws: ArchitectureWorkspace) -> DimensionScore:
    """Data architecture maturity — domains, products, flows, SLAs."""
    da = ws.data
    findings: list[str] = []

    if not da.domains and not da.products and not da.flows:
        return DimensionScore(
            key="data_maturity", label="Data Maturity",
            score=0.0, findings=["No data architecture defined"],
        )

    score = 0.0

    # Domains
    nd = len(da.domains)
    if nd >= 5:
        score += 1.0
    elif nd >= 2:
        score += 0.5
        findings.append(f"Only {nd} data domains")
    else:
        findings.append(f"Minimal domain coverage ({nd})")

    # Products (data mesh readiness)
    np_ = len(da.products)
    if np_ >= 10:
        score += 1.0
    elif np_ >= 3:
        score += 0.5
        findings.append(f"{np_} data products — room for growth")
    elif np_ > 0:
        score += 0.25
        findings.append(f"Only {np_} data product(s)")
    else:
        findings.append("No data products — consider data mesh adoption")

    # SLA tiers
    if da.products:
        high_sla = sum(1 for p in da.products if p.sla_tier in ("gold", "platinum"))
        if high_sla / len(da.products) >= 0.3:
            score += 0.75
        elif high_sla > 0:
            score += 0.25
            findings.append("Few data products at gold/platinum SLA")
        else:
            findings.append("No data products at gold+ SLA tier")

    # Flows
    nf = len(da.flows)
    if nf >= 10:
        score += 1.0
    elif nf >= 3:
        score += 0.5
        findings.append(f"{nf} data flows — consider mapping more")
    elif nf > 0:
        score += 0.25
    else:
        findings.append("No data flows defined")

    # Flow coverage — how many domains are connected
    if da.flows and da.domains:
        connected = set()
        for f in da.flows:
            connected.add(f.source_domain)
            connected.add(f.target_domain)
        ratio = len(connected) / len(da.domains)
        if ratio >= 0.8:
            score += 0.75
        elif ratio >= 0.5:
            score += 0.25
            findings.append(f"Only {len(connected)}/{nd} domains connected via flows")
        else:
            findings.append(f"Poor flow coverage: {len(connected)}/{nd} domains")

    return DimensionScore(
        key="data_maturity", label="Data Maturity",
        score=min(score, 5.0), findings=findings,
    )


def _score_solution_completeness(ws: ArchitectureWorkspace) -> DimensionScore:
    """Solution architecture completeness — designs, components, ADRs."""
    sols = ws.solutions
    findings: list[str] = []

    if not sols:
        return DimensionScore(
            key="solution_completeness", label="Solution Completeness",
            score=0.0, findings=["No solution designs defined"],
        )

    score = 0.0
    n = len(sols)

    # Count
    if n >= 5:
        score += 1.0
    elif n >= 2:
        score += 0.5
    else:
        findings.append(f"Only {n} solution(s)")

    # Status maturity
    approved = sum(1 for s in sols if s.status in ("approved", "implemented"))
    if approved / n >= 0.5:
        score += 1.0
    elif approved / n >= 0.25:
        score += 0.5
        findings.append(f"Only {approved}/{n} solutions approved/implemented")
    else:
        findings.append(f"Low approval rate ({approved}/{n})")

    # Components
    total_comps = sum(len(s.components) for s in sols)
    if total_comps >= n * 3:
        score += 1.0
    elif total_comps >= n:
        score += 0.5
        findings.append(f"Avg {total_comps / n:.1f} components/solution — add detail")
    else:
        findings.append(f"Very few components ({total_comps} across {n} solutions)")

    # ADRs (decision documentation)
    total_adrs = sum(len(s.adrs) for s in sols)
    if total_adrs >= n:
        score += 1.0
    elif total_adrs > 0:
        score += 0.5
        findings.append(f"{total_adrs} ADR(s) across {n} solutions — document more decisions")
    else:
        findings.append("No ADRs — architecture decisions should be recorded")

    # Pattern diversity
    patterns = set(s.pattern for s in sols)
    if len(patterns) >= 3:
        score += 0.5
    elif len(patterns) >= 2:
        score += 0.25
    else:
        findings.append("All solutions use one pattern — consider diversity")

    return DimensionScore(
        key="solution_completeness", label="Solution Completeness",
        score=min(score, 5.0), findings=findings,
    )


def _score_operational_readiness(ws: ArchitectureWorkspace) -> DimensionScore:
    """Operational readiness — hosting modernisation, deployment targets, automation signals."""
    findings: list[str] = []
    score = 0.0

    apps = ws.enterprise.applications
    sols = ws.solutions
    stds = ws.enterprise.standards

    # Hosting modernisation across apps
    if apps:
        modern = sum(1 for a in apps if a.hosting in ("kubernetes", "serverless"))
        ratio = modern / len(apps)
        if ratio >= 0.7:
            score += 1.5
        elif ratio >= 0.4:
            score += 0.75
            findings.append(f"{modern}/{len(apps)} apps on modern hosting")
        else:
            findings.append(f"Low modern-hosting adoption ({modern}/{len(apps)})")
    else:
        findings.append("No apps — cannot assess operational hosting")

    # Deployment target consistency across solutions
    if sols:
        targets = set(s.deployment_target for s in sols)
        if len(targets) <= 2:
            score += 1.0
        else:
            score += 0.5
            findings.append(f"Solutions spread across {len(targets)} deployment targets")
    else:
        findings.append("No solutions to assess deployment targets")

    # Tech radar health — adopt vs hold
    if stds:
        adopted = sum(1 for s in stds if s.status == "adopt")
        held = sum(1 for s in stds if s.status == "hold")
        if adopted / len(stds) >= 0.5:
            score += 1.0
        elif adopted > held:
            score += 0.5
            findings.append(f"Tech radar: {adopted} adopt vs {held} hold")
        else:
            findings.append(f"More tech on hold ({held}) than adopted ({adopted})")
    else:
        findings.append("No tech standards defined")

    # Streaming / event-driven readiness
    streaming_flows = sum(1 for f in ws.data.flows if f.mechanism in ("streaming", "cdc"))
    event_sols = sum(1 for s in sols if s.pattern == "event-driven")
    if streaming_flows >= 3 or event_sols >= 1:
        score += 1.0
    elif streaming_flows > 0:
        score += 0.5
        findings.append("Limited event-driven / streaming adoption")
    else:
        findings.append("No streaming/event-driven capabilities detected")

    return DimensionScore(
        key="operational_readiness", label="Operational Readiness",
        score=min(score, 5.0), findings=findings,
    )


def _score_governance_coverage(ws: ArchitectureWorkspace) -> DimensionScore:
    """Governance — ownership, documentation, decision records, standards compliance."""
    findings: list[str] = []
    score = 0.0

    caps = ws.enterprise.capabilities
    apps = ws.enterprise.applications
    stds = ws.enterprise.standards
    sols = ws.solutions

    # Ownership across capabilities
    if caps:
        owned = sum(1 for c in caps if c.owner)
        ratio = owned / len(caps)
        if ratio >= 0.9:
            score += 1.0
        elif ratio >= 0.5:
            score += 0.5
            findings.append(f"{len(caps) - owned} capabilities without owners")
        else:
            findings.append(f"Poor ownership: {owned}/{len(caps)} capabilities have owners")
    else:
        findings.append("No capabilities — governance baseline missing")

    # Ownership across data products
    if ws.data.products:
        owned_p = sum(1 for p in ws.data.products if p.owner_team)
        ratio_p = owned_p / len(ws.data.products)
        if ratio_p >= 0.9:
            score += 0.75
        elif ratio_p >= 0.5:
            score += 0.25
            findings.append(f"Some data products lack ownership ({len(ws.data.products) - owned_p})")
        else:
            findings.append("Most data products lack team ownership")
    else:
        if ws.data.domains:
            findings.append("Data domains exist but no data products — governance gap")

    # ADR coverage across solutions
    if sols:
        with_adrs = sum(1 for s in sols if s.adrs)
        if with_adrs / len(sols) >= 0.5:
            score += 1.0
        elif with_adrs > 0:
            score += 0.5
            findings.append(f"Only {with_adrs}/{len(sols)} solutions have ADRs")
        else:
            findings.append("No ADRs recorded — decisions are undocumented")
    else:
        findings.append("No solutions to assess ADR coverage")

    # Tech standards governance
    if stds:
        if len(stds) >= 10:
            score += 1.0
        elif len(stds) >= 5:
            score += 0.5
        else:
            findings.append(f"Only {len(stds)} tech standards — limited radar coverage")
    else:
        findings.append("No tech standards — technology choices ungoverned")

    # Cross-reference integrity (capabilities linked to apps)
    if caps and apps:
        cap_ids = {c.id for c in caps}
        linked = sum(1 for a in apps if any(cid in cap_ids for cid in a.capability_ids))
        if apps and linked / len(apps) >= 0.5:
            score += 0.75
        elif linked > 0:
            score += 0.25
            findings.append("Few apps linked to capabilities")
        else:
            findings.append("Apps and capabilities are disconnected")

    return DimensionScore(
        key="governance_coverage", label="Governance & Compliance",
        score=min(score, 5.0), findings=findings,
    )


# ── All dimension scorers ─────────────────────────────────────────────────────

_ALL_SCORERS = [
    _score_capability_coverage,
    _score_application_health,
    _score_data_maturity,
    _score_solution_completeness,
    _score_operational_readiness,
    _score_governance_coverage,
]


# ── Built-in profiles ─────────────────────────────────────────────────────────

class _TelecomProfile:
    """Telecom industry profile — emphasises operational readiness, data maturity
    and governance in line with modern digital platform architectures."""

    name = "telecom"
    description = (
        "Telecom / digital platform operator — weighted towards "
        "operational readiness, data maturity, and governance"
    )

    _WEIGHTS: dict[str, float] = {
        "capability_coverage":    1.0,
        "application_health":     1.2,
        "data_maturity":          1.5,
        "solution_completeness":  1.0,
        "operational_readiness":  1.5,
        "governance_coverage":    1.3,
    }

    _LEVELS: dict[str, tuple[float, float]] = {
        "Ad-hoc":        (0.0, 1.0),
        "Emerging":      (1.0, 2.0),
        "Defined":       (2.0, 3.0),
        "Managed":       (3.0, 4.0),
        "Optimising":    (4.0, 5.01),
    }

    def score(
        self,
        ws: ArchitectureWorkspace,
        staging: list[StagedItem] | None = None,
    ) -> ScoreResult:
        dims = [scorer(ws) for scorer in _ALL_SCORERS]
        for d in dims:
            d.weight = self._WEIGHTS.get(d.key, 1.0)
        return ScoreResult(
            profile_name=self.name,
            profile_description=self.description,
            dimensions=dims,
            level_labels=dict(self._LEVELS),
        )


class _ODAProfile:
    """TM Forum Open Digital Architecture (ODA) profile — emphasises capability coverage,
    solution completeness, and governance in line with ODA component-based architecture."""

    name = "oda"
    description = (
        "TM Forum ODA — weighted towards capability coverage, solution completeness, "
        "and governance for ODA component-based digital architectures"
    )

    _WEIGHTS: dict[str, float] = {
        "capability_coverage":    1.5,
        "application_health":     1.0,
        "data_maturity":          1.2,
        "solution_completeness":  1.5,
        "operational_readiness":  1.0,
        "governance_coverage":    1.3,
    }

    _LEVELS: dict[str, tuple[float, float]] = {
        "Ad-hoc":        (0.0, 1.0),
        "Emerging":      (1.0, 2.0),
        "Defined":       (2.0, 3.0),
        "Managed":       (3.0, 4.0),
        "Optimising":    (4.0, 5.01),
    }

    def score(
        self,
        ws: ArchitectureWorkspace,
        staging: list[StagedItem] | None = None,
    ) -> ScoreResult:
        dims = [scorer(ws) for scorer in _ALL_SCORERS]
        for d in dims:
            d.weight = self._WEIGHTS.get(d.key, 1.0)
        return ScoreResult(
            profile_name=self.name,
            profile_description=self.description,
            dimensions=dims,
            level_labels=dict(self._LEVELS),
        )


class _DefaultProfile:
    """Balanced / generic profile — equal weighting across all dimensions."""

    name = "default"
    description = "Balanced profile — equal weighting across all six dimensions"

    _LEVELS: dict[str, tuple[float, float]] = {
        "Ad-hoc":        (0.0, 1.0),
        "Emerging":      (1.0, 2.0),
        "Defined":       (2.0, 3.0),
        "Managed":       (3.0, 4.0),
        "Optimising":    (4.0, 5.01),
    }

    def score(
        self,
        ws: ArchitectureWorkspace,
        staging: list[StagedItem] | None = None,
    ) -> ScoreResult:
        dims = [scorer(ws) for scorer in _ALL_SCORERS]
        return ScoreResult(
            profile_name=self.name,
            profile_description=self.description,
            dimensions=dims,
            level_labels=dict(self._LEVELS),
        )


# ── Profile registry ──────────────────────────────────────────────────────────

PROFILES: dict[str, FrameworkProfile] = {
    "default": _DefaultProfile(),
    "telecom": _TelecomProfile(),
    "oda":     _ODAProfile(),
}


def list_profiles() -> list[str]:
    """Return available profile names."""
    return list(PROFILES.keys())


def score_workspace(
    ws: ArchitectureWorkspace,
    profile: str = "default",
    staging: list[StagedItem] | None = None,
) -> ScoreResult:
    """Score *ws* against the named profile.

    Raises ``KeyError`` if *profile* is not registered.
    """
    pf = PROFILES[profile]
    return pf.score(ws, staging)


# ── Improvement helpers ────────────────────────────────────────────────────────

@dataclass
class ImprovementItem:
    """One prioritised improvement action derived from a ScoreResult."""
    rank: int
    dimension_key: str
    dimension_label: str
    current_score: float
    gap: float          # (max_score − score) × weight — higher = more valuable
    key_action: str     # first finding or a generated message


def compute_top_improvements(result: ScoreResult, n: int = 3) -> list[ImprovementItem]:
    """Return the top *n* highest-value improvements from a ScoreResult.

    Ranked by ``(max_score − score) × weight`` — widest weighted gap first.
    """
    ranked = sorted(
        result.dimensions,
        key=lambda d: (d.max_score - d.score) * d.weight,
        reverse=True,
    )
    items: list[ImprovementItem] = []
    for i, d in enumerate(ranked[:n], start=1):
        if d.findings:
            action = d.findings[0]
        elif d.score >= 4.5:
            action = f"{d.label} is near-optimal — maintain and document practices"
        else:
            action = f"Improve {d.label} — currently at {d.score:.1f}/5.0"
        items.append(ImprovementItem(
            rank=i,
            dimension_key=d.key,
            dimension_label=d.label,
            current_score=d.score,
            gap=round((d.max_score - d.score) * d.weight, 2),
            key_action=action,
        ))
    return items


# ── Roadmap helpers ────────────────────────────────────────────────────────────

@dataclass
class RoadmapPhase:
    """One phased action in the improvement roadmap."""
    phase: str           # "1 — Quick Win", "2 — Short-term", "3 — Strategic", "✅ Maintain"
    horizon: str         # "< 2 weeks", "1–3 months", "3–12 months", "ongoing"
    dimension_key: str
    dimension_label: str
    current_score: float
    score_delta: str     # e.g. "+0.8–1.5"
    priority: str        # "🔴 High", "🟡 Medium", "🟢 Low", "✅ OK"
    priority_level: int  # 1 = highest urgency → 4 = maintain
    action: str


# ── Guided improvement workflow ────────────────────────────────────────────────

@dataclass
class DocRecommendation:
    """A concrete document recommended for one improvement step."""
    doc_type: str            # "ADR" | "HLD" | "Capability" | "Standard" | "Data Product"
    filename: str            # suggested filename, e.g. "ADR-009-solution-completeness.md"
    question_answered: str   # the key architectural question this doc answers
    requirements: list[str] = field(default_factory=list)   # requirements it captures
    capabilities: list[str] = field(default_factory=list)   # capabilities it covers
    sections: list[str] = field(default_factory=list)        # suggested headings
    draft_content: str = ""  # generated markdown draft (empty until 'draft <n>' runs)
    saved_path: str = ""     # absolute path after 'save <n>' writes to disk
    todo_type: str = "add"   # "add" (new doc needed) | "improve" (existing doc needs review/completion)


@dataclass
class WorkflowStep:
    """One step in the /improve-ai guided workflow."""
    index: int               # 0-based
    total: int               # total number of steps
    phase: RoadmapPhase      # the improvement phase this step addresses
    question: str            # internal dimension question (used to seed Phase 1 AI)
    status: str = "pending"  # "pending" | "questioning" | "answered" | "skipped"
    user_answer: str = ""    # architect's accumulated free-text replies (joined with ---)
    ai_analysis: str = ""    # Phase 1: AI's brief workspace analysis (2–4 sentences)
    ai_question: str = ""    # Phase 1: focused interview question for the architect
    decision: str = ""       # architect's formal decision recorded via 'decide <text>'
    doc_recommendations: list[DocRecommendation] = field(default_factory=list)


# ── Per-dimension question templates ──────────────────────────────────────────
_DIM_QUESTIONS: dict[str, str] = {
    "capability_coverage": (
        "Which business domains do you feel are under-documented or missing from your "
        "capability model? (e.g. missing Customer, Finance, or Operations capabilities)"
    ),
    "application_health": (
        "Which of your applications are the most critical and currently lack clear "
        "ownership, defined SLAs, or known end-of-life dates?"
    ),
    "data_maturity": (
        "Which data domains have no defined data products, unclear ownership, or are "
        "not connected to other domains via data flows?"
    ),
    "solution_completeness": (
        "Which major architectural decisions or platform choices have been made but "
        "never formally documented as an ADR or High-Level Design?"
    ),
    "operational_readiness": (
        "Which parts of your architecture lack observability, incident runbooks, or "
        "clear on-call ownership?"
    ),
    "governance_coverage": (
        "Which capabilities or applications have no defined owner, no compliance "
        "mapping, or no standards alignment in your Tech Radar?"
    ),
}

_DIM_DEFAULT_QUESTION = (
    "Describe the current state of this area and the biggest pain point your "
    "team experiences with it today."
)


_ODA_DIM_QUESTIONS: dict[str, str] = {
    "capability_coverage": (
        "Which business capability domains are under-documented or absent from your modular "
        "component model (for example customer, product, order, service, resource, or revenue areas)?"
    ),
    "application_health": (
        "Which critical modular services or legacy applications lack clear ownership, defined SLAs, "
        "or a migration/retirement plan?"
    ),
    "data_maturity": (
        "Which core data domains have no defined data products, unclear ownership, or weak "
        "cross-domain data exchange contracts?"
    ),
    "solution_completeness": (
        "Which modular design decisions or platform integration choices have been made but "
        "are still undocumented as ADRs or high-level designs?"
    ),
    "operational_readiness": (
        "Which shared platform components lack observability, incident runbooks, or clear "
        "on-call ownership across operational domains?"
    ),
    "governance_coverage": (
        "Which capabilities or components have no defined owner, weak policy traceability, "
        "or no clear alignment to your modular target architecture governance model?"
    ),
}


def build_workflow_steps(result: ScoreResult) -> list[WorkflowStep]:
    """Build an ordered list of workflow steps from a ScoreResult.

    Steps are ordered by priority (phase 1 first), skipping dimensions
    that are already at the 'Maintain' level (score ≥ 4.0).
    """
    phases = compute_roadmap_phases(result)
    actionable = [p for p in phases if p.priority_level < 4]  # skip Maintain
    total = len(actionable)
    steps: list[WorkflowStep] = []
    for i, phase in enumerate(actionable):
        question = _DIM_QUESTIONS.get(phase.dimension_key, _DIM_DEFAULT_QUESTION)
        steps.append(WorkflowStep(
            index=i,
            total=total,
            phase=phase,
            question=question,
        ))
    return steps


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _reserve_filename(candidate: str, existing: set[str]) -> str:
    if candidate.lower() not in existing:
        existing.add(candidate.lower())
        return candidate
    stem, ext = (candidate.rsplit(".", 1) + [""])[:2]
    suffix = f".{ext}" if ext else ""
    i = 2
    while True:
        alt = f"{stem}-v{i}{suffix}"
        if alt.lower() not in existing:
            existing.add(alt.lower())
            return alt
        i += 1


@dataclass
class TodoItem:
    """One concrete to-do action derived from workspace scoring findings."""
    priority: int        # 1=high, 2=medium, 3=low
    category: str        # "add" | "improve"
    doc_type: str        # "ADR" | "HLD" | "Capability" | "Standard" | "Data Product" | "Approval"
    subject: str         # specific name, e.g. "ADR-003-kafka.md" or "Resource Management"
    action: str          # imperative sentence
    dimension: str       # dimension key this addresses
    score_impact: str    # e.g. "+0.5 Solution Completeness"
    draft_content: str = ""   # generated markdown (empty until 'draft <n>' runs)
    saved_path: str = ""      # absolute path after 'save <n>'


def build_todo_list(
    result: ScoreResult,
    ws: ArchitectureWorkspace,
) -> list[TodoItem]:
    """Build a deterministic to-do list from scoring findings and workspace objects.

    Each item is specific — names actual capabilities, solutions, data domains, etc.
    Deduplicates by (category, subject). Sorted by priority (1=high first).
    Capped at 15 items.
    """
    items: list[TodoItem] = []
    seen: set[tuple[str, str]] = set()

    def _add(item: TodoItem) -> None:
        key = (item.category, item.subject)
        if key not in seen:
            seen.add(key)
            items.append(item)

    dim_map = {d.key: d for d in result.dimensions}

    def _priority(key: str) -> int:
        d = dim_map.get(key)
        if d is None:
            return 3
        if d.score < 2.0:
            return 1
        if d.score < 3.0:
            return 2
        return 3

    # ── capability_coverage ──────────────────────────────────────────────────
    d = dim_map.get("capability_coverage")
    if d and d.score < 4.0:
        caps = ws.enterprise.capabilities
        pri = _priority("capability_coverage")
        for cap in caps:
            if not cap.owner:
                _add(TodoItem(
                    priority=pri,
                    category="improve",
                    doc_type="Capability",
                    subject=cap.name,
                    action=f"Assign owner to capability '{cap.name}'",
                    dimension="capability_coverage",
                    score_impact="+0.3 Capability Coverage",
                ))
        if len(caps) < 10:
            _add(TodoItem(
                priority=pri,
                category="add",
                doc_type="Capability",
                subject="Capability coverage plan",
                action=f"Document missing capability domains — expand from current {len(caps)} capabilities",
                dimension="capability_coverage",
                score_impact="+0.5 Capability Coverage",
            ))

    # ── application_health ───────────────────────────────────────────────────
    d = dim_map.get("application_health")
    if d and d.score < 4.0:
        apps = ws.enterprise.applications
        pri = _priority("application_health")
        unlinked = [a for a in apps if not a.capability_ids]
        if unlinked:
            for app in unlinked[:3]:  # cap at 3 to avoid flooding
                _add(TodoItem(
                    priority=pri,
                    category="improve",
                    doc_type="Capability",
                    subject=app.name,
                    action=f"Link application '{app.name}' to at least one business capability",
                    dimension="application_health",
                    score_impact="+0.2 Application Health",
                ))
            if len(unlinked) > 3:
                _add(TodoItem(
                    priority=pri,
                    category="improve",
                    doc_type="HLD",
                    subject="Application-capability linkage plan",
                    action=f"Map remaining {len(unlinked) - 3} unlinked applications to capabilities",
                    dimension="application_health",
                    score_impact="+0.5 Application Health",
                ))

    # ── data_maturity ────────────────────────────────────────────────────────
    d = dim_map.get("data_maturity")
    if d and d.score < 4.0:
        covered_domains = {p.domain_id for p in ws.data.products}
        pri = _priority("data_maturity")
        for domain in ws.data.domains:
            if domain.id not in covered_domains:
                _add(TodoItem(
                    priority=pri,
                    category="add",
                    doc_type="Data Product",
                    subject=f"{domain.name} data product",
                    action=f"Add data product definition for '{domain.name}' domain",
                    dimension="data_maturity",
                    score_impact="+0.4 Data Maturity",
                ))
        for domain in ws.data.domains:
            if not domain.owner_team:
                _add(TodoItem(
                    priority=pri,
                    category="improve",
                    doc_type="Data Product",
                    subject=domain.name,
                    action=f"Assign owner team to data domain '{domain.name}'",
                    dimension="data_maturity",
                    score_impact="+0.2 Data Maturity",
                ))

    # ── solution_completeness ────────────────────────────────────────────────
    d = dim_map.get("solution_completeness")
    if d and d.score < 4.0:
        pri = _priority("solution_completeness")
        for sol in ws.solutions:
            if sol.status == "draft":
                _add(TodoItem(
                    priority=pri,
                    category="improve",
                    doc_type="Approval",
                    subject=sol.name,
                    action=f"Move solution '{sol.name}' from draft to reviewed/approved",
                    dimension="solution_completeness",
                    score_impact="+0.5 Solution Completeness",
                ))
            if not sol.adrs:
                _add(TodoItem(
                    priority=pri,
                    category="add",
                    doc_type="ADR",
                    subject=f"ADR for {sol.name}",
                    action=f"Add architecture decision record for solution '{sol.name}'",
                    dimension="solution_completeness",
                    score_impact="+0.4 Solution Completeness",
                ))

    # ── operational_readiness ─────────────────────────────────────────────────
    d = dim_map.get("operational_readiness")
    if d and d.score < 4.0:
        pri = _priority("operational_readiness")
        stds = ws.enterprise.standards
        on_hold = [s for s in stds if s.status == "hold"]
        adopted = [s for s in stds if s.status == "adopt"]
        if len(on_hold) > len(adopted):
            _add(TodoItem(
                priority=pri,
                category="improve",
                doc_type="Standard",
                subject="Tech Radar review",
                action=f"Review {len(on_hold)} on-hold tech standards — more on hold than adopted",
                dimension="operational_readiness",
                score_impact="+0.3 Operational Readiness",
            ))
        streaming_flows = [f for f in ws.data.flows if f.mechanism in ("streaming", "cdc")]
        event_sols = [s for s in ws.solutions if s.pattern == "event-driven"]
        if not streaming_flows and not event_sols:
            _add(TodoItem(
                priority=pri,
                category="add",
                doc_type="HLD",
                subject="Streaming and event-driven HLD",
                action="Add HLD for streaming/event-driven operational capabilities",
                dimension="operational_readiness",
                score_impact="+0.5 Operational Readiness",
            ))

    # ── governance_coverage ──────────────────────────────────────────────────
    d = dim_map.get("governance_coverage")
    if d and d.score < 4.0:
        pri = _priority("governance_coverage")
        stds = ws.enterprise.standards
        if len(stds) < 5:
            _add(TodoItem(
                priority=pri,
                category="add",
                doc_type="Standard",
                subject="Tech Radar expansion",
                action="Expand Tech Radar — add standards entries to reach governance baseline",
                dimension="governance_coverage",
                score_impact="+0.5 Governance & Compliance",
            ))
        for prod in ws.data.products:
            if not prod.owner_team:
                _add(TodoItem(
                    priority=pri,
                    category="improve",
                    doc_type="Data Product",
                    subject=prod.name,
                    action=f"Assign owner team to data product '{prod.name}'",
                    dimension="governance_coverage",
                    score_impact="+0.2 Governance & Compliance",
                ))

    # Sort: priority ascending (1=high), then by score_impact length desc (longer = more specific)
    items.sort(key=lambda x: (x.priority, -len(x.score_impact)))
    return items[:15]


def build_fallback_doc_recommendations(
    phase: RoadmapPhase,
    next_adr_number: int = 1,
    existing_documents: list[str] | None = None,
) -> list[DocRecommendation]:
    """Build deterministic doc recommendations when AI output is unavailable."""
    existing = {d.lower() for d in (existing_documents or [])}
    dim_slug = _slugify(phase.dimension_label) or "improvement"
    adr_num = max(1, next_adr_number)

    def _next_adr(stem: str) -> str:
        nonlocal adr_num
        while True:
            candidate = f"ADR-{adr_num:03d}-{stem}.md"
            adr_num += 1
            if candidate.lower() not in existing:
                existing.add(candidate.lower())
                return candidate

    docs: list[DocRecommendation] = []

    if phase.dimension_key == "capability_coverage":
        docs.append(DocRecommendation(
            doc_type="Capability",
            filename=_reserve_filename(f"capability-{dim_slug}-coverage-plan.md", existing),
            question_answered="Which capability gaps and owners should be prioritised in the next planning cycle?",
            requirements=[
                "Current capabilities by business domain",
                "Missing capabilities and business impact",
                "Owner assignment for each target capability",
                "Prioritised implementation sequence",
            ],
            capabilities=["Capability taxonomy", "Business domain map"],
            sections=["Context", "Current State", "Gaps", "Target Capability Model", "Ownership", "Roadmap"],
        ))
        docs.append(DocRecommendation(
            doc_type="ADR",
            filename=_next_adr("capability-taxonomy-and-ownership-model"),
            question_answered="What capability taxonomy and ownership model should become the new architectural baseline?",
            requirements=[
                "Decision scope and constraints",
                "Chosen taxonomy and ownership rules",
                "Rejected options",
                "Migration implications",
            ],
            capabilities=["Capability governance", "Portfolio alignment"],
            sections=["Status", "Context", "Decision", "Alternatives", "Consequences", "Adoption Plan"],
        ))
    elif phase.dimension_key == "application_health":
        docs.append(DocRecommendation(
            doc_type="ADR",
            filename=_next_adr("application-lifecycle-and-slo-policy"),
            question_answered="Which lifecycle and SLO policy should govern critical applications?",
            requirements=[
                "Criticality tiers and SLO expectations",
                "Ownership and support model",
                "Retirement triggers",
                "Exception handling policy",
            ],
            capabilities=["Application lifecycle governance", "Operational resilience"],
            sections=["Status", "Context", "Decision", "Policy Rules", "Exceptions", "Consequences"],
        ))
        docs.append(DocRecommendation(
            doc_type="HLD",
            filename=_reserve_filename(f"hld-{dim_slug}-stabilisation-plan.md", existing),
            question_answered="How will we stabilise and modernise the most fragile applications over the next two quarters?",
            requirements=[
                "Current health baseline",
                "Top risk applications",
                "Modernisation target architecture",
                "Delivery milestones and owners",
            ],
            capabilities=["Application platform", "Service operations"],
            sections=["Overview", "Current State", "Target State", "Migration Waves", "Risks", "Execution Plan"],
        ))
    elif phase.dimension_key == "data_maturity":
        docs.append(DocRecommendation(
            doc_type="Data Product",
            filename=_reserve_filename(f"data-product-{dim_slug}-backlog.md", existing),
            question_answered="Which data products, ownership boundaries, and SLAs are required to improve data maturity?",
            requirements=[
                "Domain-to-product mapping",
                "Ownership for each product",
                "SLA tier and quality controls",
                "Consumer commitments",
            ],
            capabilities=["Data product governance", "Data domain architecture"],
            sections=["Context", "Domain Inventory", "Data Product Backlog", "Ownership", "SLA Model", "Delivery Plan"],
        ))
        docs.append(DocRecommendation(
            doc_type="ADR",
            filename=_next_adr("data-domain-ownership-and-sla-model"),
            question_answered="What ownership and SLA model should standardise data domains and products?",
            requirements=[
                "Ownership RACI",
                "SLA tier model",
                "Escalation and incident process",
                "Cross-domain dependency rules",
            ],
            capabilities=["Data governance", "Data reliability"],
            sections=["Status", "Context", "Decision", "Alternatives", "Consequences", "Rollout"],
        ))
    elif phase.dimension_key == "solution_completeness":
        docs.append(DocRecommendation(
            doc_type="ADR",
            filename=_next_adr("solution-architecture-baseline"),
            question_answered="Which architecture decisions must be formalised to close current solution-design gaps?",
            requirements=[
                "Decision scope and boundary",
                "Chosen architecture direction",
                "Trade-off analysis",
                "Implementation constraints",
            ],
            capabilities=["Solution design governance", "Platform architecture"],
            sections=["Status", "Context", "Decision", "Alternatives", "Consequences", "Implementation Notes"],
        ))
        docs.append(DocRecommendation(
            doc_type="HLD",
            filename=_reserve_filename(f"hld-{dim_slug}-target-architecture.md", existing),
            question_answered="What target solution architecture and component boundaries should teams implement?",
            requirements=[
                "Scope and non-goals",
                "Logical architecture and components",
                "Integration and dependency map",
                "Phased delivery approach",
            ],
            capabilities=["Solution architecture", "Integration architecture"],
            sections=["Overview", "Architecture Drivers", "Target Design", "Component View", "Risks", "Roadmap"],
        ))
    elif phase.dimension_key == "operational_readiness":
        docs.append(DocRecommendation(
            doc_type="HLD",
            filename=_reserve_filename(f"hld-{dim_slug}-operations-model.md", existing),
            question_answered="What operations model (observability, incident response, on-call) is required for readiness?",
            requirements=[
                "Observability baseline",
                "Incident response workflow",
                "On-call ownership model",
                "Operational SLO metrics",
            ],
            capabilities=["SRE practices", "Incident management"],
            sections=["Overview", "Current Operational Gaps", "Target Operations Model", "Runbook Structure", "Metrics", "Adoption Plan"],
        ))
        docs.append(DocRecommendation(
            doc_type="ADR",
            filename=_next_adr("observability-and-incident-governance"),
            question_answered="Which observability and incident-governance standards become mandatory?",
            requirements=[
                "Logging, tracing, metrics minimums",
                "Incident severity and response SLAs",
                "Ownership and escalation policy",
                "Compliance and audit expectations",
            ],
            capabilities=["Platform operations", "Service governance"],
            sections=["Status", "Context", "Decision", "Policy", "Consequences", "Compliance"],
        ))
    elif phase.dimension_key == "governance_coverage":
        docs.append(DocRecommendation(
            doc_type="Standard",
            filename=_reserve_filename(f"standard-{dim_slug}-control-matrix.md", existing),
            question_answered="What governance controls and owner/accountability model should be mandatory across architecture domains?",
            requirements=[
                "Control categories and definitions",
                "Owner/accountability mapping",
                "Compliance evidence requirements",
                "Review cadence and gates",
            ],
            capabilities=["Architecture governance", "Compliance management"],
            sections=["Purpose", "Control Matrix", "Ownership", "Compliance Evidence", "Review Cadence", "Exceptions"],
        ))
        docs.append(DocRecommendation(
            doc_type="ADR",
            filename=_next_adr("governance-exception-and-waiver-process"),
            question_answered="How should governance exceptions and waivers be approved and tracked?",
            requirements=[
                "Exception decision criteria",
                "Approval workflow and roles",
                "Expiry and review policy",
                "Risk acceptance documentation",
            ],
            capabilities=["Governance operations", "Risk management"],
            sections=["Status", "Context", "Decision", "Workflow", "Consequences", "Auditability"],
        ))
    else:
        docs.append(DocRecommendation(
            doc_type="ADR",
            filename=_next_adr(f"{dim_slug}-improvement-decision"),
            question_answered="What architecture decision should be formalised first to improve this dimension?",
            requirements=[
                "Problem statement",
                "Decision and rationale",
                "Alternatives considered",
                "Expected impact",
            ],
            capabilities=["Architecture governance"],
            sections=["Status", "Context", "Decision", "Alternatives", "Consequences"],
        ))
        docs.append(DocRecommendation(
            doc_type="HLD",
            filename=_reserve_filename(f"hld-{dim_slug}-improvement-plan.md", existing),
            question_answered="How will the team implement the chosen decision in practice?",
            requirements=[
                "Scope and assumptions",
                "Target architecture",
                "Implementation milestones",
                "Risks and mitigations",
            ],
            capabilities=["Architecture implementation"],
            sections=["Overview", "Current State", "Target State", "Execution Plan", "Risks"],
        ))
    return docs


def compute_roadmap_phases(result: ScoreResult) -> list[RoadmapPhase]:
    """Build a phased improvement roadmap from a ScoreResult.

    Phase assignment is based on current score bucket, ordered by weighted gap:
      1 — Quick Win  (score < 2.0) : critical gaps, start immediately
      2 — Short-term (score 2.0–3.0): meaningful gaps, plan in next quarter
      3 — Strategic  (score 3.0–4.0): passing but room to grow
      ✅ Maintain    (score ≥ 4.0)  : near-optimal, monitor and document

    Within each phase, dimensions are ordered by weighted gap (highest first).
    """
    ranked = sorted(
        result.dimensions,
        key=lambda d: (d.max_score - d.score) * d.weight,
        reverse=True,
    )
    phases: list[RoadmapPhase] = []
    for d in ranked:
        if d.score < 2.0:
            phase = "1 — Quick Win"
            horizon = "< 2 weeks"
            priority = "🔴 High"
            priority_level = 1
            delta = "+0.8–1.5"
        elif d.score < 3.0:
            phase = "2 — Short-term"
            horizon = "1–3 months"
            priority = "🟡 Medium"
            priority_level = 2
            delta = "+0.5–1.0"
        elif d.score < 4.0:
            phase = "3 — Strategic"
            horizon = "3–12 months"
            priority = "🟢 Low"
            priority_level = 3
            delta = "+0.3–0.8"
        else:
            phase = "✅ Maintain"
            horizon = "ongoing"
            priority = "✅ OK"
            priority_level = 4
            delta = "—"
        action = (
            d.findings[0] if d.findings
            else f"Improve {d.label} — currently {d.score:.1f}/{d.max_score:.0f}"
        )
        phases.append(RoadmapPhase(
            phase=phase,
            horizon=horizon,
            dimension_key=d.key,
            dimension_label=d.label,
            current_score=d.score,
            score_delta=delta,
            priority=priority,
            priority_level=priority_level,
            action=action,
        ))
    return phases
