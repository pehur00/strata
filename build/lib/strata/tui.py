"""Full-screen terminal UI for Strata — Architecture as a Service.

Provides a conversational interface with AI-powered architecture management.

Launch:
    strata ui
"""
from __future__ import annotations

import re as _re
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, cast

from rich import box as rich_box
from rich.panel import Panel as RichPanel
from rich.table import Table as RichTable
from rich.text import Text
from textual import events, on, work
from textual.suggester import Suggester
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Footer, Header, Input, Label, RichLog, Rule, Static,
)

from .agent import AgentError, ArchitectureAgent, save_config
from .cli import _write_entity
from .models import ArchitectureWorkspace, StagedItem
from .providers import get_provider, list_provider_ids
from .renderer import (
    mermaid_live_url,
    render_capability_map,
    render_data_flow_map,
    render_diagram_preview,
    render_solution_diagram,
)
from .workspace import (
    WorkspaceError, load_staging, load_workspace,
    next_staging_id, save_staging, save_workspace,
    add_watch_folder, remove_watch_folder, load_watch_folders,
    set_advisor_interval, set_scan_interval,
)
from . import db as _db
from .watcher import FolderWatcher, is_available as _watcher_available
from .tracker import FileTracker
from .advisor import advisory_to_todo_items, load_latest_advisory, run_advisory_cycle
from .scoring import (
    score_workspace, list_profiles, ScoreResult,
    compute_top_improvements, compute_roadmap_phases, RoadmapPhase,
    TodoItem, build_todo_list,
)
from .analyzer import compute_stack_coverage

# ── Slash completions (for Tab autocomplete) ──────────────────────────

_SLASH_COMPLETIONS: list[str] = [
    "/help", "/dashboard", "/capabilities", "/applications", "/standards",
    "/domains", "/products", "/flows", "/solutions", "/folders",
    "/scan-all", "/clear", "/reload",
    "/watch", "/watch start", "/watch stop", "/watch status",
    "/watch interval ",
    "/advisor", "/advisor status", "/advisor progress", "/advisor interval ",
    "/staging", "/staging accept-all", "/staging reject-all",
    "/staging accept ", "/staging reject ", "/staging clear",
    "/staging impact",
    "/diagram", "/diagram capability-map", "/diagram data-flow",
    "/diagram solution ",
    "/add-folder ", "/remove-folder ", "/scan ",
    "/score", "/score telecom", "/score default", "/score oda",
    "/stack",
    "/improve",
    "/improve-ai",
    "/auth", "/auth copilot", "/auth claude", "/auth codex",
    "/copilot-auth",  # Backward-compatible alias; prefer /auth copilot or /model copilot
    "/model",
    "/model overview",
    "/model auto", "/model claude", "/model copilot",
    "/model codex", "/model ollama", "/model github",
    # copilot models
    "/model copilot gpt-4o", "/model copilot gpt-4.1",
    "/model copilot claude-sonnet-4-6", "/model copilot claude-sonnet-4-5",
    "/model copilot o3-mini", "/model copilot o4-mini",
    # claude models
    "/model claude claude-opus-4-5", "/model claude claude-sonnet-4-5",
    "/model claude claude-haiku-4-5",
    # github models
    "/model github gpt-4o", "/model github gpt-4o-mini",
    # ollama models
    "/model ollama llama3.1", "/model ollama llama3.3", "/model ollama mistral",
]


class SlashSuggester(Suggester):
    """Inline ghost-text suggestions for slash commands (fish-shell style).

    Tab or → accepts the greyed-out suggestion.  Shows the first
    alphabetically-sorted match; if the input already equals a completion
    exactly, suggests the next one so repeated Tab still cycles.

    Pass ``get_folders`` to inject live watched-folder paths for
    ``/remove-folder`` and ``/scan`` completions.
    """

    def __init__(self, get_folders: Callable[[], list[str]] | None = None) -> None:
        super().__init__(use_cache=False)
        self._get_folders = get_folders or (lambda: [])

    async def get_suggestion(self, value: str) -> str | None:
        if not value.startswith("/"):
            return None

        # Dynamic folder completions for /remove-folder and /scan
        for prefix in ("/remove-folder ", "/scan "):
            if value.startswith(prefix):
                typed = value[len(prefix):]
                folders = self._get_folders()
                matches = sorted(
                    f for f in folders
                    if f.startswith(typed) and (prefix + f) != value
                )
                return (prefix + matches[0]) if matches else None

        # Static slash command completions
        matches = sorted(
            c for c in _SLASH_COMPLETIONS if c.startswith(value) and c != value
        )
        return matches[0] if matches else None

# ── Staging helpers ────────────────────────────────────────────────────────────

_ENTITY_SYNONYMS: dict[str, str] = {
    "capability":    "capability",  "capabilities":  "capability",
    "application":   "application", "applications":  "application",
    "app":           "application", "apps":           "application",
    "standard":      "standard",    "standards":      "standard",
    "tech":          "standard",    "technology":     "standard",
    "domain":        "domain",      "domains":        "domain",
    "product":       "product",     "products":        "product",
    "flow":          "flow",        "flows":           "flow",
    "solution":      "solution",    "solutions":       "solution",
}

def _is_staging_id(s: str) -> bool:
    """Return True if *s* looks like a staging ID (stg-NNN) or row number."""
    return bool(_re.fullmatch(r"stg-\d+", s)) or s.isdigit()

# ── CSS ────────────────────────────────────────────────────────────────────────

APP_CSS = """
/* ── Root ──────────────────────────────────────────────────────────────────── */
Screen {
    background: $background;
    layers: base overlay;
}

#root {
    layout: horizontal;
    height: 1fr;
}

/* ── Sidebar ────────────────────────────────────────────────────────────────── */

#sidebar {
    width: 26;
    min-width: 26;
    background: $panel;
    border-right: tall $primary-darken-3;
    padding: 0;
    overflow-y: auto;
    layer: base;
}

.side-section {
    height: 1;
    padding: 0 2;
    color: $text-muted;
    text-style: bold;
    margin-top: 1;
    background: $panel;
}

.side-item {
    height: 1;
    padding: 0 0 0 4;
    width: 100%;
    color: $text;
}

.side-item:hover {
    background: $primary-darken-2;
}

.side-item.active-nav {
    background: $primary;
    color: $background;
    text-style: bold;
}

/* ── Main panel ─────────────────────────────────────────────────────────────── */

#main {
    width: 1fr;
    layout: vertical;
    height: 1fr;
}

/* ── Dashboard pane (persistent, always visible) ────────────────────────────── */

#dashboard-pane {
    height: 4fr;
    padding: 0 1;
    background: $background;
    border-bottom: tall $primary-darken-3;
    overflow-y: auto;
}

/* ── Chat log (command output / AI chat) ─────────────────────────────────────── */

#chat-log {
    height: 1fr;
    padding: 1 2;
    background: $background;
    border: none;
    overflow-y: auto;
}

/* ── Action bar (pending AI suggestion) ─────────────────────────────────────── */

#action-bar {
    height: 3;
    layout: horizontal;
    background: $warning-darken-3;
    border-top: tall $warning-darken-1;
    padding: 0 2;
    align: left middle;
    display: none;
}

#action-bar.visible {
    display: block;
    layout: horizontal;
    align: left middle;
}

#action-desc {
    width: 1fr;
    color: $text;
}

#btn-accept {
    min-width: 12;
    margin-left: 1;
}

#btn-stage {
    min-width: 10;
    margin-left: 1;
}

#btn-reject {
    min-width: 12;
    margin-left: 1;
}

/* ── Input row ──────────────────────────────────────────────────────────────── */

#input-row {
    height: 3;
    layout: horizontal;
    background: $panel;
    border-top: tall $primary-darken-3;
    padding: 0 1;
    align: left middle;
}

#prompt-icon {
    width: 5;
    color: $primary;
    text-style: bold;
    padding: 0 1;
    content-align: right middle;
}

#user-input {
    width: 1fr;
    border: none;
    background: transparent;
    color: $text;
}

/* ── Confirm modal ──────────────────────────────────────────────────────────── */

ConfirmModal {
    align: center middle;
}

#confirm-box {
    background: $surface;
    border: thick $primary;
    padding: 1 2;
    width: 62;
    height: auto;
    max-height: 24;
}

#confirm-title {
    text-style: bold;
    margin-bottom: 1;
}

#confirm-btns {
    layout: horizontal;
    height: 3;
    align: right middle;
    margin-top: 1;
}

#confirm-btns Button {
    margin-left: 1;
    min-width: 10;
}
"""

# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class PendingAction:
    entity: str
    fields: dict[str, Any]
    description: str

# ── Widgets ────────────────────────────────────────────────────────────────────

class NavItem(Static):
    """Clickable sidebar navigation label."""

    def __init__(self, text: str, view: str, nav_id: str) -> None:
        super().__init__(text, id=nav_id, markup=True)
        self._view = view

    def on_click(self) -> None:
        cast(StrataApp, self.app)._navigate(self._view)


class CommandInput(Input):
    """Input pre-wired with SlashSuggester for inline ghost-text completion."""
    pass


class ConfirmModal(ModalScreen[bool]):
    """Yes / No modal."""

    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    #confirm-box {
        background: $surface; border: thick $warning;
        padding: 1 2; width: 60; height: auto;
    }
    #confirm-btns { layout: horizontal; height: 3; align: right middle; margin-top: 1; }
    #confirm-btns Button { margin-left: 1; min-width: 10; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._msg = message

    def compose(self) -> ComposeResult:
        with Container(id="confirm-box"):
            yield Label(self._msg)
            with Horizontal(id="confirm-btns"):
                yield Button("Yes", id="yes", variant="success")
                yield Button("No", id="no", variant="error")

    @on(Button.Pressed)
    def handle_button(self, e: Button.Pressed) -> None:
        self.dismiss(e.button.id == "yes")


# ── Main application ───────────────────────────────────────────────────────────

