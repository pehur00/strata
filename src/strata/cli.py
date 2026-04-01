from __future__ import annotations

import json
import re
from datetime import UTC, date as _date, datetime
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .models import (
    Application, ArchitectureDecisionRecord, ArchitectureWorkspace,
    BusinessCapability, Component, DataDomain, DataFlow, DataProduct,
    EnterpriseArchitecture, DataArchitecture, SolutionDesign, StagedItem,
    TechnologyStandard, WorkspaceManifest,
)
from .agent import AgentError, ArchitectureAgent, save_config
from .parser import parse_psa_markdown
from .renderer import (
    print_workspace_status, render_capability_map,
    render_data_flow_map, render_solution_diagram,
)
from .workspace import (
    WorkspaceError, find_workspace_root, load_staging, load_workspace,
    next_staging_id, save_staging, save_workspace,
    add_watch_folder, remove_watch_folder, load_watch_folders,
)

# ── App + sub-apps ─────────────────────────────────────────────────────────────

app = typer.Typer(
    name="strata",
    help="Architecture as a Service — design and govern enterprise, data, and solution architecture.",
    no_args_is_help=False,
    invoke_without_command=True,
)
enterprise_app = typer.Typer(
    help="Enterprise architecture: capabilities, applications, tech standards.",
    no_args_is_help=True,
)
data_app = typer.Typer(
    help="Data architecture: domains, data products, data flows.",
    no_args_is_help=True,
)
solution_app = typer.Typer(
    help="Solution architecture: designs, components, ADRs.",
    no_args_is_help=True,
)
generate_app = typer.Typer(
    help="Generate Mermaid diagrams and architecture reports.",
    no_args_is_help=True,
)
ai_app = typer.Typer(
    help="AI-powered architecture extraction and analysis.",
    no_args_is_help=True,
)
staging_app = typer.Typer(
    help="Review AI-detected architecture items before committing to the workspace.",
    no_args_is_help=True,
)
workspace_app = typer.Typer(
    help="Manage workspace settings: watch folders, scan-all, etc.",
    no_args_is_help=True,
)

app.add_typer(enterprise_app, name="enterprise")
app.add_typer(data_app, name="data")
app.add_typer(solution_app, name="solution")
app.add_typer(generate_app, name="generate")
app.add_typer(ai_app, name="ai")
app.add_typer(staging_app, name="staging")
app.add_typer(workspace_app, name="workspace")

console = Console()


# ── Root callback: bare `strata` → launch TUI ──────────────────────────────────

@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    provider: str = typer.Option("auto", "--provider", help="AI provider override for TUI", hidden=True),
) -> None:
    """Launch the interactive TUI when called with no subcommand."""
    if ctx.invoked_subcommand is None:
        from .tui import launch_tui
        launch_tui(provider=provider)




def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _load() -> ArchitectureWorkspace:
    try:
        return load_workspace()
    except WorkspaceError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc


def _save(workspace: ArchitectureWorkspace) -> None:
    save_workspace(workspace)


# ── AI-assisted field extraction helpers ───────────────────────────────────────

def _workspace_ctx() -> dict | None:
    """Return a minimal workspace context dict for AI cross-reference resolution."""
    ws_root = find_workspace_root()
    if ws_root is None:
        return None
    try:
        ws = load_workspace()
        return {
            "domain_ids": [d.id for d in ws.data.domains],
            "capability_ids": [c.id for c in ws.enterprise.capabilities],
            "solution_ids": [s.id for s in ws.solutions],
            "application_ids": [a.id for a in ws.enterprise.applications],
        }
    except Exception:  # noqa: BLE001
        return None


def _ai_fill_fields(entity_type: str, prompt_text: str, provider: str = "auto") -> dict:
    """Call AI to extract structured fields from natural language. Prints a preview table."""
    agent = ArchitectureAgent(provider=provider)
    available, msg = agent.check_available()
    if not available:
        console.print(f"[red]AI provider not available:[/] {msg}")
        console.print("Run [cyan]strata ai status[/] or provide explicit flags (--name, --domain, etc.).")
        raise typer.Exit(code=1)
    console.print(f"[dim]AI ({msg}) extracting fields…[/]")
    try:
        fields = agent.extract_entity_fields(entity_type, prompt_text, _workspace_ctx())
    except AgentError as exc:
        console.print(f"[red]AI extraction failed:[/] {exc}")
        raise typer.Exit(code=1) from exc
    t = Table(title="Extracted from prompt", box=box.SIMPLE)
    t.add_column("Field", style="dim")
    t.add_column("Value")
    for k, v in fields.items():
        if v:
            t.add_row(k, str(v))
    console.print(t)
    return fields


# ── Top-level commands ─────────────────────────────────────────────────────────

@app.command("init")
def init(
    name: str = typer.Option(..., prompt=True, help="Organisation or project name"),
    description: str = typer.Option("", prompt=True, help="Short description"),
    cloud: str = typer.Option(
        "multi-cloud", help="Cloud provider: aws|azure|gcp|multi-cloud|on-premise|hybrid"
    ),
    path: str = typer.Option(".", help="Root directory for the workspace"),
) -> None:
    """Initialise a new architecture workspace."""
    root = Path(path).resolve()
    arch_dir = root / "architecture"
    if (arch_dir / "strata.yaml").exists():
        console.print("[yellow]Workspace already exists.[/]")
        raise typer.Exit()
    manifest = WorkspaceManifest(
        name=name,
        description=description or None,
        cloud_provider=cloud,  # type: ignore[arg-type]
    )
    workspace = ArchitectureWorkspace(manifest=manifest)
    save_workspace(workspace, root=root)
    console.print(
        Panel(
            f"[green]Workspace created:[/] [bold]{name}[/]\n"
            f"Path: [dim]{arch_dir}[/]\n\n"
            "Next steps:\n"
            "  [cyan]strata enterprise add-capability[/]    — define business capabilities\n"
            "  [cyan]strata data add-domain[/]               — model your data domains\n"
            "  [cyan]strata solution create[/]               — design a solution\n"
            "  [cyan]strata status[/]                        — workspace overview",
            title="Strata Workspace Ready",
            expand=False,
        )
    )


@app.command("status")
def status() -> None:
    """Show workspace overview — counts per architecture domain."""
    workspace = _load()
    print_workspace_status(console, workspace)


@app.command("validate")
def validate() -> None:
    """Validate cross-references across the full workspace."""
    workspace = _load()
    errors: list[str] = []
    warnings: list[str] = []
    cap_ids = {c.id for c in workspace.enterprise.capabilities}
    domain_ids = {d.id for d in workspace.data.domains}
    product_ids = {p.id for p in workspace.data.products}

    for application in workspace.enterprise.applications:
        for cid in application.capability_ids:
            if cid not in cap_ids:
                errors.append(
                    f"Application '{application.name}' references unknown capability '{cid}'"
                )
    for product in workspace.data.products:
        if product.domain_id not in domain_ids:
            errors.append(
                f"Data product '{product.name}' references unknown domain '{product.domain_id}'"
            )
    for flow in workspace.data.flows:
        if flow.source_domain not in domain_ids:
            warnings.append(
                f"Flow '{flow.name}': source '{flow.source_domain}' not modelled (external?)"
            )
        if flow.target_domain not in domain_ids:
            warnings.append(
                f"Flow '{flow.name}': target '{flow.target_domain}' not modelled (external?)"
            )
        if flow.data_product_id and flow.data_product_id not in product_ids:
            errors.append(
                f"Flow '{flow.name}' references unknown product '{flow.data_product_id}'"
            )
    for sol in workspace.solutions:
        comp_ids = {c.id for c in sol.components}
        for comp in sol.components:
            for dep in comp.dependencies:
                if dep not in comp_ids:
                    warnings.append(
                        f"Solution '{sol.name}' / component '{comp.name}' "
                        f"depends on unknown component '{dep}'"
                    )
        for cid in sol.business_capability_ids:
            if cid not in cap_ids:
                warnings.append(
                    f"Solution '{sol.name}' references unknown capability '{cid}'"
                )

    if not errors and not warnings:
        console.print("[green]✓ Workspace is valid. No issues found.[/]")
        return
    if warnings:
        t = Table(title="Warnings", box=box.SIMPLE)
        t.add_column("Warning")
        for w in warnings:
            t.add_row(f"[yellow]{w}[/]")
        console.print(t)
    if errors:
        t = Table(title="Errors", box=box.SIMPLE)
        t.add_column("Error")
        for e in errors:
            t.add_row(f"[red]{e}[/]")
        console.print(t)
        raise typer.Exit(code=1)


# ── Enterprise architecture ────────────────────────────────────────────────────

@enterprise_app.command("list-capabilities")
def list_capabilities() -> None:
    """List all business capabilities."""
    workspace = _load()
    caps = workspace.enterprise.capabilities
    if not caps:
        console.print("[dim]No capabilities yet. Use 'strata enterprise add-capability'.[/]")
        return
    t = Table(title="Business Capabilities", box=box.SIMPLE_HEAVY)
    t.add_column("ID", style="dim")
    t.add_column("Name", style="bold")
    t.add_column("Domain")
    t.add_column("Level")
    t.add_column("Maturity")
    t.add_column("Owner")
    for cap in caps:
        colour = {"strategic": "red", "core": "yellow", "supporting": "green"}.get(cap.level, "white")
        t.add_row(cap.id, cap.name, cap.domain, f"[{colour}]{cap.level}[/]", cap.maturity, cap.owner or "—")
    console.print(t)


