"""Architecture stack coverage analysis.

``compute_stack_coverage`` returns a structured view of what the workspace has
and what is missing, grouped by domain / area.  Results are used by the TUI
``/stack`` command, the ``strata stack`` CLI, and the AI agent context.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import ArchitectureWorkspace


# ── Reference taxonomy ────────────────────────────────────────────────────────
# Canonical enterprise capability domains — used to flag absent areas.
# Profiles can override this in future; for now it is a sane generic default.

_REFERENCE_CAP_DOMAINS: list[str] = [
    "Customer",
    "Finance",
    "Operations",
    "Technology",
    "HR",
    "Products",
    "Risk & Compliance",
    "Partner & Ecosystem",
]


# ── Coverage dataclasses ──────────────────────────────────────────────────────

@dataclass
class CapabilityDomainCoverage:
    domain: str
    count: int
    strategic: int
    core: int
    supporting: int
    ownership_pct: float    # 0–100
    mature_pct: float       # % at managed / optimizing
    indicator: str          # ✅ ⚠️ ❌


@dataclass
class DataDomainCoverage:
    domain_id: str
    name: str
    products_count: int
    flows_in: int
    flows_out: int
    sla_gold_plat: int
    owner: str
    indicator: str          # ✅ ⚠️ ❌


@dataclass
class SolutionCoverage:
    solution_id: str
    name: str
    pattern: str
    status: str
    has_adrs: bool
    component_count: int
    indicator: str          # ✅ ⚠️ ❌


@dataclass
class StackCoverage:
    capability_domains: list[CapabilityDomainCoverage]
    missing_cap_domains: list[str]          # reference domains with zero capabilities
    data_domains: list[DataDomainCoverage]
    isolated_data_domains: list[str]        # data domains with no flows at all
    solutions: list[SolutionCoverage]
    radar_by_category: dict[str, list[str]] # category → list of "Name (status)" strings
    gaps: list[str] = field(default_factory=list)   # plain-text gap descriptions
    total_entities: dict[str, int] = field(default_factory=dict)


# ── Main analyser ─────────────────────────────────────────────────────────────

def compute_stack_coverage(ws: ArchitectureWorkspace) -> StackCoverage:
    """Return a full coverage map for *ws*.

    Signals:
    - ✅  good coverage  (enough depth + ownership)
    - ⚠️  partial / thin (exists but needs attention)
    - ❌  absent / isolated (critical gap)
    """
    gaps: list[str] = []

    # ── Capability domains ────────────────────────────────────────────────────
    caps = ws.enterprise.capabilities
    domain_map: dict[str, list] = {}
    for c in caps:
        domain_map.setdefault(c.domain or "Uncategorised", []).append(c)

    cap_domain_cov: list[CapabilityDomainCoverage] = []
    for domain, dcaps in sorted(domain_map.items()):
        n = len(dcaps)
        strategic = sum(1 for c in dcaps if c.level == "strategic")
        core      = sum(1 for c in dcaps if c.level == "core")
        supporting = sum(1 for c in dcaps if c.level == "supporting")
        owned     = sum(1 for c in dcaps if c.owner)
        ownership_pct = round(owned / n * 100) if n else 0
        mature    = sum(1 for c in dcaps if c.maturity in ("managed", "optimizing"))
        mature_pct = round(mature / n * 100) if n else 0

        if n >= 5 and ownership_pct >= 80:
            indicator = "✅"
        elif n >= 2:
            indicator = "⚠️"
        else:
            indicator = "❌"
            gaps.append(
                f"Capability domain '{domain}' has only {n} capability — expand coverage"
            )
        cap_domain_cov.append(CapabilityDomainCoverage(
            domain=domain, count=n,
            strategic=strategic, core=core, supporting=supporting,
            ownership_pct=ownership_pct, mature_pct=mature_pct,
            indicator=indicator,
        ))

    # Missing reference domains
    known_lower = {d.lower() for d in domain_map}
    missing_cap_domains = [
        ref for ref in _REFERENCE_CAP_DOMAINS
        if ref.lower() not in known_lower
    ]
    if missing_cap_domains:
        sample = ", ".join(missing_cap_domains[:3])
        suffix = f" (+{len(missing_cap_domains) - 3} more)" if len(missing_cap_domains) > 3 else ""
        gaps.append(
            f"No capabilities for reference domains: {sample}{suffix}"
        )

    # ── Data domains ─────────────────────────────────────────────────────────
    flow_in:  dict[str, int] = {}
    flow_out: dict[str, int] = {}
    for fl in ws.data.flows:
        flow_in[fl.target_domain]  = flow_in.get(fl.target_domain, 0) + 1
        flow_out[fl.source_domain] = flow_out.get(fl.source_domain, 0) + 1

    product_by_domain: dict[str, list] = {}
    for p in ws.data.products:
        product_by_domain.setdefault(p.domain_id, []).append(p)

    data_domain_cov: list[DataDomainCoverage] = []
    isolated: list[str] = []
    for dd in ws.data.domains:
        prods     = product_by_domain.get(dd.id, [])
        fi        = flow_in.get(dd.id, 0)
        fo        = flow_out.get(dd.id, 0)
        gold_plat = sum(1 for p in prods if p.sla_tier in ("gold", "platinum"))

        if fi + fo == 0:
            indicator = "❌"
            isolated.append(dd.name)
            gaps.append(f"Data domain '{dd.name}' has no data flows — isolated domain")
        elif not prods:
            indicator = "⚠️"
            gaps.append(f"Data domain '{dd.name}' has no data products")
        else:
            indicator = "✅"

        data_domain_cov.append(DataDomainCoverage(
            domain_id=dd.id, name=dd.name,
            products_count=len(prods),
            flows_in=fi, flows_out=fo,
            sla_gold_plat=gold_plat,
            owner=dd.owner_team or "—",
            indicator=indicator,
        ))

    # ── Solutions ─────────────────────────────────────────────────────────────
    sol_cov: list[SolutionCoverage] = []
    for s in ws.solutions:
        has_adrs = bool(s.adrs)
        nc       = len(s.components)
        if s.status in ("approved", "implemented") and has_adrs and nc >= 3:
            indicator = "✅"
        elif not has_adrs or nc == 0:
            indicator = "❌"
            if not has_adrs:
                gaps.append(f"Solution '{s.name}' has no ADRs — decisions undocumented")
        else:
            indicator = "⚠️"
        sol_cov.append(SolutionCoverage(
            solution_id=s.id, name=s.name, pattern=s.pattern,
            status=s.status, has_adrs=has_adrs, component_count=nc,
            indicator=indicator,
        ))

    # ── Tech radar by category ────────────────────────────────────────────────
    radar: dict[str, list[str]] = {}
    for std in ws.enterprise.standards:
        radar.setdefault(std.category or "Other", []).append(f"{std.name} ({std.status})")

    if not ws.enterprise.standards:
        gaps.append("No tech standards on radar — technology governance missing")

    total = {
        "capabilities":  len(caps),
        "applications":  len(ws.enterprise.applications),
        "standards":     len(ws.enterprise.standards),
        "data_domains":  len(ws.data.domains),
        "data_products": len(ws.data.products),
        "data_flows":    len(ws.data.flows),
        "solutions":     len(ws.solutions),
    }

    return StackCoverage(
        capability_domains=cap_domain_cov,
        missing_cap_domains=missing_cap_domains,
        data_domains=data_domain_cov,
        isolated_data_domains=isolated,
        solutions=sol_cov,
        radar_by_category=radar,
        gaps=gaps,
        total_entities=total,
    )
