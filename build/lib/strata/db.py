"""Optional PostgreSQL + pgvector persistence layer for Strata.

Gracefully degrades when psycopg2 is not installed or the database
is not reachable — all public functions return empty results / 0.

Schema
------
entities      — all architecture artefacts mirrored from YAML (queryable, vector-searchable)
staging       — staging queue persisted in Postgres (YAML is the fallback/export)
folder_events — filesystem event log written by the live watcher

Usage
-----
    from strata import db

    if db.probe():                     # returns False if no DB
        db.init_schema()
        db.sync_workspace(ws)

    db.log_event("/docs/arch.md", "modified")   # no-op if unavailable
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Generator, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ArchitectureWorkspace, StagedItem

log = logging.getLogger(__name__)

# ── Optional dependency ────────────────────────────────────────────────────────

_AVAILABLE = False
try:
    import psycopg2
    import psycopg2.extras
    from psycopg2.extras import Json, execute_values  # type: ignore[assignment]
    _AVAILABLE = True
except ImportError:
    pass

DEFAULT_URL = "postgresql://strata:strata@localhost:5432/strata"

# ── Schema ─────────────────────────────────────────────────────────────────────

_SCHEMA = """\
CREATE EXTENSION IF NOT EXISTS vector;

-- All architecture entities (capabilities, apps, standards, domains, …)
-- `embedding` column is filled lazily when semantic search is requested.
CREATE TABLE IF NOT EXISTS entities (
    id          TEXT        NOT NULL,
    type        TEXT        NOT NULL,          -- capability | application | standard | …
    workspace   TEXT        NOT NULL DEFAULT 'default',
    data        JSONB       NOT NULL,
    embedding   vector(1536),                  -- OpenAI / compatible embedding
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (id, type, workspace)
);

-- Staging queue — primary store; YAML is a fallback export
CREATE TABLE IF NOT EXISTS staging (
    id          TEXT        PRIMARY KEY,
    entity      TEXT        NOT NULL,
    fields      JSONB       NOT NULL,
    source      TEXT        DEFAULT '',
    status      TEXT        DEFAULT 'pending', -- pending | accepted | rejected
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Filesystem events from the live watcher
CREATE TABLE IF NOT EXISTS folder_events (
    id          SERIAL      PRIMARY KEY,
    path        TEXT        NOT NULL,
    event_type  TEXT        NOT NULL,          -- created | modified | moved | deleted
    processed   BOOLEAN     DEFAULT false,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_entities_type      ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_workspace ON entities(workspace);
CREATE INDEX IF NOT EXISTS idx_staging_status     ON staging(status);
CREATE INDEX IF NOT EXISTS idx_fevents_processed  ON folder_events(processed);
"""

# ── Internal helpers ───────────────────────────────────────────────────────────


@contextmanager
def _open(url: str) -> Generator:
    """Yield a psycopg2 connection; commit on success, rollback on error."""
    conn = psycopg2.connect(url, connect_timeout=3)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Public API ─────────────────────────────────────────────────────────────────


def is_available() -> bool:
    """Return True if psycopg2 is installed (DB may still be unreachable)."""
    return _AVAILABLE


def probe(url: str = DEFAULT_URL) -> bool:
    """Return True if the database is reachable right now."""
    if not _AVAILABLE:
        return False
    try:
        with _open(url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


def init_schema(url: str = DEFAULT_URL) -> None:
    """Create all tables and extensions (idempotent — safe to call repeatedly)."""
    with _open(url) as conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)


def sync_workspace(ws: ArchitectureWorkspace, url: str = DEFAULT_URL) -> int:
    """Upsert every entity in *ws* into Postgres. Returns row count written."""
    if not _AVAILABLE:
        return 0

    workspace_name = ws.manifest.name
    rows: list[tuple[str, str, str, Any]] = []

    for cap in ws.enterprise.capabilities:
        rows.append((cap.id, "capability",  workspace_name, Json(cap.model_dump())))
    for app in ws.enterprise.applications:
        rows.append((app.id, "application", workspace_name, Json(app.model_dump())))
    for std in ws.enterprise.standards:
        rows.append((std.id, "standard",    workspace_name, Json(std.model_dump())))
    for dom in ws.data.domains:
        rows.append((dom.id, "domain",      workspace_name, Json(dom.model_dump())))
    for prod in ws.data.products:
        rows.append((prod.id, "product",    workspace_name, Json(prod.model_dump())))
    for flow in ws.data.flows:
        rows.append((flow.id, "flow",       workspace_name, Json(flow.model_dump())))
    for sol in ws.solutions:
        rows.append((sol.id, "solution",    workspace_name, Json(sol.model_dump())))

    if not rows:
        return 0

    with _open(url) as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO entities (id, type, workspace, data)
                VALUES %s
                ON CONFLICT (id, type, workspace)
                DO UPDATE SET data = EXCLUDED.data, updated_at = now()
                """,
                rows,
            )
    return len(rows)


def sync_staging(items: list[StagedItem], url: str = DEFAULT_URL) -> int:
    """Upsert staging items into Postgres. Returns count."""
    if not _AVAILABLE or not items:
        return 0

    with _open(url) as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO staging (id, entity, fields, source, status, notes)
                VALUES %s
                ON CONFLICT (id) DO UPDATE
                  SET entity     = EXCLUDED.entity,
                      fields     = EXCLUDED.fields,
                      source     = EXCLUDED.source,
                      status     = EXCLUDED.status,
                      notes      = EXCLUDED.notes,
                      updated_at = now()
                """,
                [
                    (i.id, i.entity, Json(i.fields), i.source, i.status, i.notes)
                    for i in items
                ],
            )
    return len(items)


def log_event(path: str, event_type: str, url: str = DEFAULT_URL) -> None:
    """Record a folder watch event (best-effort — never raises)."""
    if not _AVAILABLE:
        return
    try:
        with _open(url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO folder_events (path, event_type) VALUES (%s, %s)",
                    (path, event_type),
                )
    except Exception as exc:
        log.debug("folder event log failed: %s", exc)


def stats(url: str = DEFAULT_URL) -> dict[str, int]:
    """Return {table: row_count} for a quick health snapshot."""
    if not _AVAILABLE:
        return {}
    try:
        with _open(url) as conn:
            with conn.cursor() as cur:
                result: dict[str, int] = {}
                for table in ("entities", "staging", "folder_events"):
                    cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
                    row = cur.fetchone()
                    result[table] = row[0] if row else 0
                return result
    except Exception:
        return {}


def search_similar(
    embedding: list[float],
    entity_type: str | None = None,
    limit: int = 10,
    url: str = DEFAULT_URL,
) -> list[dict[str, Any]]:
    """Vector similarity search over entities.

    Returns list of ``{id, type, data, distance}`` ordered nearest-first.
    Requires psycopg2 + pgvector + a running database with populated embeddings.
    """
    if not _AVAILABLE:
        return []

    vec_literal = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
    where_clause = "WHERE type = %s " if entity_type else ""
    params: list[Any] = ([entity_type] if entity_type else []) + [vec_literal, limit]

    try:
        with _open(url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT id, type, data,
                           embedding <-> %s::vector AS distance
                    FROM   entities
                    {where_clause}
                    ORDER  BY distance
                    LIMIT  %s
                    """,
                    ([entity_type, vec_literal, limit] if entity_type else [vec_literal, limit]),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        log.debug("vector search failed: %s", exc)
        return []
