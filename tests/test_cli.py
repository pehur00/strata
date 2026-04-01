"""CLI integration tests for the Strata workspace-based CLI."""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from strata.cli import app
from strata.workspace import load_workspace, save_workspace
from strata.models import ArchitectureWorkspace, WorkspaceManifest

runner = CliRunner()


def _init_workspace(tmp_path: Path, name: str = "TestOrg") -> Path:
    """Helper: create a valid workspace in tmp_path and return root."""
    result = runner.invoke(
        app,
        ["init", "--name", name, "--description", "Test org", "--path", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    return tmp_path


# ── init ──────────────────────────────────────────────────────────────────────

def test_init_creates_workspace(tmp_path):
    result = runner.invoke(
        app,
        ["init", "--name", "Acme Corp", "--description", "Test", "--path", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert (tmp_path / "architecture" / "strata.yaml").exists()
    assert (tmp_path / "architecture" / "enterprise" / "architecture.yaml").exists()
    assert (tmp_path / "architecture" / "data" / "architecture.yaml").exists()
    assert (tmp_path / "architecture" / "solutions").exists()


def test_init_idempotent(tmp_path):
    _init_workspace(tmp_path)
    result = runner.invoke(
        app,
        ["init", "--name", "Acme Corp", "--description", "", "--path", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert "already exists" in result.output


# ── status ────────────────────────────────────────────────────────────────────

def test_status_shows_workspace(tmp_path, monkeypatch):
    _init_workspace(tmp_path, name="StatusOrg")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "StatusOrg" in result.output


# ── enterprise ────────────────────────────────────────────────────────────────

def test_add_and_list_capability(tmp_path, monkeypatch):
    _init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "enterprise", "add-capability",
            "--name", "Payment Processing",
            "--domain", "Payments",
            "--level", "core",
            "--owner", "Payments Team",
        ],
    )
    assert result.exit_code == 0
    assert "Payment Processing" in result.output

    result = runner.invoke(app, ["enterprise", "list-capabilities"])
    assert result.exit_code == 0
    # Rich wraps long cell values; check domain (8 chars) and level (4 chars) which always fit
    assert "Payments" in result.output
    assert "core" in result.output


def test_add_and_list_application(tmp_path, monkeypatch):
    _init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "enterprise", "add-application",
            "--name", "Payments API",
            "--hosting", "kubernetes",
            "--criticality", "critical",
            "--owner", "Payments Team",
        ],
    )
    assert result.exit_code == 0
    result = runner.invoke(app, ["enterprise", "list-applications"])
    assert result.exit_code == 0
    assert "Payments API" in result.output


def test_add_standard_and_tech_radar(tmp_path, monkeypatch):
    _init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner.invoke(
        app,
        [
            "enterprise", "add-standard",
            "--name", "Kafka",
            "--category", "messaging",
            "--status", "adopt",
            "--rationale", "Proven streaming platform",
        ],
    )
    result = runner.invoke(app, ["enterprise", "tech-radar"])
    assert result.exit_code == 0
    assert "ADOPT" in result.output
    assert "Kafka" in result.output


# ── data ──────────────────────────────────────────────────────────────────────

def test_add_and_list_domain(tmp_path, monkeypatch):
    _init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "data", "add-domain",
            "--name", "Payments Domain",
            "--owner", "Payments Team",
            "--storage", "operational",
        ],
    )
    assert result.exit_code == 0
    result = runner.invoke(app, ["data", "list-domains"])
    assert result.exit_code == 0
    assert "Payments Domain" in result.output


