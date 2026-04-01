# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **workspace.py**: All file writes are now atomic (write-to-temp + `os.replace`) to prevent partial-write corruption on crash or concurrent access.
- **workspace.py**: `save_workspace` now removes stale `architecture/solutions/*.yaml` files when a solution is deleted or renamed.
- **workspace.py**: An exclusive advisory lock (`fcntl.flock`) is held for the duration of every `save_workspace` call to prevent concurrent-write corruption.
- **tracker.py**: `file_index.yaml` is now written inside `architecture/` (not the workspace root) as documented in the module docstring.
- **tracker.py**: Index writes use the same atomic helper as `workspace.py`.

### Changed
- Package renamed from `strata-cli` to `strata-arch` to avoid the pre-existing unrelated PyPI package.
- `pyproject.toml`: Added `[project.urls]` for Homepage, Repository, Bug Tracker, and Changelog.
- `.gitignore`: Added `build/`, `dist/`, and `.strata.lock`.
- `build/` directory removed from version control.

### Added
- `LICENSE` (MIT)
- `CONTRIBUTING.md`
- `CODE_OF_CONDUCT.md`
- `SECURITY.md`
- `CHANGELOG.md`
- GitHub Actions CI workflow (`.github/workflows/ci.yml`)
- GitHub issue and PR templates

## [0.2.0] — Initial public release