@enterprise_app.command("add-capability")
def add_capability(
    name: str | None = typer.Option(None, "--name", help="Capability name"),
    domain: str | None = typer.Option(None, "--domain", help="Business domain (e.g. Payments)"),
    level: str = typer.Option("core", "--level", help="strategic | core | supporting"),
    owner: str = typer.Option("", "--owner", help="Owning team or person"),
    description: str = typer.Option("", "--description", help="Short description"),
    ai_prompt: str | None = typer.Option(None, "--prompt", "-p", help="Describe in natural language — AI fills missing fields"),
    ai_provider: str = typer.Option("auto", "--ai-provider", help="AI provider for --prompt: auto | copilot | claude | github | codex | openai | ollama"),
) -> None:
    """Add a business capability to the enterprise architecture."""
    if ai_prompt:
        fields = _ai_fill_fields("capability", ai_prompt, ai_provider)
        name = name or fields.get("name") or ""
        domain = domain or fields.get("domain") or ""
        level = fields.get("level") or level
        owner = owner or fields.get("owner") or ""
        description = description or fields.get("description") or ""
    if not name:
        name = typer.prompt("Capability name")
    if not domain:
        domain = typer.prompt("Business domain")
    workspace = _load()
    cap_id = _slug(name)
    existing = {c.id for c in workspace.enterprise.capabilities}
    if cap_id in existing:
        cap_id = f"{cap_id}-{len(existing)}"
    workspace.enterprise.capabilities.append(
        BusinessCapability(
            id=cap_id, name=name, domain=domain,
            level=level,  # type: ignore[arg-type]
            owner=owner or None, description=description or None,
        )
    )
    _save(workspace)
    console.print(f"[green]Added capability:[/] {name}  [dim]({cap_id})[/]")


@enterprise_app.command("list-applications")
def list_applications() -> None:
    """List all applications in the portfolio."""
    workspace = _load()
    apps = workspace.enterprise.applications
    if not apps:
        console.print("[dim]No applications yet. Use 'strata enterprise add-application'.[/]")
        return
    t = Table(title="Application Portfolio", box=box.SIMPLE_HEAVY)
    t.add_column("ID", style="dim")
    t.add_column("Name", style="bold")
    t.add_column("Status")
    t.add_column("Hosting")
    t.add_column("Criticality")
    t.add_column("Owner")
    for a in apps:
        sc = {"active": "green", "retiring": "yellow", "planned": "cyan", "decommissioned": "red"}.get(a.status, "white")
        cc = {"critical": "red", "high": "yellow", "medium": "white", "low": "dim"}.get(a.criticality, "white")
        t.add_row(a.id, a.name, f"[{sc}]{a.status}[/]", a.hosting, f"[{cc}]{a.criticality}[/]", a.owner_team or "—")
    console.print(t)


@enterprise_app.command("add-application")
def add_application(
    name: str | None = typer.Option(None, "--name", help="Application name"),
    hosting: str = typer.Option("kubernetes", "--hosting", help="kubernetes | serverless | vm | managed-service | saas"),
    criticality: str = typer.Option("medium", "--criticality", help="low | medium | high | critical"),
    owner: str = typer.Option("", "--owner", help="Owning team"),
    status: str = typer.Option("active", "--status", help="active | retiring | planned | decommissioned"),
    description: str = typer.Option("", "--description", help="Short description"),
    ai_prompt: str | None = typer.Option(None, "--prompt", "-p", help="Describe in natural language — AI fills missing fields"),
    ai_provider: str = typer.Option("auto", "--ai-provider", help="AI provider for --prompt"),
) -> None:
    """Add an application to the portfolio."""
    if ai_prompt:
        fields = _ai_fill_fields("application", ai_prompt, ai_provider)
        name = name or fields.get("name") or ""
        hosting = fields.get("hosting") or hosting
        criticality = fields.get("criticality") or criticality
        owner = owner or fields.get("owner") or ""
        status = fields.get("status") or status
        description = description or fields.get("description") or ""
    if not name:
        name = typer.prompt("Application name")
    workspace = _load()
    app_id = _slug(name)
    existing = {a.id for a in workspace.enterprise.applications}
    if app_id in existing:
        app_id = f"{app_id}-{len(existing)}"
    workspace.enterprise.applications.append(
        Application(
            id=app_id, name=name,
            hosting=hosting,  # type: ignore[arg-type]
            criticality=criticality,  # type: ignore[arg-type]
            owner_team=owner or None,
            status=status,  # type: ignore[arg-type]
            description=description or None,
        )
    )
    _save(workspace)
    console.print(f"[green]Added application:[/] {name}  [dim]({app_id})[/]")


@enterprise_app.command("list-standards")
def list_standards() -> None:
    """List all technology standards."""
    workspace = _load()
    stds = workspace.enterprise.standards
    if not stds:
        console.print("[dim]No standards yet. Use 'strata enterprise add-standard'.[/]")
        return
    t = Table(title="Technology Standards", box=box.SIMPLE_HEAVY)
    t.add_column("ID", style="dim")
    t.add_column("Name", style="bold")
    t.add_column("Category")
    t.add_column("Status")
    t.add_column("Rationale")
    for s in stds:
        colour = {"adopt": "green", "trial": "cyan", "assess": "yellow", "hold": "red"}.get(s.status, "white")
        t.add_row(s.id, s.name, s.category, f"[{colour}]{s.status}[/]", s.rationale or "—")
    console.print(t)


@enterprise_app.command("add-standard")
def add_standard(
    name: str | None = typer.Option(None, "--name", help="Technology or tool name"),
    category: str | None = typer.Option(None, "--category", help="Category (e.g. messaging, database)"),
    status: str = typer.Option("assess", "--status", help="adopt | trial | assess | hold"),
    rationale: str = typer.Option("", "--rationale", help="Rationale for this status"),
    ai_prompt: str | None = typer.Option(None, "--prompt", "-p", help="Describe in natural language — AI fills missing fields"),
    ai_provider: str = typer.Option("auto", "--ai-provider", help="AI provider for --prompt"),
) -> None:
    """Add a technology standard to the tech radar."""
    if ai_prompt:
        fields = _ai_fill_fields("standard", ai_prompt, ai_provider)
        name = name or fields.get("name") or ""
        category = category or fields.get("category") or ""
        status = fields.get("status") or status
        rationale = rationale or fields.get("rationale") or ""
    if not name:
        name = typer.prompt("Technology name")
    if not category:
        category = typer.prompt("Category")
    workspace = _load()
    std_id = _slug(name)
    existing = {s.id for s in workspace.enterprise.standards}
    if std_id in existing:
        std_id = f"{std_id}-{len(existing)}"
    workspace.enterprise.standards.append(
        TechnologyStandard(
            id=std_id, name=name, category=category,
            status=status,  # type: ignore[arg-type]
            rationale=rationale or None,
        )
    )
    _save(workspace)
    console.print(f"[green]Added standard:[/] {name}  [{status}]")


@enterprise_app.command("tech-radar")
def tech_radar() -> None:
    """Display the technology radar (adopt / trial / assess / hold)."""
    workspace = _load()
    stds = workspace.enterprise.standards
    if not stds:
        console.print("[dim]No standards defined yet.[/]")
        return
    for ring in ["adopt", "trial", "assess", "hold"]:
        items = [s for s in stds if s.status == ring]
        if not items:
            continue
        colour = {"adopt": "green", "trial": "cyan", "assess": "yellow", "hold": "red"}[ring]
        console.print(f"\n[bold {colour}]● {ring.upper()}[/]")
        for s in items:
            suffix = f"  [dim]— {s.rationale}[/]" if s.rationale else ""
            console.print(f"  [dim]{s.category}[/]  {s.name}{suffix}")


# ── Data architecture ──────────────────────────────────────────────────────────

@data_app.command("list-domains")
def list_domains() -> None:
    """List all data domains."""
    workspace = _load()
    domains = workspace.data.domains
    if not domains:
        console.print("[dim]No data domains yet. Use 'strata data add-domain'.[/]")
        return
    t = Table(title="Data Domains", box=box.SIMPLE_HEAVY)
    t.add_column("ID", style="dim")
    t.add_column("Name", style="bold")
    t.add_column("Storage Pattern")
    t.add_column("Entities")
    t.add_column("Owner")
    for d in domains:
        t.add_row(d.id, d.name, d.storage_pattern, str(len(d.entities)), d.owner_team or "—")
    console.print(t)


@data_app.command("add-domain")
def add_domain(
    name: str | None = typer.Option(None, "--name", help="Domain name"),
    owner: str = typer.Option("", "--owner", help="Owning team"),
    storage: str = typer.Option("operational", "--storage", help="warehouse | lakehouse | operational | streaming | mixed"),
    description: str = typer.Option("", "--description", help="Short description"),
    ai_prompt: str | None = typer.Option(None, "--prompt", "-p", help="Describe in natural language — AI fills missing fields"),
    ai_provider: str = typer.Option("auto", "--ai-provider", help="AI provider for --prompt"),
) -> None:
    """Add a data domain."""
    if ai_prompt:
        fields = _ai_fill_fields("domain", ai_prompt, ai_provider)
        name = name or fields.get("name") or ""
        owner = owner or fields.get("owner") or ""
        storage = fields.get("storage_pattern") or fields.get("storage") or storage
        description = description or fields.get("description") or ""
    if not name:
        name = typer.prompt("Domain name")
    workspace = _load()
    domain_id = _slug(name)
    existing = {d.id for d in workspace.data.domains}
    if domain_id in existing:
        domain_id = f"{domain_id}-{len(existing)}"
    workspace.data.domains.append(
        DataDomain(
            id=domain_id, name=name, owner_team=owner or None,
            storage_pattern=storage,  # type: ignore[arg-type]
            description=description or None,
        )
    )
    _save(workspace)
    console.print(f"[green]Added domain:[/] {name}  [dim]({domain_id})[/]")


@data_app.command("list-products")
def list_products() -> None:
    """List all data products."""
    workspace = _load()
    products = workspace.data.products
    if not products:
        console.print("[dim]No data products yet. Use 'strata data add-product'.[/]")
        return
    t = Table(title="Data Products", box=box.SIMPLE_HEAVY)
    t.add_column("ID", style="dim")
    t.add_column("Name", style="bold")
    t.add_column("Domain")
    t.add_column("Output Port")
    t.add_column("SLA Tier")
    t.add_column("Owner")
    domain_map = {d.id: d.name for d in workspace.data.domains}
    for p in products:
        tc = {"platinum": "cyan", "gold": "yellow", "silver": "white", "bronze": "dim"}.get(p.sla_tier, "white")
        t.add_row(p.id, p.name, domain_map.get(p.domain_id, p.domain_id), p.output_port, f"[{tc}]{p.sla_tier}[/]", p.owner_team or "—")
    console.print(t)