def test_add_data_product_and_flow(tmp_path, monkeypatch):
    _init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["data", "add-domain", "--name", "Orders", "--owner", "", "--storage", "operational"])
    runner.invoke(app, ["data", "add-domain", "--name", "Finance", "--owner", "", "--storage", "warehouse"])
    runner.invoke(
        app,
        ["data", "add-product", "--name", "Orders Feed", "--domain-id", "orders", "--output-port", "streaming", "--sla-tier", "gold", "--owner", ""],
    )
    result = runner.invoke(app, ["data", "add-flow", "--name", "Orders to Finance", "--source", "orders", "--target", "finance", "--mechanism", "streaming"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["data", "list-flows"])
    assert "orders" in result.output.lower()
    assert "finance" in result.output.lower()


# ── solution ──────────────────────────────────────────────────────────────────

def test_create_and_show_solution(tmp_path, monkeypatch):
    _init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "solution", "create",
            "--name", "API Platform",
            "--description", "Central API gateway",
            "--pattern", "api-gateway",
            "--target", "aws",
        ],
    )
    assert result.exit_code == 0
    assert "api-platform" in result.output

    result = runner.invoke(app, ["solution", "show", "api-platform"])
    assert result.exit_code == 0
    assert "API Platform" in result.output


def test_add_component_to_solution(tmp_path, monkeypatch):
    _init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["solution", "create", "--name", "Data Mesh", "--description", "", "--pattern", "data-mesh", "--target", "gcp"])
    result = runner.invoke(
        app,
        [
            "solution", "add-component", "data-mesh",
            "--name", "Data Catalog",
            "--comp-type", "service",
            "--technology", "Dataplex",
            "--hosting", "managed-service",
        ],
    )
    assert result.exit_code == 0
    assert "Data Catalog" in result.output


def test_add_adr_to_solution(tmp_path, monkeypatch):
    _init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["solution", "create", "--name", "Event Bus", "--description", "", "--pattern", "event-driven", "--target", "azure"])
    result = runner.invoke(
        app,
        [
            "solution", "add-adr", "event-bus",
            "--title", "Use Kafka over RabbitMQ",
            "--context", "Need durable event streaming",
            "--decision", "Adopt Kafka",
        ],
    )
    assert result.exit_code == 0
    assert "ADR-001" in result.output

    result = runner.invoke(app, ["solution", "list-adrs", "event-bus"])
    assert result.exit_code == 0
    assert "Use Kafka over RabbitMQ" in result.output


# ── generate ──────────────────────────────────────────────────────────────────

def test_generate_capability_map(tmp_path, monkeypatch):
    _init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["enterprise", "add-capability", "--name", "Checkout", "--domain", "Commerce", "--level", "core", "--owner", ""])
    out = str(tmp_path / "cap.mmd")
    result = runner.invoke(app, ["generate", "capability-map", "--output", out])
    assert result.exit_code == 0
    assert Path(out).exists()
    content = Path(out).read_text()
    assert "graph TD" in content
    assert "Checkout" in content


def test_generate_solution_diagram(tmp_path, monkeypatch):
    _init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["solution", "create", "--name", "My Solution", "--description", "", "--pattern", "microservices", "--target", "aws"])
    runner.invoke(app, ["solution", "add-component", "my-solution", "--name", "API", "--comp-type", "gateway", "--technology", "Kong", "--hosting", "kubernetes"])
    out = str(tmp_path / "sol.mmd")
    result = runner.invoke(app, ["generate", "solution-diagram", "my-solution", "--output", out])
    assert result.exit_code == 0
    assert Path(out).exists()
    content = Path(out).read_text()
    assert "graph LR" in content
    assert "API" in content


def test_generate_report(tmp_path, monkeypatch):
    _init_workspace(tmp_path, name="ReportOrg")
    monkeypatch.chdir(tmp_path)
    out = str(tmp_path / "report.json")
    result = runner.invoke(app, ["generate", "report", "--output", out])
    assert result.exit_code == 0
    data = json.loads(Path(out).read_text())
    assert data["workspace"]["name"] == "ReportOrg"
    assert "summary" in data
    assert "detail" in data


# ── validate ─────────────────────────────────────────────────────────────────

def test_validate_clean_workspace(tmp_path, monkeypatch):
    _init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0
    assert "valid" in result.output


