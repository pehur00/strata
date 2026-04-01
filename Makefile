# Use the pipx that owns ~/.local/bin — this is the standard system pipx location.
# ~/Library/Python/3.13/bin/pipx uses a different PIPX_HOME and does NOT write to
# ~/.local/bin, so we must not use it for global installs.
PIPX   := $(shell \
  if [ -x "$$HOME/.local/bin/pipx" ]; then echo "$$HOME/.local/bin/pipx"; \
  elif [ -x "$$HOME/Library/Python/3.13/bin/pipx" ]; then echo "$$HOME/Library/Python/3.13/bin/pipx"; \
  else echo "pipx"; fi)
PYTHON := $(shell command -v python3 2>/dev/null || echo python)
PKG_DIR := $(shell pwd)

.PHONY: install update uninstall reinstall dev test clean \
        install-full full db-start db-stop db-reset db-shell db-logs

## ── Global install (like brew install / git) ─────────────────────────────────
## ⚠  Always use 'make install' — NOT 'pipx install strata-cli'.
## ⚠  There is an unrelated 'strata-cli' on PyPI with the same package name.
install:
	@echo "→ Installing strata globally…"
	@echo "   (using local source at $(PKG_DIR) — not PyPI)"
	@# Force-replace any existing strata-cli (including the unrelated PyPI package)
	$(PIPX) install --force "$(PKG_DIR)"
	@echo "✅  strata is now available globally.  Try: strata --help"

## ── Update after code changes ────────────────────────────────────────────────
update:
	@echo "→ Updating strata global install…"
	$(PIPX) install --force "$(PKG_DIR)"
	@echo "✅  strata updated."

## ── Uninstall ─────────────────────────────────────────────────────────────────
uninstall:
	@echo "→ Removing strata global install…"
	$(PIPX) uninstall strata-cli
	@echo "✅  strata removed."

## ── Reinstall from scratch ───────────────────────────────────────────────────
reinstall: uninstall install

## ── Dev install (editable, in local venv) ────────────────────────────────────
dev:
	@echo "→ Setting up dev environment…"
	$(PYTHON) -m venv .venv
	.venv/bin/pip install -e ".[dev]"
	@echo "✅  Dev env ready. Activate with: source .venv/bin/activate"

## ── Tests ────────────────────────────────────────────────────────────────────
test:
	.venv/bin/python -m pytest tests/ -q

## ── Clean build artefacts ────────────────────────────────────────────────────
clean:
	rm -rf dist/ build/ src/*.egg-info src/**/__pycache__ __pycache__

## ── Build distributable wheel ────────────────────────────────────────────────
build: clean
	$(PYTHON) -m pip install --quiet build
	$(PYTHON) -m build
	@echo "✅  Wheel in dist/"

## ── Install with all optional features ─────────────────────────────────────
## ⚠  Always use 'make install-full' — NOT 'pipx install strata-cli[full]'.
## ⚠  There is an unrelated 'strata-cli' package on PyPI; the bare pipx
## ⚠  command will pull that instead of this local package.
install-full:
	@echo "→ Installing strata globally with all optional features…"
	@echo "   (using local source at $(PKG_DIR) — not PyPI)"
	$(PIPX) install --force "$(PKG_DIR)[full]"
	@echo "✅  strata (full) installed: postgres + pgvector + live watching."

# Alias to support `make install full` usage.
full: install-full

## ── Database (PostgreSQL + pgvector via Docker Compose) ─────────────────────
db-start:
	@echo "→ Starting Strata database…"
	docker compose up -d
	@echo "   Waiting for postgres to be ready…"
	@sleep 2
	@docker compose exec db pg_isready -U strata -d strata 2>/dev/null && echo "✅  Database ready at localhost:5432" || echo "⏳  Still starting — try again in a few seconds."

db-stop:
	@echo "→ Stopping Strata database…"
	docker compose down
	@echo "✅  Database stopped."

db-reset:
	@echo "→ Wiping and restarting database (all data will be lost)…"
	docker compose down -v
	docker compose up -d
	@echo "✅  Fresh database started."

db-shell:
	docker compose exec db psql -U strata -d strata

db-logs:
	docker compose logs -f db

## ── Help ─────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  make install       Global install via pipx  (like brew install)"
	@echo "  make install-full  Global install with postgres + live watching"
	@echo ""
	@echo "  ⚠  NEVER use 'pipx install strata-cli' directly — a different"
	@echo "     unrelated package on PyPI has the same name."
	@echo "  make update        Re-install after code changes"
	@echo "  make uninstall     Remove global install"
	@echo "  make reinstall     Uninstall + install"
	@echo "  make dev           Set up local dev venv"
	@echo "  make test          Run test suite"
	@echo "  make build         Build distributable wheel"
	@echo "  make clean         Remove build artefacts"
	@echo ""
	@echo "  make db-start      Start local PostgreSQL + pgvector (Docker)"
	@echo "  make db-stop       Stop database container"
	@echo "  make db-reset      Wipe and restart database"
	@echo "  make db-shell      Open psql shell"
	@echo "  make db-logs       Tail database logs"
	@echo ""
