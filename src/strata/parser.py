"""PSA (Problem-Solution-Architecture) markdown parser.

Parses a structured markdown file into an ArchitectureWorkspace — no AI required.

Expected document structure
────────────────────────────
    ---                                       # YAML frontmatter (optional)
    name: My Organisation
    cloud_provider: aws
    environment: production
    ---

    # Architecture: My Organisation          # optional title (used as fallback name)

    ## Capabilities
    | Name | Domain | Level | Owner | Description |
    ...

    ## Applications
    | Name | Hosting | Criticality | Owner | Stack | Status |
    ...

    ## Tech Standards
    | Name | Category | Status | Rationale |
    ...

    ## Data Domains
    | Name | Owner | Storage | Description |
    ...

    ## Data Products
    | Name | Domain | Output Port | SLA | Owner |
    ...

    ## Data Flows
    | Name | From | To | Mechanism | Classification |
    ...

    ## Solutions

    ### My Solution
    - pattern: api-gateway
    - target: aws
    - status: draft
    - description: Optional free text description

    #### Components
    | Name | Type | Technology | Hosting |
    ...

    #### ADRs
    | Title | Status | Context | Decision |
    ...

All section headings are matched case-insensitively.
Column headers are matched case-insensitively and spaces/underscores are normalised.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .models import (
    Application,
    ArchitectureDecisionRecord,
    ArchitectureWorkspace,
    BusinessCapability,
    Component,
    DataArchitecture,
    DataDomain,
    DataFlow,
    DataProduct,
    EnterpriseArchitecture,
    SolutionDesign,
    TechnologyStandard,
    WorkspaceManifest,
)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _norm_key(key: str) -> str:
    """Normalise a column header: lowercase, strip, spaces→underscore."""
    return re.sub(r"[\s_]+", "_", key.strip().lower())


# ── Markdown table parser ──────────────────────────────────────────────────────

def _parse_table(block: str) -> list[dict[str, str]]:
    """Parse a markdown pipe-table block into a list of normalised dicts."""
    rows: list[dict[str, str]] = []
    headers: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Separator row (e.g. |---|---| or |:---:|)
        if all(re.match(r"^[-: ]+$", c) for c in cells if c):
            continue
        if not headers:
            headers = [_norm_key(h) for h in cells]
        else:
            row = {headers[i]: cells[i] for i in range(min(len(headers), len(cells)))}
            # Only keep non-empty rows
            if any(v for v in row.values()):
                rows.append(row)
    return rows


# ── Section splitting ──────────────────────────────────────────────────────────

def _split_sections(text: str) -> dict[str, str]:
    """Split document body into top-level '## …' sections (keyed lowercase)."""
    sections: dict[str, str] = {}
    current_key = "__preamble__"
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^## (.+)$", line)
        if m:
            sections[current_key] = "\n".join(buf)
            current_key = m.group(1).strip().lower()
            buf = []
        else:
            buf.append(line)
    sections[current_key] = "\n".join(buf)
    return sections


def _split_h3_blocks(text: str) -> list[tuple[str, str]]:
    """Split '## Solutions' body into individual '### Name' blocks."""
    blocks: list[tuple[str, str]] = []
    current_name = ""
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^### (.+)$", line)
        if m:
            if current_name:
                blocks.append((current_name, "\n".join(buf)))
            current_name = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    if current_name:
        blocks.append((current_name, "\n".join(buf)))
    return blocks


def _split_h4_sections(text: str) -> dict[str, str]:
    """Split a solution block into '#### …' sub-sections."""
    secs: dict[str, str] = {}
    current_key = "__default__"
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^#### (.+)$", line)
        if m:
            secs[current_key] = "\n".join(buf)
            current_key = m.group(1).strip().lower()
            buf = []
        else:
            buf.append(line)
    secs[current_key] = "\n".join(buf)
    return secs


# ── Key-value line parser ──────────────────────────────────────────────────────

def _parse_kv(text: str) -> dict[str, str]:
    """Parse '- key: value' lines from a block of text."""
    kv: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^[-*]\s+([\w _-]+):\s*(.+)$", line.strip())
        if m:
            kv[_norm_key(m.group(1))] = m.group(2).strip()
    return kv


# ── Per-section parsers ────────────────────────────────────────────────────────

def _parse_capabilities(text: str) -> list[BusinessCapability]:
    caps: list[BusinessCapability] = []
    for row in _parse_table(text):
        name = row.get("name", "").strip()
        if not name:
            continue
        caps.append(
            BusinessCapability(
                id=_slug(name),
                name=name,
                domain=row.get("domain", "general") or "general",
                level=row.get("level", "core") or "core",  # type: ignore[arg-type]
                owner=row.get("owner") or None,
                description=row.get("description") or None,
                maturity=row.get("maturity", "initial") or "initial",  # type: ignore[arg-type]
            )
        )
    return caps


def _parse_applications(text: str) -> list[Application]:
    apps: list[Application] = []
    for row in _parse_table(text):
        name = row.get("name", "").strip()
        if not name:
            continue
        raw_stack = row.get("stack", "") or ""
        stack = [s.strip() for s in raw_stack.split(",") if s.strip()]
        apps.append(
            Application(
                id=_slug(name),
                name=name,
                hosting=row.get("hosting", "kubernetes") or "kubernetes",  # type: ignore[arg-type]
                criticality=row.get("criticality", "medium") or "medium",  # type: ignore[arg-type]
                owner_team=row.get("owner") or None,
                status=row.get("status", "active") or "active",  # type: ignore[arg-type]
                technology_stack=stack,
                description=row.get("description") or None,
            )
        )
    return apps


def _parse_standards(text: str) -> list[TechnologyStandard]:
    stds: list[TechnologyStandard] = []
    for row in _parse_table(text):
        name = row.get("name", "").strip()
        if not name:
            continue
        stds.append(
            TechnologyStandard(
                id=_slug(name),
                name=name,
                category=row.get("category", "other") or "other",
                status=row.get("status", "assess") or "assess",  # type: ignore[arg-type]
                rationale=row.get("rationale") or None,
            )
        )
    return stds


def _parse_data_domains(text: str) -> list[DataDomain]:
    domains: list[DataDomain] = []
    for row in _parse_table(text):
        name = row.get("name", "").strip()
        if not name:
            continue
        domains.append(
            DataDomain(
                id=_slug(name),
                name=name,
                owner_team=row.get("owner") or None,
                storage_pattern=row.get("storage", "operational") or "operational",  # type: ignore[arg-type]
                description=row.get("description") or None,
            )
        )
    return domains


def _parse_data_products(text: str) -> list[DataProduct]:
    products: list[DataProduct] = []
    for row in _parse_table(text):
        name = row.get("name", "").strip()
        if not name:
            continue
        # accept "domain" or "domain_id"
        domain_raw = row.get("domain", row.get("domain_id", "")) or ""
        products.append(
            DataProduct(
                id=_slug(name),
                name=name,
                domain_id=_slug(domain_raw) if domain_raw else "unknown",
                output_port=row.get("output_port", row.get("output", "api")) or "api",  # type: ignore[arg-type]
                sla_tier=row.get("sla", row.get("sla_tier", "silver")) or "silver",  # type: ignore[arg-type]
                owner_team=row.get("owner") or None,
            )
        )
    return products


def _parse_flows(text: str) -> list[DataFlow]:
    flows: list[DataFlow] = []
    for row in _parse_table(text):
        name = row.get("name", "").strip()
        if not name:
            continue
        src = row.get("from", row.get("source", row.get("source_domain", ""))) or ""
        tgt = row.get("to", row.get("target", row.get("target_domain", ""))) or ""
        flows.append(
            DataFlow(
                id=_slug(name),
                name=name,
                source_domain=_slug(src) if src else "unknown",
                target_domain=_slug(tgt) if tgt else "unknown",
                mechanism=row.get("mechanism", "api") or "api",  # type: ignore[arg-type]
                classification=row.get("classification", "internal") or "internal",  # type: ignore[arg-type]
            )
        )
    return flows


def _parse_components(text: str) -> list[Component]:
    comps: list[Component] = []
    for row in _parse_table(text):
        name = row.get("name", "").strip()
        if not name:
            continue
        comps.append(
            Component(
                id=_slug(name),
                name=name,
                type=row.get("type", "service") or "service",  # type: ignore[arg-type]
                technology=row.get("technology", row.get("tech")) or None,
                hosting=row.get("hosting", "kubernetes") or "kubernetes",  # type: ignore[arg-type]
                description=row.get("description") or None,
            )
        )
    return comps


def _parse_adrs(text: str) -> list[ArchitectureDecisionRecord]:
    adrs: list[ArchitectureDecisionRecord] = []
    for i, row in enumerate(_parse_table(text), 1):
        title = row.get("title", "").strip()
        if not title:
            continue
        adrs.append(
            ArchitectureDecisionRecord(
                id=f"ADR-{i:03d}",
                title=title,
                status=row.get("status", "proposed") or "proposed",  # type: ignore[arg-type]
                context=row.get("context") or None,
                decision=row.get("decision") or None,
                consequences=row.get("consequences") or None,
                date=str(date.today()),
            )
        )
    return adrs


def _parse_solution(name: str, body: str) -> SolutionDesign:
    """Parse a single ### Solution block."""
    kv = _parse_kv(body)
    h4 = _split_h4_sections(body)

    components = _parse_components(h4.get("components", ""))
    adrs = _parse_adrs(h4.get("adrs", h4.get("architecture decision records", "")))

    # Description: first prose line that isn't a kv marker, table row, or heading
    description: str | None = kv.get("description")
    if not description:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith(("-", "*", "|", "#")):
                description = stripped
                break

    return SolutionDesign(
        id=_slug(name),
        name=name,
        description=description or None,
        pattern=kv.get("pattern", "microservices") or "microservices",  # type: ignore[arg-type]
        deployment_target=kv.get("target", kv.get("deployment_target", "multi-cloud")) or "multi-cloud",  # type: ignore[arg-type]
        status=kv.get("status", "draft") or "draft",  # type: ignore[arg-type]
        components=components,
        adrs=adrs,
    )


