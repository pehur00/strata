# Contributing to Strata

Thank you for considering a contribution. This document covers the essentials.

## Getting started

```bash
git clone https://github.com/pehur00/strata.git
cd strata
make dev          # creates .venv and installs in editable mode
source .venv/bin/activate
make test         # run the test suite
```

## Workflow

1. Fork the repo and create a branch from `main`.
2. Make your changes, add tests where relevant.
3. Ensure `make test` passes with no failures.
4. Open a pull request against `main` with a clear description of the change.

## Code style

- Python 3.11+, type hints throughout.
- Format with `black`, lint with `ruff` (both are checked in CI).
- Keep new modules focused — avoid growing `cli.py` or `tui.py` further; prefer adding a service module and calling it from the front end.

## Reporting bugs

Open an issue at <https://github.com/pehur00/strata/issues>. Include:
- `strata --version` output
- OS and Python version
- Steps to reproduce
- Expected vs actual behaviour

## Feature requests

Open an issue labelled `enhancement`. Check existing issues first to avoid duplicates.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating you agree to abide by its terms.

## Security

See [SECURITY.md](SECURITY.md) for how to report vulnerabilities privately.
