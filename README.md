# Strata — Architecture as a Service

An open-source CLI for designing, governing, and automating **enterprise**, **data**, and **solution** architecture. Your entire architecture lives as declarative YAML — GitOps-ready, team-shareable, and AI-augmented.

> **Strata** — architectural layers, from the ground up.

## Concept

Architecture artefacts should be treated like code:

- **File-based** — stored in `architecture/` as YAML, committable to git
- **Declarative** — describe *what* your architecture looks like, not how to build it
- **Multi-domain** — enterprise, data, and solution architecture in one workspace
- **AI-augmented** — extract architecture from documents using Copilot, Claude, or Codex (your existing subscriptions — no extra API keys)
- **Diagram-ready** — generate Mermaid diagrams from your architecture data

---

## Architecture domains

| Domain | What you model |
|---|---|
| **Enterprise** | Business capabilities, application portfolio, technology radar |
| **Data** | Data domains, data products (data mesh), data flows between domains |
| **Solution** | Solution designs, components, Architecture Decision Records (ADRs) |

---

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

---

## Quick start

### 1. Initialise a workspace

```bash
strata init --name "AcmeCorp" --description "Cloud-native retail platform" --cloud aws
```

Creates an `architecture/` directory:

```
architecture/
  strata.yaml
  enterprise/
    architecture.yaml
  data/
    architecture.yaml
  solutions/
    (one .yaml file per solution design)
```

### 2. Import from an existing architecture document

```bash
strata import path/to/architecture.md
```

Parses PSA-format markdown files and populates the workspace automatically. Supports capabilities, applications, tech standards, data domains, data products, data flows, and solution designs.

```bash
strata import architecture.md --dry-run   # preview without writing
```

### 3. Extract architecture using AI

```bash
strata ai extract path/to/document.md
```

Uses your existing AI subscription (Copilot, Claude Code, or Codex) to extract structured architecture from any free-form document.

---

## AI providers

Strata uses an **OAuth/CLI-only provider policy**.

| Provider | Status | Auth mechanism |
|---|---|---|
| **GitHub Copilot** | Enabled | `/model copilot` (auto-started device OAuth flow) |
| **Claude Code** | Enabled | `claude auth login` (Claude CLI OAuth) |
| **Codex CLI** | Enabled | `codex login` (Codex CLI OAuth) |
| GitHub Models | Disabled by policy | Visible for discoverability only |
| OpenAI API | Disabled by policy | Visible for discoverability only |
| Ollama | Disabled by policy | Visible for discoverability only |

```bash
strata ai status                      # show providers and policy state
strata ui                             # then use /model overview in TUI (or /auth <provider>)
```

`/model <provider> <model-id>` is transactional: Strata validates first, then persists.  
If validation fails, provider/model remains unchanged.

---

## Enterprise architecture

### Business capabilities

```bash
strata enterprise add-capability \
  --name "Order Management" \
  --domain "Commerce" \
  --level core \
  --owner "Commerce Team"

strata enterprise list-capabilities
```

Levels: `strategic` | `core` | `supporting`

### Application portfolio

```bash
strata enterprise add-application \
  --name "Order Service" \
  --hosting kubernetes \
  --criticality critical \
  --owner "Commerce Team"

strata enterprise list-applications
```

### Technology radar

```bash
strata enterprise add-standard \
  --name "Kafka" \
  --category "messaging" \
  --status adopt \
  --rationale "Proven durable event streaming"

strata enterprise tech-radar
```

Status rings: `adopt` | `trial` | `assess` | `hold`

---

## Data architecture

### Data domains

```bash
strata data add-domain --name "Orders" --owner "Commerce Team" --storage operational
strata data add-domain --name "Analytics" --owner "Data Team" --storage lakehouse
strata data list-domains
```

### Data products

```bash
strata data add-product \
  --name "Orders Feed" \
  --domain-id orders \
  --output-port streaming \
  --sla-tier gold \
  --owner "Commerce Team"

strata data list-products
```

SLA tiers: `bronze` | `silver` | `gold` | `platinum`

### Data flows

```bash
strata data add-flow \
  --name "Orders to Analytics" \
  --source orders \
  --target analytics \
  --mechanism streaming

strata data list-flows
```

---

## Solution architecture

### Create a solution design

```bash
strata solution create \
  --name "API Platform" \
  --description "Central API gateway layer" \
  --pattern api-gateway \
  --target aws

strata solution list
```

