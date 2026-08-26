"""Storage: one SQLite file, stdlib sqlite3, no ORM.

Why no ORM: the whole point of bevis is that a reader can audit the rules in an
afternoon. Six CREATE TABLE statements you can read is a feature. The file is
also the interchange format — `sqlite3 .bevis/bevis.db` is a supported way to
inspect a board, and every gate lives in Python, not in a trigger, so nothing
about the schema is load-bearing magic.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import List, Optional

from .errors import NotFound, UsageError

DEFAULT_DIR = ".bevis"
DEFAULT_FILE = "bevis.db"
SCHEMA_VERSION = 3

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
  -- The negative control, when one was run: a command that had to FAIL for the
  -- evidence above to mean anything. Optional, because bevis cannot invent one.
  control_cmd    TEXT,
  control_exit   INTEGER,
  control_output TEXT,
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

-- Named adapters: a NAME for a command line, so `bevis run --adapter myagent`
-- does not mean retyping it. Names and commands only. bevis holds no endpoint,
-- no model name and no credential of yours -- the command owns its own config,
-- and `bevis adapter add` refuses a command with a secret written into it.
CREATE TABLE IF NOT EXISTS adapter (
  name       TEXT PRIMARY KEY,
  cmd        TEXT NOT NULL,
  note       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
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


#: Columns added after a release, for boards that already exist. Additive only:
#: nothing here drops, renames or rewrites anything, so applying it to a current
#: board is a no-op and applying it to an old one cannot lose evidence.
MIGRATIONS = (
    ("job", "control_cmd", "TEXT"),
    ("job", "control_exit", "INTEGER"),
    ("job", "control_output", "TEXT"),
)


def columns(conn: sqlite3.Connection, table: str) -> set:
    """The column names a table actually has, asked of the file.

    A fact about the database is read from the database, never inferred from
    catching an exception raised somewhere down the line.
    """
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def has_negative_control(conn: sqlite3.Connection) -> bool:
    """Can this board store a negative control at all?

    False on a board created by bevis 0.1.x. `bevis init` is idempotent and adds
    the columns without touching a job, so the fix is one command.
    """
    return "control_cmd" in columns(conn, "job")


def migrate(conn: sqlite3.Connection) -> List[str]:
    """Bring an older board up to the current schema. Returns what it added."""
    added = []
    for table, column, decl in MIGRATIONS:
        if column not in columns(conn, table):
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
            added.append("%s.%s" % (table, column))
    return added


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
    try:
        # WAL + a real busy timeout: `bevis run --slots N` has N writers.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.DatabaseError as exc:
        # A --db or $BEVIS_DB pointing at something that is not a database is a
        # user's mistake, not a bug, and it must not reach them as a traceback.
        conn.close()
        raise UsageError(
            "%s is not a SQLite database (%s) — point --db or $BEVIS_DB at a "
            "bevis board, or run `bevis init` to create one" % (path, exc)
        ) from exc
    return conn


def init_db(path) -> Path:
    """Create the schema. Idempotent — running init twice is not an error."""
    path = Path(path)
    conn = connect(path, create=True)
    try:
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS does nothing to a table that already
        # exists, so a board from an older bevis needs its new columns added
        # explicitly. This is why `bevis init` is worth running again.
        migrate(conn)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
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
