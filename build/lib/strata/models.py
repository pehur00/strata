"""Domain models for Architecture as a Service (Strata).

Three architecture domains are supported:
  - Enterprise architecture  (capabilities, applications, tech standards)
  - Data architecture        (domains, data products, data flows)
  - Solution architecture    (designs, components, ADRs)
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ── Workspace manifest ─────────────────────────────────────────────────────────

class WorkspaceManifest(BaseModel):
    name: str
    description: str | None = None
    cloud_provider: Literal[
        "aws", "azure", "gcp", "multi-cloud", "on-premise", "hybrid"
    ] = "multi-cloud"
    environment: Literal["dev", "staging", "production"] = "production"
    version: str = "1"
    watch_folders: list[str] = Field(default_factory=list)
    """Filesystem paths that Strata will scan for architecture artefacts."""
    scan_interval_minutes: int = 0
    """Auto-scan interval in minutes.  0 = disabled (manual only)."""
    advisor_enabled: bool = True
    """Enable scheduled multi-agent advisory runs."""
    advisor_interval_minutes: int = 30
    """Advisor run interval in minutes.  0 = disabled."""
    advisor_profile: Literal["default", "telecom", "oda"] = "oda"
    """Scoring/advisory profile used by scheduled advisor runs."""
    database_url: str | None = None
    """PostgreSQL connection string (default: postgresql://strata:strata@localhost:5432/strata).
    Leave None to use the default local Docker Compose URL."""


# ── Enterprise architecture ────────────────────────────────────────────────────

class BusinessCapability(BaseModel):
    id: str
    name: str
    description: str | None = None
    level: Literal["strategic", "core", "supporting"] = "core"
    domain: str
    owner: str | None = None
    maturity: Literal[
        "initial", "developing", "defined", "managed", "optimizing"
    ] = "initial"
    parent_id: str | None = None


class Application(BaseModel):
    id: str
    name: str
    description: str | None = None
    capability_ids: list[str] = Field(default_factory=list)
    technology_stack: list[str] = Field(default_factory=list)
    deployment: Literal[
        "cloud-native", "lift-and-shift", "saas", "on-premise", "hybrid"
    ] = "cloud-native"
    status: Literal["active", "retiring", "planned", "decommissioned"] = "active"
    owner_team: str | None = None
    hosting: Literal[
        "kubernetes", "serverless", "vm", "managed-service", "saas"
    ] = "kubernetes"
    criticality: Literal["low", "medium", "high", "critical"] = "medium"


class TechnologyStandard(BaseModel):
    id: str
    name: str
    category: str
    status: Literal["adopt", "trial", "assess", "hold"] = "assess"
    description: str | None = None
    rationale: str | None = None
    alternatives: list[str] = Field(default_factory=list)


class EnterpriseArchitecture(BaseModel):
    capabilities: list[BusinessCapability] = Field(default_factory=list)
    applications: list[Application] = Field(default_factory=list)
    standards: list[TechnologyStandard] = Field(default_factory=list)


# ── Data architecture ──────────────────────────────────────────────────────────

class DataEntity(BaseModel):
    name: str
    description: str | None = None
    type: Literal["entity", "event", "aggregate"] = "entity"
    classification: Literal[
        "public", "internal", "confidential", "restricted"
    ] = "internal"


class DataDomain(BaseModel):
    id: str
    name: str
    description: str | None = None
    owner_team: str | None = None
    entities: list[DataEntity] = Field(default_factory=list)
    storage_pattern: Literal[
        "warehouse", "lakehouse", "operational", "streaming", "mixed"
    ] = "operational"


class DataProduct(BaseModel):
    id: str
    name: str
    description: str | None = None
    domain_id: str
    output_port: Literal["api", "files", "streaming", "sql", "graphql"] = "api"
    sla_tier: Literal["bronze", "silver", "gold", "platinum"] = "silver"
    consumers: list[str] = Field(default_factory=list)
    owner_team: str | None = None


class DataFlow(BaseModel):
    id: str
    name: str
    source_domain: str
    target_domain: str
    data_product_id: str | None = None
    mechanism: Literal[
        "streaming", "batch", "api", "cdc", "file-transfer"
    ] = "api"
    frequency: str | None = None
    classification: Literal[
        "public", "internal", "confidential", "restricted"
    ] = "internal"


class DataArchitecture(BaseModel):
    domains: list[DataDomain] = Field(default_factory=list)
    products: list[DataProduct] = Field(default_factory=list)
    flows: list[DataFlow] = Field(default_factory=list)


# ── Solution architecture ──────────────────────────────────────────────────────

class ArchitectureDecisionRecord(BaseModel):
    id: str
    title: str
    status: Literal[
        "proposed", "accepted", "deprecated", "superseded"
    ] = "proposed"
    context: str | None = None
    decision: str | None = None
    consequences: str | None = None
    date: str | None = None


class Component(BaseModel):
    id: str
    name: str
    type: Literal[
        "service", "gateway", "database", "queue", "cache",
        "cdn", "identity", "storage", "external",
    ] = "service"
    technology: str | None = None
    hosting: Literal[
        "kubernetes", "serverless", "managed-service", "saas", "external"
    ] = "kubernetes"
    dependencies: list[str] = Field(default_factory=list)
    interfaces: list[str] = Field(default_factory=list)
    description: str | None = None


class SolutionDesign(BaseModel):
    id: str
    name: str
    description: str | None = None
    status: Literal[
        "draft", "review", "approved", "implemented", "deprecated"
    ] = "draft"
    business_capability_ids: list[str] = Field(default_factory=list)
    components: list[Component] = Field(default_factory=list)
    adrs: list[ArchitectureDecisionRecord] = Field(default_factory=list)
    deployment_target: str = "multi-cloud"
    pattern: str = "microservices"


# ── Staging (AI-detected items awaiting manual review) ────────────────────────

class StagedItem(BaseModel):
    """A candidate artefact detected by AI, awaiting manual review."""
    id: str                    # e.g. "stg-001"
    entity: str                # capability | application | standard | domain | product | flow | solution
    fields: dict = Field(default_factory=dict)
    source: str = ""           # originating file path, "ask", or "ai-extract"
    status: Literal["pending", "accepted", "rejected"] = "pending"
    notes: str | None = None


# ── Full workspace ─────────────────────────────────────────────────────────────

class ArchitectureWorkspace(BaseModel):
    manifest: WorkspaceManifest
    enterprise: EnterpriseArchitecture = Field(
        default_factory=EnterpriseArchitecture
    )
    data: DataArchitecture = Field(default_factory=DataArchitecture)
    solutions: list[SolutionDesign] = Field(default_factory=list)
