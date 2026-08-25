"""Storage: one SQLite file, stdlib sqlite3, no ORM.

Why no ORM: the whole point of bevis is that a reader can audit the rules in an
afternoon. Five CREATE TABLE statements you can read is a feature. The file is
also the interchange format — `sqlite3 .bevis/bevis.db` is a supported way to
inspect a board, and every gate lives in Python, not in a trigger, so nothing
about the schema is load-bearing magic.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

from .errors import NotFound, UsageError

DEFAULT_DIR = ".bevis"
DEFAULT_FILE = "bevis.db"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
  id            INTEGER PRIMARY KEY,
  parent_id     INTEGER REFERENCES job(id),
  title         TEXT    NOT NULL,
  description   TEXT    NOT NULL DEFAULT '',
  status        TEXT    NOT NULL DEFAULT 'open',
  acceptance    TEXT    NOT NULL,            -- the bar, in prose. Required.
  assignee      TEXT    NOT NULL DEFAULT '',
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL,
  closed_at     TEXT,
  -- The evidence. All three or the job is not closed.
  verify_cmd    TEXT,
  verify_exit   INTEGER,
  verify_output TEXT,
  -- Provenance of the state changes that matter.
  claimed_by     TEXT,
  claimed_at     TEXT,
  closed_by      TEXT,
  verified_by    TEXT,
  verified_at    TEXT,
  blocked_reason TEXT
);

-- Explicit ordering edges: job_id may not start until blocker_id is closed.
-- Separate from parent/child, which is decomposition, not sequencing.
CREATE TABLE IF NOT EXISTS job_dep (
  job_id     INTEGER NOT NULL REFERENCES job(id),
  blocker_id INTEGER NOT NULL REFERENCES job(id),
  PRIMARY KEY (job_id, blocker_id)
);

-- A check is a durable row, not a log line. Its last outcome survives the
-- process that produced it, which is what lets it gate anything at all.
CREATE TABLE IF NOT EXISTS job_check (
  id           INTEGER PRIMARY KEY,
  job_id       INTEGER NOT NULL REFERENCES job(id),
  name         TEXT    NOT NULL,
  cmd          TEXT    NOT NULL,
  blocking     INTEGER NOT NULL DEFAULT 0,
  last_exit    INTEGER,          -- NULL = never run = unproven
  last_output  TEXT,
  last_run_at  TEXT,
  created_at   TEXT    NOT NULL,
  UNIQUE (job_id, name)
);

-- One row per adapter execution. finished_at IS NULL means the run never
-- reported back: that is a crash, and it is why `bevis reclaim` exists.
CREATE TABLE IF NOT EXISTS job_run (
  id          INTEGER PRIMARY KEY,
  job_id      INTEGER NOT NULL REFERENCES job(id),
  slot        INTEGER NOT NULL DEFAULT 0,
  actor       TEXT    NOT NULL DEFAULT '',
  adapter_cmd TEXT    NOT NULL,
  exit_code   INTEGER,
  stdout      TEXT    NOT NULL DEFAULT '',
  stderr      TEXT    NOT NULL DEFAULT '',
  started_at  TEXT    NOT NULL,
  finished_at TEXT
);

-- Append-only audit trail. Nothing reads it to make a decision; it exists so a
-- human can reconstruct how a job reached its status.
CREATE TABLE IF NOT EXISTS event (
  id      INTEGER PRIMARY KEY,
  ts      TEXT    NOT NULL,
  job_id  INTEGER,
  actor   TEXT    NOT NULL DEFAULT '',
  kind    TEXT    NOT NULL,
  detail  TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_status  ON job(status, id);
CREATE INDEX IF NOT EXISTS idx_job_parent  ON job(parent_id, id);
CREATE INDEX IF NOT EXISTS idx_check_job   ON job_check(job_id);
CREATE INDEX IF NOT EXISTS idx_run_job     ON job_run(job_id, id);
CREATE INDEX IF NOT EXISTS idx_event_job   ON event(job_id, id);
"""


def resolve_db_path(explicit: Optional[str] = None) -> Path:
    """--db beats $BEVIS_DB beats the nearest .bevis/bevis.db walking upward.

    Walking upward is the git habit: run bevis from anywhere inside a project
    and you address the same board.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("BEVIS_DB")
    if env:
        return Path(env).expanduser().resolve()
    here = Path.cwd().resolve()
    for directory in [here, *here.parents]:
        candidate = directory / DEFAULT_DIR / DEFAULT_FILE
        if candidate.exists():
            return candidate
    return here / DEFAULT_DIR / DEFAULT_FILE


def connect(path, create: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if not path.exists() and not create:
        raise UsageError(
            "no bevis database at %s — run `bevis init` first (or pass --db)" % path
        )
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL + a real busy timeout: `bevis run --slots N` has N writers.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path) -> Path:
    """Create the schema. Idempotent — running init twice is not an error."""
    path = Path(path)
    conn = connect(path, create=True)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (str(SCHEMA_VERSION),),
        )
    finally:
        conn.close()
    return path


# ── Identifiers ──────────────────────────────────────────────────────────────
# A job has an internal integer id and, if it has a parent, a dotted display id
# ("7.2" = the second child of job 7). Both resolve. The dotted form is what a
# human reads in a plan; the integer is what the database joins on.
_DOTTED_RE = re.compile(r"^(\d+)\.(\d+)$")


def display_id(conn: sqlite3.Connection, job_id: int, parent_id: Optional[int]) -> str:
    if parent_id is None:
        return str(job_id)
    seq = conn.execute(
        "SELECT COUNT(*) FROM job WHERE parent_id=? AND id<=?", (parent_id, job_id)
    ).fetchone()[0]
    return "%d.%d" % (parent_id, seq)


def resolve_id(conn: sqlite3.Connection, raw) -> int:
    """Map '12' or '7.2' to an internal id, or raise NotFound.

    Never returns a best guess. A reference that does not resolve is an error a
    human has to look at, not a silent no-op against row zero.
    """
    if raw is None:
        raise NotFound("no job id given")
    text = str(raw).strip()
    if text.isdigit():
        row = conn.execute("SELECT id FROM job WHERE id=?", (int(text),)).fetchone()
        if row:
            return int(row["id"])
        raise NotFound("job %s not found" % text)
    match = _DOTTED_RE.match(text)
    if match:
        parent, seq = int(match.group(1)), int(match.group(2))
        rows = conn.execute(
            "SELECT id FROM job WHERE parent_id=? ORDER BY id", (parent,)
        ).fetchall()
        if 1 <= seq <= len(rows):
            return int(rows[seq - 1]["id"])
        raise NotFound("job %s not found (parent %d has %d children)"
                       % (text, parent, len(rows)))
    raise NotFound("job %r not found (expected an id like 12 or 7.2)" % text)


def get_job(conn: sqlite3.Connection, raw) -> sqlite3.Row:
    job_id = resolve_id(conn, raw)
    row = conn.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
    if row is None:  # pragma: no cover - resolve_id already proved it exists
        raise NotFound("job %s not found" % raw)
    return row


def log_event(conn: sqlite3.Connection, job_id, actor: str, kind: str, detail: str = "") -> None:
    from .model import now_ts

    conn.execute(
        "INSERT INTO event (ts, job_id, actor, kind, detail) VALUES (?,?,?,?,?)",
        (now_ts(), job_id, actor or "", kind, detail or ""),
    )
