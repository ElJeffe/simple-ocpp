"""The SQLite database: connection, schema and the data directory."""

import sqlite3
import tempfile
from collections.abc import Generator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..config import DATA_DIR, DB_FILE


def now_iso() -> str:
  return datetime.now(timezone.utc).isoformat()


def ensure_data_dir() -> None:
  """Creates the data directory, or exits with a clear message if we can't.

  Without this, a missing or read-only directory would only show up as
  "Could not write" errors while every change is silently lost.
  """
  try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    probe = DATA_DIR / ".write-test"
    probe.write_text("")
    probe.unlink()
  except OSError as e:
    raise SystemExit(
        f"Data directory {DATA_DIR} is not writable ({e.strerror}). Create it"
        " with the right owner, or set OCPP_DATA_DIR to another directory."
    ) from e


SCHEMA = """
CREATE TABLE IF NOT EXISTS chargers (
  id             TEXT PRIMARY KEY,
  desired_locked INTEGER NOT NULL DEFAULT 1,
  lock_on_plugin INTEGER NOT NULL DEFAULT 1,
  vendor         TEXT NOT NULL DEFAULT '',
  model          TEXT NOT NULL DEFAULT '',
  last_seen      TEXT NOT NULL DEFAULT '',
  last_rfid      TEXT NOT NULL DEFAULT '',
  last_rfid_at   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tags (
  key        TEXT PRIMARY KEY,  -- tag_key(id_tag)
  id_tag     TEXT NOT NULL,
  name       TEXT NOT NULL DEFAULT '',
  authorized INTEGER NOT NULL DEFAULT 0,
  added      TEXT NOT NULL DEFAULT '',
  parent_id_tag TEXT NOT NULL DEFAULT ''   -- groups.name, or ''
);
CREATE TABLE IF NOT EXISTS groups (
  key   TEXT PRIMARY KEY,  -- tag_key(name)
  name  TEXT NOT NULL,
  added TEXT NOT NULL DEFAULT ''
);
-- One row per StartTransaction; the id is the OCPP transaction ID, so
-- AUTOINCREMENT keeps it unique even after the newest rows are deleted.
CREATE TABLE IF NOT EXISTS sessions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  charger_id     TEXT NOT NULL,
  connector_id   INTEGER NOT NULL,
  id_tag         TEXT NOT NULL,
  tag_name       TEXT NOT NULL DEFAULT '',  -- as registered when it started
  accepted       INTEGER NOT NULL,          -- 0 if we refused the tag
  meter_start_wh INTEGER NOT NULL,
  meter_stop_wh  INTEGER,                   -- NULL while charging
  started_at     TEXT NOT NULL,             -- charger's timestamps
  stopped_at     TEXT,
  stop_reason    TEXT,
  stop_id_tag    TEXT                       -- tag that stopped it, if any
);
CREATE INDEX IF NOT EXISTS sessions_by_charger ON sessions (charger_id, id);
-- The energy meter register, logged once a day per connector: the row for
-- today is overwritten with each new reading, so it ends up holding the last
-- reading of the day.
-- Total energy the user read off a charger on some day before it was
-- connected here (settings page), so that energy counts too.
CREATE TABLE IF NOT EXISTS initial_energy (
  charger_id TEXT PRIMARY KEY,
  day        TEXT NOT NULL,  -- YYYY-MM-DD
  energy_wh  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL  -- JSON
);
CREATE TABLE IF NOT EXISTS energy_readings (
  charger_id   TEXT NOT NULL,
  connector_id INTEGER NOT NULL,
  day          TEXT NOT NULL,  -- YYYY-MM-DD, server's local time zone
  energy_wh    REAL NOT NULL,
  updated      TEXT NOT NULL,
  PRIMARY KEY (charger_id, connector_id, day)
);
"""


ADDED_COLUMNS = [  # (table, column, definition), oldest first
    ("chargers", "lock_on_plugin", "INTEGER NOT NULL DEFAULT 1"),
    ("tags", "parent_id_tag", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "stop_id_tag", "TEXT"),
]


@contextmanager
def db() -> Generator[sqlite3.Connection]:
  """A connection that commits on success and rolls back on error."""
  with closing(sqlite3.connect(DB_FILE)) as conn, conn:
    conn.row_factory = sqlite3.Row
    yield conn


def init_db() -> None:
  with db() as conn:
    conn.executescript(SCHEMA)
    # Columns added after the first release; CREATE TABLE IF NOT EXISTS
    # leaves older tables as they were.
    for table, column, definition in ADDED_COLUMNS:
      columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
      if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def backup(target: Path) -> None:
  """Writes a consistent copy of the database to `target` (overwritten).

  Uses SQLite's backup API, so the copy is complete and valid even while the
  server keeps writing; copying the file itself could catch it half-written.
  """
  with closing(sqlite3.connect(DB_FILE)) as source, closing(sqlite3.connect(target)) as copy:
    source.backup(copy)


def backup_bytes() -> bytes:
  """The same, as the bytes of a database file (for a download)."""
  with tempfile.TemporaryDirectory() as tmp:
    target = Path(tmp) / "backup.db"
    backup(target)
    return target.read_bytes()