@data_app.command("add-product")
def add_product(
    name: str | None = typer.Option(None, "--name", help="Data product name"),
    domain_id: str | None = typer.Option(None, "--domain-id", help="Parent domain ID"),
    output_port: str = typer.Option("api", "--output-port", help="api | files | streaming | sql | graphql"),
    sla_tier: str = typer.Option("silver", "--sla-tier", help="bronze | silver | gold | platinum"),
    owner: str = typer.Option("", "--owner", help="Owning team"),
    ai_prompt: str | None = typer.Option(None, "--prompt", "-p", help="Describe in natural language — AI fills missing fields"),
    ai_provider: str = typer.Option("auto", "--ai-provider", help="AI provider for --prompt"),
) -> None:
    """Add a data product."""
    if ai_prompt:
        fields = _ai_fill_fields("product", ai_prompt, ai_provider)
        name = name or fields.get("name") or ""
        domain_id = domain_id or fields.get("domain_id") or ""
        output_port = fields.get("output_port") or output_port
        sla_tier = fields.get("sla_tier") or sla_tier
        owner = owner or fields.get("owner") or ""
    if not name:
        name = typer.prompt("Data product name")
    if not domain_id:
        domain_id = typer.prompt("Parent domain ID")
    workspace = _load()
    prod_id = _slug(name)
    existing = {p.id for p in workspace.data.products}
    if prod_id in existing:
        prod_id = f"{prod_id}-{len(existing)}"
    workspace.data.products.append(
        DataProduct(
            id=prod_id, name=name, domain_id=domain_id,
            output_port=output_port,  # type: ignore[arg-type]
            sla_tier=sla_tier,  # type: ignore[arg-type]
            owner_team=owner or None,
        )
    )
    _save(workspace)
    console.print(f"[green]Added data product:[/] {name}  [dim]({prod_id})[/]")


@data_app.command("list-flows")
def list_flows() -> None:
    """List all data flows."""
    workspace = _load()
    flows = workspace.data.flows
    if not flows:
        console.print("[dim]No data flows yet. Use 'strata data add-flow'.[/]")
        return
    t = Table(title="Data Flows", box=box.SIMPLE_HEAVY)
    t.add_column("ID", style="dim")
    t.add_column("Name", style="bold")
    t.add_column("Source")
    t.add_column("→")
    t.add_column("Target")
    t.add_column("Mechanism")
    t.add_column("Classification")
    domain_map = {d.id: d.name for d in workspace.data.domains}
    for f in flows:
        cc = {"restricted": "red", "confidential": "yellow", "internal": "white", "public": "green"}.get(f.classification, "white")
        t.add_row(f.id, f.name, domain_map.get(f.source_domain, f.source_domain), "→",
                  domain_map.get(f.target_domain, f.target_domain),
                  f.mechanism, f"[{cc}]{f.classification}[/]")
    console.print(t)


@data_app.command("add-flow")
def add_flow(
    name: str | None = typer.Option(None, "--name", help="Flow name"),
    source: str | None = typer.Option(None, "--source", help="Source domain ID"),
    target: str | None = typer.Option(None, "--target", help="Target domain ID"),
    mechanism: str = typer.Option("api", "--mechanism", help="streaming | batch | api | cdc | file-transfer"),
    classification: str = typer.Option("internal", "--classification", help="public | internal | confidential | restricted"),
    ai_prompt: str | None = typer.Option(None, "--prompt", "-p", help="Describe in natural language — AI fills missing fields"),
    ai_provider: str = typer.Option("auto", "--ai-provider", help="AI provider for --prompt"),
) -> None:
    """Add a data flow between domains."""
    if ai_prompt:
        fields = _ai_fill_fields("flow", ai_prompt, ai_provider)
        name = name or fields.get("name") or ""
        source = source or fields.get("source_domain") or ""
        target = target or fields.get("target_domain") or ""
        mechanism = fields.get("mechanism") or mechanism
        classification = fields.get("classification") or classification
    if not name:
        name = typer.prompt("Flow name")
    if not source:
        source = typer.prompt("Source domain ID")
    if not target:
        target = typer.prompt("Target domain ID")
    workspace = _load()
    flow_id = _slug(name)
    existing = {f.id for f in workspace.data.flows}
    if flow_id in existing:
        flow_id = f"{flow_id}-{len(existing)}"
    workspace.data.flows.append(
        DataFlow(
            id=flow_id, name=name, source_domain=source, target_domain=target,
            mechanism=mechanism,  # type: ignore[arg-type]
            classification=classification,  # type: ignore[arg-type]
        )
    )
    _save(workspace)
    console.print(f"[green]Added flow:[/] {name}  [dim]{source} → {target} via {mechanism}[/]")


# ── Solution architecture ──────────────────────────────────────────────────────

@solution_app.command("list")
def list_solutions() -> None:
    """List all solution designs."""
    workspace = _load()
    solutions = workspace.solutions
    if not solutions:
        console.print("[dim]No solutions yet. Use 'strata solution create'.[/]")
        return
    t = Table(title="Solution Designs", box=box.SIMPLE_HEAVY)
    t.add_column("ID", style="dim")
    t.add_column("Name", style="bold")
    t.add_column("Pattern")
    t.add_column("Status")
    t.add_column("Components")
    t.add_column("ADRs")
    t.add_column("Target")
    for s in solutions:
        sc = {"approved": "green", "review": "yellow", "draft": "dim", "implemented": "cyan", "deprecated": "red"}.get(s.status, "white")
        t.add_row(s.id, s.name, s.pattern, f"[{sc}]{s.status}[/]", str(len(s.components)), str(len(s.adrs)), s.deployment_target)
    console.print(t)


@solution_app.command("create")
def create_solution(
    name: str | None = typer.Option(None, "--name", help="Solution name"),
    description: str = typer.Option("", "--description", help="Short description"),
    pattern: str = typer.Option("microservices", "--pattern", help="microservices | event-driven | api-gateway | layered | serverless | modular-monolith | data-mesh"),
    target: str = typer.Option("multi-cloud", "--target", help="aws | azure | gcp | multi-cloud | on-premise | hybrid"),
    ai_prompt: str | None = typer.Option(None, "--prompt", "-p", help="Describe in natural language — AI fills missing fields"),
    ai_provider: str = typer.Option("auto", "--ai-provider", help="AI provider for --prompt"),
) -> None:
    """Create a new solution design."""
    if ai_prompt:
        fields = _ai_fill_fields("solution", ai_prompt, ai_provider)
        name = name or fields.get("name") or ""
        description = description or fields.get("description") or ""
        pattern = fields.get("pattern") or pattern
        target = fields.get("deployment_target") or fields.get("target") or target
    if not name:
        name = typer.prompt("Solution name")
    workspace = _load()
    sol_id = _slug(name)
    existing = {s.id for s in workspace.solutions}
    if sol_id in existing:
        sol_id = f"{sol_id}-{len(existing)}"
    workspace.solutions.append(
        SolutionDesign(
            id=sol_id, name=name, description=description or None,
            pattern=pattern,  # type: ignore[arg-type]
            deployment_target=target,  # type: ignore[arg-type]
        )
    )
    _save(workspace)
    console.print(
        Panel(
            f"[green]Solution created:[/] [bold]{name}[/]  [dim]({sol_id})[/]\n"
            f"Pattern: [cyan]{pattern}[/]  Target: [cyan]{target}[/]\n\n"
            f"  [cyan]strata solution add-component {sol_id}[/]   — add components\n"
            f"  [cyan]strata solution add-adr {sol_id}[/]          — record a decision\n"
            f"  [cyan]strata generate solution-diagram {sol_id}[/] — visualise",
            title="Solution Design Ready",
            expand=False,
        )
    )


@solution_app.command("show")
def show_solution(
    solution_id: str = typer.Argument(..., help="Solution ID"),
) -> None:
    """Show full details of a solution design."""
    workspace = _load()
    sol = next((s for s in workspace.solutions if s.id == solution_id), None)
    if sol is None:
        console.print(f"[red]Solution '{solution_id}' not found.[/]")
        raise typer.Exit(code=1)
    console.print(
        Panel(
            f"{sol.description or ''}\n"
            f"Pattern: [cyan]{sol.pattern}[/]  "
            f"Target: [cyan]{sol.deployment_target}[/]  "
            f"Status: [yellow]{sol.status}[/]",
            title=f"[bold]{sol.name}[/]  [dim]({sol.id})[/]",
            expand=False,
        )
    )
    if sol.components:
        t = Table(title="Components", box=box.SIMPLE)
        t.add_column("ID", style="dim")
        t.add_column("Name", style="bold")
        t.add_column("Type")
        t.add_column("Technology")
        t.add_column("Hosting")
        t.add_column("Dependencies")
        for c in sol.components:
            t.add_row(c.id, c.name, c.type, c.technology or "—", c.hosting, ", ".join(c.dependencies) or "—")
        console.print(t)
    if sol.adrs:
        t = Table(title="Architecture Decision Records", box=box.SIMPLE)
        t.add_column("ID", style="dim")
        t.add_column("Title", style="bold")
        t.add_column("Status")
        t.add_column("Date")
        for a in sol.adrs:
            sc = {"accepted": "green", "proposed": "yellow", "deprecated": "red", "superseded": "dim"}.get(a.status, "white")
            t.add_row(a.id, a.title, f"[{sc}]{a.status}[/]", a.date or "—")
        console.print(t)


