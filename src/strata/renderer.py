from __future__ import annotations
import base64
import json
import re
import shutil
import subprocess
import tempfile
import zlib
from hashlib import md5
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .models import ArchitectureWorkspace, SolutionDesign


def _nid(prefix: str, value: str) -> str:
    digest = md5(value.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{prefix}_{digest}"


def print_workspace_status(console: Console, workspace: ArchitectureWorkspace) -> None:
    ea = workspace.enterprise
    da = workspace.data
    cloud = workspace.manifest.cloud_provider
    env = workspace.manifest.environment
    desc = workspace.manifest.description or "No description set"
    console.print(
        Panel(
            f"[dim]{desc}[/]\nCloud: [yellow]{cloud}[/]  Env: [yellow]{env}[/]",
            title=f"[bold cyan]{workspace.manifest.name}[/]  Architecture Workspace",
            expand=False,
        )
    )
    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("Domain", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Details")
    table.add_row(
        "Enterprise — Capabilities", str(len(ea.capabilities)),
        ", ".join(sorted({c.domain for c in ea.capabilities})) or "—",
    )
    table.add_row(
        "Enterprise — Applications", str(len(ea.applications)),
        f"{sum(1 for a in ea.applications if a.status == 'active')} active",
    )
    table.add_row(
        "Enterprise — Tech Standards", str(len(ea.standards)),
        f"{sum(1 for s in ea.standards if s.status == 'adopt')} adopted",
    )
    table.add_row("Data — Domains", str(len(da.domains)), "")
    table.add_row("Data — Products", str(len(da.products)), "")
    table.add_row("Data — Flows", str(len(da.flows)), "")
    table.add_row(
        "Solutions", str(len(workspace.solutions)),
        f"{sum(1 for s in workspace.solutions if s.status == 'approved')} approved",
    )
    console.print(table)


def render_capability_map(workspace: ArchitectureWorkspace, output: Path) -> None:
    org = workspace.manifest.name

    LEVEL_ORDER: dict[str, int] = {"strategic": 0, "core": 1, "supporting": 2}
    LEVEL_LABELS: dict[str, str] = {
        "strategic": "⚡ Strategic",
        "core":      "◆ Core",
        "supporting": "◇ Supporting",
    }
    # fill / stroke / color for the level *subgraph* container
    LEVEL_SG_STYLE: dict[str, str] = {
        "strategic": "fill:#112a1a,stroke:#2ea043,color:#aff5b4",
        "core":      "fill:#0d1f38,stroke:#388bfd,color:#a5d6ff",
        "supporting": "fill:#161b22,stroke:#6e7681,color:#8b949e",
    }
    # node fill matches its container but slightly lighter
    LEVEL_NODE_STYLE: dict[str, str] = {
        "strategic": "fill:#1a3a2a,stroke:#2ea043,color:#aff5b4,font-weight:bold",
        "core":      "fill:#1a2d4a,stroke:#388bfd,color:#a5d6ff",
        "supporting": "fill:#21262d,stroke:#6e7681,color:#8b949e",
    }

    lines = [
        "%%{init: {'theme': 'dark', 'themeVariables': {"
        "'fontSize': '13px', 'fontFamily': 'ui-monospace,monospace', "
        "'clusterBkg': '#0d1117', 'clusterBorder': '#30363d'}}}%%",
        "graph TD",
        "",
        "  classDef org fill:#2d1f3d,stroke:#8957e5,color:#d2a8ff,font-weight:bold",
    ] + [
        f"  classDef {lvl} {style}"
        for lvl, style in LEVEL_NODE_STYLE.items()
    ] + [
        "",
        f'  ORG["{org}"]',
        "  class ORG org",
        "",
    ]

    domains: dict[str, list] = {}
    for cap in workspace.enterprise.capabilities:
        domains.setdefault(cap.domain, []).append(cap)

    # Class + style directives must come after all node/subgraph definitions
    class_lines: list[str] = []
    style_lines: list[str] = []

    for domain, caps in sorted(domains.items()):
        d_id = _nid("D", domain)

        # Bucket capabilities by level
        by_level: dict[str, list] = {}
        for cap in caps:
            lvl = cap.level if cap.level in LEVEL_ORDER else "supporting"
            by_level.setdefault(lvl, []).append(cap)

        lines.append(f'  subgraph {d_id}["{domain}"]')
        lines.append(f'    direction TB')

        for lvl in sorted(by_level, key=lambda l: LEVEL_ORDER.get(l, 99)):
            sg_id = f"{d_id}_{lvl}"
            sg_label = LEVEL_LABELS.get(lvl, lvl.capitalize())
            lines.append(f'    subgraph {sg_id}["{sg_label}"]')
            for cap in sorted(by_level[lvl], key=lambda c: c.name):
                c_id = _nid("C", cap.id)
                name_parts = cap.name.split(" & ", 1)
                label = (" &\n".join(name_parts) if len(name_parts) > 1 else cap.name)
                lines.append(f'      {c_id}["{label}"]')
                class_lines.append(f"  class {c_id} {lvl}")
            lines.append("    end")
            style_lines.append(
                f"  style {sg_id} {LEVEL_SG_STYLE.get(lvl, LEVEL_SG_STYLE['supporting'])}"
            )

        lines.append("  end")
        lines.append(f"  ORG --> {d_id}")
        lines.append("")

    lines.append("  %% Node colours")
    lines.extend(class_lines)
    lines.append("  %% Level subgraph colours")
    lines.extend(style_lines)

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_data_flow_map(workspace: ArchitectureWorkspace, output: Path) -> None:
    lines = ["graph LR"]
    domain_ids = {d.id: _nid("DD", d.id) for d in workspace.data.domains}
    for domain in workspace.data.domains:
        nid = domain_ids[domain.id]
        lines.append(f'  {nid}["{domain.name}"]')
    for flow in workspace.data.flows:
        src_id = domain_ids.get(flow.source_domain, _nid("EXT", flow.source_domain))
        tgt_id = domain_ids.get(flow.target_domain, _nid("EXT", flow.target_domain))
        if flow.source_domain not in domain_ids:
            lines.append(f'  {src_id}["{flow.source_domain} (ext)"]')
        if flow.target_domain not in domain_ids:
            lines.append(f'  {tgt_id}["{flow.target_domain} (ext)"]')
        lines.append(f'  {src_id} -->|"{flow.mechanism}"| {tgt_id}')
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _component_shape(comp_type: str) -> tuple[str, str]:
    return {
        "database": ("[(", ")]"),
        "queue": ("([", "])"),
        "cache": ("((", "))"),
        "gateway": ("[/", "/]"),
        "external": ("{{", "}}"),
    }.get(comp_type, ("[", "]"))


def render_solution_diagram(solution: SolutionDesign, output: Path) -> None:
    lines = [f'graph LR', f'  subgraph "{solution.name} [{solution.pattern}]"']
    comp_ids = {c.id: _nid("C", c.id) for c in solution.components}
    for comp in solution.components:
        nid = comp_ids[comp.id]
        o, c = _component_shape(comp.type)
        label = comp.name
        if comp.technology:
            label += f"\\n{comp.technology}"
        lines.append(f'    {nid}{o}"{label}"{c}')
    lines.append("  end")
    for comp in solution.components:
        for dep in comp.dependencies:
            src = comp_ids.get(comp.id)
            tgt = comp_ids.get(dep)
            if src and tgt:
                lines.append(f"  {src} --> {tgt}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Diagram preview helpers ───────────────────────────────────────────────────


def mermaid_live_url(code: str) -> str:
    """Return a mermaid.live URL with the diagram pre-loaded (pako/zlib + base64url)."""
    state = json.dumps({"code": code, "mermaid": {"theme": "dark"}})
    # pako.deflate uses standard zlib framing (wbits=15)
    compressed = zlib.compress(state.encode("utf-8"), level=9)
    encoded = base64.urlsafe_b64encode(compressed).decode().rstrip("=")
    return f"https://mermaid.live/edit#pako:{encoded}"


def _build_mermaid_html(title: str, code: str) -> str:
    """Return a self-contained HTML page that renders the Mermaid diagram via CDN."""
    safe = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Strata — {title}</title>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 2rem 3rem;
      background: #0d1117; color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    h1 {{ color: #58a6ff; margin-bottom: 1.5rem; font-size: 1.4rem; }}
    .badge {{
      display: inline-block; background: #161b22; border: 1px solid #30363d;
      border-radius: 6px; padding: 0.15rem 0.6rem; font-size: 0.75rem;
      color: #8b949e; margin-bottom: 1.5rem;
    }}
    .diagram-wrap {{
      background: #161b22; border: 1px solid #30363d; border-radius: 8px;
      padding: 2rem; overflow: auto;
    }}
    .mermaid {{ display: flex; justify-content: center; }}
  </style>
</head>
<body>
  <h1>📊 {title}</h1>
  <span class="badge">generated by strata-cli</span>
  <div class="diagram-wrap">
    <div class="mermaid">{safe}</div>
  </div>
  <script>mermaid.initialize({{ startOnLoad: true, theme: "dark" }});</script>
</body>
</html>
"""


def render_diagram_preview(mermaid_code: str, title: str) -> Path:
    """Render a Mermaid diagram to a file and return its path.

    Strategy:
    1. Try ``mmdc`` (mermaid-cli) → renders to SVG for pixel-perfect output.
    2. Fall back to a self-contained HTML file using mermaid.js CDN.

    The caller is responsible for opening the returned path.
    """
    safe_title = re.sub(r"[^\w-]", "-", title.lower())[:40].strip("-")
    out_dir = Path(tempfile.gettempdir()) / "strata-diagrams"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Try mmdc ──────────────────────────────────────────────────────────
    mmdc = shutil.which("mmdc")
    if mmdc:
        mmd_file = out_dir / f"{safe_title}.mmd"
        svg_file = out_dir / f"{safe_title}.svg"
        mmd_file.write_text(mermaid_code, encoding="utf-8")
        try:
            result = subprocess.run(
                [
                    mmdc,
                    "-i", str(mmd_file),
                    "-o", str(svg_file),
                    "--theme", "dark",
                    "--width", "1600",
                    "--backgroundColor", "#0d1117",
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0 and svg_file.exists() and svg_file.stat().st_size > 0:
                return svg_file
        except (subprocess.TimeoutExpired, OSError):
            pass

    # ── 2. HTML fallback ─────────────────────────────────────────────────────
    html_file = out_dir / f"{safe_title}.html"
    html_file.write_text(_build_mermaid_html(title, mermaid_code), encoding="utf-8")
    return html_file