Patterns: `microservices` | `event-driven` | `api-gateway` | `layered` | `serverless` | `modular-monolith` | `data-mesh`

### Add components

```bash
strata solution add-component api-platform \
  --name "API Gateway" \
  --comp-type gateway \
  --technology "Kong" \
  --hosting kubernetes

strata solution show api-platform
```

Component types: `service` | `gateway` | `database` | `queue` | `cache` | `cdn` | `identity` | `storage` | `external`

### Architecture Decision Records (ADRs)

```bash
strata solution add-adr api-platform \
  --title "Use Kong over AWS API Gateway" \
  --context "Need vendor-neutral gateway for multi-cloud" \
  --decision "Adopt Kong — portable, extensible, Kubernetes-native"

strata solution list-adrs api-platform
```

---

## Generate diagrams and reports

```bash
# Mermaid business capability map
strata generate capability-map --output capability-map.mmd

# Mermaid data flow map
strata generate data-flow-map --output data-flow-map.mmd

# Mermaid solution architecture diagram
strata generate solution-diagram api-platform --output api-platform.mmd

# Full JSON architecture report
strata generate report --output architecture-report.json
```

---

## Validate cross-references

```bash
strata validate
```

Checks:
- Applications reference valid capability IDs
- Data products reference valid domain IDs
- Data flows reference valid domain IDs
- Solution components reference valid dependency IDs
- Solutions reference valid capability IDs

---

## Interactive improvement workflow

Run Strata without subcommands to open the TUI:

```bash
strata
```

Then use:

- `/improve` for a phased roadmap from maturity scoring
- `/improve-ai` to open the latest scheduled hybrid domain advisory backlog
- `/advisor` (or `/advisor status`) for the domain-rich advisor control panel
- `/advisor progress` for live run phases, recent events, and per-domain progress
- `/advisor status` to see advisor scheduler, provider health, latest run metadata, and run quality
- `/advisor interval <min>` to configure scheduled advisor cadence (`0` disables)

Workspace defaults (in `architecture/strata.yaml`):
- `advisor_enabled: true`
- `advisor_interval_minutes: 30`
- `advisor_profile: oda`

Inside `/improve-ai`:

- review domain attention board, domain insights, and synthesized decisions
- `draft <n>` generates document draft `n`
- `save <n>` writes draft `n` to a watch folder
- `quit` exits advisory drafting mode

---

## PSA markdown format

Strata can import PSA (Problem-Solution-Architecture) markdown files. See [`examples/psa-example.md`](examples/psa-example.md) for a full worked example.

Supported sections: `## Capabilities`, `## Applications`, `## Tech Standards`, `## Data Domains`, `## Data Products`, `## Data Flows`, `## Solutions`.

```bash
strata import examples/psa-example.md
strata import examples/psa-example.md --dry-run
```

---

## Workspace structure (GitOps-ready)

Commit your `architecture/` folder to git:

```
architecture/
  strata.yaml                       # workspace manifest
  enterprise/
    architecture.yaml               # capabilities, apps, tech standards
  data/
    architecture.yaml               # domains, products, flows
  solutions/
    api-platform.yaml               # one file per solution
    data-mesh-platform.yaml
```

---

## Command reference

```
strata init                          Initialise a new workspace
strata status                        Workspace overview
strata validate                      Cross-reference validation
strata import <file>                 Import PSA markdown into workspace

strata enterprise add-capability     Add a business capability
strata enterprise list-capabilities  List capabilities
strata enterprise add-application    Add to application portfolio
strata enterprise list-applications  List applications
strata enterprise add-standard       Add to tech radar
strata enterprise list-standards     List standards
strata enterprise tech-radar         Display technology radar

strata data add-domain               Add a data domain
strata data list-domains             List data domains
strata data add-product              Add a data product
strata data list-products            List data products
strata data add-flow                 Add a data flow
strata data list-flows               List data flows

strata solution create               Create a solution design
strata solution list                 List solution designs
strata solution show <id>            Show solution details
strata solution add-component <id>   Add a component
strata solution add-adr <id>         Record an ADR
strata solution list-adrs <id>       List ADRs

strata generate capability-map       Mermaid capability map
strata generate data-flow-map        Mermaid data flow diagram
strata generate solution-diagram     Mermaid solution diagram
strata generate report               Full JSON architecture report

strata ai status                     Show AI provider availability
strata ai configure                  Configure AI provider
strata ai extract <file>             Extract architecture from any document
```

---

## Development

```bash
pip install -e ".[dev]"
pytest
```