@solution_app.command("add-component")
def add_component(
    solution_id: str = typer.Argument(..., help="Solution ID"),
    name: str = typer.Option(..., prompt=True, help="Component name"),
    comp_type: str = typer.Option(
        "service", prompt=True,
        help="service | gateway | database | queue | cache | cdn | identity | storage | external"
    ),
    technology: str = typer.Option("", prompt=True, help="Technology (e.g. PostgreSQL, Kafka)"),
    hosting: str = typer.Option(
        "kubernetes", prompt=True,
        help="kubernetes | serverless | managed-service | saas | external"
    ),
    description: str = typer.Option("", help="Short description"),
) -> None:
    """Add a component to a solution design."""
    workspace = _load()
    sol = next((s for s in workspace.solutions if s.id == solution_id), None)
    if sol is None:
        console.print(f"[red]Solution '{solution_id}' not found.[/]")
        raise typer.Exit(code=1)
    comp_id = _slug(name)
    existing = {c.id for c in sol.components}
    if comp_id in existing:
        comp_id = f"{comp_id}-{len(existing)}"
    sol.components.append(
        Component(
            id=comp_id, name=name,
            type=comp_type,  # type: ignore[arg-type]
            technology=technology or None,
            hosting=hosting,  # type: ignore[arg-type]
            description=description or None,
        )
    )
    _save(workspace)
    console.print(f"[green]Added component:[/] {name} [{comp_type}] → {sol.name}")


@solution_app.command("add-adr")
def add_adr(
    solution_id: str = typer.Argument(..., help="Solution ID"),
    title: str = typer.Option(..., prompt=True, help="ADR title"),
    context: str = typer.Option("", prompt=True, help="Context and problem statement"),
    decision: str = typer.Option("", prompt=True, help="Decision taken"),
    consequences: str = typer.Option("", help="Consequences / trade-offs"),
    status: str = typer.Option("proposed", help="proposed | accepted | deprecated | superseded"),
) -> None:
    """Record an Architecture Decision Record (ADR) for a solution."""
    workspace = _load()
    sol = next((s for s in workspace.solutions if s.id == solution_id), None)
    if sol is None:
        console.print(f"[red]Solution '{solution_id}' not found.[/]")
        raise typer.Exit(code=1)
    adr_id = f"ADR-{len(sol.adrs) + 1:03d}"
    sol.adrs.append(
        ArchitectureDecisionRecord(
            id=adr_id, title=title,
            context=context or None, decision=decision or None,
            consequences=consequences or None,
            status=status,  # type: ignore[arg-type]
            date=str(_date.today()),
        )
    )
    _save(workspace)
    console.print(f"[green]Recorded:[/] {adr_id} — {title}")


@solution_app.command("list-adrs")
def list_adrs(
    solution_id: str = typer.Argument(..., help="Solution ID"),
) -> None:
    """List Architecture Decision Records for a solution."""
    workspace = _load()
    sol = next((s for s in workspace.solutions if s.id == solution_id), None)
    if sol is None:
        console.print(f"[red]Solution '{solution_id}' not found.[/]")
        raise typer.Exit(code=1)
    if not sol.adrs:
        console.print("[dim]No ADRs yet. Use 'strata solution add-adr'.[/]")
        return
    for adr in sol.adrs:
        sc = {"accepted": "green", "proposed": "yellow", "deprecated": "red", "superseded": "dim"}.get(adr.status, "white")
        console.print(
            Panel(
                f"[bold]Context:[/] {adr.context or 'n/a'}\n"
                f"[bold]Decision:[/] {adr.decision or 'n/a'}\n"
                f"[bold]Consequences:[/] {adr.consequences or 'n/a'}",
                title=f"[{sc}]{adr.id}[/] — {adr.title}  [dim]{adr.date or ''}[/]",
            )
        )


# ── Generate outputs ───────────────────────────────────────────────────────────

@generate_app.command("capability-map")
def gen_capability_map(
    output: str = typer.Option("capability-map.mmd", help="Output Mermaid file"),
) -> None:
    """Generate a Mermaid business capability map."""
    workspace = _load()
    render_capability_map(workspace, Path(output))
    console.print(f"[green]Capability map written:[/] {output}")


@generate_app.command("data-flow-map")
def gen_data_flow_map(
    output: str = typer.Option("data-flow-map.mmd", help="Output Mermaid file"),
) -> None:
    """Generate a Mermaid data flow map."""
    workspace = _load()
    render_data_flow_map(workspace, Path(output))
    console.print(f"[green]Data flow map written:[/] {output}")


@generate_app.command("solution-diagram")
def gen_solution_diagram(
    solution_id: str = typer.Argument(..., help="Solution ID"),
    output: str = typer.Option("", help="Output Mermaid file (default: <id>-diagram.mmd)"),
) -> None:
    """Generate a Mermaid solution architecture diagram."""
    workspace = _load()
    sol = next((s for s in workspace.solutions if s.id == solution_id), None)
    if sol is None:
        console.print(f"[red]Solution '{solution_id}' not found.[/]")
        raise typer.Exit(code=1)
    out_path = Path(output) if output else Path(f"{solution_id}-diagram.mmd")
    render_solution_diagram(sol, out_path)
    console.print(f"[green]Solution diagram written:[/] {out_path}")