# ── import (PSA markdown) ─────────────────────────────────────────────────────

PSA_MINIMAL = textwrap.dedent("""\
    ---
    name: ImportTest
    cloud_provider: aws
    ---
    # ImportTest Architecture

    ## Capabilities
    | Name         | Domain  | Level | Owner     |
    |--------------|---------|-------|-----------|
    | Payments     | Finance | core  | Pay Team  |
    | Analytics    | Data    | supporting | BI Team |

    ## Applications
    | Name         | Hosting    | Criticality | Owner    |
    |--------------|------------|-------------|----------|
    | Pay Service  | kubernetes | critical    | Pay Team |

    ## Data Domains
    | Name     | Owner   | Storage     |
    |----------|---------|-------------|
    | Payments | Pay Team| operational |

    ## Data Flows
    | Name          | From     | To       | Mechanism |
    |---------------|----------|----------|-----------|
    | Pay to BI     | payments | analytics| streaming |

    ## Solutions

    ### Payment Platform
    - pattern: api-gateway
    - target: aws

    #### Components
    | Name       | Type     | Technology | Hosting    |
    |------------|----------|------------|------------|
    | API Gateway| gateway  | Kong       | kubernetes |
""")


def test_import_dry_run(tmp_path, monkeypatch):
    """--dry-run should show counts but write nothing."""
    monkeypatch.chdir(tmp_path)
    md_file = tmp_path / "test.md"
    md_file.write_text(PSA_MINIMAL)
    result = runner.invoke(app, ["import", str(md_file), "--dry-run"])
    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "Capabilities" in result.output
    # Nothing should be written
    assert not (tmp_path / "architecture").exists()


def test_import_creates_workspace_and_populates(tmp_path, monkeypatch):
    """Import without existing workspace should auto-create and populate."""
    monkeypatch.chdir(tmp_path)
    md_file = tmp_path / "arch.md"
    md_file.write_text(PSA_MINIMAL)
    result = runner.invoke(app, ["import", str(md_file)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "architecture" / "strata.yaml").exists()
    # Check content was loaded
    ws = load_workspace(tmp_path)
    assert ws.manifest.name == "ImportTest"
    assert any(c.name == "Payments" for c in ws.enterprise.capabilities)
    assert any(a.name == "Pay Service" for a in ws.enterprise.applications)
    assert any(d.name == "Payments" for d in ws.data.domains)
    assert len(ws.solutions) == 1
    assert ws.solutions[0].name == "Payment Platform"
    assert len(ws.solutions[0].components) == 1


def test_import_is_idempotent(tmp_path, monkeypatch):
    """Importing the same file twice should not duplicate entries."""
    monkeypatch.chdir(tmp_path)
    md_file = tmp_path / "arch.md"
    md_file.write_text(PSA_MINIMAL)
    runner.invoke(app, ["import", str(md_file)])
    result = runner.invoke(app, ["import", str(md_file)])
    assert result.exit_code == 0
    assert "Nothing new" in result.output
    ws = load_workspace(tmp_path)
    assert len(ws.enterprise.capabilities) == 2  # not 4


def test_import_merges_into_existing_workspace(tmp_path, monkeypatch):
    """Import into existing workspace should only add new items."""
    _init_workspace(tmp_path, name="Existing")
    monkeypatch.chdir(tmp_path)
    # Pre-populate one capability
    runner.invoke(app, ["enterprise", "add-capability",
                        "--name", "Existing Cap", "--domain", "IT",
                        "--level", "core", "--owner", ""])
    md_file = tmp_path / "arch.md"
    md_file.write_text(PSA_MINIMAL)
    result = runner.invoke(app, ["import", str(md_file)])
    assert result.exit_code == 0
    ws = load_workspace(tmp_path)
    # 1 existing + 2 from PSA
    assert len(ws.enterprise.capabilities) == 3


def test_import_missing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["import", "nonexistent.md"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "File not found" in result.output