# ── Front-matter ───────────────────────────────────────────────────────────────

def _strip_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML front-matter block if present, return (meta, body)."""
    if text.lstrip().startswith("---"):
        parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)
        if len(parts) >= 3:
            try:
                fm: dict[str, Any] = yaml.safe_load(parts[1]) or {}
                return fm, parts[2]
            except yaml.YAMLError:
                pass
    return {}, text


# ── Public entry-point ─────────────────────────────────────────────────────────

def parse_psa_markdown(path: Path) -> ArchitectureWorkspace:
    """Parse a PSA markdown file and return a populated ArchitectureWorkspace.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if parsing fails critically.
    """
    text = path.read_text(encoding="utf-8")
    fm, body = _strip_frontmatter(text)

    # Fallback workspace name from # heading or filename
    title_m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    default_name = title_m.group(1).strip() if title_m else path.stem

    manifest = WorkspaceManifest(
        name=fm.get("name", default_name),
        description=fm.get("description") or None,
        cloud_provider=fm.get("cloud_provider", "multi-cloud"),  # type: ignore[arg-type]
        environment=fm.get("environment", "production"),  # type: ignore[arg-type]
    )

    secs = _split_sections(body)

    # ── Enterprise ────────────────────────────────────────────────────────────
    caps = _parse_capabilities(secs.get("capabilities", ""))
    apps = _parse_applications(secs.get("applications", ""))
    stds = _parse_standards(
        secs.get(
            "tech standards",
            secs.get("standards", secs.get("technology standards", "")),
        )
    )

    # ── Data ──────────────────────────────────────────────────────────────────
    domains = _parse_data_domains(secs.get("data domains", secs.get("domains", "")))
    products = _parse_data_products(secs.get("data products", secs.get("products", "")))
    flows = _parse_flows(secs.get("data flows", secs.get("flows", "")))

    # ── Solutions ─────────────────────────────────────────────────────────────
    solutions = [
        _parse_solution(sol_name, sol_body)
        for sol_name, sol_body in _split_h3_blocks(secs.get("solutions", ""))
    ]

    return ArchitectureWorkspace(
        manifest=manifest,
        enterprise=EnterpriseArchitecture(capabilities=caps, applications=apps, standards=stds),
        data=DataArchitecture(domains=domains, products=products, flows=flows),
        solutions=solutions,
    )