class StrataApp(App[None]):
    """Full-screen TUI for Strata."""

    TITLE = "Strata"
    CSS = APP_CSS

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+e", "nav_enterprise", "Enterprise", show=False),
        Binding("ctrl+d", "nav_data", "Data", show=False),
        Binding("ctrl+s", "nav_solutions", "Solutions", show=False),
        Binding("ctrl+t", "nav_staging", "Staging", show=False),
        Binding("escape", "escape_action", "Back", show=False),
        Binding("f1", "show_help", "Help"),
        Binding("ctrl+y", "copy_response", "Copy", show=False),
    ]

    def __init__(self, provider: str = "auto") -> None:
        super().__init__()
        self._provider = provider
        self._workspace: ArchitectureWorkspace | None = None
        self._pending: PendingAction | None = None
        self._history: list[dict[str, str]] = []   # conversation turns (no system)
        self._pending_queue: list[PendingAction] = []  # multi-action queue
        self._watcher: FolderWatcher | None = None
        self._debounce_timer: Any = None           # Textual Timer handle
        self._debounce_paths: set[str] = set()     # paths queued for auto-scan
        self._db_url: str = _db.DEFAULT_URL
        self._tracker: FileTracker | None = None
        self._scan_timer: Any = None               # scheduled scan interval handle
        self._advisor_timer: Any = None            # scheduled advisor interval handle
        self._advisor_last_run: str = ""
        self._advisor_last_status: str = ""
        self._advisor_next_run_at: datetime | None = None
        self._advisor_runtime_run_id: str = ""
        self._advisor_runtime_state: str = "idle"  # idle|running|ok|failed|degraded
        self._advisor_runtime_phase: str = "idle"
        self._advisor_runtime_phase_started_at: str = ""
        self._advisor_runtime_phase_started_mono: float = 0.0
        self._advisor_phase_durations: dict[str, float] = {}
        self._advisor_recent_events: list[dict[str, Any]] = []
        self._advisor_last_timeline: list[dict[str, Any]] = []
        self._advisor_domain_progress: dict[str, dict[str, Any]] = {}
        self._last_score: ScoreResult | None = None  # cached from last dashboard render
        self._last_ai_text: str = ""               # last AI response for Ctrl+Y copy
        self._cached_copilot_models: list[dict] = []   # live models from Copilot API
        # ── /improve-ai workflow state ──────────────────────────────────────
        self._wf_active: bool = False
        self._wf_todo: list[TodoItem] = []
        self._wf_existing_docs: list[str] = []
        self._wf_next_adr_num: int = 1
        self._wf_profile: str = "default"

    # ── Layout ─────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Horizontal(id="root"):
            # ── Sidebar ──
            with Container(id="sidebar"):
                yield Static(" ◈  STRATA", classes="side-section")
                yield Rule()

                yield Static("  WORKSPACE", classes="side-section")
                yield NavItem("  📊  Dashboard", "dashboard", "nav-dashboard")

                yield Static("  ENTERPRISE", classes="side-section")
                yield NavItem("  ⚡  Capabilities", "capabilities", "nav-capabilities")
                yield NavItem("  📱  Applications", "applications", "nav-applications")
                yield NavItem("  🔬  Standards", "standards", "nav-standards")

                yield Static("  DATA", classes="side-section")
                yield NavItem("  🗄   Domains", "domains", "nav-domains")
                yield NavItem("  📦  Products", "products", "nav-products")
                yield NavItem("  🔀  Flows", "flows", "nav-flows")

                yield Static("  SOLUTIONS", classes="side-section")
                yield NavItem("  🔷  Solutions", "solutions", "nav-solutions")

                yield Static("  DIAGRAMS", classes="side-section")
                yield NavItem("  📊  Capability Map", "diagram-caps", "nav-diagram-caps")
                yield NavItem("  📊  Data Flow", "diagram-data", "nav-diagram-data")

                yield Static("  AI", classes="side-section")
                yield NavItem("  📋  Staging", "staging", "nav-staging")
                yield NavItem("  📁  Watch Folders", "folders", "nav-folders")
                yield NavItem("  👁  Live Watch", "live-watch", "nav-live-watch")
                yield NavItem("  📈  Maturity Score", "score", "nav-score")
                yield NavItem("  🗺️   Stack Coverage", "stack", "nav-stack")
                yield NavItem("  💬  Chat", "chat", "nav-chat")

            # ── Main ──
            with Container(id="main"):
                yield RichLog(id="dashboard-pane", markup=True, highlight=False, wrap=True)
                yield RichLog(id="chat-log", markup=True, highlight=False, wrap=True)

        # ── Action bar (shown when AI returns a suggestion) ──
        with Horizontal(id="action-bar"):
            yield Label("", id="action-desc")
            yield Button("✓ Accept", id="btn-accept", variant="success")
            yield Button("⏸ Stage", id="btn-stage", variant="warning")
            yield Button("✗ Reject", id="btn-reject", variant="error")

        # ── Input ──
        with Horizontal(id="input-row"):
            yield Label(" ❯ ", id="prompt-icon")
            yield CommandInput(
                placeholder="Ask anything, or /help for commands…",
                id="user-input",
                suggester=SlashSuggester(
                    get_folders=lambda: list(
                        self._workspace.manifest.watch_folders
                    ) if self._workspace else []
                ),
            )

        yield Footer()

    # ── Startup ────────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._boot()

    def _boot(self) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write(
            "\n[bold cyan]◈  Strata[/bold cyan]  "
            "[dim]Architecture as a Service[/dim]\n"
        )
        try:
            self._workspace = load_workspace()
            self.sub_title = self._workspace.manifest.name
            self._refresh_sidebar()
            self._show_dashboard()
            # Initialise file tracker
            try:
                self._tracker = FileTracker()
            except Exception:
                self._tracker = None
            # Start scheduler if interval configured
            self._start_scheduler()
            self._start_advisor_scheduler()
            log.write(
                "[dim]Dashboard pinned above.  "
                "Type [cyan]/help[/cyan] for commands, or ask anything naturally."
                "  [cyan]Ctrl+Q[/cyan] to quit.[/dim]\n"
            )
            self._check_startup_provider_health()
        except WorkspaceError:
            self.sub_title = "no workspace"
            log.write(
                "[yellow]No workspace found.[/yellow]  "
                "Run [cyan]strata init[/cyan] first, then "
                "[cyan]strata ui[/cyan] again.\n"
            )
            log.write(
                "[dim]Type a question or command.  "
                "[cyan]/help[/cyan] for all commands.  "
                "[cyan]Ctrl+Q[/cyan] to quit.[/dim]\n"
            )
            self._start_advisor_scheduler()
            self._check_startup_provider_health()
        self.query_one("#user-input", CommandInput).focus()

    def _check_startup_provider_health(self) -> None:
        """Auto-recover invalid persisted provider by starting auth if supported."""
        configured = self._config_get("provider", "auto").strip().lower()
        if configured in ("", "auto"):
            return
        if configured not in list_provider_ids():
            return
        try:
            ok, msg = ArchitectureAgent(provider=configured).check_available()
        except Exception as exc:
            ok, msg = False, str(exc)
        if ok:
            return
        provider = get_provider(configured)
        if not provider.supports_interactive_auth():
            self._log(
                f"[yellow]Configured provider '{configured}' failed runtime validation.[/yellow]\n"
                f"  [dim]{msg}[/dim]\n"
                f"  [dim]{provider.auth_remediation()}[/dim]\n"
            )
            return

        self._log(
            f"[yellow]Configured provider '{configured}' failed runtime validation.[/yellow]\n"
            f"  [dim]{msg}[/dim]\n"
            f"  [dim]Starting automatic {configured} authentication flow…[/dim]\n"
        )
        self._start_provider_auth(configured, self._provider)

    def on_unmount(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
        if self._advisor_timer is not None:
            try:
                self._advisor_timer.stop()
            except Exception:
                pass

    # ── Sidebar helpers ────────────────────────────────────────────────────────

    def _refresh_sidebar(self) -> None:
        if not self._workspace:
            return
        ws = self._workspace
        staging = load_staging()
        pending = sum(1 for s in staging if s.status == "pending")

        updates: dict[str, str] = {
            "nav-capabilities": f"  ⚡  Capabilities ({len(ws.enterprise.capabilities)})",
            "nav-applications": f"  📱  Applications ({len(ws.enterprise.applications)})",
            "nav-standards":    f"  🔬  Standards ({len(ws.enterprise.standards)})",
            "nav-domains":      f"  🗄   Domains ({len(ws.data.domains)})",
            "nav-products":     f"  📦  Products ({len(ws.data.products)})",
            "nav-flows":        f"  🔀  Flows ({len(ws.data.flows)})",
            "nav-solutions":    f"  🔷  Solutions ({len(ws.solutions)})",
            "nav-staging":      f"  📋  Staging ([yellow]{pending}[/yellow] pending)"
                                if pending else "  📋  Staging",
            "nav-folders":      f"  📁  Watch Folders ({len(ws.manifest.watch_folders)})",
            "nav-live-watch": (
                f"  👁  [green]● Watching ({self._watcher.active_folder_count})[/green]"
                if self._watcher and self._watcher.is_running
                else "  👁  Live Watch"
            ),
        }
        for nav_id, text in updates.items():
            try:
                self.query_one(f"#{nav_id}", NavItem).update(text)
            except Exception:
                pass

    # ── Chat output helpers ────────────────────────────────────────────────────

    def _log(self, text: str) -> None:
        self.query_one("#chat-log", RichLog).write(text)

    def _log_user(self, text: str) -> None:
        self.query_one("#chat-log", RichLog).write(
            f"[bold cyan]You[/bold cyan]  {text}"
        )

    def _log_strata(self, text: str) -> None:
        self.query_one("#chat-log", RichLog).write(
            f"[bold green]Strata[/bold green]  {text}"
        )

    def _log_error(self, text: str) -> None:
        self.query_one("#chat-log", RichLog).write(
            f"[bold red]Error[/bold red]  {text}"
        )

    def _log_table(self, table: RichTable) -> None:
        self.query_one("#chat-log", RichLog).write(table)

    def _log_ai_response(self, text: str) -> None:
        """Log an AI response to chat and capture it for Ctrl+Y copy."""
        self.query_one("#chat-log", RichLog).write(text)
        self._last_ai_text = text

    # ── Main-pane writers (data views replace dashboard content) ──────────────

    def _main(self, text: str) -> None:
        """Write rich text into the main (dashboard) pane."""
        self.query_one("#dashboard-pane", RichLog).write(text)

    def _main_table(self, table: RichTable) -> None:
        """Write a Rich table into the main (dashboard) pane."""
        self.query_one("#dashboard-pane", RichLog).write(table)

    def _open_main(self, title: str) -> None:
        """Clear the main pane and write a section header — call before any _main() writes."""
        pane = self.query_one("#dashboard-pane", RichLog)
        pane.clear()
        if title:
            pane.write(f"\n[bold]{title}[/bold]\n")

    # ── Input handler ──────────────────────────────────────────────────────────

    @on(Input.Submitted, "#user-input")
    def handle_input(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        # ── /improve-ai workflow intercept ─────────────────────────────────
        if self._wf_active:
            low = text.lower().strip()
            if low in ("quit", "exit", "q"):
                self._exit_wf()
                return
            if low.startswith("decide "):
                decision_text = text[7:].strip()
                self._log(f"[green]📌 Decision recorded:[/green] {decision_text}\n")
                return
            if low.startswith("draft"):
                parts = low.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    self._log("[dim]Usage: [cyan]draft <n>[/cyan]  e.g. [cyan]draft 3[/cyan][/dim]")
                    return
                todo_idx = int(parts[1]) - 1
                if not self._wf_todo:
                    self._log("[dim]No to-do list yet — wait for AI to finish.[/dim]")
                    return
                if todo_idx < 0 or todo_idx >= len(self._wf_todo):
                    self._log(f"[dim]No item #{parts[1]}.[/dim]")
                    return
                self._ai_draft_todo_item(todo_idx)
                return
            if low.startswith("save"):
                parts = low.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    self._log("[dim]Usage: [cyan]save <n>[/cyan]  e.g. [cyan]save 3[/cyan][/dim]")
                    return
                todo_idx = int(parts[1]) - 1
                if not self._wf_todo or todo_idx < 0 or todo_idx >= len(self._wf_todo):
                    self._log(f"[dim]No item #{parts[1]}.[/dim]")
                    return
                item = self._wf_todo[todo_idx]
                if not item.draft_content:
                    self._log(
                        f"[dim]No draft for #{parts[1]} yet. "
                        f"Type [cyan]draft {todo_idx + 1}[/cyan] first.[/dim]"
                    )
                    return
                self._save_todo_item(todo_idx)
                return
            # Free text while workflow active
            self._log_user(text)
            self._log(
                "[dim]Workflow active. Type [cyan]draft <n>[/cyan], "
                "[cyan]save <n>[/cyan], or [cyan]quit[/cyan].[/dim]"
            )
            return

        # Pending action shorthand
        if self._pending:
            low = text.lower()
            if low in ("y", "yes", "accept", "ok"):
                self._accept_pending()
                return
            if low in ("n", "no", "reject", "skip", "discard"):
                self._reject_pending()
                return
            if low in ("s", "stage"):
                self._stage_pending()
                return

        self._log_user(text)

        # Slash commands
        if text.startswith("/"):
            self._handle_slash(text[1:].strip())
            return

        # ── Staging commands ────────────────────────────────────────────────
        # accept/reject/approve — handle IDs, bulk "all", and entity-filtered bulk
        low = text.lower()
        low_parts = low.split()
        raw_parts = text.split()
        verb = low_parts[0] if low_parts else ""

        is_accept = verb in ("accept", "approve")
        is_reject = verb in ("reject", "discard", "decline")

        # Also catch full-sentence variants: "please accept all", "can you reject all apps"
        if not (is_accept or is_reject):
            for i, word in enumerate(low_parts):
                if word in ("accept", "approve"):
                    is_accept = True
                    low_parts = low_parts[i:]  # re-anchor to verb
                    break
                if word in ("reject", "discard", "decline"):
                    is_reject = True
                    low_parts = low_parts[i:]
                    break
            verb = low_parts[0] if low_parts else ""

        if is_accept or is_reject:
            action = "accept" if is_accept else "reject"
            rest = low_parts[1:]
            # strip filler words so "accept all pending staged items" → rest=["all", ...]
            rest = [w for w in rest if w not in ("please", "the", "pending", "staged", "items", "item")]

            if not rest:
                # bare "accept" / "reject" with no qualifiers — let AI handle
                pass
            elif rest[0] == "all":
                # "accept all" / "accept all capabilities" / "accept all pending"
                entity_filter: str | None = None
                for word in rest[1:]:
                    if word in _ENTITY_SYNONYMS:
                        entity_filter = _ENTITY_SYNONYMS[word]
                        break
                if action == "accept":
                    self._accept_all_staging(entity_filter)
                else:
                    self._reject_all_staging(entity_filter)
                return
            elif len(rest) == 1 and _is_staging_id(rest[0]):
                # "accept stg-007" / "accept 5"
                self._staging_action(action, rest[0])
                return
            # else: ambiguous natural language — fall through to AI

        # ── Folder inline commands ──────────────────────────────────────────
        if len(raw_parts) >= 3 and raw_parts[0].lower() == "add" and raw_parts[1].lower() == "folder":
            self._add_folder(raw_parts[2])
            return
        if len(raw_parts) >= 3 and raw_parts[0].lower() == "remove" and raw_parts[1].lower() == "folder":
            self._remove_folder(raw_parts[2])
            return

        # Natural language → AI
        if not self._workspace:
            self._log_error("No workspace. Run [cyan]strata init[/cyan] first.")
            return

        self._log("[dim]Thinking…[/dim]")
        self._ai_chat(text)

    # ── Slash command dispatch ─────────────────────────────────────────────────

    def _handle_slash(self, cmd: str) -> None:
        parts = cmd.split(maxsplit=1)
        verb = parts[0].lower() if parts else ""
        arg = parts[1] if len(parts) > 1 else ""

        # ── /staging <subcommand> submenu ───────────────────────────────────
        if verb == "staging":
            self._handle_staging_slash(arg.strip())
            self.query_one("#user-input", CommandInput).focus()
            return

        # ── /diagram <subcommand> submenu ────────────────────────────────
        if verb in ("diagram", "diagrams"):
            self._handle_diagram_slash(arg.strip())
            self.query_one("#user-input", CommandInput).focus()
            return

        if verb == "watch":
            self._handle_watch_slash(arg)
            self.query_one("#user-input", CommandInput).focus()
            return

        if verb == "advisor":
            self._handle_advisor_slash(arg)
            self.query_one("#user-input", CommandInput).focus()
            return

        if verb == "score":
            self._show_score(arg.strip() if arg else "")
            self.query_one("#user-input", CommandInput).focus()
            return

        if verb == "stack":
            self._show_stack()
            self.query_one("#user-input", CommandInput).focus()
            return

        if verb == "improve":
            self._show_improve_roadmap()
            self.query_one("#user-input", CommandInput).focus()
            return

        if verb == "copilot-auth":
            self._start_provider_auth("copilot", self._provider)
            self.query_one("#user-input", CommandInput).focus()
            return

        if verb == "auth":
            provider = arg.strip().lower() if arg.strip() else self._provider
            if provider == "auto":
                provider = ArchitectureAgent(provider="auto")._effective_provider()
            self._start_provider_auth(provider, self._provider)
            self.query_one("#user-input", CommandInput).focus()
            return

        if verb == "improve-ai":
            self._start_improve_ai_workflow()
            self.query_one("#user-input", CommandInput).focus()
            return

        if verb == "model":
            _mparts = arg.strip().split(None, 1) if arg.strip() else []
            if (not _mparts) or (_mparts[0].lower() in {"overview", "status", "list"}):
                self._show_model_picker()
            else:
                _mprovider = _mparts[0].lower()
                _mmodel = _mparts[1].strip() if len(_mparts) > 1 else ""
                self._set_model_provider(_mprovider, _mmodel)
            self.query_one("#user-input", CommandInput).focus()
            return

        routes: dict[str, Any] = {
            "help":         self._show_help,
            "dashboard":    self._show_dashboard,
            "home":         self._show_dashboard,
            "caps":         self._show_capabilities,
            "capabilities": self._show_capabilities,
            "apps":         self._show_applications,
            "applications": self._show_applications,
            "standards":    self._show_standards,
            "radar":        self._show_standards,
            "domains":      self._show_domains,
            "products":     self._show_products,
            "flows":        self._show_flows,
            "solutions":    self._show_solutions,
            "folders":      self._show_folders,
            "scan-all":     self._scan_all_folders,
            "chat":         lambda: None,
            "clear":        self._clear_chat,
            "reload":       self._reload_workspace,
        }

        if verb in routes:
            routes[verb]()
        elif verb == "scan":
            if not arg:
                self._log_error("Usage: /scan <path>")
            else:
                self._scan_path(arg)
        elif verb in ("add-folder",) and arg:
            self._add_folder(arg)
        elif verb in ("remove-folder",) and arg:
            self._remove_folder(arg)
        else:
            self._log_error(
                f"Unknown command: [cyan]/{verb}[/cyan]  —  type [cyan]/help[/cyan]"
            )

        self.query_one("#user-input", CommandInput).focus()

    def _handle_staging_slash(self, sub: str) -> None:
        """Dispatch /staging <subcommand>."""
        parts = sub.split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""
        arg = parts[1] if len(parts) > 1 else ""

        if subcmd in ("", "list", "show"):
            self._show_staging()
        elif subcmd in ("accept-all", "acceptall", "approve-all", "approveall"):
            # optional entity filter: /staging accept-all capabilities
            ef = _ENTITY_SYNONYMS.get(arg.strip().lower()) if arg.strip() else None
            self._accept_all_staging(ef)
        elif subcmd in ("reject-all", "rejectall"):
            ef = _ENTITY_SYNONYMS.get(arg.strip().lower()) if arg.strip() else None
            self._reject_all_staging(ef)
        elif subcmd in ("accept", "approve"):
            if not arg:
                self._log_error("Usage: /staging accept <id>")
            else:
                self._staging_action("accept", arg.strip())
        elif subcmd in ("reject", "decline"):
            if not arg:
                self._log_error("Usage: /staging reject <id>")
            else:
                self._staging_action("reject", arg.strip())
        elif subcmd == "scan-all":
            self._scan_all_folders()
        elif subcmd == "clear":
            self._clear_staging()
        elif subcmd == "impact":
            self._ai_staging_impact()
        else:
            self._log(
                "[bold]Staging subcommands:[/bold]\n"
                "  [cyan]/staging[/cyan]                      — show pending items\n"
                "  [cyan]/staging accept-all[/cyan]           — accept all pending\n"
                "  [cyan]/staging accept-all <entity>[/cyan]  — accept by type (capabilities, apps…)\n"
                "  [cyan]/staging reject-all[/cyan]           — reject all pending\n"
                "  [cyan]/staging accept <id>[/cyan]          — accept one item\n"
                "  [cyan]/staging reject <id>[/cyan]          — reject one item\n"
                "  [cyan]/staging clear[/cyan]                — discard all staging data\n"
                "  [cyan]/staging impact[/cyan]               — AI-powered impact analysis\n"
            )

    def _handle_diagram_slash(self, sub: str) -> None:
        """Dispatch /diagram <subcommand>."""
        parts = sub.split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if subcmd in ("", "list", "help"):
            self._log(
                "[bold]Diagram subcommands:[/bold]\n"
                "  [cyan]/diagram capability-map[/cyan]    — Business capability map (Mermaid)\n"
                "  [cyan]/diagram data-flow[/cyan]         — Data flow diagram (Mermaid)\n"
                "  [cyan]/diagram solution <id>[/cyan]     — Solution architecture diagram (Mermaid)\n"
            )
        elif subcmd in ("capability-map", "capability", "caps", "capabilities"):
            self._show_diagram_capability_map()
        elif subcmd in ("data-flow", "dataflow", "data", "flows"):
            self._show_diagram_data_flow()
        elif subcmd in ("solution", "solutions"):
            if not arg:
                self._log_error("Usage: /diagram solution <solution-id>")
                # List available solutions as a hint
                if self._workspace:
                    for s in self._workspace.solutions:
                        self._log(f"  [dim]{s.id}[/dim]  {s.name}")
            else:
                self._show_diagram_solution(arg)
        else:
            self._log_error(
                f"Unknown diagram type: [cyan]{subcmd}[/cyan]  —  "
                "try [cyan]/diagram capability-map[/cyan], "
                "[cyan]/diagram data-flow[/cyan], or "
                "[cyan]/diagram solution <id>[/cyan]"
            )

    def _render_mermaid(self, title: str, mermaid: str) -> None:
        """Display a Mermaid diagram in the chat log and open a live preview."""
        import subprocess
        from rich.syntax import Syntax

        self._log(f"\n[bold]📊 {title}[/bold]\n")
        self.query_one("#chat-log", RichLog).write(
            Syntax(mermaid.strip(), "markdown", theme="monokai", word_wrap=True)
        )

        # ── Render and open preview ───────────────────────────────────────
        try:
            preview_path = render_diagram_preview(mermaid, title)
            subprocess.Popen(["open", str(preview_path)])
            kind = "SVG" if preview_path.suffix == ".svg" else "HTML"
            self._log(
                f"[dim]🖼  Opening [bold]{kind}[/bold] preview → "
                f"[cyan]{preview_path.name}[/cyan][/dim]"
            )
        except Exception as exc:
            self._log(f"[dim yellow]⚠  Preview unavailable: {exc}[/dim yellow]")

        # ── mermaid.live share link ───────────────────────────────────────
        try:
            live_url = mermaid_live_url(mermaid)
            self._log(
                f"[dim]🔗 Edit online: [link={live_url}]mermaid.live[/link][/dim]\n"
            )
        except Exception:
            pass


    def _show_diagram_capability_map(self) -> None:
        if not self._workspace:
            self._log_error("No workspace loaded.")
            return
        ws = self._workspace
        if not ws.enterprise.capabilities:
            self._log_strata("No capabilities yet — add some first.")
            return
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mmd", delete=False) as f:
            tmp = Path(f.name)
        render_capability_map(ws, tmp)
        mermaid = tmp.read_text(encoding="utf-8")
        tmp.unlink(missing_ok=True)
        self._render_mermaid("Business Capability Map", mermaid)

    def _show_diagram_data_flow(self) -> None:
        if not self._workspace:
            self._log_error("No workspace loaded.")
            return
        ws = self._workspace
        if not ws.data.domains and not ws.data.flows:
            self._log_strata("No data domains or flows yet — add some first.")
            return
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mmd", delete=False) as f:
            tmp = Path(f.name)
        render_data_flow_map(ws, tmp)
        mermaid = tmp.read_text(encoding="utf-8")
        tmp.unlink(missing_ok=True)
        self._render_mermaid("Data Flow Map", mermaid)

    def _show_diagram_solution(self, solution_id: str) -> None:
        if not self._workspace:
            self._log_error("No workspace loaded.")
            return
        ws = self._workspace
        # match by id or name prefix (case-insensitive)
        match = next(
            (s for s in ws.solutions
             if s.id == solution_id or s.id.startswith(solution_id.lower())
             or s.name.lower().startswith(solution_id.lower())),
            None,
        )
        if not match:
            self._log_error(
                f"Solution [cyan]{solution_id!r}[/cyan] not found.  "
                "Available: " + ", ".join(s.id for s in ws.solutions) or "(none)"
            )
            return
        if not match.components:
            self._log_strata(
                f"Solution [bold]{match.name}[/bold] has no components yet."
            )
            return
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mmd", delete=False) as f:
            tmp = Path(f.name)
        render_solution_diagram(match, tmp)
        mermaid = tmp.read_text(encoding="utf-8")
        tmp.unlink(missing_ok=True)
        self._render_mermaid(f"Solution: {match.name}", mermaid)

    def _show_help(self) -> None:
        t = RichTable(title="Commands", box=rich_box.SIMPLE)
        t.add_column("Command", style="cyan", no_wrap=True)
        t.add_column("Description")
        rows = [
            # ─ Navigation
            ("/dashboard",                  "Workspace overview"),
            ("/capabilities",               "List business capabilities"),
            ("/applications",               "List application portfolio"),
            ("/standards",                  "Technology radar"),
            ("/domains",                    "List data domains"),
            ("/products",                   "List data products"),
            ("/flows",                      "List data flows"),
            ("/solutions",                  "List solution designs"),
            # ─ Diagrams submenu
            ("/diagram",                    "List available diagrams"),
            ("/diagram capability-map",     "Mermaid business capability map"),
            ("/diagram data-flow",          "Mermaid data flow diagram"),
            ("/diagram solution <id>",      "Mermaid solution architecture diagram"),
            # ─ Staging submenu
            ("/staging",                    "Show pending staged items"),
            ("/staging accept-all",         "Accept all pending items"),
            ("/staging accept-all <type>",  "Accept by type: capabilities, apps…"),
            ("/staging reject-all",         "Reject all pending items"),
            ("/staging accept <id>",        "Accept one item by ID or row #"),
            ("/staging reject <id>",        "Reject one item by ID or row #"),
            ("/staging clear",              "Discard all staging data"),
            ("/staging impact",             "AI-powered impact analysis of pending items"),
            # ─ Scoring
            ("/score",                      "Architecture maturity score (default profile)"),
            ("/score <profile>",            "Score with a specific profile (telecom, default, oda)"),
            ("/improve",                    "Phased improvement roadmap"),
            ("/improve-ai",                 "Open latest scheduled hybrid domain advisory backlog"),
            ("draft <n> / save <n> / quit", "Generate doc, save to file, or exit advisory workflow"),
            # ─ AI model / provider
            ("/model overview",             "Show provider status + model list for active provider"),
            ("/model",                      "Alias for /model overview"),
            ("/model <provider>",           "Switch provider — auto · copilot · claude · codex (github/openai/ollama are policy-disabled)"),
            ("/model copilot <model-id>",   "Switch to Copilot with a specific model"),
            ("/model claude <model-id>",    "Switch Claude model  e.g. /model claude claude-sonnet-4-6"),
            ("/auth [provider]",            "Start provider OAuth flow (provider defaults to current)"),
            ("/copilot-auth",               "Alias for /auth copilot"),
            # ─ Natural-language staging shortcuts
            ("accept all",                  "Accept all pending (natural language)"),
            ("accept all <type>",           "Accept all of one entity type"),
            ("reject all",                  "Reject all pending"),
            ("accept stg-001 / accept 5",   "Accept one item by ID or row #"),
            # ─ Folders
            ("/folders",                    "List configured watch folders"),
            ("/scan <path>",                "Scan folder / file for architecture"),
            ("/scan-all",                   "Scan all configured watch folders"),
            ("add folder <path>",           "Add a watch folder"),
            ("remove folder <path>",        "Remove a watch folder"),
            ("/watch",                      "Live watch status"),
            ("/watch start",                "Start watcher (auto-scans on change)"),
            ("/watch stop",                 "Stop the folder watcher"),
            ("/watch interval <min>",       "Set auto-scan interval (0 = off)"),
            ("/advisor",                    "Show hybrid domain advisor status"),
            ("/advisor status",             "Show advisor scheduler + latest run info"),
            ("/advisor progress",           "Show advisor run phase timeline and domain progress"),
            ("/advisor interval <min>",     "Set advisor interval (0 = off)"),
            # ─ Workspace
            ("/reload",                     "Reload workspace from disk"),
            ("/clear",                      "Clear chat log"),
            ("Ctrl+B",                      "Toggle sidebar"),
            ("Ctrl+E/D/S/T",               "Enterprise / Data / Solutions / Staging"),
            ("Ctrl+Y",                      "Copy last AI response to clipboard"),
            ("Ctrl+Q",                      "Quit"),
        ]
        for cmd, desc in rows:
            t.add_row(cmd, desc)
        self._open_main("❓ Help")
        self._main_table(t)
        self._main("[dim]Or just type naturally — AI will understand.[/dim]")

    def action_copy_response(self) -> None:
        """Copy the last AI response to the system clipboard (Ctrl+Y)."""
        if not self._last_ai_text:
            self._log_error("No AI response to copy yet.")
            return
        self.copy_to_clipboard(self._last_ai_text)
        self._log("[dim]✓ Copied to clipboard[/dim]")

    def _clear_chat(self) -> None:
        self.query_one("#chat-log", RichLog).clear()
        self._history.clear()
        self._pending_queue.clear()

    def _reload_workspace(self) -> None:
        try:
            self._workspace = load_workspace()
            self._start_scheduler()
            self._start_advisor_scheduler()
            self._refresh_sidebar()
            self._show_dashboard()
            self._log_strata("Workspace reloaded.")
        except WorkspaceError as exc:
            self._log_error(str(exc))

    # ── AI workers ─────────────────────────────────────────────────────────────

    def _build_workspace_context(self) -> dict[str, Any] | None:
        """Return a rich workspace context dict for the agentic system prompt."""
        ws = self._workspace
        if not ws:
            return None
        return {
            "workspace_name": ws.manifest.name,
            "watch_folders": list(ws.manifest.watch_folders),
            "entities": {
                "capabilities": [
                    {"id": c.id, "name": c.name, "domain": c.domain,
                     "level": c.level, "owner": c.owner or ""}
                    for c in ws.enterprise.capabilities
                ],
                "applications": [
                    {"id": a.id, "name": a.name, "hosting": a.hosting,
                     "criticality": a.criticality, "status": a.status,
                     "owner": a.owner_team or ""}
                    for a in ws.enterprise.applications
                ],
                "standards": [
                    {"id": s.id, "name": s.name, "category": s.category, "status": s.status}
                    for s in ws.enterprise.standards
                ],
                "domains": [
                    {"id": d.id, "name": d.name, "owner": d.owner_team or "",
                     "storage": d.storage_pattern}
                    for d in ws.data.domains
                ],
                "products": [
                    {"id": p.id, "name": p.name, "domain_id": p.domain_id,
                     "sla_tier": p.sla_tier, "owner": p.owner_team or ""}
                    for p in ws.data.products
                ],
                "flows": [
                    {"id": f.id, "name": f.name,
                     "source": f.source_domain, "target": f.target_domain}
                    for f in ws.data.flows
                ],
                "solutions": [
                    {"id": s.id, "name": s.name, "pattern": s.pattern}
                    for s in ws.solutions
                ],
            },
            "staging": [
                {
                    "id": s.id,
                    "entity": s.entity,
                    "name": s.fields.get("name", ""),
                    "source": s.source or "",
                }
                for s in load_staging()
                if s.status == "pending"
            ],
            # Architecture advisor context
            "maturity_score": (
                {"overall": self._last_score.overall, "level": self._last_score.level,
                 "top_improvements": [
                     {"dimension": imp.dimension_label,
                      "score": imp.current_score,
                      "action": imp.key_action}
                     for imp in compute_top_improvements(self._last_score, n=3)
                 ]}
                if self._last_score else None
            ),
            "stack_gaps": (
                compute_stack_coverage(ws).gaps
                if ws else []
            ),
        }

    @work(thread=True)
    def _ai_chat(self, text: str) -> None:
        """Agentic chat worker — maintains conversation history, responds naturally,
        and proposes entity actions when the user wants to create something."""
        try:
            agent = ArchitectureAgent(provider=self._provider)
            available, msg = agent.check_available()
            if not available:
                self.call_from_thread(
                    self._log_error,
                    f"AI not available: {msg}  —  run [cyan]strata ai configure[/cyan]",
                )
                return

            ctx = self._build_workspace_context()

            # Append user turn to history before calling
            self._history.append({"role": "user", "content": text})

            result = agent.chat(list(self._history), workspace_context=ctx)

            message: str = result.get("message", "")
            actions: list[dict[str, Any]] = result.get("actions", [])
            tools: list[dict[str, Any]] = result.get("tools", [])

            # Show conversational reply
            if message:
                self.call_from_thread(self._log_strata, message)
                # Append assistant turn to history
                self._history.append({"role": "assistant", "content": message})

            # Dispatch tool calls immediately (scan_folder, accept_staged, reject_staged, show_diagram)
            for tool_call in tools:
                if tool_call.get("tool") == "scan_folder":
                    path = tool_call.get("path", "")
                    if path:
                        self.call_from_thread(
                            self._log,
                            f"[dim]Starting folder scan: [cyan]{path}[/cyan][/dim]",
                        )
                        self._scan_path(path)
                elif tool_call.get("tool") == "accept_all_staged":
                    self.call_from_thread(self._accept_all_staging)
                elif tool_call.get("tool") == "accept_staged":
                    item_id = tool_call.get("id", "")
                    if item_id:
                        self.call_from_thread(self._staging_action, "accept", item_id)
                elif tool_call.get("tool") == "reject_staged":
                    item_id = tool_call.get("id", "")
                    if item_id:
                        self.call_from_thread(self._staging_action, "reject", item_id)
                elif tool_call.get("tool") == "show_diagram":
                    dtype = tool_call.get("type", "")
                    if dtype == "capability-map":
                        self.call_from_thread(self._show_diagram_capability_map)
                    elif dtype == "data-flow":
                        self.call_from_thread(self._show_diagram_data_flow)
                    elif dtype == "solution":
                        sol_id = tool_call.get("id", "")
                        if sol_id:
                            self.call_from_thread(self._show_diagram_solution, sol_id)
                        else:
                            self.call_from_thread(
                                self._log_error, "show_diagram solution requires an id"
                            )
                elif tool_call.get("tool") == "add_folder":
                    path = tool_call.get("path", "")
                    if path:
                        self.call_from_thread(self._add_folder, path)
                elif tool_call.get("tool") == "remove_folder":
                    path = tool_call.get("path", "")
                    if path:
                        self.call_from_thread(self._remove_folder, path)
                elif tool_call.get("tool") == "start_watching":
                    self.call_from_thread(self._start_watcher)
                elif tool_call.get("tool") == "stop_watching":
                    self.call_from_thread(self._stop_watcher)

            if not actions:
                return

            # Build PendingAction list — filter to valid entity types
            valid = ("capability", "application", "standard",
                     "domain", "product", "flow", "solution")
            pending_actions: list[PendingAction] = [
                PendingAction(
                    entity=a["entity"],
                    fields=a.get("fields", {}),
                    description=f"{a['entity']}  ·  {a.get('fields', {}).get('name', '?')}",
                )
                for a in actions
                if a.get("entity") in valid
            ]

            if not pending_actions:
                return

            # Queue remaining actions; show first one immediately
            self._pending_queue = pending_actions[1:]
            first = pending_actions[0]

            t = RichTable(
                title=f"Proposed  ·  {first.entity}",
                box=rich_box.SIMPLE_HEAVY,
                show_header=True,
            )
            t.add_column("Field", style="dim")
            t.add_column("Value")
            for k, v in first.fields.items():
                if v not in (None, "", []):
                    t.add_row(k, str(v))

            self.call_from_thread(self._log_table, t)
            self.call_from_thread(
                self._set_pending,
                first.entity,
                first.fields,
                first.description,
                len(self._pending_queue),
            )

        except AgentError as exc:
            self.call_from_thread(self._log_error, str(exc))
        except Exception as exc:
            self.call_from_thread(self._log_error, f"Unexpected error: {exc}")

    def _process_next_queued(self) -> None:
        """Pop and display the next action from the multi-action queue, if any."""
        if not self._pending_queue:
            return
        next_action = self._pending_queue.pop(0)
        t = RichTable(
            title=f"Proposed  ·  {next_action.entity}",
            box=rich_box.SIMPLE_HEAVY,
            show_header=True,
        )
        t.add_column("Field", style="dim")
        t.add_column("Value")
        for k, v in next_action.fields.items():
            if v not in (None, "", []):
                t.add_row(k, str(v))
        self._log_table(t)
        self._set_pending(
            next_action.entity,
            next_action.fields,
            next_action.description,
            len(self._pending_queue),
        )

    # ── Pending action ─────────────────────────────────────────────────────────

    def _set_pending(
        self, entity: str, fields: dict[str, Any], desc: str, queue_size: int = 0
    ) -> None:
        self._pending = PendingAction(entity=entity, fields=fields, description=desc)
        bar = self.query_one("#action-bar")
        bar.add_class("visible")
        queue_info = f"  [dim]({queue_size} more queued)[/dim]" if queue_size > 0 else ""
        self.query_one("#action-desc", Label).update(
            f"[yellow]Pending:[/yellow]  {desc}{queue_info}   "
            "[dim]— type y / n / s  or use buttons[/dim]"
        )

    def _clear_pending(self) -> None:
        self._pending = None
        self.query_one("#action-bar").remove_class("visible")

    def _accept_pending(self) -> None:
        if not self._pending or not self._workspace:
            return
        err = _write_entity(self._pending.entity, self._pending.fields, self._workspace)
        if err:
            self._log_error(f"Failed: {err}")
        else:
            try:
                save_workspace(self._workspace)
            except Exception as exc:
                self._log_error(f"Could not save workspace: {exc}")
                self._clear_pending()
                self._process_next_queued()
                return
            self._refresh_sidebar()
            name = self._pending.fields.get("name", "?")
            self._log_strata(
                f"[green]Added[/green]  {self._pending.entity}  [bold]{name}[/bold]"
            )
        self._clear_pending()
        self._process_next_queued()

    def _reject_pending(self) -> None:
        if not self._pending:
            return
        self._log("[dim]Discarded.[/dim]")
        self._clear_pending()
        self._process_next_queued()

    def _stage_pending(self) -> None:
        if not self._pending:
            return
        existing = load_staging()
        sid = next_staging_id(existing)
        item = StagedItem(
            id=sid,
            entity=self._pending.entity,
            fields=self._pending.fields,
            source="tui",
        )
        save_staging(existing + [item])
        self._refresh_sidebar()
        name = self._pending.fields.get("name", "?")
        self._log_strata(
            f"[yellow]Staged[/yellow]  {self._pending.entity}  "
            f"[bold]{name}[/bold]  ({sid})  —  review with [cyan]/staging[/cyan]"
        )
        self._clear_pending()
        self._process_next_queued()

    @on(Button.Pressed, "#btn-accept")
    def _on_accept(self) -> None:
        self._accept_pending()

    @on(Button.Pressed, "#btn-reject")
    def _on_reject(self) -> None:
        self._reject_pending()

    @on(Button.Pressed, "#btn-stage")
    def _on_stage(self) -> None:
        self._stage_pending()

    # ── Inline staging actions ─────────────────────────────────────────────────

    def _staging_action(self, action: str, id_or_num: str) -> None:
        items = load_staging()
        idx: int | None = None
        if id_or_num.isdigit():
            i = int(id_or_num) - 1
            if 0 <= i < len(items):
                idx = i
        else:
            for i, item in enumerate(items):
                if item.id == id_or_num:
                    idx = i
                    break

        if idx is None:
            self._log_error(f"Staging item not found: {id_or_num!r}")
            return

        item = items[idx]
        if item.status != "pending":
            self._log(f"[dim]{item.id} is already {item.status}.[/dim]")
            return

        if action == "accept":
            if not self._workspace:
                self._log_error("No workspace loaded.")
                return
            err = _write_entity(item.entity, item.fields, self._workspace)
            if err:
                self._log_error(f"Failed: {err}")
                return
            save_workspace(self._workspace)
            items[idx] = item.model_copy(update={"status": "accepted"})
            save_staging(items)
            self._refresh_sidebar()
            name = item.fields.get("name", item.id)
            self._log_strata(
                f"[green]Accepted[/green]  {item.entity}  [bold]{name}[/bold]"
            )
        else:
            items[idx] = item.model_copy(update={"status": "rejected"})
            save_staging(items)
            self._refresh_sidebar()
            name = item.fields.get("name", item.id)
            self._log_strata(f"[red]Rejected[/red]  {item.entity}  {name}")

    # ── Navigation ─────────────────────────────────────────────────────────────

    def _navigate(self, view: str) -> None:
        for item in self.query(".side-item"):
            item.remove_class("active-nav")
        try:
            self.query_one(f"#nav-{view}", NavItem).add_class("active-nav")
        except Exception:
            pass

        dispatch: dict[str, Any] = {
            "dashboard":      self._show_dashboard,
            "capabilities":   self._show_capabilities,
            "applications":   self._show_applications,
            "standards":      self._show_standards,
            "domains":        self._show_domains,
            "products":       self._show_products,
            "flows":          self._show_flows,
            "solutions":      self._show_solutions,
            "staging":        self._show_staging,
            "folders":        self._show_folders,
            "live-watch":     self._show_live_watch,
            "score":          lambda: self._show_score(),
            "stack":          lambda: self._show_stack(),
            "improve":        lambda: self._show_improve_roadmap(),
            "diagram-caps":   self._show_diagram_capability_map,
            "diagram-data":   self._show_diagram_data_flow,
            "chat":           lambda: None,
        }
        fn = dispatch.get(view)
        if fn:
            fn()
        self.query_one("#user-input", CommandInput).focus()

    # ── Data views (written into the chat log) ─────────────────────────────────

    def _show_dashboard(self) -> None:  # noqa: PLR0912,PLR0914
        import os
        if not self._workspace:
            self._log_error("No workspace loaded.")
            return

        ws      = self._workspace
        ea      = ws.enterprise
        da      = ws.data
        sols    = ws.solutions
        staging = load_staging()

        pending  = [s for s in staging if s.status == "pending"]
        accepted = [s for s in staging if s.status == "accepted"]
        rejected = [s for s in staging if s.status == "rejected"]

        log = self.query_one("#dashboard-pane", RichLog)
        log.clear()

        # ── Inline progress-bar helper ────────────────────────────────────
        def _bar(n: int, total: int, width: int = 14) -> str:
            if total == 0:
                return f"[dim]{'░' * width} —[/dim]"
            pct = n / total
            filled = round(pct * width)
            col = "green" if pct >= 0.75 else "yellow" if pct >= 0.40 else "red"
            return (
                f"[{col}]{'█' * filled}{'░' * (width - filled)}[/{col}]"
                f" [dim]{pct * 100:.0f}%[/dim]"
            )

        # ── Header panel ──────────────────────────────────────────────────
        env_col = {
            "production": "green", "staging": "yellow", "dev": "blue",
        }.get(ws.manifest.environment, "white")
        desc = ws.manifest.description or ""
        header_body = (
            (f"[dim]{desc}[/dim]\n" if desc else "")
            + f"[dim]Cloud:[/dim] [bold]{ws.manifest.cloud_provider}[/bold]   "
            + f"[dim]Environment:[/dim] [{env_col}]{ws.manifest.environment}[/{env_col}]   "
            + f"[dim]Version:[/dim] {ws.manifest.version}"
        )
        log.write(RichPanel(
            header_body,
            title=f"[bold cyan]◈  {ws.manifest.name}[/bold cyan]",
            subtitle="[dim]Architecture Workspace[/dim]",
            border_style="cyan",
            padding=(0, 2),
        ))

        # ── Enterprise card ───────────────────────────────────────────────
        caps       = ea.capabilities
        apps       = ea.applications
        stds       = ea.standards
        strategic  = sum(1 for c in caps if c.level == "strategic")
        core_c     = sum(1 for c in caps if c.level == "core")
        supporting = sum(1 for c in caps if c.level == "supporting")
        cap_domains = len(set(c.domain for c in caps))
        active_apps  = sum(1 for a in apps if a.status == "active")
        retiring     = sum(1 for a in apps if a.status == "retiring")
        adopted_stds = sum(1 for s in stds if s.status == "adopt")
        trial_stds   = sum(1 for s in stds if s.status == "trial")
        hold_stds    = sum(1 for s in stds if s.status == "hold")

        ent_t = RichTable(box=None, show_header=False, padding=(0, 0))
        ent_t.add_column("")
        ent_t.add_row(f"[bold cyan]{len(caps)}[/bold cyan] capabilities  [dim]across {cap_domains} domains[/dim]")
        ent_t.add_row(f"  [green]⚡[/green] {strategic} strategic  [blue]◆[/blue] {core_c} core  [dim]◇ {supporting} supporting[/dim]")
        ent_t.add_row("")
        ent_t.add_row(f"[bold cyan]{len(apps)}[/bold cyan] applications")
        ent_t.add_row(f"  {_bar(active_apps, len(apps))}  {active_apps} active  [dim]{retiring} retiring[/dim]")
        ent_t.add_row("")
        ent_t.add_row(f"[bold cyan]{len(stds)}[/bold cyan] tech standards")
        ent_t.add_row(f"  {_bar(adopted_stds, len(stds))}  {adopted_stds} adopted  [yellow]{trial_stds} trial[/yellow]  [dim]{hold_stds} hold[/dim]")
        ent_panel = RichPanel(ent_t, title="[bold]⚡ Enterprise[/bold]", border_style="blue", padding=(0, 1))

        # ── Data card ─────────────────────────────────────────────────────
        flow_domains = (
            set(f.source_domain for f in da.flows)
            | set(f.target_domain for f in da.flows)
        ) if da.flows else set()
        connected    = len(flow_domains)
        gold_plat    = sum(1 for p in da.products if p.sla_tier in ("gold", "platinum"))
        silv_bron    = sum(1 for p in da.products if p.sla_tier in ("silver", "bronze"))
        mech_counts: dict[str, int] = {}
        for fl in da.flows:
            mech_counts[fl.mechanism] = mech_counts.get(fl.mechanism, 0) + 1
        top_mechs = sorted(mech_counts, key=lambda k: -mech_counts[k])[:3]

        data_t = RichTable(box=None, show_header=False, padding=(0, 0))
        data_t.add_column("")
        data_t.add_row(f"[bold cyan]{len(da.domains)}[/bold cyan] data domains")
        data_t.add_row(f"  {_bar(connected, len(da.domains))}  {connected} connected via flows")
        data_t.add_row("")
        data_t.add_row(f"[bold cyan]{len(da.products)}[/bold cyan] data products")
        if da.products:
            data_t.add_row(f"  [yellow]◆[/yellow] {gold_plat} gold/platinum   [dim]◇ {silv_bron} silver/bronze[/dim]")
        data_t.add_row("")
        data_t.add_row(f"[bold cyan]{len(da.flows)}[/bold cyan] data flows")
        if top_mechs:
            data_t.add_row("  " + "  ".join(f"[dim]{m}[/dim] ×{mech_counts[m]}" for m in top_mechs))
        data_panel = RichPanel(data_t, title="[bold]🗄  Data[/bold]", border_style="cyan", padding=(0, 1))

        # ── Solutions card ────────────────────────────────────────────────
        s_approved    = sum(1 for s in sols if s.status == "approved")
        s_implemented = sum(1 for s in sols if s.status == "implemented")
        s_review      = sum(1 for s in sols if s.status == "review")
        s_draft       = sum(1 for s in sols if s.status == "draft")
        total_comps   = sum(len(s.components) for s in sols)
        total_adrs    = sum(len(s.adrs) for s in sols)
        pat_counts: dict[str, int] = {}
        for s in sols:
            pat_counts[s.pattern] = pat_counts.get(s.pattern, 0) + 1
        top_pats = sorted(pat_counts, key=lambda k: -pat_counts[k])[:3]

        sol_t = RichTable(box=None, show_header=False, padding=(0, 0))
        sol_t.add_column("")
        sol_t.add_row(f"[bold cyan]{len(sols)}[/bold cyan] solutions")
        sol_t.add_row(f"  {_bar(s_approved + s_implemented, len(sols))}  approved/implemented")
        sol_t.add_row(f"  [green]✓[/green] {s_approved} approved  [dim]◎ {s_implemented} implemented[/dim]")
        sol_t.add_row(f"  [yellow]⏳[/yellow] {s_review} review   [dim]✎ {s_draft} draft[/dim]")
        sol_t.add_row("")
        sol_t.add_row(f"[bold cyan]{total_comps}[/bold cyan] components   [bold cyan]{total_adrs}[/bold cyan] ADRs")
        if top_pats:
            sol_t.add_row("  " + "  [dim]·[/dim]  ".join(f"[dim]{p}[/dim] ×{pat_counts[p]}" for p in top_pats))
        sol_panel = RichPanel(sol_t, title="[bold]🔷 Solutions[/bold]", border_style="magenta", padding=(0, 1))

        # ── Row 1: three stat cards side-by-side ──────────────────────────
        cards = RichTable.grid(padding=(0, 1))
        cards.add_column(ratio=1)
        cards.add_column(ratio=1)
        cards.add_column(ratio=1)
        cards.add_row(ent_panel, data_panel, sol_panel)
        log.write(cards)

        # ── Watch folders panel ───────────────────────────────────────────
        folders = ws.manifest.watch_folders
        folder_lines: list[str] = []
        for fpath in folders:
            fp = Path(fpath)
            if fp.exists():
                try:
                    entries   = list(os.scandir(fpath))
                    n_files   = sum(1 for e in entries if e.is_file())
                    n_dirs    = sum(1 for e in entries if e.is_dir())
                    folder_lines.append(
                        f"  [green]●[/green] [cyan]{fpath}[/cyan]  "
                        f"[dim]{n_files} files  {n_dirs} subdirs[/dim]"
                    )
                except OSError:
                    folder_lines.append(f"  [green]●[/green] [cyan]{fpath}[/cyan]")
            else:
                folder_lines.append(
                    f"  [red]○[/red] [dim]{fpath}[/dim]  [red italic]path not found[/red italic]"
                )
        folder_body = (
            "\n".join(folder_lines)
            if folder_lines
            else "[dim]None configured.  Type [cyan]add folder <path>[/cyan][/dim]"
        )
        folder_panel = RichPanel(
            folder_body,
            title=f"[bold]📁 Watch Folders ({len(folders)})[/bold]",
            border_style="yellow" if folders else "dim",
            padding=(0, 1),
        )

        # ── Staging queue panel ───────────────────────────────────────────
        stg_lines = [
            f"  [yellow]⏳[/yellow] [bold yellow]{len(pending)}[/bold yellow]  pending review",
            f"  [green]✓[/green]  [bold green]{len(accepted)}[/bold green]  accepted",
            f"  [red]✗[/red]  [bold red]{len(rejected)}[/bold red]  rejected",
        ]
        if staging:
            stg_lines.append(
                f"\n  {_bar(len(accepted) + len(rejected), len(staging))}  processed"
            )
        stg_lines.append(
            "\n  [dim]→ [cyan]/staging[/cyan]" + (" to review" if pending else "") + "[/dim]"
        )
        stg_panel = RichPanel(
            "\n".join(stg_lines),
            title="[bold]📋 Staging Queue[/bold]",
            border_style="yellow" if pending else "dim",
            padding=(0, 1),
        )

        # ── Row 2: folders (wide) + staging side-by-side ──────────────────
        row2 = RichTable.grid(padding=(0, 1))
        row2.add_column(ratio=2)
        row2.add_column(ratio=1)
        row2.add_row(folder_panel, stg_panel)
        log.write(row2)

        # ── Row 3: DB + live watcher ─────────────────────────────────────
        db_avail = _db.is_available()
        if db_avail:
            db_body = (
                f"[cyan]{self._db_url}[/cyan]\n"
                "  [dim]Sync status reported in chat log on startup[/dim]\n"
                "  [dim]Run [cyan]make db-start[/cyan] for local Postgres[/dim]"
            )
        else:
            db_body = (
                "[yellow]psycopg2 not installed[/yellow]\n"
                "  [dim]Run [cyan]pipx install 'strata-cli[full]'[/cyan] to enable persistence[/dim]"
            )
        db_panel = RichPanel(
            db_body,
            title="[bold]🗄  Database[/bold]",
            border_style="blue" if db_avail else "dim",
            padding=(0, 1),
        )
        if self._watcher and self._watcher.is_running:
            watch_body = (
                f"[green]● watching {self._watcher.active_folder_count} folder(s)[/green]  "
                "[dim]OS-native events active[/dim]"
            )
            watch_border = "green"
        elif not ws.manifest.watch_folders:
            watch_body = "[dim]○ no watch folders — type [cyan]add folder <path>[/cyan][/dim]"
            watch_border = "dim"
        elif not _watcher_available():
            watch_body = (
                "[yellow]○ watchdog not installed[/yellow]\n"
                "  [dim]Run [cyan]pipx install 'strata-cli[full]'[/cyan][/dim]"
            )
            watch_border = "yellow"
        else:
            watch_body = "[dim]○ not active — [cyan]/watch start[/cyan] to begin[/dim]"
            watch_border = "dim"
        watch_panel = RichPanel(
            watch_body,
            title="[bold]👁  Live Watching[/bold]",
            border_style=watch_border,
            padding=(0, 1),
        )
        row3 = RichTable.grid(padding=(0, 1))
        row3.add_column(ratio=1)
        row3.add_column(ratio=1)
        row3.add_row(db_panel, watch_panel)
        log.write(row3)

        # ── Row 4: Architecture maturity score badge ──────────────────────
        try:
            result = score_workspace(ws, profile="default")
            ov = result.overall
            col = "green" if ov >= 3.0 else "yellow" if ov >= 2.0 else "red"
            bar_w = 20
            filled = round((ov / 5.0) * bar_w)
            bar = f"[{col}]{'█' * filled}{'░' * (bar_w - filled)}[/{col}]"
            score_body = (
                f"  {bar}  [{col}]{ov:.1f}[/{col}] / 5.0   "
                f"[bold {col}]{result.level}[/bold {col}]\n"
            )
            # Compact dimension summary
            for d in result.dimensions:
                dc = "green" if d.score >= 3.0 else "yellow" if d.score >= 2.0 else "red"
                mini = round(d.score / 5.0 * 8)
                score_body += (
                    f"  [{dc}]{'█' * mini}{'░' * (8 - mini)}[/{dc}] "
                    f"{d.score:.1f}  [dim]{d.label}[/dim]\n"
                )
            score_body += "\n  [dim]→ [cyan]/score[/cyan] for full breakdown · [cyan]/score telecom[/cyan] for telecom profile[/dim]"
            score_panel = RichPanel(
                score_body,
                title="[bold]📈 Architecture Maturity[/bold]",
                border_style=col,
                padding=(0, 1),
            )
            # Scheduler badge
            interval = ws.manifest.scan_interval_minutes
            advisor_interval = ws.manifest.advisor_interval_minutes if ws.manifest.advisor_enabled else 0
            sched_parts = []
            if interval > 0:
                sched_parts.append(f"[green]● auto-scan every {interval}m[/green]")
            else:
                sched_parts.append("[dim]○ no scheduled scans[/dim]")
            if advisor_interval > 0:
                sched_parts.append(
                    f"[green]● advisor every {advisor_interval}m[/green]  "
                    f"[dim]({ws.manifest.advisor_profile})[/dim]"
                )
            else:
                sched_parts.append("[dim]○ advisor disabled[/dim]")
            if self._tracker:
                ts = self._tracker.summary()
                sched_parts.append(f"  [dim]{ts['tracked_files']} files tracked[/dim]")
            sched_body = "\n".join(sched_parts)
            sched_body += (
                "\n  [dim]→ [cyan]/watch interval <min>[/cyan] / "
                "[cyan]/advisor interval <min>[/cyan][/dim]"
            )
            sched_panel = RichPanel(
                sched_body,
                title="[bold]⏱  Scheduler[/bold]",
                border_style="green" if interval > 0 else "dim",
                padding=(0, 1),
            )
            row4 = RichTable.grid(padding=(0, 1))
            row4.add_column(ratio=2)
            row4.add_column(ratio=1)
            row4.add_row(score_panel, sched_panel)
            log.write(row4)

            # Cache for AI advisor context
            self._last_score = result

            # ── Row 5: Top 3 Improvements ──────────────────────────────────
            improvements = compute_top_improvements(result, n=3)
            rank_icons = {1: "🥇", 2: "🥈", 3: "🥉"}
            impr_lines: list[str] = []
            for imp in improvements:
                dc = "green" if imp.current_score >= 3.0 else "yellow" if imp.current_score >= 2.0 else "red"
                impr_lines.append(
                    f"  {rank_icons.get(imp.rank, str(imp.rank))}  "
                    f"[bold]{imp.dimension_label}[/bold]  "
                    f"[{dc}]{imp.current_score:.1f}[/{dc}]/5.0   "
                    f"[dim]{imp.key_action}[/dim]"
                )
            impr_body = (
                "\n".join(impr_lines)
                + "\n\n  [dim]→ [cyan]/improve[/cyan] AI recommendations"
                "  ·  [cyan]/stack[/cyan] full coverage view[/dim]"
            )
            impr_panel = RichPanel(
                impr_body,
                title="[bold]🎯 Top 3 Improvements[/bold]",
                border_style="yellow",
                padding=(0, 1),
            )
            log.write(impr_panel)
        except Exception:
            pass  # scoring is best-effort on dashboard

        # ── Quick navigation hint ─────────────────────────────────────────
        log.write(
            "[dim]  /capabilities · /applications · /standards · "
            "/solutions · /staging · /score · /stack · /diagram capability-map[/dim]\n"
        )

    def _show_capabilities(self) -> None:
        if not self._workspace:
            self._log_error("No workspace.")
            return
        items = self._workspace.enterprise.capabilities
        self._open_main(f"⚡ Capabilities ({len(items)})")
        if not items:
            self._main(
                "[dim]No capabilities yet.  Try: "
                '[cyan]"Add Order Management as a core Commerce capability"[/cyan][/dim]'
            )
            return
        t = RichTable(box=rich_box.SIMPLE_HEAVY)
        t.add_column("ID", style="dim", no_wrap=True)
        t.add_column("Name", style="bold")
        t.add_column("Domain")
        t.add_column("Level")
        t.add_column("Maturity", style="dim")
        t.add_column("Owner")
        LC = {"strategic": "green", "core": "blue", "supporting": "dim"}
        for c in items:
            lc = LC.get(c.level, "")
            t.add_row(c.id, c.name, c.domain, f"[{lc}]{c.level}[/{lc}]", c.maturity or "—", c.owner or "—")
        self._main_table(t)

    def _show_applications(self) -> None:
        if not self._workspace:
            self._log_error("No workspace.")
            return
        items = self._workspace.enterprise.applications
        self._open_main(f"📱 Applications ({len(items)})")
        if not items:
            self._main("[dim]No applications yet.[/dim]")
            return
        SC = {"active": "green", "retiring": "yellow", "legacy": "red", "planned": "cyan"}
        CC = {"critical": "red", "high": "yellow", "medium": "cyan", "low": "dim"}
        HC = {"kubernetes": "green", "serverless": "cyan"}
        t = RichTable(box=rich_box.SIMPLE_HEAVY)
        t.add_column("ID", style="dim", no_wrap=True)
        t.add_column("Name", style="bold")
        t.add_column("Hosting")
        t.add_column("Status")
        t.add_column("Criticality")
        t.add_column("Owner")
        for a in items:
            sc = SC.get(a.status, "")
            cc = CC.get(a.criticality, "")
            hc = HC.get(a.hosting, "dim")
            t.add_row(
                a.id, a.name,
                f"[{hc}]{a.hosting}[/{hc}]",
                f"[{sc}]{a.status}[/{sc}]",
                f"[{cc}]{a.criticality}[/{cc}]",
                a.owner_team or "—",
            )
        self._main_table(t)

    def _show_standards(self) -> None:
        if not self._workspace:
            self._log_error("No workspace.")
            return
        items = self._workspace.enterprise.standards
        self._open_main(f"🔬 Technology Radar ({len(items)})")
        if not items:
            self._main("[dim]No standards yet.[/dim]")
            return
        SC = {"adopt": "green", "trial": "yellow", "assess": "cyan", "hold": "red"}
        # Group by category
        cats: dict[str, list] = {}
        for s in items:
            cats.setdefault(s.category or "Other", []).append(s)
        for cat, stds in sorted(cats.items()):
            t = RichTable(title=f"[bold]{cat}[/bold]", box=rich_box.SIMPLE_HEAVY)
            t.add_column("ID", style="dim", no_wrap=True)
            t.add_column("Name", style="bold")
            t.add_column("Status")
            t.add_column("Rationale")
            for s in stds:
                c = SC.get(s.status, "")
                t.add_row(s.id, s.name, f"[{c}]{s.status}[/{c}]", (s.rationale or "")[:70])
            self._main_table(t)

    def _show_domains(self) -> None:
        if not self._workspace:
            self._log_error("No workspace.")
            return
        items = self._workspace.data.domains
        self._open_main(f"🗄  Data Domains ({len(items)})")
        if not items:
            self._main("[dim]No data domains yet.[/dim]")
            return
        t = RichTable(box=rich_box.SIMPLE_HEAVY)
        t.add_column("ID", style="dim", no_wrap=True)
        t.add_column("Name", style="bold")
        t.add_column("Owner")
        t.add_column("Storage")
        t.add_column("Entities", justify="right")
        for d in items:
            t.add_row(d.id, d.name, d.owner_team or "—", d.storage_pattern, str(len(d.entities)))
        self._main_table(t)

    def _show_products(self) -> None:
        if not self._workspace:
            self._log_error("No workspace.")
            return
        items = self._workspace.data.products
        self._open_main(f"📦 Data Products ({len(items)})")
        if not items:
            self._main("[dim]No data products yet.[/dim]")
            return
        SC = {"platinum": "magenta", "gold": "yellow", "silver": "cyan", "bronze": "dim"}
        t = RichTable(box=rich_box.SIMPLE_HEAVY)
        t.add_column("ID", style="dim", no_wrap=True)
        t.add_column("Name", style="bold")
        t.add_column("Domain")
        t.add_column("Port")
        t.add_column("SLA")
        t.add_column("Owner")
        for p in items:
            c = SC.get(p.sla_tier, "")
            t.add_row(p.id, p.name, p.domain_id, p.output_port, f"[{c}]{p.sla_tier}[/{c}]", p.owner_team or "—")
        self._main_table(t)

    def _show_flows(self) -> None:
        if not self._workspace:
            self._log_error("No workspace.")
            return
        items = self._workspace.data.flows
        self._open_main(f"🔀 Data Flows ({len(items)})")
        if not items:
            self._main("[dim]No data flows yet.[/dim]")
            return
        MC = {"streaming": "green", "cdc": "cyan", "batch": "yellow", "api": "blue"}
        t = RichTable(box=rich_box.SIMPLE_HEAVY)
        t.add_column("ID", style="dim", no_wrap=True)
        t.add_column("Name", style="bold")
        t.add_column("Source")
        t.add_column("→", width=2, no_wrap=True)
        t.add_column("Target")
        t.add_column("Mechanism")
        t.add_column("Classification", style="dim")
        for f in items:
            mc = MC.get(f.mechanism, "")
            t.add_row(f.id, f.name, f.source_domain, "→", f.target_domain, f"[{mc}]{f.mechanism}[/{mc}]", f.classification)
        self._main_table(t)

    def _show_solutions(self) -> None:
        if not self._workspace:
            self._log_error("No workspace.")
            return
        items = self._workspace.solutions
        self._open_main(f"🔷 Solutions ({len(items)})")
        if not items:
            self._main("[dim]No solutions yet.[/dim]")
            return
        SC = {"approved": "green", "implemented": "cyan", "review": "yellow", "draft": "dim"}
        for s in items:
            sc = SC.get(s.status, "")
            adrs = len(s.adrs)
            comps = len(s.components)
            self._main(
                f"  [bold]{s.name}[/bold]  [{sc}]{s.status}[/{sc}]  "
                f"[dim]{s.pattern}  ·  {s.deployment_target}  ·  "
                f"{comps} component(s)  ·  {adrs} ADR(s)[/dim]"
            )
            if s.components:
                CT = {"gateway": "blue", "database": "cyan", "queue": "yellow",
                      "service": "green", "external": "dim"}
                for c in s.components:
                    cc = CT.get(c.comp_type, "")
                    self._main(
                        f"    [dim]└[/dim] [{cc}]{c.comp_type}[/{cc}]  "
                        f"[bold]{c.name}[/bold]  [dim]{c.technology or ''}  {c.hosting or ''}[/dim]"
                    )
            self._main("")

    def _show_staging(self) -> None:
        staging = load_staging()
        pending = [s for s in staging if s.status == "pending"]
        self._open_main(f"📋 Staging  —  {len(pending)} pending / {len(staging)} total")

        if not staging:
            self._main(
                "[dim]Staging area is empty.  "
                "Run [cyan]strata scan <path>[/cyan] or [cyan]strata ai extract <file>[/cyan] to populate it.[/dim]"
            )
            return

        t = RichTable(box=rich_box.SIMPLE_HEAVY)
        t.add_column("#", style="dim", justify="right", no_wrap=True)
        t.add_column("ID", style="dim", no_wrap=True)
        t.add_column("Status")
        t.add_column("Entity")
        t.add_column("Name")
        t.add_column("Source", style="dim")

        SC = {"pending": "yellow", "accepted": "green", "rejected": "red"}
        for i, item in enumerate(staging, 1):
            c = SC.get(item.status, "")
            name = item.fields.get("name", "—")
            t.add_row(str(i), item.id, f"[{c}]{item.status}[/{c}]", item.entity, name, item.source or "")
        self._main_table(t)

        if pending:
            self._main(
                "[dim]Type [cyan]accept <id>[/cyan] or [cyan]reject <id>[/cyan] "
                "to review, e.g.  [cyan]accept stg-001[/cyan][/dim]"
            )

    def _show_folders(self) -> None:
        """Display configured watch folders."""
        ws = self._workspace
        folders = ws.manifest.watch_folders if ws else []
        self._open_main(f"📁 Watch Folders ({len(folders)})")
        if not folders:
            self._main(
                "[dim]No watch folders configured.\n\n"
                "Add one:\n"
                "  [cyan]add folder /path/to/docs[/cyan]\n"
                "  [cyan]/add-folder /path/to/docs[/cyan]\n\n"
                "Then scan all folders:\n"
                "  [cyan]/scan-all[/cyan][/dim]"
            )
            return
        t = RichTable(box=rich_box.SIMPLE_HEAVY, show_header=True)
        t.add_column("#", style="dim", justify="right")
        t.add_column("Path")
        t.add_column("Exists", justify="center")
        for i, folder in enumerate(folders, 1):
            exists = "[green]✓[/green]" if Path(folder).exists() else "[red]✗[/red]"
            t.add_row(str(i), folder, exists)
        self._main_table(t)
        self._main(
            "[dim]Scan all folders: [cyan]/scan-all[/cyan]  |  "
            "Add: [cyan]add folder <path>[/cyan]  |  "
            "Remove: [cyan]remove folder <path>[/cyan][/dim]"
        )

    def _add_folder(self, path: str) -> None:
        """Add a watch folder to the workspace manifest."""
        if not self._workspace:
            self._log_error("No workspace loaded.")
            return
        try:
            resolved = str(Path(path).expanduser().resolve())
            add_watch_folder(path)
            # Reload manifest to keep _workspace in sync
            self._workspace = load_workspace()
            self._refresh_sidebar()
            self._show_dashboard()
            self._log_strata(
                f"[green]Watch folder added:[/green]  [bold]{resolved}[/bold]\n"
                "Type [cyan]/scan-all[/cyan] to scan it, or [cyan]scan it now[/cyan]"
            )
        except Exception as exc:
            self._log_error(f"Could not add folder: {exc}")

    def _remove_folder(self, path: str) -> None:
        """Remove a watch folder from the workspace manifest."""
        if not self._workspace:
            self._log_error("No workspace loaded.")
            return
        try:
            remove_watch_folder(path)
            self._workspace = load_workspace()
            self._refresh_sidebar()
            self._show_dashboard()
            self._log_strata(f"[yellow]Watch folder removed:[/yellow]  {path}")
        except Exception as exc:
            self._log_error(f"Could not remove folder: {exc}")

    def _scan_all_folders(self) -> None:
        """Scan every configured watch folder."""
        ws = self._workspace
        if not ws:
            self._log_error("No workspace loaded.")
            return
        folders = list(ws.manifest.watch_folders)
        if not folders:
            self._log_strata(
                "No watch folders configured.\n"
                "Add one with [cyan]add folder <path>[/cyan] "
                "or [cyan]/add-folder <path>[/cyan]"
            )
            return
        self._log(f"[dim]Scanning {len(folders)} watch folder(s)…[/dim]")
        for folder in folders:
            self._scan_path(folder)

    def _accept_all_staging(self, entity_filter: str | None = None) -> None:
        """Accept every pending staged item, optionally filtered by entity type."""
        if not self._workspace:
            self._log_error("No workspace loaded.")
            return
        items = load_staging()
        pending_indices = [
            i for i, s in enumerate(items)
            if s.status == "pending"
            and (entity_filter is None or s.entity == entity_filter)
        ]
        if not pending_indices:
            label = f"{entity_filter} " if entity_filter else ""
            self._log_strata(f"No pending {label}items to accept.")
            return
        accepted = 0
        failed = 0
        for i in pending_indices:
            item = items[i]
            err = _write_entity(item.entity, item.fields, self._workspace)
            if err:
                self._log(f"[dim][yellow]Skipped {item.id}:[/yellow] {err}[/dim]")
                failed += 1
            else:
                items[i] = item.model_copy(update={"status": "accepted"})
                accepted += 1
        if accepted:
            save_workspace(self._workspace)
        save_staging(items)
        self._refresh_sidebar()
        self._show_dashboard()
        label = f" {entity_filter}" if entity_filter else ""
        msg = f"[green]Accepted {accepted}{label} item(s)[/green]"
        if failed:
            msg += f"  ([yellow]{failed} skipped[/yellow] — check errors above)"
        self._log_strata(msg)

    def _reject_all_staging(self, entity_filter: str | None = None) -> None:
        """Reject every pending staged item, optionally filtered by entity type."""
        items = load_staging()
        pending_indices = [
            i for i, s in enumerate(items)
            if s.status == "pending"
            and (entity_filter is None or s.entity == entity_filter)
        ]
        if not pending_indices:
            label = f"{entity_filter} " if entity_filter else ""
            self._log_strata(f"No pending {label}items to reject.")
            return
        for i in pending_indices:
            items[i] = items[i].model_copy(update={"status": "rejected"})
        save_staging(items)
        self._refresh_sidebar()
        self._show_dashboard()
        label = f" {entity_filter}" if entity_filter else ""
        self._log_strata(f"[red]Rejected {len(pending_indices)}{label} item(s)[/red]")

    def _clear_staging(self) -> None:
        """Discard all staging data entirely."""
        save_staging([])
        self._refresh_sidebar()
        self._show_dashboard()
        self._log_strata("[yellow]Staging area cleared.[/yellow]")

    # ── Scan worker ────────────────────────────────────────────────────────────

    @work(thread=True)
    def _scan_path(self, path: str) -> None:
        target = Path(path).resolve()
        if not target.exists():
            self.call_from_thread(self._log_error, f"Path not found: {path}")
            return

        self.call_from_thread(self._log, f"[dim]Scanning {target}…[/dim]")

        try:
            agent = ArchitectureAgent(provider=self._provider)
            available, msg = agent.check_available()
            if not available:
                self.call_from_thread(self._log_error, f"AI not available: {msg}")
                return

            all_files = [target] if target.is_file() else sorted(target.glob("**/*.md"))
            if not all_files:
                self.call_from_thread(self._log_strata, f"No .md files found in {path}")
                return

            # Filter through FileTracker — skip unchanged files
            tracker = self._tracker
            files: list[Path] = []
            skipped = 0
            for fp in all_files:
                if tracker and not tracker.is_changed(str(fp)):
                    skipped += 1
                else:
                    files.append(fp)
            if skipped:
                self.call_from_thread(
                    self._log,
                    f"[dim]  ↳ {skipped} file(s) unchanged — skipped[/dim]",
                )
            if not files:
                self.call_from_thread(
                    self._log_strata, "All files unchanged since last scan."
                )
                return

            ws = self._workspace
            ctx: dict[str, Any] | None = None
            if ws:
                ctx = {
                    "capability_ids": [c.id for c in ws.enterprise.capabilities],
                    "domain_ids":     [d.id for d in ws.data.domains],
                    "solution_ids":   [s.id for s in ws.solutions],
                }

            existing = load_staging()
            new_items: list[StagedItem] = []
            total_found = 0

            for fp in files:
                text = fp.read_text(encoding="utf-8", errors="replace")
                rel = fp.name
                self.call_from_thread(self._log, f"[dim]  · {rel}[/dim]")
                found = agent.scan_document(text, source_name=rel, workspace_context=ctx)
                total_found += len(found)
                file_staging_ids: list[str] = []
                for item in found:
                    sid = next_staging_id(existing + new_items)
                    new_items.append(StagedItem(
                        id=sid,
                        entity=item.get("entity", "unknown"),
                        fields=item.get("fields", {}),
                        source=rel,
                    ))
                    file_staging_ids.append(sid)
                # Record file in tracker (update hash even if no items found)
                if tracker:
                    tracker.record(str(fp), file_staging_ids)

            # Persist tracker
            if tracker:
                try:
                    tracker.save()
                except Exception:
                    pass

            if not new_items:
                self.call_from_thread(self._log_strata, "No architecture artefacts detected.")
                return

            save_staging(existing + new_items)
            self.call_from_thread(self._refresh_sidebar)
            self.call_from_thread(self._show_dashboard)
            self.call_from_thread(
                self._log_strata,
                f"[green]Staged {len(new_items)} item(s)[/green] from {len(files)} file(s).  "
                "Type [cyan]/staging[/cyan] to review.",
            )

        except Exception as exc:
            self.call_from_thread(self._log_error, str(exc))


    # ── Watcher + DB ───────────────────────────────────────────────────────

    def _start_watcher(self) -> None:
        """Start the folder watcher if watch_folders are configured."""
        if not self._workspace:
            return
        folders = list(self._workspace.manifest.watch_folders)
        if not folders:
            return
        self._watcher = FolderWatcher(
            folders,
            on_event=lambda p, e: self.call_from_thread(self._on_watch_event, p, e),
        )
        started = self._watcher.start()
        if started:
            n = self._watcher.active_folder_count
            self._log(f"[dim]👁 Watching {n} folder(s) for changes…[/dim]")
            self._refresh_sidebar()
        elif not self._watcher.available:
            self._log(
                "[dim]Live watching unavailable — "
                "run [cyan]pip install 'strata-cli[watch]'[/cyan][/dim]"
            )

    def _stop_watcher(self) -> None:
        """Stop the folder watcher."""
        if self._watcher and self._watcher.is_running:
            self._watcher.stop()
            self._log("[dim]👁 Folder watcher stopped.[/dim]")
            self._refresh_sidebar()

    def _on_watch_event(self, path: str, event_type: str) -> None:
        """Called on the Textual event loop when a watched file changes."""
        pname = Path(path).name
        self._log(f"[dim]👁 {event_type}  [cyan]{pname}[/cyan][/dim]")
        # Log to DB (best-effort)
        _db.log_event(path, event_type, self._db_url)
        # Debounce: collect changed paths, scan after quiet window
        if event_type in ("created", "modified", "moved"):
            self._debounce_paths.add(path)
            if self._debounce_timer is not None:
                try:
                    self._debounce_timer.stop()
                except Exception:
                    pass
            self._debounce_timer = self.set_timer(1.5, self._flush_debounce_watch)

    def _flush_debounce_watch(self) -> None:
        """Scan architecture files that changed during the debounce window."""
        self._debounce_timer = None
        scannable = {".md", ".yaml", ".yml"}
        paths = list(self._debounce_paths)
        self._debounce_paths.clear()
        for path in paths:
            p = Path(path)
            if p.suffix.lower() in scannable and p.is_file():
                self._scan_path(path)

    @work(thread=True)
    def _db_sync(self) -> None:
        """Background: connect to Postgres and sync workspace + staging."""
        if not self._workspace or not _db.is_available():
            return
        url = self._db_url
        if not _db.probe(url):
            self.call_from_thread(
                self._log,
                "[dim]🗄  DB not reachable — run [cyan]make db-start[/cyan] to connect[/dim]",
            )
            return
        try:
            _db.init_schema(url)
            n = _db.sync_workspace(self._workspace, url)
            if n:
                self.call_from_thread(
                    self._log, f"[dim]🗄  Synced {n} entities to Postgres[/dim]"
                )
            _db.sync_staging(load_staging(), url)
        except Exception as exc:
            self.call_from_thread(
                self._log, f"[dim yellow]⚠  DB sync error: {exc}[/dim yellow]"
            )

    def _show_live_watch(self) -> None:
        """Display live watch configuration and status."""
        if not self._workspace:
            self._log_error("No workspace loaded.")
            return
        ws = self._workspace
        folders = ws.manifest.watch_folders
        self._open_main("👁  Live Watch")
        if not folders:
            self._main(
                "[dim]No watch folders configured.  "
                "Type [cyan]add folder <path>[/cyan] to add one.[/dim]"
            )
            return
        if not _watcher_available():
            self._main(
                "[yellow]watchdog not installed[/yellow]\n"
                "Install: [cyan]pipx install 'strata-cli[full]'[/cyan]"
            )
        running = self._watcher is not None and self._watcher.is_running
        if running:
            status = f"[green]● Watching ({self._watcher.active_folder_count} folders active)[/green]"
        else:
            status = "[yellow]○ Not watching — type [cyan]/watch start[/cyan] to begin[/yellow]"
        self._main(status + "\n")
        for f in folders:
            fp = Path(f)
            icon = "[green]●[/green]" if fp.is_dir() else "[red]○[/red]"
            self._main(f"  {icon} [cyan]{f}[/cyan]")
        self._main("\n[dim]  /watch start  · /watch stop  · /watch status[/dim]")

    def _handle_watch_slash(self, sub: str) -> None:
        """Dispatch /watch <subcommand>."""
        parts = sub.strip().split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if subcmd in ("", "status"):
            self._show_live_watch()
        elif subcmd == "start":
            if self._watcher and self._watcher.is_running:
                self._log("[dim]👁 Watcher is already running.[/dim]")
            else:
                self._start_watcher()
        elif subcmd == "stop":
            self._stop_watcher()
        elif subcmd == "interval":
            if not arg or not arg.isdigit():
                cur = self._workspace.manifest.scan_interval_minutes if self._workspace else 0
                self._log(
                    f"[bold]Current scan interval:[/bold] "
                    f"{'[green]' + str(cur) + ' minutes[/green]' if cur > 0 else '[dim]disabled (0)[/dim]'}\n"
                    "  Usage: [cyan]/watch interval <minutes>[/cyan]  (0 to disable)"
                )
            else:
                minutes = int(arg)
                try:
                    set_scan_interval(minutes)
                    self._workspace = load_workspace()
                    self._start_scheduler()
                    self._start_advisor_scheduler()
                    self._show_dashboard()
                    if minutes > 0:
                        self._log_strata(
                            f"[green]Auto-scan interval set to {minutes} minute(s)[/green]"
                        )
                    else:
                        self._log_strata("[yellow]Scheduled scanning disabled[/yellow]")
                except Exception as exc:
                    self._log_error(f"Could not set interval: {exc}")
        else:
            self._log(
                "[bold]Watch subcommands:[/bold]\n"
                "  [cyan]/watch[/cyan]                — show watch status\n"
                "  [cyan]/watch start[/cyan]          — start the folder watcher\n"
                "  [cyan]/watch stop[/cyan]           — stop the folder watcher\n"
                "  [cyan]/watch status[/cyan]         — show watch status\n"
                "  [cyan]/watch interval <min>[/cyan] — set auto-scan interval (0 = off)\n"
            )

    def _show_advisor_status(self) -> None:
        """Render advisor scheduler and latest advisory status."""
        self._open_main("🧠 Hybrid Domain Advisor")
        self._render_advisor_status_view(show_progress_hint=True)

    def _advisor_interval_minutes(self) -> int:
        if not self._workspace:
            return 0
        if not self._workspace.manifest.advisor_enabled:
            return 0
        return max(0, int(self._workspace.manifest.advisor_interval_minutes or 0))

    def _finalize_advisor_phase_duration(self, now_mono: float | None = None) -> None:
        if self._advisor_runtime_phase == "idle" or self._advisor_runtime_phase_started_mono <= 0:
            return
        end = now_mono if now_mono is not None else _time.monotonic()
        dur = max(0.0, end - self._advisor_runtime_phase_started_mono)
        self._advisor_phase_durations[self._advisor_runtime_phase] = (
            self._advisor_phase_durations.get(self._advisor_runtime_phase, 0.0) + dur
        )
        self._advisor_runtime_phase_started_mono = 0.0

    def _reset_advisor_runtime(self, run_id: str) -> None:
        now = datetime.now(timezone.utc)
        self._advisor_runtime_run_id = run_id
        self._advisor_runtime_state = "running"
        self._advisor_runtime_phase = "preflight"
        self._advisor_runtime_phase_started_at = now.isoformat()
        self._advisor_runtime_phase_started_mono = _time.monotonic()
        self._advisor_phase_durations = {}
        self._advisor_recent_events = []
        self._advisor_domain_progress = {}

    def _record_advisor_progress_event(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        run_id = str(event.get("run_id") or "").strip()
        if run_id and run_id != self._advisor_runtime_run_id:
            self._reset_advisor_runtime(run_id)

        now_mono = _time.monotonic()
        phase = str(event.get("phase") or "").strip() or self._advisor_runtime_phase
        state = str(event.get("state") or "running").strip().lower()
        ts = str(event.get("ts") or datetime.now(timezone.utc).isoformat())
        message = str(event.get("message") or "").strip()
        domain = str(event.get("domain") or "").strip()

        if phase and phase != self._advisor_runtime_phase:
            self._finalize_advisor_phase_duration(now_mono)
            self._advisor_runtime_phase = phase
            self._advisor_runtime_phase_started_at = ts
            self._advisor_runtime_phase_started_mono = now_mono

        self._advisor_runtime_state = state
        if domain:
            self._advisor_domain_progress[domain] = {
                "phase": phase,
                "state": state,
                "ts": ts,
                "message": message,
            }

        event_row = {
            "ts": ts,
            "phase": phase,
            "state": state,
            "domain": domain,
            "message": message,
        }
        self._advisor_recent_events.append(event_row)
        if len(self._advisor_recent_events) > 40:
            self._advisor_recent_events = self._advisor_recent_events[-40:]

        if phase in {"complete", "failed"} or state in {"ok", "failed"}:
            self._finalize_advisor_phase_duration(now_mono)
            self._advisor_last_timeline = list(self._advisor_recent_events)

    def _format_next_run_eta(self) -> str:
        if not self._advisor_next_run_at:
            return "n/a"
        now = datetime.now(timezone.utc)
        delta = self._advisor_next_run_at - now
        secs = int(delta.total_seconds())
        if secs <= 0:
            return "due now"
        mins, sec = divmod(secs, 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            return f"in {hours}h {mins}m"
        if mins > 0:
            return f"in {mins}m {sec}s"
        return f"in {sec}s"

    def _render_advisor_status_view(self, show_progress_hint: bool = False) -> None:
        ws = self._workspace
        enabled = bool(ws and ws.manifest.advisor_enabled and ws.manifest.advisor_interval_minutes > 0)
        interval = ws.manifest.advisor_interval_minutes if ws else 0
        profile = ws.manifest.advisor_profile if ws else "oda"
        running = self._advisor_timer is not None

        status = (
            "[green]● scheduled[/green]"
            if enabled and running
            else "[yellow]○ disabled[/yellow]"
        )
        self._main(
            f"{status}  profile=[cyan]{profile}[/cyan]  "
            f"interval={interval if interval > 0 else 0} min  "
            f"next={self._format_next_run_eta()}\n"
        )

        runtime_color = (
            "green" if self._advisor_runtime_state == "ok" else
            "yellow" if self._advisor_runtime_state in {"running", "degraded"} else
            "red" if self._advisor_runtime_state == "failed" else
            "dim"
        )
        self._main(
            f"[dim]Runtime:[/dim] [{runtime_color}]{self._advisor_runtime_state}[/{runtime_color}]  "
            f"[dim]run_id:[/dim] {self._advisor_runtime_run_id or 'n/a'}  "
            f"[dim]phase:[/dim] {self._advisor_runtime_phase or 'idle'}\n"
        )

        if self._advisor_last_run:
            self._main(f"[dim]Last run: {self._advisor_last_run}[/dim]\n")
        if self._advisor_last_status:
            self._main(f"[dim]{self._advisor_last_status}[/dim]\n")

        latest = load_latest_advisory()
        if latest:
            meta = latest.get("meta", {})
            synthesis = latest.get("panel", {}).get("synthesis", {})
            domain_scores = synthesis.get("domain_scores", [])
            critical_count = sum(
                1 for d in domain_scores if str(d.get("attention_level", "")).lower() == "critical"
            )
            unresolved_count = sum(
                1
                for d in synthesis.get("decisions_needed", [])
                if str(d.get("status", "open")).lower() != "resolved"
            )
            self._main(
                "\n[bold]Latest advisory[/bold]\n"
                f"  run: [cyan]{meta.get('run_id', 'n/a')}[/cyan]\n"
                f"  generated: {meta.get('generated_at', 'n/a')}\n"
                f"  provider/model: {meta.get('provider', 'n/a')} / {meta.get('model', 'n/a')}\n"
                f"  degraded: {'yes' if meta.get('degraded') else 'no'}\n"
                f"  critical domains: {critical_count}\n"
                f"  unresolved decisions: {unresolved_count}\n"
            )

            paths = meta.get("paths", {}) if isinstance(meta.get("paths", {}), dict) else {}
            if paths:
                self._main(
                    f"  artifacts: latest={paths.get('latest_yaml', 'n/a')}  backlog={paths.get('decision_backlog', 'n/a')}\n"
                )

            self._main("\n[bold]Domain Agents[/bold]\n")
            domains = latest.get("panel", {}).get("domains", [])
            docs = synthesis.get("recommended_docs", [])
            decisions = synthesis.get("decisions_needed", [])
            for dom in domains:
                dom_id = str(dom.get("domain") or dom.get("role_id") or "unknown")
                score_row = next((d for d in domain_scores if str(d.get("domain")) == dom_id), {})
                score = float(score_row.get("weighted_score", 0.0))
                confidence = float(score_row.get("confidence", 0.0))
                attention = str(score_row.get("attention_level", "n/a"))
                dims = score_row.get("dimensions", []) if isinstance(score_row.get("dimensions", []), list) else []
                low_dims = sorted(
                    [d for d in dims if isinstance(d, dict)],
                    key=lambda d: float(d.get("score", 0.0)),
                )[:2]
                dim_summary = ", ".join(
                    f"{d.get('key','?')}={float(d.get('score', 0.0)):.2f}" for d in low_dims
                ) or "n/a"

                d_decisions = [
                    d for d in decisions
                    if isinstance(d, dict) and str(d.get("domain", "")) == dom_id and str(d.get("status", "open")).lower() != "resolved"
                ]
                d_decisions = sorted(d_decisions, key=lambda d: -float(d.get("priority_score", 0.0)))[:2]
                d_decision_text = " | ".join(
                    f"{d.get('priority','medium')}:{d.get('decision','')}({d.get('priority_score', 0)})"
                    for d in d_decisions
                ) or "none"

                d_docs = [
                    d for d in docs
                    if isinstance(d, dict) and str(d.get("domain", "")) == dom_id
                ]
                d_docs = sorted(d_docs, key=lambda d: -float(d.get("priority_score", 0.0)))[:2]
                d_doc_text = " | ".join(
                    f"{d.get('doc_type','Doc')}:{d.get('title','')}[{d.get('priority','medium')}]"
                    for d in d_docs
                ) or "none"

                dep_count = len(dom.get("cross_domain_dependencies", []) or [])
                risk_count = len(dom.get("interop_risks", []) or [])

                self._main(
                    f"  [cyan]{dom_id}[/cyan]  score={score:.2f}/5  attention={attention}  confidence={confidence:.2f}\n"
                    f"    lowest_dims: {dim_summary}\n"
                    f"    decisions: {d_decision_text}\n"
                    f"    docs: {d_doc_text}\n"
                    f"    dependencies={dep_count}  interop_risks={risk_count}\n"
                )
        else:
            self._main("\n[dim]No advisory artifacts found yet in architecture/advice/.[/dim]\n")

        try:
            ok, msg = ArchitectureAgent(provider=self._provider).check_available()
            color = "green" if ok else "yellow"
            self._main(f"\n[{color}]Provider status:[/{color}] {msg}\n")
        except Exception as exc:
            self._main(f"\n[yellow]Provider check failed:[/yellow] {exc}\n")

        self._main(
            "\n[dim]Commands: [cyan]/advisor status[/cyan] · "
            "[cyan]/advisor progress[/cyan] · "
            "[cyan]/advisor interval <min>[/cyan] (0 disables) · "
            "[cyan]/improve-ai[/cyan] to view latest advisory[/dim]\n"
        )

        if show_progress_hint:
            self._main("[dim]Use [cyan]/advisor progress[/cyan] to inspect live run phases and timeline events.[/dim]\n")

    def _show_advisor_progress(self) -> None:
        """Render a focused progress/timeline view for current or last advisory run."""
        self._open_main("🧠 Advisor Progress")
        self._main(
            f"[dim]state:[/dim] {self._advisor_runtime_state}  "
            f"[dim]run_id:[/dim] {self._advisor_runtime_run_id or 'n/a'}  "
            f"[dim]phase:[/dim] {self._advisor_runtime_phase or 'idle'}\n"
        )

        if self._advisor_phase_durations:
            self._main("\n[bold]Phase Durations[/bold]\n")
            for phase, dur in sorted(self._advisor_phase_durations.items()):
                self._main(f"  - {phase}: {dur:.2f}s\n")

        if self._advisor_domain_progress:
            self._main("\n[bold]Domain Progress[/bold]\n")
            for dom, info in sorted(self._advisor_domain_progress.items()):
                self._main(
                    f"  - [cyan]{dom}[/cyan] phase={info.get('phase','n/a')} "
                    f"state={info.get('state','n/a')}\n"
                )

        timeline = self._advisor_recent_events or self._advisor_last_timeline
        self._main("\n[bold]Recent Events[/bold]\n")
        if not timeline:
            self._main("  [dim]No advisor run events captured yet.[/dim]\n")
        else:
            for event in timeline[-20:]:
                ts = str(event.get("ts", ""))
                phase = str(event.get("phase", ""))
                state = str(event.get("state", ""))
                dom = str(event.get("domain", ""))
                msg = str(event.get("message", ""))
                dom_tag = f" ({dom})" if dom else ""
                self._main(f"  - {ts} [{state}] {phase}{dom_tag} — {msg}\n")

    def _handle_advisor_slash(self, sub: str) -> None:
        """Dispatch /advisor <subcommand>."""
        parts = sub.strip().split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if subcmd in ("", "status"):
            self._show_advisor_status()
            return
        if subcmd == "progress":
            self._show_advisor_progress()
            return
        if subcmd == "interval":
            if not arg or not arg.isdigit():
                cur = self._workspace.manifest.advisor_interval_minutes if self._workspace else 0
                self._log(
                    f"[bold]Current advisor interval:[/bold] "
                    f"{'[green]' + str(cur) + ' minutes[/green]' if cur > 0 else '[dim]disabled (0)[/dim]'}\n"
                    "  Usage: [cyan]/advisor interval <minutes>[/cyan]  (0 to disable)"
                )
                return
            minutes = int(arg)
            try:
                set_advisor_interval(minutes)
                self._workspace = load_workspace()
                self._start_advisor_scheduler()
                self._show_dashboard()
                if minutes > 0:
                    self._log_strata(f"[green]Advisor interval set to {minutes} minute(s)[/green]")
                else:
                    self._log_strata("[yellow]Scheduled advisor disabled[/yellow]")
            except Exception as exc:
                self._log_error(f"Could not set advisor interval: {exc}")
            return

        self._log(
            "[bold]Advisor subcommands:[/bold]\n"
            "  [cyan]/advisor[/cyan]                — show advisor status\n"
            "  [cyan]/advisor status[/cyan]         — show advisor status\n"
            "  [cyan]/advisor progress[/cyan]       — show run phase/timeline details\n"
            "  [cyan]/advisor interval <min>[/cyan] — set scheduled advisor interval (0 = off)\n"
        )

    # ── Scheduler ──────────────────────────────────────────────────────────

    def _start_scheduler(self) -> None:
        """Start (or restart) the scheduled auto-scan timer from the manifest."""
        # Cancel any existing timer
        if self._scan_timer is not None:
            try:
                self._scan_timer.stop()
            except Exception:
                pass
            self._scan_timer = None
        if not self._workspace:
            return
        interval = self._workspace.manifest.scan_interval_minutes
        if interval <= 0:
            return
        self._scan_timer = self.set_interval(
            interval * 60, self._scheduled_scan, name="auto-scan"
        )
        self._log(f"[dim]⏱ Auto-scan every {interval} minute(s)[/dim]")

    def _start_advisor_scheduler(self) -> None:
        """Start (or restart) the scheduled hybrid domain advisor timer."""
        if self._advisor_timer is not None:
            try:
                self._advisor_timer.stop()
            except Exception:
                pass
            self._advisor_timer = None
        self._advisor_next_run_at = None
        if not self._workspace:
            return
        manifest = self._workspace.manifest
        if not manifest.advisor_enabled:
            return
        interval = manifest.advisor_interval_minutes
        if interval <= 0:
            return
        self._advisor_timer = self.set_interval(
            interval * 60, self._scheduled_advisor, name="auto-advisor"
        )
        self._advisor_next_run_at = datetime.now(timezone.utc) + timedelta(minutes=interval)
        self._log(
            f"[dim]🧠 Advisor run every {interval} minute(s) "
            f"(profile: {manifest.advisor_profile})[/dim]"
        )

    def _scheduled_scan(self) -> None:
        """Called by the interval timer — runs _scan_all_folders."""
        self._log("[dim]⏱ Scheduled scan triggered…[/dim]")
        self._scan_all_folders()

    def _scheduled_advisor(self) -> None:
        """Called by the advisor timer — runs background hybrid domain advisory cycle."""
        if not self._workspace:
            return
        self._advisor_next_run_at = None
        self._log("[dim]🧠 Scheduled hybrid domain advisory run triggered…[/dim]")
        self._run_scheduled_advisory()

    @work(thread=True)
    def _run_scheduled_advisory(self) -> None:
        """Background worker: run and persist one advisory cycle."""
        ws = self._workspace
        if not ws:
            return
        profile = ws.manifest.advisor_profile
        run_hint = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.call_from_thread(self._reset_advisor_runtime, run_hint)
        try:
            run = run_advisory_cycle(
                workspace=ws,
                profile=profile,
                provider=self._provider,
                progress_cb=lambda e: self.call_from_thread(self._record_advisor_progress_event, e),
            )
            meta = run.get("meta", {})
            self._advisor_runtime_run_id = str(meta.get("run_id") or self._advisor_runtime_run_id)
            self._advisor_runtime_state = "degraded" if meta.get("degraded") else "ok"
            self._advisor_runtime_phase = "complete"
            self._advisor_last_run = str(meta.get("generated_at", ""))
            self._advisor_last_status = (
                f"last run {meta.get('run_id', '')} "
                f"({'degraded' if meta.get('degraded') else 'ok'}) "
                f"provider={meta.get('provider', '')} model={meta.get('model', '')}"
            )
            interval = self._advisor_interval_minutes()
            self._advisor_next_run_at = (
                datetime.now(timezone.utc) + timedelta(minutes=interval)
                if interval > 0
                else None
            )
            self.call_from_thread(
                self._log,
                f"[dim]🧠 Advisory run complete — "
                f"[cyan]{meta.get('run_id','') }[/cyan] "
                f"{'(degraded)' if meta.get('degraded') else ''}[/dim]",
            )
        except Exception as exc:
            self.call_from_thread(
                self._record_advisor_progress_event,
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "run_id": self._advisor_runtime_run_id or run_hint,
                    "phase": "failed",
                    "state": "failed",
                    "message": f"Advisory run failed: {exc}",
                },
            )
            self._advisor_runtime_state = "failed"
            self._advisor_runtime_phase = "failed"
            self._advisor_last_status = f"advisor run failed: {exc}"
            interval = self._advisor_interval_minutes()
            self._advisor_next_run_at = (
                datetime.now(timezone.utc) + timedelta(minutes=interval)
                if interval > 0
                else None
            )
            self.call_from_thread(
                self._log,
                f"[dim yellow]⚠ Advisor run failed: {exc}[/dim yellow]",
            )

    # ── Scoring ────────────────────────────────────────────────────────────

    def _show_score(self, profile_arg: str = "") -> None:
        """Render a full architecture maturity score."""
        if not self._workspace:
            self._log_error("No workspace loaded.")
            return

        profile = profile_arg.lower() if profile_arg else "default"
        available = list_profiles()
        if profile not in available:
            self._log_error(
                f"Unknown profile: [cyan]{profile}[/cyan]  "
                f"Available: {', '.join(available)}"
            )
            return

        try:
            result = score_workspace(self._workspace, profile=profile)
        except Exception as exc:
            self._log_error(f"Scoring failed: {exc}")
            return

        ov = result.overall
        col = "green" if ov >= 3.0 else "yellow" if ov >= 2.0 else "red"
        bar_w = 30
        filled = round((ov / 5.0) * bar_w)
        bar = f"[{col}]{'█' * filled}{'░' * (bar_w - filled)}[/{col}]"

        self._open_main(f"📈 Architecture Maturity — {result.profile_name}")
        self._main(f"[dim]{result.profile_description}[/dim]\n")
        self._main(
            f"  Overall:  {bar}  [{col}][bold]{ov:.1f}[/bold][/{col}] / 5.0   "
            f"Level: [bold {col}]{result.level}[/bold {col}]\n"
        )

        t = RichTable(box=rich_box.SIMPLE_HEAVY, title="Dimension Breakdown")
        t.add_column("Dimension", style="bold")
        t.add_column("Score", justify="right")
        t.add_column("Weight", justify="right", style="dim")
        t.add_column("Bar", no_wrap=True)
        t.add_column("Findings")

        for d in result.dimensions:
            dc = "green" if d.score >= 3.0 else "yellow" if d.score >= 2.0 else "red"
            bw = 12
            bf = round((d.score / d.max_score) * bw)
            dim_bar = f"[{dc}]{'█' * bf}{'░' * (bw - bf)}[/{dc}]"
            findings_str = "; ".join(d.findings[:3]) if d.findings else "[dim]—[/dim]"
            t.add_row(
                d.label,
                f"[{dc}]{d.score:.1f}[/{dc}]",
                f"×{d.weight:.1f}",
                dim_bar,
                findings_str,
            )
        self._main_table(t)

        # Top improvements
        improvements = compute_top_improvements(result, n=3)
        rank_icons = {1: "🥇", 2: "🥈", 3: "🥉"}
        self._main("\n[bold]🎯 Top Improvements[/bold]")
        for imp in improvements:
            dc = "green" if imp.current_score >= 3.0 else "yellow" if imp.current_score >= 2.0 else "red"
            self._main(
                f"  {rank_icons.get(imp.rank, str(imp.rank))}  "
                f"[bold]{imp.dimension_label}[/bold]  [{dc}]{imp.current_score:.1f}[/{dc}]/5.0   "
                f"[dim]{imp.key_action}[/dim]"
            )

        # Level legend
        legend = "  ".join(
            f"[dim]{label} ({lo:.0f}–{hi:.0f})[/dim]"
            for label, (lo, hi) in result.level_labels.items()
        )
        self._main(f"\n[dim]Levels: {legend}[/dim]")
        self._main(f"[dim]Available profiles: {', '.join(available)}[/dim]\n")

    # ── Staging impact (AI) ────────────────────────────────────────────────

    @work(thread=True)
    def _ai_staging_impact(self) -> None:
        """Ask AI to summarise the impact of pending staged items."""
        if not self._workspace:
            self.call_from_thread(self._log_error, "No workspace loaded.")
            return

        staging = load_staging()
        pending = [s for s in staging if s.status == "pending"]
        if not pending:
            self.call_from_thread(
                self._log_strata, "No pending items to analyse."
            )
            return

        self.call_from_thread(self._log, "[dim]Analysing staged item impact…[/dim]")

        try:
            agent = ArchitectureAgent(provider=self._provider)
            available, msg = agent.check_available()
            if not available:
                self.call_from_thread(self._log_error, f"AI not available: {msg}")
                return

            # Build a compact workspace snapshot
            ws = self._workspace
            snapshot = {
                "workspace": ws.manifest.name,
                "current_state": {
                    "capabilities": len(ws.enterprise.capabilities),
                    "applications": len(ws.enterprise.applications),
                    "standards": len(ws.enterprise.standards),
                    "data_domains": len(ws.data.domains),
                    "data_products": len(ws.data.products),
                    "data_flows": len(ws.data.flows),
                    "solutions": len(ws.solutions),
                },
                "pending_items": [
                    {"id": s.id, "entity": s.entity, "fields": s.fields, "source": s.source}
                    for s in pending
                ],
            }
            # Add current score
            try:
                result = score_workspace(ws)
                snapshot["current_score"] = {
                    "overall": result.overall,
                    "level": result.level,
                    "dimensions": {
                        d.key: d.score for d in result.dimensions
                    },
                }
            except Exception:
                pass

            import json
            prompt = (
                "You are an architecture advisor. Analyse the impact of accepting "
                "these pending staged items into the workspace.\n\n"
                "Current workspace state and pending items:\n"
                f"```json\n{json.dumps(snapshot, indent=2)}\n```\n\n"
                "Provide a structured impact analysis:\n"
                "1. **Summary** — one-paragraph overview of what these items bring\n"
                "2. **Cross-domain effects** — how they affect enterprise, data, and solution layers\n"
                "3. **Capability gaps filled** — which gaps or weaknesses are addressed\n"
                "4. **Risk indicators** — any concerns, duplications, or conflicts\n"
                "5. **Recommendation** — accept all, review specific items, or reject with reasons\n\n"
                "Keep it concise and actionable. Use markdown formatting."
            )

            messages = [{"role": "user", "content": prompt}]
            resp = agent.chat(messages)
            response = resp.get("message", "") if isinstance(resp, dict) else str(resp)
            self.call_from_thread(
                self._log,
                f"\n[bold]📋 Staging Impact Analysis[/bold]  "
                f"[dim]({len(pending)} pending item(s))[/dim]\n"
            )
            self.call_from_thread(self._log_ai_response, response)

        except Exception as exc:
            self.call_from_thread(self._log_error, f"Impact analysis failed: {exc}")

    # ── Stack coverage view ────────────────────────────────────────────────────

    def _show_stack(self) -> None:
        """Render the full stack coverage map — capabilities, data, solutions, gaps."""
        if not self._workspace:
            self._log_error("No workspace loaded.")
            return

        ws = self._workspace
        cov = compute_stack_coverage(ws)

        self._open_main(f"🗺️  Stack Coverage — {ws.manifest.name}")

        # ── Capability domains ──────────────────────────────────────────────
        if cov.capability_domains:
            t = RichTable(
                title=f"Business Capability Domains ({len(cov.capability_domains)})",
                box=rich_box.SIMPLE_HEAVY,
            )
            t.add_column("", width=3, no_wrap=True)
            t.add_column("Domain", style="bold")
            t.add_column("Caps", justify="right")
            t.add_column("Strategic / Core / Supporting")
            t.add_column("Ownership", justify="right")
            t.add_column("Mature", justify="right")
            for d in cov.capability_domains:
                oc = "green" if d.ownership_pct >= 80 else "yellow" if d.ownership_pct >= 50 else "red"
                mc = "green" if d.mature_pct >= 50 else "yellow" if d.mature_pct >= 25 else "red"
                t.add_row(
                    d.indicator, d.domain, str(d.count),
                    f"[green]{d.strategic}[/green] / [blue]{d.core}[/blue] / [dim]{d.supporting}[/dim]",
                    f"[{oc}]{d.ownership_pct:.0f}%[/{oc}]",
                    f"[{mc}]{d.mature_pct:.0f}%[/{mc}]",
                )
            self._main_table(t)
        else:
            self._main("[dim]No capabilities defined yet.[/dim]")

        if cov.missing_cap_domains:
            self._main(
                f"  [yellow]⚠[/yellow]  [dim]Not covered (reference domains): "
                f"{', '.join(cov.missing_cap_domains)}[/dim]\n"
            )

        # ── Data domains ────────────────────────────────────────────────────
        if cov.data_domains:
            dt = RichTable(
                title=f"Data Domains ({len(cov.data_domains)})",
                box=rich_box.SIMPLE_HEAVY,
            )
            dt.add_column("", width=3, no_wrap=True)
            dt.add_column("Domain", style="bold")
            dt.add_column("Owner")
            dt.add_column("Products", justify="right")
            dt.add_column("Gold/Plat SLA", justify="right")
            dt.add_column("Flows In", justify="right")
            dt.add_column("Flows Out", justify="right")
            for d in cov.data_domains:
                sla = f"[yellow]{d.sla_gold_plat}[/yellow]" if d.sla_gold_plat else "[dim]—[/dim]"
                dt.add_row(
                    d.indicator, d.name, d.owner, str(d.products_count),
                    sla, str(d.flows_in), str(d.flows_out),
                )
            self._main_table(dt)

        # ── Solutions ───────────────────────────────────────────────────────
        if cov.solutions:
            st = RichTable(
                title=f"Solution Designs ({len(cov.solutions)})",
                box=rich_box.SIMPLE_HEAVY,
            )
            st.add_column("", width=3, no_wrap=True)
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
            self._main_table(st)

        # ── Tech radar by category ──────────────────────────────────────────
        if cov.radar_by_category:
            rt = RichTable(title="Tech Radar by Category", box=rich_box.SIMPLE_HEAVY)
            rt.add_column("Category", style="bold")
            rt.add_column("Standards")
            for cat, items in sorted(cov.radar_by_category.items()):
                rt.add_row(cat, "  ".join(f"[dim]{item}[/dim]" for item in items[:5]))
            self._main_table(rt)

        # ── Gaps summary ────────────────────────────────────────────────────
        if cov.gaps:
            self._main(
                f"\n[bold yellow]⚠  Gaps & Recommendations ({len(cov.gaps)})[/bold yellow]"
            )
            for i, g in enumerate(cov.gaps, 1):
                self._main(f"  [yellow]{i}.[/yellow] {g}")
            self._main(
                "\n  [dim]→ [cyan]/improve[/cyan] roadmap  ·  "
                "[cyan]/improve-ai[/cyan] latest hybrid domain advisory[/dim]\n"
            )
        else:
            self._main("\n  [green]✅  No critical gaps detected[/green]\n")

    # ── Improvement roadmap (local, instant — no AI required) ─────────────────

    def _show_improve_roadmap(self) -> None:
        """Render a phased improvement roadmap in the main pane."""
        if not self._workspace:
            self._log_error("No workspace loaded.")
            return

        try:
            result = score_workspace(self._workspace, profile="default")
        except Exception as exc:
            self._log_error(f"Scoring failed: {exc}")
            return

        ws = self._workspace
        phases = compute_roadmap_phases(result)

        ov = result.overall
        col = "green" if ov >= 3.0 else "yellow" if ov >= 2.0 else "red"
        bar_w = 24
        filled = round((ov / 5.0) * bar_w)
        bar = f"[{col}]{'█' * filled}{'░' * (bar_w - filled)}[/{col}]"

        self._open_main(f"🗺  Improvement Roadmap — {ws.manifest.name}")
        self._main(
            f"  {bar}  [{col}][bold]{ov:.1f}[/bold][/{col}] / 5.0   "
            f"Level: [bold {col}]{result.level}[/bold {col}]   "
            f"[dim]Profile: {result.profile_name}[/dim]\n"
        )

        # ── Phased roadmap table ──────────────────────────────────────────────
        t = RichTable(box=rich_box.SIMPLE_HEAVY, title="Phased Improvement Plan")
        t.add_column("Phase", style="bold", no_wrap=True)
        t.add_column("Horizon", style="dim", no_wrap=True)
        t.add_column("Dimension", style="bold")
        t.add_column("Priority", no_wrap=True)
        t.add_column("Now", justify="right", no_wrap=True)
        t.add_column("Δ", justify="right", style="dim", no_wrap=True)
        t.add_column("Action")

        for p in phases:
            dc = (
                "red" if p.priority_level == 1
                else "yellow" if p.priority_level == 2
                else "green" if p.priority_level == 3
                else "dim"
            )
            t.add_row(
                p.phase,
                p.horizon,
                p.dimension_label,
                f"[{dc}]{p.priority}[/{dc}]",
                f"[{dc}]{p.current_score:.1f}[/{dc}]",
                p.score_delta,
                p.action,
            )
        self._main_table(t)

        # ── Gap heatmap ───────────────────────────────────────────────────────
        self._main(
            "\n[bold]📊 Dimension Gap Heatmap[/bold]  "
            "[dim](widest weighted gap → top priority)[/dim]"
        )
        ranked_dims = sorted(
            result.dimensions,
            key=lambda d: (d.max_score - d.score) * d.weight,
            reverse=True,
        )
        ht = RichTable(box=rich_box.SIMPLE, show_header=False, padding=(0, 1))
        ht.add_column("dim", style="bold", min_width=26)
        ht.add_column("bar", no_wrap=True)
        ht.add_column("score", justify="right", no_wrap=True)
        ht.add_column("gap", justify="right", style="dim", no_wrap=True)
        for d in ranked_dims:
            dc = "green" if d.score >= 3.0 else "yellow" if d.score >= 2.0 else "red"
            gap = round((d.max_score - d.score) * d.weight, 2)
            bw = 16
            bf = round((d.score / d.max_score) * bw)
            dim_bar = f"[{dc}]{'█' * bf}{'░' * (bw - bf)}[/{dc}]"
            ht.add_row(
                d.label,
                dim_bar,
                f"[{dc}]{d.score:.1f}[/{dc}]",
                f"gap {gap:.2f}",
            )
        self._main_table(ht)

        self._main(
            "\n  [dim]→ [cyan]/improve-ai[/cyan] latest hybrid domain advisory  ·  "
            "[cyan]/score[/cyan] full breakdown  ·  "
            "[cyan]/stack[/cyan] coverage view[/dim]\n"
        )

    # ── AI improvement recommendations ────────────────────────────────────────

    @work(thread=True)
    def _ai_improve(self) -> None:
        """Ask AI for improvement recommendations based on current score and stack gaps."""
        if not self._workspace:
            self.call_from_thread(self._log_error, "No workspace loaded.")
            return

        self.call_from_thread(self._log, "[dim]Generating improvement recommendations…[/dim]")

        try:
            agent = ArchitectureAgent(provider=self._provider)
            available, msg = agent.check_available()
            if not available:
                self.call_from_thread(self._log_error, f"AI not available: {msg}")
                return

            ws = self._workspace

            # Score and stack coverage
            try:
                result = score_workspace(ws, profile="default")
            except Exception:
                result = None

            cov = compute_stack_coverage(ws)
            improvements = compute_top_improvements(result, n=3) if result else []

            import json
            context = {
                "workspace": ws.manifest.name,
                "overall_score": (
                    {"score": result.overall, "level": result.level}
                    if result else None
                ),
                "top_dimension_gaps": [
                    {
                        "dimension": i.dimension_label,
                        "score": i.current_score,
                        "key_finding": i.key_action,
                    }
                    for i in improvements
                ],
                "stack_gaps": cov.gaps,
                "missing_capability_domains": cov.missing_cap_domains,
                "isolated_data_domains": cov.isolated_data_domains,
            }

            prompt = (
                "You are a senior enterprise architect. "
                "Based on the workspace analysis below, provide actionable improvement recommendations.\n\n"
                f"Workspace analysis:\n```json\n{json.dumps(context, indent=2)}\n```\n\n"
                "Provide:\n"
                "1. **Quick wins** — 2-3 improvements achievable in days with the highest maturity-score impact\n"
                "2. **Strategic priorities** — 2-3 medium-term improvements (weeks/months)\n"
                "3. **Architecture patterns** — specific patterns or practices to adopt for this stack\n"
                "4. **Next concrete step** — one specific action to take right now\n\n"
                "Be specific, reference the actual domains and dimensions by name. "
                "Keep it concise and practical. Use markdown formatting."
            )

            messages = [{"role": "user", "content": prompt}]
            resp = agent.chat(messages)
            response = resp.get("message", "") if isinstance(resp, dict) else str(resp)
            self.call_from_thread(
                self._log,
                f"\n[bold]💡 Improvement Recommendations — {ws.manifest.name}[/bold]\n",
            )
            self.call_from_thread(self._log_ai_response, response)

        except Exception as exc:
            self.call_from_thread(self._log_error, f"Improve analysis failed: {exc}")

    # ── /model provider picker ─────────────────────────────────────────────────

    def _show_model_picker(self) -> None:
        """Show provider overview and model catalog for the active provider."""
        self._open_main("🤖 AI Provider & Model Overview")
        self._main(
            "[dim]Checking provider availability — this may take a second…[/dim]\n"
        )
        self._wk_check_all_providers()

    @work(thread=True)
    def _wk_check_all_providers(self) -> None:
        """Background worker: check all providers and render a status table."""
        from rich.table import Table as RichTable
        import rich.box as rich_box

        _AUTH = {
            "copilot": "OAuth  (device flow auto-starts on /model switch)",
            "github":  "Disabled by OAuth-only policy",
            "claude":  "OAuth  (Claude Code CLI)",
            "codex":   "OAuth  (Codex CLI / ~/.codex)",
            "ollama":  "Disabled by OAuth-only policy",
            "openai":  "Disabled by OAuth-only policy",
        }

        try:
            agent = ArchitectureAgent(provider="auto")
            results = agent.check_all()
            current = self._provider
            resolved = ArchitectureAgent(provider=current)._effective_provider()

            t = RichTable(
                box=rich_box.SIMPLE_HEAVY,
                title="Available AI Providers",
                expand=False,
            )
            t.add_column("", width=2)
            t.add_column("Provider", style="bold", min_width=10)
            t.add_column("Auth", min_width=30)
            t.add_column("Status", min_width=12)
            t.add_column("Details")

            availability: dict[str, bool] = {}
            for name, ok, msg in results:
                availability[name] = ok
                icon = "✅" if ok else "⚠️ "
                is_active = (name == current) or (current == "auto" and name == resolved)
                active_tag = "  [cyan]◀ active[/cyan]" if is_active else ""
                if current == "auto" and name == resolved:
                    active_tag = "  [cyan]◀ active (auto)[/cyan]"
                if not ok and "disabled by OAuth-only policy" in (msg or "").lower():
                    status_label = "[yellow]Policy Disabled[/yellow]"
                else:
                    status_label = "[green]Available[/green]" if ok else "[dim]Unavailable[/dim]"
                auth_label = _AUTH.get(name, "")
                t.add_row(icon, f"{name}{active_tag}", auth_label, status_label, msg or "")

            auto_note = f" → resolves to [bold]{resolved}[/bold]" if current == "auto" else ""
            hint = (
                f"\n  [dim]Current: [bold]{current}[/bold]{auto_note}\n"
                "  [cyan]/model overview[/cyan] to view providers + active provider models\n"
                "  [cyan]/model <provider>[/cyan] to switch  ·  "
                "[cyan]/model <provider> <model-id>[/cyan] to switch with a specific model\n"
                "  [cyan]/auth [provider][/cyan] to force interactive OAuth for a provider[/dim]\n"
            )

            self.call_from_thread(self._open_main, "🤖 AI Provider & Model Selection")
            self.call_from_thread(self._main_table, t)
            self.call_from_thread(self._main, hint)

            active_provider = resolved if current == "auto" else current
            if active_provider in list_provider_ids():
                try:
                    models = agent.list_models(active_provider)
                except Exception as exc:
                    models = []
                    self.call_from_thread(
                        self._main,
                        f"\n  [dim]Unable to load model catalog for [bold]{active_provider}[/bold]: {exc}[/dim]\n",
                    )
                if models:
                    live_ids = [m.get("id", "") for m in models if m.get("id")]
                    self.call_from_thread(self._update_provider_model_completions, active_provider, live_ids)
                    self.call_from_thread(self._render_provider_model_table, active_provider, models)
                else:
                    is_up = availability.get(active_provider, False)
                    if is_up:
                        self.call_from_thread(
                            self._main,
                            f"\n  [dim]{active_provider} does not expose a model catalog.[/dim]\n",
                        )
                    else:
                        self.call_from_thread(
                            self._main,
                            f"\n  [dim]{active_provider} is currently unavailable; no model list shown.[/dim]\n",
                        )

        except Exception as exc:
            self.call_from_thread(self._log_error, f"Provider check failed: {exc}")

    def _config_get(self, key: str, default: str = "") -> str:
        """Read a value from the loaded agent config."""
        from .agent import _load_config as _lc
        return _lc().get(key, default)

    def _update_provider_model_completions(self, provider_name: str, model_ids: list[str]) -> None:
        """Extend slash completions with provider-specific model IDs."""
        new_entries = [f"/model {provider_name} {mid}" for mid in model_ids]
        existing = set(_SLASH_COMPLETIONS)
        added = [e for e in new_entries if e not in existing]
        if added:
            _SLASH_COMPLETIONS.extend(added)

    def _render_provider_model_table(self, provider_name: str, models: list[dict[str, Any]]) -> None:
        """Render model catalog for a single provider."""
        _VENDOR_COLOR = {
            "Anthropic": "magenta",
            "OpenAI": "cyan",
            "Azure OpenAI": "cyan",
            "Google": "green",
            "xAI": "yellow",
        }
        _MODEL_KEY = self._MODEL_CONFIG_KEY.get(provider_name, "")
        selected_model = self._config_get(_MODEL_KEY, "") if _MODEL_KEY else ""

        mt = RichTable(
            box=rich_box.SIMPLE_HEAVY,
            title=f"{provider_name.capitalize()} — Available Models",
            expand=False,
        )
        mt.add_column("Model ID", style="bold", min_width=28)
        mt.add_column("Name", min_width=22)
        mt.add_column("Vendor", min_width=12)
        mt.add_column("Category", min_width=12)
        mt.add_column("")

        for m in models:
            mid = m.get("id", "")
            mname = m.get("name", "") or mid
            vendor = m.get("vendor", "")
            cat = m.get("model_picker_category", "") or m.get("category", "")
            vc = _VENDOR_COLOR.get(vendor, "white")
            is_cur = bool(selected_model and mid == selected_model)
            cur_tag = "  [cyan]◀ selected[/cyan]" if is_cur else ""
            mt.add_row(
                f"[{vc}]{mid}[/{vc}]{cur_tag}",
                mname,
                f"[{vc}]{vendor}[/{vc}]" if vendor else "",
                cat,
                f"[dim]/model {provider_name} {mid}[/dim]",
            )

        self._main(
            f"\n  [bold]Active Provider Models: [cyan]{provider_name}[/cyan] ({len(models)})[/bold]\n"
        )
        self._main_table(mt)

    # Maps provider name → config key for model overrides
    _MODEL_CONFIG_KEY: dict[str, str] = {
        "copilot": "copilot_model",
        "claude":  "claude_model",
        "github":  "github_model",
        "codex":   "openai_model",
        "openai":  "openai_model",
        "ollama":  "ollama_model",
    }

    def _set_model_provider(self, name: str, model: str = "") -> None:
        """Verify then switch provider/model transactionally."""
        _VALID = {"auto", *list_provider_ids()}
        if name not in _VALID:
            self._log_error(
                f"Unknown provider [bold]{name!r}[/bold]. "
                f"Valid options: {', '.join(sorted(_VALID))}"
            )
            return

        previous = self._provider
        self._wk_verify_provider(name, model, previous, False)

    def _maybe_start_interactive_auth(
        self,
        provider_name: str,
        previous_provider: str,
        model: str,
        failure_message: str,
        already_attempted: bool,
    ) -> bool:
        """Start provider auth flow once, when supported."""
        if already_attempted or provider_name in ("", "auto"):
            return False
        if provider_name not in list_provider_ids():
            return False
        provider = get_provider(provider_name)
        if not provider.supports_interactive_auth():
            return False
        self.call_from_thread(
            self._log,
            f"[yellow]{provider_name} requires authentication.[/yellow]\n"
            f"  [dim]{failure_message}[/dim]\n"
            f"  [dim]Starting automatic {provider_name} authentication flow…[/dim]\n",
        )
        self.call_from_thread(
            self._start_provider_auth,
            provider_name,
            previous_provider,
            model,
            True,
        )
        return True

    @work(thread=True)
    def _wk_verify_provider(
        self,
        name: str,
        model: str = "",
        previous_provider: str = "auto",
        auto_auth_attempted: bool = False,
    ) -> None:
        """Background worker: transactional verify-and-switch."""
        try:
            candidate = ArchitectureAgent(provider=name)

            # Strict model validation before any switch/persist.
            if model and name != "auto":
                m_ok, m_msg = candidate.validate_model(name, model)
                if not m_ok:
                    self._provider = previous_provider
                    if self._maybe_start_interactive_auth(
                        name,
                        previous_provider,
                        model,
                        m_msg,
                        auto_auth_attempted,
                    ):
                        return
                    self.call_from_thread(
                        self._log_error,
                        f"Model validation failed for provider '{name}': {m_msg}\n"
                        f"[dim]Provider unchanged: {previous_provider}[/dim]",
                    )
                    return

            ok, msg = candidate.check_available()
            if not ok:
                self._provider = previous_provider
                if self._maybe_start_interactive_auth(
                    name,
                    previous_provider,
                    model,
                    msg,
                    auto_auth_attempted,
                ):
                    return
                self.call_from_thread(
                    self._log_error,
                    f"Provider switch blocked: [bold]{name}[/bold] is unavailable.\n"
                    f"{msg}\n[dim]Provider unchanged: {previous_provider}[/dim]",
                )
                return

            # Persist only after successful verification.
            updates: dict[str, str] = {"provider": name}
            if model and name != "auto":
                config_key = self._MODEL_CONFIG_KEY.get(name)
                if config_key:
                    updates[config_key] = model
            save_config(updates)
            self._provider = name

            icon = "✅"
            status = "[green]Available[/green]"
            model_note = f" · model [bold]{model}[/bold]" if model else ""
            self.call_from_thread(
                self._log,
                f"\n{icon}  Provider switched to [bold]{name}[/bold]{model_note} — {status}\n"
                f"   {msg}\n"
                "   [dim]Saved to ~/.strata/config.yaml[/dim]\n",
            )
            self.call_from_thread(self._open_main, f"🤖 AI Provider — {name}")
            model_line = f"  Model: [bold]{model}[/bold]\n\n" if model else ""
            self.call_from_thread(
                self._main,
                f"  {icon}  [bold]{name}[/bold] is now the active provider.\n"
                f"  {model_line}"
                f"  {msg}\n\n"
                f"  [dim]All subsequent AI calls (chat, /score, /improve, /improve-ai)\n"
                f"  will use this provider.[/dim]\n\n"
                f"  [dim]Use [cyan]/model overview[/cyan] to see all available providers,\n"
                f"  [cyan]/model {name} <model-id>[/cyan] to switch model, or\n"
                f"  [cyan]/model auto[/cyan] to let Strata choose automatically.[/dim]\n",
            )

            if name != "auto":
                try:
                    models = candidate.list_models(name)
                except Exception:
                    models = []
                if models:
                    ids = [m.get("id", "") for m in models if m.get("id")]
                    self.call_from_thread(self._update_provider_model_completions, name, ids)
                    self.call_from_thread(self._render_provider_model_table, name, models)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self._log_error, f"Provider check failed: {exc}"
            )

    @work(thread=True)
    def _start_provider_auth(
        self,
        provider_name: str,
        previous_provider: str = "auto",
        model: str = "",
        verify_after_auth: bool = True,
    ) -> None:
        """Run provider interactive auth flow and optionally retry provider verification."""
        provider_name = (provider_name or "").strip().lower()
        if provider_name not in list_provider_ids():
            self.call_from_thread(self._log_error, f"Unknown provider '{provider_name}'.")
            return

        provider = get_provider(provider_name)
        if not provider.supports_interactive_auth():
            self.call_from_thread(
                self._log_error,
                f"Provider '{provider_name}' does not support in-app auth.\n"
                f"[dim]{provider.auth_remediation()}[/dim]",
            )
            return

        from .agent import _load_config as _lc

        display = provider_name.capitalize()
        self.call_from_thread(
            self._log,
            f"\n[bold cyan]🔐 {display} Authentication[/bold cyan]\n"
            "[dim]Starting interactive OAuth flow…[/dim]\n",
        )
        try:
            ok, detail = provider.run_interactive_auth(
                _lc(),
                log_fn=lambda msg: self.call_from_thread(self._log, msg),
            )
            if not ok:
                self.call_from_thread(self._log_error, f"{display} auth failed: {detail}")
                return
            self.call_from_thread(
                self._log,
                f"[green]✅ {display} authenticated.[/green]  "
                "[dim]Validating provider activation now…[/dim]\n",
            )
            if verify_after_auth:
                self.call_from_thread(
                    self._wk_verify_provider,
                    provider_name,
                    model,
                    previous_provider,
                    True,
                )
        except AgentError as exc:
            self.call_from_thread(self._log_error, f"{display} auth failed: {exc}")
        except Exception as exc:
            self.call_from_thread(self._log_error, f"{display} auth error: {exc}")

    def _start_copilot_auth(self) -> None:
        """Backward-compatible alias for the old /copilot-auth command."""
        self._start_provider_auth("copilot", self._provider)

    # ── /improve-ai guided workflow ────────────────────────────────────────────

    def _start_improve_ai_workflow(self, profile: str = "default") -> None:
        """Open latest scheduled hybrid domain advisory output."""
        if not self._workspace:
            self._log_error("No workspace loaded.")
            return
        latest = load_latest_advisory()
        if not latest:
            self._wf_active = False
            self._wf_todo = []
            self._open_main("🧠 Hybrid Domain Advisory")
            self._main(
                "[dim]No advisory results found yet in [cyan]architecture/advice/latest.yaml[/cyan].[/dim]\n"
                "  [dim]The advisor runs on schedule only. Use [cyan]/advisor status[/cyan] and "
                "[cyan]/advisor interval <min>[/cyan] to configure it.[/dim]\n"
            )
            return

        meta = latest.get("meta", {})
        self._wf_profile = str(meta.get("profile", profile))
        self._wf_todo = advisory_to_todo_items(latest)
        self._wf_active = True
        self._render_advisory_overview(latest, self._wf_todo)

    def _render_advisory_overview(self, advisory: dict[str, Any], todo_items: list[TodoItem]) -> None:
        """Render latest advisory synthesis, domain insights, and document actions."""
        meta = advisory.get("meta", {})
        panel = advisory.get("panel", {})
        synthesis = panel.get("synthesis", {})
        domain_insights = panel.get("domains") or panel.get("subagents") or []

        self._open_main("🧠 Hybrid Domain Advisory Overview")
        self._main(
            f"[bold]{meta.get('workspace_name', 'workspace')}[/bold]\n"
            f"[dim]Run:[/dim] {meta.get('run_id', 'n/a')}  "
            f"[dim]Generated:[/dim] {meta.get('generated_at', 'n/a')}\n"
            f"[dim]Profile:[/dim] {meta.get('profile', 'n/a')}  "
            f"[dim]Provider:[/dim] {meta.get('provider', 'n/a')} / {meta.get('model', 'n/a')}  "
            f"[dim]Degraded:[/dim] {'yes' if meta.get('degraded') else 'no'}\n"
        )

        summary = str(synthesis.get("domain_summary", "")).strip()
        if summary:
            self._main(f"\n[bold]Synthesis[/bold]\n  {summary}\n")

        self._main("\n[bold]Domain Attention[/bold]\n")
        domain_scores = synthesis.get("domain_scores", [])
        if not domain_scores:
            self._main("  [dim]No domain scoring payload available.[/dim]\n")
        else:
            for ds in domain_scores:
                score = float(ds.get("weighted_score", 0.0))
                attn = str(ds.get("attention_level", "n/a"))
                confidence = float(ds.get("confidence", 0.0))
                attn_color = (
                    "red" if attn == "critical" else
                    "yellow" if attn == "high" else
                    "cyan" if attn == "medium" else
                    "green"
                )
                self._main(
                    f"  - [cyan]{ds.get('domain', 'domain')}[/cyan] "
                    f"score={score:.2f}/5  "
                    f"attention=[{attn_color}]{attn}[/{attn_color}]  "
                    f"confidence={confidence:.2f}\n"
                )

        decisions = synthesis.get("decisions_needed", [])
        self._main("\n[bold]Decision Backlog[/bold]\n")
        if decisions:
            for d in decisions[:12]:
                self._main(
                    f"  - [{d.get('priority', 'medium')}] "
                    f"({d.get('domain', 'n/a')}) {d.get('decision', '')} "
                    f"[dim]score={d.get('priority_score', 0)}[/dim]\n"
                )
        else:
            self._main("  [dim]No open decisions captured.[/dim]\n")

        self._main("\n[bold]Domain Insights[/bold]\n")
        if domain_insights:
            for sub in domain_insights:
                self._main(
                    f"  [cyan]{sub.get('role_name', 'Subagent')}[/cyan]: "
                    f"{sub.get('domain_summary', '')}\n"
                )
        else:
            self._main("  [dim]No domain insights available.[/dim]\n")

        benefits = synthesis.get("organization_benefits", [])
        self._main("\n[bold]Organization Benefits[/bold]\n")
        if benefits:
            for benefit in benefits:
                self._main(
                    f"  - [cyan]{benefit.get('category', 'benefit')}[/cyan] "
                    f"{benefit.get('score', 0)}  "
                    f"[dim]{benefit.get('summary', '')}[/dim]\n"
                )
        else:
            self._main("  [dim]No benefit summary available.[/dim]\n")

        self._main("\n[bold]Recommended Documents[/bold]\n")
        if not todo_items:
            self._main("  [dim]No document actions available.[/dim]\n")
        else:
            recommended_docs = synthesis.get("recommended_docs", [])
            docs_by_domain: dict[str, list[dict[str, Any]]] = {}
            for doc in recommended_docs:
                if not isinstance(doc, dict):
                    continue
                domain = str(doc.get("domain") or "unassigned")
                docs_by_domain.setdefault(domain, []).append(doc)

            index_lookup = {item.subject: idx for idx, item in enumerate(todo_items, start=1)}
            used_subjects: set[str] = set()
            for domain, docs in sorted(docs_by_domain.items()):
                self._main(f"  [cyan]{domain}[/cyan]\n")
                for doc in sorted(docs, key=lambda d: -float(d.get("priority_score", 0.0))):
                    subject = str(doc.get("subject", "")).strip()
                    idx = index_lookup.get(subject)
                    if idx is None:
                        continue
                    item = todo_items[idx - 1]
                    used_subjects.add(item.subject)
                    pri = "🔴" if item.priority == 1 else "🟡" if item.priority == 2 else "🟢"
                    impact = str(doc.get("expected_benefit") or "")
                    self._main(
                        f"    {pri} [dim]{idx:2d}.[/dim] "
                        f"[bold]{item.doc_type}[/bold] {item.subject} "
                        f"[dim]impact={impact} score={doc.get('priority_score', 0)}[/dim]\n"
                        f"        [dim]{item.action}[/dim]\n"
                    )
            orphan_items = [i for i in todo_items if i.subject not in used_subjects]
            if orphan_items:
                for item in orphan_items:
                    pri = "🔴" if item.priority == 1 else "🟡" if item.priority == 2 else "🟢"
                    self._main(
                        f"  {pri} [bold]{item.doc_type}[/bold] {item.subject}\n"
                        f"      [dim]{item.action}[/dim]\n"
                    )

        if not synthesis.get("recommended_docs"):
            for idx, item in enumerate(todo_items, start=1):
                pri = "🔴" if item.priority == 1 else "🟡" if item.priority == 2 else "🟢"
                state = "✅ saved" if item.saved_path else ("📄 drafted" if item.draft_content else "")
                state_suffix = f"  [dim]{state}[/dim]" if state else ""
                self._main(
                    f"  {pri} [dim]{idx:2d}.[/dim] "
                    f"[bold]{item.doc_type}[/bold] {item.subject} "
                    f"[dim]{item.score_impact}[/dim]{state_suffix}\n"
                    f"      [dim]{item.action}[/dim]\n"
                )

        self._main(
            "\n[dim]Actions: [cyan]draft <n>[/cyan] generate doc · "
            "[cyan]save <n>[/cyan] write to file · [cyan]quit[/cyan] exit workflow[/dim]\n"
        )

    def _exit_wf(self) -> None:
        """Exit the workflow and restore the roadmap view."""
        self._wf_active = False
        self._wf_todo = []
        self._log("[dim]Workflow exited.[/dim]")
        self._show_improve_roadmap()

    def _render_todo_list(self, items: list[TodoItem]) -> None:
        """Render the flat to-do list in the main pane."""
        from datetime import date as _date
        ws_name = self._workspace.manifest.name if self._workspace else "workspace"
        today = _date.today().strftime("%Y-%m-%d")

        self._open_main(f"📋  Architecture To-Do List   {ws_name}   {today}")

        tc_map = {
            "ADR": "cyan", "HLD": "blue", "Capability": "green",
            "Standard": "magenta", "Data Product": "yellow", "Approval": "red",
        }

        add_items     = [item for item in items if item.category == "add"]
        improve_items = [item for item in items if item.category == "improve"]

        # Build global numbering: add items first, then improve items
        numbered: list[tuple[int, TodoItem]] = []
        for item in add_items:
            numbered.append((len(numbered) + 1, item))
        for item in improve_items:
            numbered.append((len(numbered) + 1, item))

        def _fmt_item(n: int, item: TodoItem) -> None:
            tc = tc_map.get(item.doc_type, "white")
            pri_tag = (
                "[red]●[/red]" if item.priority == 1
                else "[yellow]●[/yellow]" if item.priority == 2
                else "[green]●[/green]"
            )
            if item.saved_path:
                status = "  [green][dim]✅ saved[/dim][/green]"
            elif item.draft_content:
                status = f"  [yellow][dim]📄 drafted — [cyan]save {n}[/cyan][/dim][/yellow]"
            else:
                status = f"  [dim][cyan]draft {n}[/cyan] to generate[/dim]"
            self._main(
                f"  {pri_tag} [dim]{n:2d}.[/dim]  "
                f"[{tc}]{item.doc_type:<14}[/{tc}]"
                f"[bold]{item.subject[:50]}[/bold]"
                f"  [dim]{item.score_impact}[/dim]{status}"
            )
            self._main(f"           [dim]{item.action}[/dim]")

        if add_items:
            self._main(
                f"\n  [bold]Add new documents[/bold]"
                f"  [dim]({len(add_items)} item{'s' if len(add_items) != 1 else ''})[/dim]\n"
                "  " + "─" * 74 + "\n"
            )
            for n, item in numbered[:len(add_items)]:
                _fmt_item(n, item)
                self._main("")

        if improve_items:
            self._main(
                f"\n  [bold]Improve existing documents[/bold]"
                f"  [dim]({len(improve_items)} item{'s' if len(improve_items) != 1 else ''})[/dim]\n"
                "  " + "─" * 74 + "\n"
            )
            for n, item in numbered[len(add_items):]:
                _fmt_item(n, item)
                self._main("")

        if not items:
            self._main("  [green]✅  No critical gaps detected — architecture is near-optimal![/green]\n")

        self._main(
            "\n  [dim]→ [cyan]/score[/cyan] full breakdown  ·  "
            "[cyan]/improve[/cyan] roadmap  ·  "
            "[cyan]draft <n>[/cyan] generate doc  ·  "
            "[cyan]save <n>[/cyan] write to file  ·  "
            "[cyan]quit[/cyan] exit[/dim]\n"
        )

    @work(thread=True)
    def _ai_build_todo_list(
        self,
        result: "ScoreResult",
        ws: "ArchitectureWorkspace",
        deterministic_items: "list[TodoItem]",
        existing_docs: "list[str]",
        next_adr_num: int,
    ) -> None:
        """AI worker: single call to build a specific to-do list from workspace data."""
        import json as _json
        try:
            agent = ArchitectureAgent(provider=self._provider)
            available, msg = agent.check_available()
            if not available:
                self._wf_todo = deterministic_items
                self.call_from_thread(self._render_todo_list, deterministic_items)
                self.call_from_thread(
                    self._log,
                    f"[yellow]AI not available:[/yellow] {msg}\n"
                    "  [dim]Showing deterministic analysis.[/dim]\n"
                )
                return

            caps = [
                {"id": c.id, "name": c.name, "domain": c.domain,
                 "owner": c.owner or "", "maturity": c.maturity or ""}
                for c in ws.enterprise.capabilities
            ]
            apps = [
                {"id": a.id, "name": a.name, "status": a.status,
                 "owner": a.owner_team or "", "hosting": a.hosting,
                 "capability_ids": a.capability_ids}
                for a in ws.enterprise.applications
            ]
            standards = [
                {"id": s.id, "name": s.name, "category": s.category, "status": s.status}
                for s in ws.enterprise.standards
            ]
            solutions = [
                {"id": s.id, "name": s.name, "status": s.status,
                 "pattern": s.pattern,
                 "adrs": [r.id for r in s.adrs] if s.adrs else [],
                 "components_count": len(s.components)}
                for s in ws.solutions
            ]
            domains = [
                {"id": d.id, "name": d.name, "owner": d.owner_team or ""}
                for d in ws.data.domains
            ]
            products = [
                {"id": p.id, "name": p.name, "domain_id": p.domain_id,
                 "owner": p.owner_team or "", "sla_tier": p.sla_tier}
                for p in ws.data.products
            ]

            scores_block = "\n".join(
                f"  - {d.label}: {d.score:.1f}/5.0  findings: {'; '.join(d.findings) or 'none'}"
                for d in result.dimensions
            )

            context = {
                "workspace": ws.manifest.name,
                "profile": result.profile_name,
                "capabilities": caps,
                "applications": apps,
                "standards": standards,
                "solutions": solutions,
                "data_domains": domains,
                "data_products": products,
                "existing_documents": existing_docs,
                "next_adr_number": next_adr_num,
                "dimension_scores": [
                    {"key": d.key, "label": d.label, "score": d.score, "findings": d.findings}
                    for d in result.dimensions
                ],
            }

            prompt = (
                "You are a senior enterprise architect reviewing an architecture workspace.\n\n"
                f"Workspace data:\n```json\n{_json.dumps(context, indent=2)}\n```\n\n"
                "Dimension scores and findings:\n"
                f"{scores_block}\n\n"
                "Task: Produce a prioritised architecture to-do list.\n"
                "Rules:\n"
                "  1. Each item must be SPECIFIC — name actual IDs, filenames, capability names, "
                "solution names from the workspace data.\n"
                "  2. Cross-reference existing_documents with solutions/capabilities to identify "
                "documents that exist but may be incomplete (e.g. solutions in 'draft' status with "
                "no matching ADR, capabilities with no owner).\n"
                "  3. category='add' for missing new documents; category='improve' for existing "
                "items that need completion, approval, or correction.\n"
                "  4. Maximum 15 items total. Prioritise by impact on the lowest-scoring dimensions.\n"
                "  5. For new ADR filenames use next_adr_number to suggest sequential names.\n\n"
                "Return ONLY a JSON array (no wrapper object). Each element must have:\n"
                "  priority    : 1 (high), 2 (medium), or 3 (low)\n"
                "  category    : 'add' or 'improve'\n"
                "  doc_type    : one of: ADR, HLD, Capability, Standard, Data Product, Approval\n"
                "  subject     : specific name (e.g. 'ADR-003-kafka-event-bus.md' or 'Resource Management')\n"
                "  action      : imperative sentence — what exactly needs to be done\n"
                "  dimension   : one of: capability_coverage, application_health, data_maturity, "
                "solution_completeness, operational_readiness, governance_coverage\n"
                "  score_impact: e.g. '+0.5 Solution Completeness'\n\n"
                "No text outside the JSON array."
            )

            raw = agent.ask(prompt)

            try:
                items_raw = _json.loads(raw)
                if not isinstance(items_raw, list):
                    raise ValueError("not a list")
                todo_items: list[TodoItem] = [
                    TodoItem(
                        priority=int(i.get("priority", 2)),
                        category=i.get("category", "add") if i.get("category") in ("add", "improve") else "add",
                        doc_type=i.get("doc_type", "ADR"),
                        subject=i.get("subject", ""),
                        action=i.get("action", ""),
                        dimension=i.get("dimension", ""),
                        score_impact=i.get("score_impact", ""),
                    )
                    for i in items_raw
                    if isinstance(i, dict) and i.get("subject") and i.get("action")
                ]
                if not todo_items:
                    raise ValueError("empty list")
            except Exception:
                todo_items = deterministic_items

            # Bump ADR counter from any AI-suggested ADR filenames
            for item in todo_items:
                m = _re.match(r"ADR-(\d+)", item.subject, _re.IGNORECASE)
                if m:
                    self._wf_next_adr_num = max(self._wf_next_adr_num, int(m.group(1)) + 1)

            self._wf_todo = todo_items
            self.call_from_thread(self._render_todo_list, todo_items)

        except Exception as exc:
            self._wf_todo = deterministic_items
            self.call_from_thread(self._render_todo_list, deterministic_items)
            self.call_from_thread(
                self._log,
                f"[yellow]AI analysis failed:[/yellow] {exc}\n"
                "  [dim]Showing deterministic analysis.[/dim]\n"
            )

    @work(thread=True)
    def _ai_draft_todo_item(self, todo_idx: int) -> None:
        """AI worker: generate full markdown content for todo item todo_idx."""
        import json as _json
        import glob as _glob
        import os as _os
        try:
            if todo_idx < 0 or todo_idx >= len(self._wf_todo):
                return
            item = self._wf_todo[todo_idx]
            agent = ArchitectureAgent(provider=self._provider)
            available, msg = agent.check_available()
            if not available:
                self.call_from_thread(self._log_error, f"AI not available: {msg}")
                return

            ws = self._workspace

            # Find a format template from the watch folders
            template_content = ""
            template_name = ""
            if ws and ws.manifest.watch_folders:
                prefix = "ADR-" if item.doc_type == "ADR" else "hld-"
                for folder in ws.manifest.watch_folders:
                    if not _os.path.isdir(folder):
                        continue
                    matches = sorted(_glob.glob(_os.path.join(folder, f"{prefix}*.md")))
                    if matches:
                        with open(matches[-1], encoding="utf-8") as fh:
                            template_content = fh.read()
                        template_name = _os.path.basename(matches[-1])
                        break

            template_block = (
                f"Use the following existing document as a format/structure template "
                f"(mirror its structure but fill with NEW content):\n"
                f"--- TEMPLATE: {template_name} ---\n{template_content[:3000]}\n"
                f"--- END TEMPLATE ---\n\n"
                if template_content else ""
            )

            prompt = (
                f"You are a senior enterprise architect. "
                f"Write a complete, production-ready {item.doc_type} document in Markdown.\n\n"
                f"{template_block}"
                f"Document subject: **{item.subject}**\n"
                f"Purpose: {item.action}\n"
                f"Dimension: {item.dimension}\n"
                f"Score impact: {item.score_impact}\n\n"
                "Write the full document in Markdown. "
                "Start directly with the document content (no preamble)."
            )

            draft = agent.ask(prompt)
            item.draft_content = draft

            def _show_draft() -> None:
                self._log(
                    f"[green]📄 Draft ready:[/green] [bold]{item.subject}[/bold]  "
                    f"[dim]→ type [cyan]save {todo_idx + 1}[/cyan] to write to file[/dim]\n"
                )
                self._open_main(f"📄 Draft — {item.subject}")
                preview = draft[:4000]
                if len(draft) > 4000:
                    preview += "\n\n[dim]… (truncated — full content saved in memory)[/dim]"
                self._main(preview)
                self._main(
                    f"\n\n  [dim]Type [cyan]save {todo_idx + 1}[/cyan] to write to the "
                    f"watch folder, or [cyan]quit[/cyan] to exit.[/dim]\n"
                )

            self.call_from_thread(_show_draft)

        except Exception as exc:
            self.call_from_thread(self._log_error, f"Draft generation failed: {exc}")

    def _save_todo_item(self, todo_idx: int) -> None:
        """Write a drafted todo item to the appropriate watch folder."""
        import os as _os
        try:
            if todo_idx < 0 or todo_idx >= len(self._wf_todo):
                return
            item = self._wf_todo[todo_idx]
            if not item.draft_content:
                self._log(f"[dim]No draft for #{todo_idx + 1}.[/dim]")
                return
            ws = self._workspace
            if not ws or not ws.manifest.watch_folders:
                self._log_error("No watch folders configured — cannot save.")
                return

            # Choose sub-folder based on doc_type
            subdir_map = {"ADR": "adrs", "HLD": "hld"}
            preferred_subdir = subdir_map.get(item.doc_type, "")

            target_folder = None
            for folder in ws.manifest.watch_folders:
                if not _os.path.isdir(folder):
                    continue
                if preferred_subdir:
                    candidate = _os.path.join(folder, preferred_subdir)
                    if _os.path.isdir(candidate):
                        target_folder = candidate
                        break
                target_folder = folder
                break

            if not target_folder:
                target_folder = ws.manifest.watch_folders[0]
                _os.makedirs(target_folder, exist_ok=True)

            # Build a safe filename from subject
            fname = item.subject if item.subject.endswith(".md") else f"{item.subject}.md"
            fname = _re.sub(r"[^\w\-.]", "-", fname).lower()
            fpath = _os.path.join(target_folder, fname)

            # Avoid overwriting — append -v2, -v3 etc.
            if _os.path.exists(fpath):
                stem = fname[:-3]
                i = 2
                while _os.path.exists(_os.path.join(target_folder, f"{stem}-v{i}.md")):
                    i += 1
                fpath = _os.path.join(target_folder, f"{stem}-v{i}.md")

            with open(fpath, "w", encoding="utf-8") as fh:
                fh.write(item.draft_content)
            item.saved_path = fpath

            self._log(
                f"[green]✅ Saved:[/green] [bold]{_os.path.basename(fpath)}[/bold]  "
                f"[dim]{fpath}[/dim]\n"
            )
            self._render_todo_list(self._wf_todo)

        except Exception as exc:
            self._log_error(f"Failed to save {self._wf_todo[todo_idx].subject}: {exc}")

    # ── Key bindings ───────────────────────────────────────────────────────────

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar")
        sidebar.display = not sidebar.display

    def action_nav_enterprise(self) -> None:
        self._navigate("capabilities")

    def action_nav_data(self) -> None:
        self._navigate("domains")

    def action_nav_solutions(self) -> None:
        self._navigate("solutions")

    def action_nav_staging(self) -> None:
        self._navigate("staging")

    def action_escape_action(self) -> None:
        if self._pending:
            self._reject_pending()
        else:
            self.query_one("#user-input", CommandInput).focus()

    def action_show_help(self) -> None:
        self._show_help()


# ── Entry point ────────────────────────────────────────────────────────────────

def launch_tui(provider: str = "auto") -> None:
    """Start the full-screen TUI."""
    StrataApp(provider=provider).run()