@generate_app.command("report")
def gen_report(
    output: str = typer.Option("architecture-report.json", help="Output JSON report path"),
) -> None:
    """Generate a full architecture report as JSON."""
    workspace = _load()
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace": workspace.manifest.model_dump(exclude_none=True),
        "summary": {
            "capabilities": len(workspace.enterprise.capabilities),
            "applications": len(workspace.enterprise.applications),
            "standards": len(workspace.enterprise.standards),
            "data_domains": len(workspace.data.domains),
            "data_products": len(workspace.data.products),
            "data_flows": len(workspace.data.flows),
            "solutions": len(workspace.solutions),
        },
        "detail": workspace.model_dump(exclude_none=True),
    }
    Path(output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    console.print(f"[green]Report written:[/] {output}")


# ── PSA markdown import ──────────────────────────────────────────────────────

def _merge_workspace(
    target: ArchitectureWorkspace,
    source: ArchitectureWorkspace,
) -> dict[str, int]:
    """Merge *source* into *target* (additive, skips duplicate IDs). Returns counts."""
    counts: dict[str, int] = {
        "capabilities": 0, "applications": 0, "standards": 0,
        "domains": 0, "products": 0, "flows": 0, "solutions": 0,
    }
    existing_caps = {c.id for c in target.enterprise.capabilities}
    for item in source.enterprise.capabilities:
        if item.id not in existing_caps:
            target.enterprise.capabilities.append(item)
            counts["capabilities"] += 1

    existing_apps = {a.id for a in target.enterprise.applications}
    for item in source.enterprise.applications:
        if item.id not in existing_apps:
            target.enterprise.applications.append(item)
            counts["applications"] += 1

    existing_stds = {s.id for s in target.enterprise.standards}
    for item in source.enterprise.standards:
        if item.id not in existing_stds:
            target.enterprise.standards.append(item)
            counts["standards"] += 1

    existing_domains = {d.id for d in target.data.domains}
    for item in source.data.domains:
        if item.id not in existing_domains:
            target.data.domains.append(item)
            counts["domains"] += 1

    existing_products = {p.id for p in target.data.products}
    for item in source.data.products:
        if item.id not in existing_products:
            target.data.products.append(item)
            counts["products"] += 1

    existing_flows = {f.id for f in target.data.flows}
    for item in source.data.flows:
        if item.id not in existing_flows:
            target.data.flows.append(item)
            counts["flows"] += 1

    existing_sols = {s.id for s in target.solutions}
    for item in source.solutions:
        if item.id not in existing_sols:
            target.solutions.append(item)
            counts["solutions"] += 1

    return counts


@app.command("import")
def import_psa(
    file: str = typer.Argument(..., help="Path to PSA markdown file (.md)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview what would be imported without writing"),
    init_if_missing: bool = typer.Option(True, help="Auto-init workspace if none exists"),
) -> None:
    """Import a PSA (Problem-Solution-Architecture) markdown file.

    Parses the structured markdown file and merges capabilities, applications,
    data domains, flows, and solutions into the current workspace.
    Use --dry-run to preview without writing.

    \b
    PSA markdown format: see examples/psa-example.md
    """
    src_path = Path(file).resolve()
    if not src_path.exists():
        console.print(f"[red]File not found:[/] {src_path}")
        raise typer.Exit(code=1)

    console.print(f"Parsing [cyan]{src_path.name}[/] …")
    try:
        source = parse_psa_markdown(src_path)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Parse error:[/] {exc}")
        raise typer.Exit(code=1) from exc

    # Summary of what was found
    found = {
        "capabilities": len(source.enterprise.capabilities),
        "applications": len(source.enterprise.applications),
        "standards": len(source.enterprise.standards),
        "domains": len(source.data.domains),
        "products": len(source.data.products),
        "flows": len(source.data.flows),
        "solutions": len(source.solutions),
    }
    t = Table(title="Found in PSA file", box=box.SIMPLE)
    t.add_column("Type")
    t.add_column("Count", justify="right")
    for k, v in found.items():
        if v:
            t.add_row(k.replace("_", " ").title(), str(v))
    console.print(t)

    if dry_run:
        console.print("[yellow]Dry run — nothing written.[/]")
        return

    # Load or init workspace
    ws_root = find_workspace_root()
    if ws_root is None:
        if not init_if_missing:
            console.print("[red]No workspace found. Run 'strata init' first.[/]")
            raise typer.Exit(code=1)
        console.print("[dim]No workspace found — creating one from the PSA file manifest.[/]")
        workspace = ArchitectureWorkspace(manifest=source.manifest)
        save_workspace(workspace)
    else:
        workspace = _load()

    counts = _merge_workspace(workspace, source)
    _save(workspace)

    added = {k: v for k, v in counts.items() if v}
    if added:
        summary = "  ".join(f"[green]+{v}[/] {k}" for k, v in added.items())
        console.print(f"Imported: {summary}")
    else:
        console.print("[yellow]Nothing new to import (all IDs already exist).[/]")


# ── AI sub-app ────────────────────────────────────────────────────────────────

@ai_app.command("status")
def ai_status() -> None:
    """Show the AI provider configuration and availability of all providers."""
    agent = ArchitectureAgent()
    active_provider = agent._effective_provider()  # noqa: SLF001

    t = Table(title="AI Providers", box=box.SIMPLE_HEAVY)
    t.add_column("Provider")
    t.add_column("Status")
    t.add_column("Details")

    provider_labels = {
        "copilot": "GitHub Copilot",
        "claude":  "Claude (Claude Code)",
        "github":  "GitHub Models",
        "codex":   "OpenAI Codex",
        "openai":  "OpenAI",
        "ollama":  "Ollama (local)",
    }
    for name, ok, msg in agent.check_all():
        icon = "[green]\u2713[/]" if ok else "[dim]\u2717[/]"
        label = provider_labels.get(name, name)
        active_marker = " [yellow](active)[/]" if name == active_provider else ""
        t.add_row(f"{label}{active_marker}", icon, msg)
    console.print(t)

    if not agent.check_available()[0]:
        console.print(
            "\n[yellow]No provider is ready.[/] Quick setup:\n"
            "  [cyan]strata ui[/] then [cyan]/model copilot[/]  # GitHub Copilot OAuth (auto flow)\n"
            "  [cyan]claude auth login[/]                      # Claude Code OAuth\n"
            "  [cyan]codex login[/]                            # Codex CLI OAuth\n"
            "  [dim]Note: github/openai/ollama are visible but disabled by OAuth-only policy.[/dim]"
        )


@ai_app.command("configure")
def ai_configure(
    provider: str = typer.Option(
        "", "--provider",
        help="Default provider: copilot | claude | codex (github/openai/ollama are policy-disabled)",
    ),
    github_token: str = typer.Option(
        "", "--github-token",
        help="GitHub token (overrides GITHUB_TOKEN env and gh CLI \u2014 usually not needed)",
    ),
    copilot_model: str = typer.Option("", "--copilot-model", help="Copilot model (default: gpt-4o)"),
    claude_model: str = typer.Option("", "--claude-model", help="Claude model (default: claude-opus-4-5)"),
    anthropic_key: str = typer.Option(
        "", "--anthropic-key",
        help="Anthropic API key (leave blank to use Claude Code credentials or ANTHROPIC_API_KEY)",
    ),
    github_model: str = typer.Option("", "--github-model", help="GitHub Models model (default: gpt-4o-mini)"),
    openai_key: str = typer.Option("", "--openai-key", help="OpenAI API key"),
    openai_model: str = typer.Option("", "--openai-model", help="OpenAI model (default: gpt-4o)"),
    ollama_host: str = typer.Option("", "--ollama-host", help="Ollama host URL"),
    ollama_model: str = typer.Option("", "--ollama-model", help="Ollama model name"),
) -> None:
    """Configure the AI provider for 'strata ai extract'.

    \b
    OAuth/CLI providers (enabled):

      GitHub Copilot  \u2014 just run: gh auth login
        strata ai configure --provider copilot

      Claude Code     \u2014 just run: claude auth login
        strata ai configure --provider claude

      Codex CLI       \u2014 just run: codex login
        strata ai configure --provider codex

    \b
    Note: github/openai/ollama providers are visible but disabled by OAuth-only policy.
    """
    cfg: dict[str, str] = {}
    if provider:
        cfg["provider"] = provider.strip().lower()
    if github_token:
        cfg["github_token"] = github_token.strip()
    if copilot_model:
        cfg["copilot_model"] = copilot_model.strip()
    if claude_model:
        cfg["claude_model"] = claude_model.strip()
    if anthropic_key:
        cfg["anthropic_api_key"] = anthropic_key.strip()
    if github_model:
        cfg["github_model"] = github_model.strip()
    if openai_key:
        cfg["openai_api_key"] = openai_key.strip()
    if openai_model:
        cfg["openai_model"] = openai_model.strip()
    if ollama_host:
        cfg["ollama_host"] = ollama_host.strip()
    if ollama_model:
        cfg["ollama_model"] = ollama_model.strip()

    if not cfg:
        console.print("[yellow]No changes specified. Use --provider, --copilot-model, --claude-model, etc.[/]")
        return

    save_config(cfg)
    console.print("[green]AI configuration saved[/] \u2192 ~/.strata/config.yaml")

    agent = ArchitectureAgent()
    available, msg = agent.check_available()
    icon = "[green]\u2713[/]" if available else "[yellow]\u26a0[/]"
    console.print(f"{icon} Active provider: {msg}")


@ai_app.command("extract")
def ai_extract(
    file: str = typer.Argument(..., help="Path to any document (.md, .txt, .pdf text etc.)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview extracted architecture without writing"),
    provider: str = typer.Option("auto", "--provider", help="Override AI provider: copilot|claude|github|codex|openai|ollama"),
    init_if_missing: bool = typer.Option(True, help="Auto-init workspace if none exists"),
    stage: bool = typer.Option(True, "--stage/--no-stage", help="Stage items for manual review (default) or merge directly"),
) -> None:
    """Use AI to extract architecture from any freeform document.

    Unlike 'strata import', this command accepts any free-form text — meeting notes,
    RFC documents, solution briefs, etc. — and uses an LLM to identify
    capabilities, applications, domains, and solutions.

    By default, detected items are placed in the staging area for manual review.
    Use --no-stage to merge directly into the workspace.
    Run 'strata staging list' to review staged items.

    Requires a configured AI provider. Run 'strata ai status' to check.
    """
    src_path = Path(file).resolve()
    if not src_path.exists():
        console.print(f"[red]File not found:[/] {src_path}")
        raise typer.Exit(code=1)

    agent = ArchitectureAgent(provider=provider)
    available, msg = agent.check_available()
    if not available:
        console.print(f"[red]AI provider not available:[/] {msg}")
        console.print("Run [cyan]strata ai status[/] to see all available providers.")
        raise typer.Exit(code=1)

    console.print(f"Using [cyan]{msg}[/]")
    console.print(f"Extracting from [cyan]{src_path.name}[/] …")

    text = src_path.read_text(encoding="utf-8", errors="replace")
    try:
        source = agent.extract_from_text(text, workspace_name=src_path.stem)
    except AgentError as exc:
        console.print(f"[red]AI extraction failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    # Summary
    found = {
        "capabilities": len(source.enterprise.capabilities),
        "applications": len(source.enterprise.applications),
        "standards": len(source.enterprise.standards),
        "domains": len(source.data.domains),
        "products": len(source.data.products),
        "flows": len(source.data.flows),
        "solutions": len(source.solutions),
    }
    t = Table(title="AI extracted", box=box.SIMPLE)
    t.add_column("Type")
    t.add_column("Count", justify="right")
    for k, v in found.items():
        if v:
            t.add_row(k.replace("_", " ").title(), str(v))
    console.print(t)

    if dry_run:
        console.print("[yellow]Dry run — nothing written.[/]")
        return

    if stage:
        # Flatten extracted workspace into staged items for review
        existing = load_staging()
        new_items: list[StagedItem] = []
        src_name = Path(file).name

        def _stage_objs(entity_type: str, objs: list) -> None:  # type: ignore[type-arg]
            for obj in objs:
                sid = next_staging_id(existing + new_items)
                new_items.append(StagedItem(
                    id=sid, entity=entity_type,
                    fields=obj.model_dump(exclude_none=True),
                    source=src_name,
                ))

        _stage_objs("capability", source.enterprise.capabilities)
        _stage_objs("application", source.enterprise.applications)
        _stage_objs("standard", source.enterprise.standards)
        _stage_objs("domain", source.data.domains)
        _stage_objs("product", source.data.products)
        _stage_objs("flow", source.data.flows)
        _stage_objs("solution", source.solutions)

        if not new_items:
            console.print("[yellow]Nothing extracted to stage.[/]")
            return

        save_staging(existing + new_items)
        console.print(
            f"[green]Staged {len(new_items)} item(s).[/] "
            "Run [cyan]strata staging list[/] to review."
        )
        return

    # --no-stage: merge directly into workspace
    ws_root = find_workspace_root()
    if ws_root is None:
        if not init_if_missing:
            console.print("[red]No workspace found. Run 'strata init' first.[/]")
            raise typer.Exit(code=1)
        console.print("[dim]No workspace — creating from extracted manifest.[/]")
        workspace = ArchitectureWorkspace(manifest=source.manifest)
        save_workspace(workspace)
    else:
        workspace = _load()

    counts = _merge_workspace(workspace, source)
    _save(workspace)

    added = {k: v for k, v in counts.items() if v}
    if added:
        summary = "  ".join(f"[green]+{v}[/] {k}" for k, v in added.items())
        console.print(f"Merged: {summary}")
    else:
        console.print("[yellow]Nothing new (all IDs already exist).[/]")




# ── Entity-write helper (shared by ask + staging accept) ──────────────────────

def _write_entity(
    entity: str, fields: dict, workspace: ArchitectureWorkspace
) -> str | None:
    """Write a single entity dict into *workspace* in-place.

    Returns an error message string on failure, or ``None`` on success.
    """
    if entity == "capability":
        n = fields.get("name", "")
        if not n:
            return "AI did not extract a name."
        eid = _slug(n)
        if eid in {c.id for c in workspace.enterprise.capabilities}:
            eid = f"{eid}-{len(workspace.enterprise.capabilities)}"
        workspace.enterprise.capabilities.append(BusinessCapability(
            id=eid, name=n,
            domain=fields.get("domain", ""),
            level=fields.get("level", "core"),  # type: ignore[arg-type]
            owner=fields.get("owner") or None,
            description=fields.get("description") or None,
        ))

    elif entity == "application":
        n = fields.get("name", "")
        if not n:
            return "AI did not extract a name."
        eid = _slug(n)
        if eid in {a.id for a in workspace.enterprise.applications}:
            eid = f"{eid}-{len(workspace.enterprise.applications)}"
        workspace.enterprise.applications.append(Application(
            id=eid, name=n,
            hosting=fields.get("hosting", "kubernetes"),  # type: ignore[arg-type]
            criticality=fields.get("criticality", "medium"),  # type: ignore[arg-type]
            owner_team=fields.get("owner") or None,
            status=fields.get("status", "active"),  # type: ignore[arg-type]
            description=fields.get("description") or None,
        ))

    elif entity == "standard":
        n = fields.get("name", "")
        if not n:
            return "AI did not extract a name."
        eid = _slug(n)
        if eid in {s.id for s in workspace.enterprise.standards}:
            eid = f"{eid}-{len(workspace.enterprise.standards)}"
        workspace.enterprise.standards.append(TechnologyStandard(
            id=eid, name=n,
            category=fields.get("category", ""),
            status=fields.get("status", "assess"),  # type: ignore[arg-type]
            rationale=fields.get("rationale") or None,
        ))

    elif entity == "domain":
        n = fields.get("name", "")
        if not n:
            return "AI did not extract a name."
        eid = _slug(n)
        if eid in {d.id for d in workspace.data.domains}:
            eid = f"{eid}-{len(workspace.data.domains)}"
        workspace.data.domains.append(DataDomain(
            id=eid, name=n,
            owner_team=fields.get("owner") or None,
            storage_pattern=fields.get("storage_pattern", "operational"),  # type: ignore[arg-type]
            description=fields.get("description") or None,
        ))

    elif entity == "product":
        n = fields.get("name", "")
        did = fields.get("domain_id", "")
        if not n or not did:
            return "Missing name or domain_id."
        eid = _slug(n)
        if eid in {p.id for p in workspace.data.products}:
            eid = f"{eid}-{len(workspace.data.products)}"
        workspace.data.products.append(DataProduct(
            id=eid, name=n, domain_id=did,
            output_port=fields.get("output_port", "api"),  # type: ignore[arg-type]
            sla_tier=fields.get("sla_tier", "silver"),  # type: ignore[arg-type]
            owner_team=fields.get("owner") or None,
        ))

    elif entity == "flow":
        n = fields.get("name", "")
        src = fields.get("source_domain", "")
        tgt = fields.get("target_domain", "")
        if not n or not src or not tgt:
            return "Missing name, source_domain, or target_domain."
        eid = _slug(n)
        if eid in {f.id for f in workspace.data.flows}:
            eid = f"{eid}-{len(workspace.data.flows)}"
        workspace.data.flows.append(DataFlow(
            id=eid, name=n, source_domain=src, target_domain=tgt,
            mechanism=fields.get("mechanism", "api"),  # type: ignore[arg-type]
            classification=fields.get("classification", "internal"),  # type: ignore[arg-type]
        ))

    elif entity == "solution":
        n = fields.get("name", "")
        if not n:
            return "AI did not extract a name."
        eid = _slug(n)
        if eid in {s.id for s in workspace.solutions}:
            eid = f"{eid}-{len(workspace.solutions)}"
        workspace.solutions.append(SolutionDesign(
            id=eid, name=n,
            description=fields.get("description") or None,
            pattern=fields.get("pattern", "microservices"),  # type: ignore[arg-type]
            deployment_target=fields.get("deployment_target", "multi-cloud"),  # type: ignore[arg-type]
        ))

    else:
        return f"Unknown entity type: {entity!r}"

    return None


@app.command("ask")
def ask(
    prompt: str = typer.Argument(..., help="Plain English — what to add to the workspace"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing"),
    provider: str = typer.Option("auto", "--provider", help="AI provider: auto | copilot | claude | github | codex | openai | ollama"),
) -> None:
    """Add architecture artefacts using plain English.

    \b
    Examples:
      strata ask "Order Management is a core Commerce capability, owned by the Commerce Team"
      strata ask "Kafka is adopted for messaging — proven for durable event streaming"
      strata ask "We need an Orders data domain for the Commerce team using operational storage"
      strata ask "Create an API Platform solution using api-gateway pattern on AWS"
      strata ask "Add Order Service as a critical Kubernetes app owned by Commerce Team"
    """
    agent = ArchitectureAgent(provider=provider)
    available, msg = agent.check_available()
    if not available:
        console.print(f"[red]AI provider not available:[/] {msg}")
        console.print("Run [cyan]strata ai status[/] to check providers.")
        raise typer.Exit(code=1)
    console.print(f"[dim]Using {msg}…[/]")
    try:
        result = agent.classify_and_extract(prompt, _workspace_ctx())
    except AgentError as exc:
        console.print(f"[red]AI failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    entity = result.get("entity", "")
    fields = result.get("fields", {})
    valid_entities = ("capability", "application", "standard", "domain", "product", "flow", "solution")
    if entity not in valid_entities:
        console.print(f"[red]Could not classify intent.[/] AI returned entity='{entity}'")
        console.print("Try being more specific, e.g. 'Add Kafka as a tech standard'")
        raise typer.Exit(code=1)

    t = Table(title=f"Add {entity}", box=box.SIMPLE)
    t.add_column("Field", style="dim")
    t.add_column("Value")
    for k, v in fields.items():
        if v:
            t.add_row(k, str(v))
    console.print(t)

    if dry_run:
        console.print("[yellow]Dry run — nothing written.[/]")
        return

    workspace = _load()

    err = _write_entity(entity, fields, workspace)
    if err:
        console.print(f"[red]{err}[/]")
        raise typer.Exit(code=1)

    _save(workspace)
    console.print(f"[green]Added {entity}:[/] [bold]{fields.get('name', '')}[/]")



# ═══════════════════════════════════════════════════════════════════════════════
# strata scan
# ═══════════════════════════════════════════════════════════════════════════════

@app.command("scan")
def scan(
    path: str = typer.Argument(..., help="File or folder to scan for architecture artefacts"),
    glob: str = typer.Option("**/*.md", "--glob", help="Glob pattern when scanning a folder"),
    provider: str = typer.Option("auto", "--provider", help="AI provider: auto|copilot|claude|codex|openai|ollama"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be staged without writing"),
) -> None:
    """Scan markdown files for architecture artefacts and stage them for review.

    \b
    Examples:
      strata scan ./docs/
      strata scan ./docs/architecture.md
      strata scan ./docs/ --glob "**/*.txt"
      strata scan ./docs/ --dry-run
    """
    agent = ArchitectureAgent(provider=provider)
    available, msg = agent.check_available()
    if not available:
        console.print(f"[red]AI provider not available:[/] {msg}")
        console.print("Run [cyan]strata ai status[/] to check providers.")
        raise typer.Exit(code=1)
    console.print(f"[dim]Using {msg}[/]")

    target = Path(path).resolve()
    if not target.exists():
        console.print(f"[red]Path not found:[/] {target}")
        raise typer.Exit(code=1)

    files: list[Path] = []
    if target.is_file():
        files = [target]
    else:
        files = sorted(target.glob(glob))

    if not files:
        console.print(f"[yellow]No files matched[/] {glob!r} in {target}")
        return

    ctx = _workspace_ctx()
    existing = load_staging()
    all_found: list[tuple[Path, list[dict]]] = []

    for fp in files:
        text = fp.read_text(encoding="utf-8", errors="replace")
        rel = str(fp.relative_to(target) if target.is_dir() else fp.name)
        console.print(f"  Scanning [cyan]{rel}[/] …")
        try:
            found = agent.scan_document(text, source_name=rel, workspace_context=ctx)
        except Exception as exc:
            console.print(f"  [yellow]Warning:[/] {exc}")
            found = []
        if found:
            all_found.append((fp, found))
            console.print(f"  [green]+{len(found)}[/] item(s) detected")

    total = sum(len(f) for _, f in all_found)
    if total == 0:
        console.print("[yellow]No architecture artefacts found.[/]")
        return

    t = Table(title=f"Scan results ({total} items)", box=box.SIMPLE)
    t.add_column("File", style="dim")
    t.add_column("Entity")
    t.add_column("Name")
    for fp, items in all_found:
        rel = fp.name
        for item in items:
            entity = item.get("entity", "?")
            name = item.get("fields", {}).get("name", "—")
            t.add_row(rel, entity, name)
    console.print(t)

    if dry_run:
        console.print(f"[yellow]Dry run — {total} item(s) not staged.[/]")
        return

    new_items: list[StagedItem] = []
    for fp, items in all_found:
        rel = str(fp.relative_to(target) if target.is_dir() else fp.name)
        for item in items:
            sid = next_staging_id(existing + new_items)
            new_items.append(StagedItem(
                id=sid,
                entity=item.get("entity", "unknown"),
                fields=item.get("fields", {}),
                source=rel,
            ))

    save_staging(existing + new_items)
    console.print(
        f"[green]Staged {len(new_items)} item(s).[/] "
        "Run [cyan]strata staging list[/] to review."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# strata staging sub-app
# ═══════════════════════════════════════════════════════════════════════════════

def _staging_index(items: list[StagedItem], id_or_index: str) -> int | None:
    """Return the list index for an item by its staging ID or 1-based integer index."""
    if id_or_index.isdigit():
        idx = int(id_or_index) - 1
        if 0 <= idx < len(items):
            return idx
        return None
    for i, item in enumerate(items):
        if item.id == id_or_index:
            return i
    return None


@staging_app.command("list")
def staging_list(
    status: str = typer.Option("pending", "--status", help="Filter by status: pending|accepted|rejected|all"),
) -> None:
    """List staged artefacts awaiting review."""
    items = load_staging()
    if not items:
        console.print("[dim]No staged items. Run [cyan]strata scan[/] or [cyan]strata ai extract[/] first.[/]")
        return

    filtered = items if status == "all" else [i for i in items if i.status == status]
    if not filtered:
        console.print(f"[dim]No items with status={status!r}[/]")
        return

    t = Table(title=f"Staging area ({len(filtered)} item(s))", box=box.SIMPLE)
    t.add_column("#", justify="right", style="dim")
    t.add_column("ID", style="dim")
    t.add_column("Status")
    t.add_column("Entity")
    t.add_column("Name")
    t.add_column("Source", style="dim")

    STATUS_STYLE = {"pending": "yellow", "accepted": "green", "rejected": "red"}
    for pos, item in enumerate(filtered, 1):
        style = STATUS_STYLE.get(item.status, "")
        name = item.fields.get("name", "—")
        t.add_row(
            str(pos), item.id,
            f"[{style}]{item.status}[/]",
            item.entity, name,
            item.source or "",
        )
    console.print(t)
    console.print(
        "[dim]Use [cyan]strata staging show <id>[/] for details, "
        "[cyan]strata staging accept <id>[/] to commit.[/]"
    )


@staging_app.command("show")
def staging_show(
    id_or_index: str = typer.Argument(..., help="Staging ID (stg-001) or 1-based list index"),
) -> None:
    """Show full details for a single staged item."""
    items = load_staging()
    idx = _staging_index(items, id_or_index)
    if idx is None:
        console.print(f"[red]Item not found:[/] {id_or_index!r}")
        raise typer.Exit(code=1)
    item = items[idx]
    rows = [
        ("ID", item.id),
        ("Entity", item.entity),
        ("Status", item.status),
        ("Source", item.source or ""),
        ("Notes", item.notes or ""),
    ] + [(k, str(v)) for k, v in item.fields.items()]

    t = Table(box=box.SIMPLE)
    t.add_column("Field", style="dim")
    t.add_column("Value")
    for k, v in rows:
        if v:
            t.add_row(k, v)
    console.print(Panel(t, title=f"[bold]{item.entity}[/]  {item.id}"))


@staging_app.command("accept")
def staging_accept(
    id_or_index: str = typer.Argument(..., help="Staging ID, 1-based index, or 'all' to accept all pending"),
) -> None:
    """Accept a staged item and commit it to the workspace."""
    items = load_staging()
    if not items:
        console.print("[dim]Staging area is empty.[/]")
        return

    to_accept: list[int] = []
    if id_or_index.lower() == "all":
        to_accept = [i for i, item in enumerate(items) if item.status == "pending"]
    else:
        idx = _staging_index(items, id_or_index)
        if idx is None:
            console.print(f"[red]Item not found:[/] {id_or_index!r}")
            raise typer.Exit(code=1)
        to_accept = [idx]

    workspace = _load()
    accepted = 0
    for idx in to_accept:
        item = items[idx]
        if item.status != "pending":
            console.print(f"[dim]Skipping {item.id} (status={item.status})[/]")
            continue
        err = _write_entity(item.entity, item.fields, workspace)
        if err:
            console.print(f"[red]Error accepting {item.id}:[/] {err}")
            continue
        items[idx] = item.model_copy(update={"status": "accepted"})
        name = item.fields.get("name", item.id)
        console.print(f"[green]Accepted[/] {item.entity} [bold]{name}[/]  ({item.id})")
        accepted += 1

    if accepted:
        _save(workspace)
        save_staging(items)
        console.print(f"[green]{accepted} item(s) committed to workspace.[/]")
    else:
        console.print("[yellow]Nothing was accepted.[/]")


@staging_app.command("reject")
def staging_reject(
    id_or_index: str = typer.Argument(..., help="Staging ID, 1-based index, or 'all' to reject all pending"),
    note: str = typer.Option("", "--note", help="Optional reason for rejection"),
) -> None:
    """Reject a staged item (marks it as rejected; does not delete it)."""
    items = load_staging()
    if not items:
        console.print("[dim]Staging area is empty.[/]")
        return

    to_reject: list[int] = []
    if id_or_index.lower() == "all":
        to_reject = [i for i, item in enumerate(items) if item.status == "pending"]
    else:
        idx = _staging_index(items, id_or_index)
        if idx is None:
            console.print(f"[red]Item not found:[/] {id_or_index!r}")
            raise typer.Exit(code=1)
        to_reject = [idx]

    rejected = 0
    updates: dict = {"status": "rejected"}
    if note:
        updates["notes"] = note
    for idx in to_reject:
        item = items[idx]
        if item.status != "pending":
            console.print(f"[dim]Skipping {item.id} (status={item.status})[/]")
            continue
        items[idx] = item.model_copy(update=updates)
        name = item.fields.get("name", item.id)
        console.print(f"[red]Rejected[/] {item.entity} [bold]{name}[/]  ({item.id})")
        rejected += 1

    if rejected:
        save_staging(items)
    else:
        console.print("[yellow]Nothing was rejected.[/]")


@staging_app.command("edit")
def staging_edit(
    id_or_index: str = typer.Argument(..., help="Staging ID or 1-based index"),
    set_fields: list[str] = typer.Option([], "--set", help="Override a field: --set name=Foo --set owner=Bar"),
    accept: bool = typer.Option(True, "--accept/--no-accept", help="Accept the item after editing (default: yes)"),
) -> None:
    """Edit fields on a staged item and optionally accept it.

    \b
    Example:
      strata staging edit stg-001 --set name="Order Management" --set owner="Commerce Team"
    """
    items = load_staging()
    idx = _staging_index(items, id_or_index)
    if idx is None:
        console.print(f"[red]Item not found:[/] {id_or_index!r}")
        raise typer.Exit(code=1)

    item = items[idx]
    new_fields = dict(item.fields)
    for pair in set_fields:
        if "=" not in pair:
            console.print(f"[red]Invalid --set value:[/] {pair!r}  (expected field=value)")
            raise typer.Exit(code=1)
        k, v = pair.split("=", 1)
        new_fields[k.strip()] = v.strip()

    items[idx] = item.model_copy(update={"fields": new_fields})

    # Show updated fields
    t = Table(box=box.SIMPLE)
    t.add_column("Field", style="dim")
    t.add_column("Value")
    for k, v in new_fields.items():
        t.add_row(k, str(v))
    console.print(t)

    if accept:
        workspace = _load()
        err = _write_entity(item.entity, new_fields, workspace)
        if err:
            console.print(f"[red]Error:[/] {err}")
            raise typer.Exit(code=1)
        items[idx] = items[idx].model_copy(update={"status": "accepted"})
        _save(workspace)
        name = new_fields.get("name", item.id)
        console.print(f"[green]Accepted[/] {item.entity} [bold]{name}[/]")

    save_staging(items)


@staging_app.command("clear")
def staging_clear(
    status: str = typer.Option("accepted,rejected", "--status", help="Comma-separated statuses to remove: accepted|rejected|pending|all"),
    confirmed: bool = typer.Option(False, "--yes", help="Skip confirmation prompt"),
) -> None:
    """Remove accepted/rejected items from the staging file.

    \b
    Examples:
      strata staging clear                    # remove accepted + rejected
      strata staging clear --status all       # wipe everything
      strata staging clear --status rejected  # only remove rejected
    """
    items = load_staging()
    if not items:
        console.print("[dim]Staging area is already empty.[/]")
        return

    if status.lower() == "all":
        target_statuses = {"pending", "accepted", "rejected"}
    else:
        target_statuses = {s.strip() for s in status.split(",")}

    to_remove = [i for i, item in enumerate(items) if item.status in target_statuses]
    if not to_remove:
        console.print(f"[dim]No items with status in {target_statuses}.[/]")
        return

    if not confirmed:
        console.print(f"This will remove [bold]{len(to_remove)}[/] item(s).")
        typer.confirm("Continue?", abort=True)

    kept = [item for item in items if item.status not in target_statuses]
    save_staging(kept)
    console.print(f"[green]Removed {len(to_remove)} item(s).[/] {len(kept)} remain.")




# ═══════════════════════════════════════════════════════════════════════════════
# strata ui  — full-screen TUI
# ═══════════════════════════════════════════════════════════════════════════════

@app.command("stack")
def stack_cmd() -> None:
    """Show full stack coverage map — capabilities by domain, data domains, solutions, and gaps.

    \b
    Indicators:
      ✅  Good coverage (sufficient depth + ownership)
      ⚠️  Partial / thin (exists but needs attention)
      ❌  Absent / isolated (critical gap)

    \b
    Reference capability domains checked:
      Customer · Finance · Operations · Technology
      HR · Products · Risk & Compliance · Partner & Ecosystem
    """
    from .analyzer import compute_stack_coverage, StackCoverage

    ws = _load()
    cov: StackCoverage = compute_stack_coverage(ws)

    console.print(f"\n[bold]🗺️  Stack Coverage — {ws.manifest.name}[/bold]\n")

    # Capability domains
    if cov.capability_domains:
        t = Table(
            title=f"Business Capability Domains ({len(cov.capability_domains)})",
            box=box.SIMPLE_HEAVY,
        )
        t.add_column("", width=3)
        t.add_column("Domain", style="bold")
        t.add_column("Caps", justify="right")
        t.add_column("Strategic / Core / Supporting")
        t.add_column("Ownership %", justify="right")
        t.add_column("Mature %", justify="right")
        for d in cov.capability_domains:
            oc = "green" if d.ownership_pct >= 80 else "yellow" if d.ownership_pct >= 50 else "red"
            mc = "green" if d.mature_pct >= 50 else "yellow" if d.mature_pct >= 25 else "red"
            t.add_row(
                d.indicator, d.domain, str(d.count),
                f"{d.strategic} / {d.core} / {d.supporting}",
                f"[{oc}]{d.ownership_pct:.0f}%[/{oc}]",
                f"[{mc}]{d.mature_pct:.0f}%[/{mc}]",
            )
        console.print(t)

    if cov.missing_cap_domains:
        console.print(
            f"  [yellow]⚠[/yellow]  Not covered (reference domains): "
            f"{', '.join(cov.missing_cap_domains)}\n"
        )

    # Data domains
    if cov.data_domains:
        dt = Table(
            title=f"Data Domains ({len(cov.data_domains)})",
            box=box.SIMPLE_HEAVY,
        )
        dt.add_column("", width=3)
        dt.add_column("Domain", style="bold")
        dt.add_column("Owner")
        dt.add_column("Products", justify="right")
        dt.add_column("Gold/Plat SLA", justify="right")
        dt.add_column("Flows In", justify="right")
        dt.add_column("Flows Out", justify="right")
        for d in cov.data_domains:
            sla = str(d.sla_gold_plat) if d.sla_gold_plat else "—"
            dt.add_row(
                d.indicator, d.name, d.owner, str(d.products_count),
                sla, str(d.flows_in), str(d.flows_out),
            )
        console.print(dt)

    # Solutions
    if cov.solutions:
        st = Table(
            title=f"Solution Designs ({len(cov.solutions)})",
            box=box.SIMPLE_HEAVY,
        )
        st.add_column("", width=3)
        st.add_column("Name", style="bold")
        st.add_column("Pattern")
        st.add_column("Status")
        st.add_column("ADRs")
        st.add_column("Components", justify="right")
        SC = {"approved": "green", "implemented": "cyan", "review": "yellow", "draft": "dim"}
        for s in cov.solutions:
            sc = SC.get(s.status, "")
            st.add_row(
                s.indicator, s.name, s.pattern,
                f"[{sc}]{s.status}[/{sc}]",
                "[green]✓[/green]" if s.has_adrs else "[red]✗[/red]",
                str(s.component_count),
            )
        console.print(st)

    # Tech radar by category
    if cov.radar_by_category:
        rt = Table(title="Tech Radar by Category", box=box.SIMPLE_HEAVY)
        rt.add_column("Category", style="bold")
        rt.add_column("Standards")
        for cat, items in sorted(cov.radar_by_category.items()):
            rt.add_row(cat, "  ·  ".join(items[:6]))
        console.print(rt)

    # Gaps
    if cov.gaps:
        console.print(f"\n[bold yellow]⚠  Gaps & Recommendations ({len(cov.gaps)})[/bold yellow]")
        for i, g in enumerate(cov.gaps, 1):
            console.print(f"  [yellow]{i}.[/yellow] {g}")
        console.print()
    else:
        console.print("\n  [green]✅  No critical gaps detected[/green]\n")


@app.command("score")
def score_cmd(
    profile: str = typer.Option("default", "--profile", "-p", help="Scoring profile: default, telecom"),
) -> None:
    """Score architecture maturity against a framework profile.

    \b
    Built-in profiles:
      default    Balanced — equal weight across all dimensions
      telecom    Telecom / digital platform — weighted towards ops & data

    \b
    Six dimensions are scored 0.0–5.0:
      Capability Coverage    — breadth and depth of business capabilities
      Application Health     — portfolio lifecycle and modernisation
      Data Maturity          — domains, products, flows, SLA tiers
      Solution Completeness  — designs, components, ADRs
      Operational Readiness  — hosting, tech radar, event-driven adoption
      Governance & Compliance — ownership, documentation, cross-references
    """
    from .scoring import score_workspace, list_profiles, ScoreResult

    ws = _load()
    available = list_profiles()
    if profile not in available:
        console.print(f"[red]Unknown profile: {profile}[/red]  Available: {', '.join(available)}")
        raise typer.Exit(code=1)

    result = score_workspace(ws, profile=profile)
    ov = result.overall
    col = "green" if ov >= 3.0 else "yellow" if ov >= 2.0 else "red"
    bar_w = 30
    filled = round((ov / 5.0) * bar_w)
    bar = f"[{col}]{'█' * filled}{'░' * (bar_w - filled)}[/{col}]"

    console.print(
        f"\n[bold]📈  Architecture Maturity — {result.profile_name}[/bold]\n"
        f"[dim]{result.profile_description}[/dim]\n"
    )
    console.print(f"  Overall:  {bar}  [{col}][bold]{ov:.1f}[/bold][/{col}] / 5.0   Level: [bold {col}]{result.level}[/bold {col}]\n")

    t = Table(box=box.SIMPLE_HEAVY, title="Dimension Breakdown")
    t.add_column("Dimension", style="bold")
    t.add_column("Score", justify="right")
    t.add_column("Weight", justify="right", style="dim")
    t.add_column("Bar", no_wrap=True)
    t.add_column("Top Findings")

    for d in result.dimensions:
        dc = "green" if d.score >= 3.0 else "yellow" if d.score >= 2.0 else "red"
        bw = 14
        bf = round((d.score / d.max_score) * bw)
        dim_bar = f"[{dc}]{'█' * bf}{'░' * (bw - bf)}[/{dc}]"
        findings = "; ".join(d.findings[:2]) if d.findings else "—"
        t.add_row(d.label, f"[{dc}]{d.score:.1f}[/{dc}]", f"×{d.weight:.1f}", dim_bar, findings)

    console.print(t)
    console.print(
        "\n[dim]Levels: " +
        "  ".join(f"{label} ({lo:.0f}–{hi:.0f})" for label, (lo, hi) in result.level_labels.items()) +
        f"\nAvailable profiles: {', '.join(available)}[/dim]\n"
    )


@app.command("ui")
def ui(
    provider: str = typer.Option("auto", "--provider", help="AI provider: auto|copilot|claude|codex|openai|ollama"),
) -> None:
    """Launch the full-screen interactive UI.

    \b
    Provides a conversational interface for managing your architecture workspace:
      • Chat with AI in plain English to add capabilities, apps, standards, etc.
      • Browse and navigate your workspace via the sidebar
      • Review AI-detected items in the staging area
      • Scan folders of markdown files for architecture artefacts

    \b
    Key bindings inside the UI:
      Ctrl+Q          Quit
      Ctrl+B          Toggle sidebar
      Ctrl+E          Enterprise (capabilities)
      Ctrl+D          Data (domains)
      Ctrl+S          Solutions
      Ctrl+T          Staging
      F1              Help
    """
    from .tui import launch_tui
    launch_tui(provider=provider)


# ═══════════════════════════════════════════════════════════════════════════════
# strata workspace  — watch folders and workspace-level settings
# ═══════════════════════════════════════════════════════════════════════════════

@workspace_app.command("list-folders")
def workspace_list_folders() -> None:
    """List all configured watch folders for this workspace."""
    folders = load_watch_folders()
    if not folders:
        console.print("[dim]No watch folders configured.  Use [cyan]strata workspace add-folder <path>[/cyan][/]")
        return
    t = Table(title="Watch Folders", box=box.SIMPLE_HEAVY)
    t.add_column("#", style="dim", justify="right")
    t.add_column("Path")
    t.add_column("Exists", justify="center")
    for i, folder in enumerate(folders, 1):
        exists = "✓" if Path(folder).exists() else "[red]✗[/]"
        t.add_row(str(i), folder, exists)
    console.print(t)


@workspace_app.command("add-folder")
def workspace_add_folder(
    path: str = typer.Argument(..., help="Path to the folder to watch (absolute or relative)"),
) -> None:
    """Add a folder to the workspace watch list.

    Strata will automatically know to scan this folder for architecture artefacts.
    Use [cyan]strata workspace scan-all[/] to trigger a scan of all configured folders.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        console.print(f"[yellow]Warning:[/] path does not exist: {resolved}")
        if not typer.confirm("Add anyway?", default=False):
            raise typer.Exit()
    folders = add_watch_folder(path)
    console.print(f"[green]✓[/] Added [bold]{resolved}[/]")
    console.print(f"[dim]Watch folders: {len(folders)}[/]")


@workspace_app.command("remove-folder")
def workspace_remove_folder(
    path: str = typer.Argument(..., help="Path to remove (as shown in list-folders)"),
) -> None:
    """Remove a folder from the workspace watch list."""
    folders = remove_watch_folder(path)
    console.print(f"[green]✓[/] Removed.  Remaining folders: {len(folders)}")


@workspace_app.command("scan-all")
def workspace_scan_all(
    provider: str = typer.Option("auto", "--provider", help="AI provider"),
    stage: bool = typer.Option(True, "--stage/--no-stage", help="Stage items for review instead of writing directly"),
) -> None:
    """Scan all configured watch folders and stage detected architecture artefacts.

    Runs the AI scanner over every watch folder in sequence.
    """
    folders = load_watch_folders()
    if not folders:
        console.print("[yellow]No watch folders configured.[/]  Run [cyan]strata workspace add-folder <path>[/cyan] first.")
        raise typer.Exit()

    ws = _load()
    agent = ArchitectureAgent(provider=provider)
    available, msg = agent.check_available()
    if not available:
        console.print(f"[red]AI not available:[/] {msg}")
        raise typer.Exit(code=1)

    ctx = {
        "capability_ids": [c.id for c in ws.enterprise.capabilities],
        "domain_ids": [d.id for d in ws.data.domains],
        "solution_ids": [s.id for s in ws.solutions],
    }

    total_staged = 0
    total_files = 0

    for folder in folders:
        target = Path(folder)
        if not target.exists():
            console.print(f"[yellow]Skipping (not found):[/] {folder}")
            continue

        files = [target] if target.is_file() else sorted(target.glob("**/*.md"))
        if not files:
            console.print(f"[dim]No .md files in {folder}[/]")
            continue

        console.print(f"\n[bold]Scanning[/] [cyan]{folder}[/]  ({len(files)} files)")
        existing = load_staging()
        new_items: list[StagedItem] = []

        for fp in files:
            rel = fp.name
            console.print(f"  [dim]· {rel}[/]")
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
                found = agent.scan_document(text, source_name=rel, workspace_context=ctx)
                for item in found:
                    sid = next_staging_id(existing + new_items)
                    new_items.append(StagedItem(
                        id=sid,
                        entity=item.get("entity", "unknown"),
                        fields=item.get("fields", {}),
                        source=str(fp),
                    ))
                total_files += 1
            except Exception as exc:  # noqa: BLE001
                console.print(f"  [red]Error scanning {rel}:[/] {exc}")

        if new_items:
            save_staging(existing + new_items)
            total_staged += len(new_items)
            console.print(f"  [green]Staged {len(new_items)} item(s)[/]")

    console.print(
        f"\n[bold green]Done.[/]  {total_staged} item(s) staged from {total_files} file(s)."
    )
    if total_staged:
        console.print("Review with [cyan]strata staging list[/]")


if __name__ == "__main__":
    app()
