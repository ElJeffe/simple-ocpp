"""Persistent state: chargers, RFID tags and their groups, and charge sessions."""

import calendar
import json
import logging
import os
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal, TypedDict, cast

from ocpp.v16.enums import AuthorizationStatus

from .config import DATA_DIR, DB_FILE, NETWORK_DEFAULTS


def now_iso() -> str:
  return datetime.now(timezone.utc).isoformat()


LockConfig = Literal["locked", "unlocked", "unknown"]


class ConnectorInfo(TypedDict, total=False):
  """Latest known state of one connector (0 = the charger as a whole)."""

  status: str
  error_code: str
  updated: str
  power_w: float
  energy_wh: float
  meter_updated: str


class TagRecord(TypedDict):
  id_tag: str  # spelling as first registered
  name: str
  authorized: bool
  added: str  # ISO timestamp
  # Group name, sent to chargers as OCPP parentIdTag; "" = not in a group.
  # Any tag in the same group may stop a session another member started.
  parent_id_tag: str


class GroupRecord(TypedDict):
  name: str  # sent as parentIdTag, so at most 20 characters
  added: str


class ChargerJSON(TypedDict):
  """A charger as the web interface sees it."""

  id: str
  desired_locked: bool
  lock_on_plugin: bool
  lock_config: LockConfig
  lock_busy: bool
  last_message: str
  vendor: str
  model: str
  connected: bool
  last_seen: str
  connectors: dict[str, ConnectorInfo]
  last_rfid: str
  last_rfid_at: str


@dataclass
class ChargerState:
  """Everything we know about a charger, kept across reconnects."""

  id: str
  desired_locked: bool = True  # what the operator wants (persisted)
  lock_on_plugin: bool = True  # lock the cable whenever one is plugged in
  lock_config: LockConfig = "unknown"  # as reported by the charger
  lock_busy: bool = False
  last_message: str = ""
  vendor: str = ""
  model: str = ""
  connected: bool = False
  last_seen: str = ""
  connectors: dict[int, ConnectorInfo] = field(
      default_factory=dict[int, ConnectorInfo]
  )
  last_rfid: str = ""  # most recent RFID tag presented at the charger
  last_rfid_at: str = ""

  def to_json(self) -> ChargerJSON:
    return {
        "id": self.id,
        "desired_locked": self.desired_locked,
        "lock_on_plugin": self.lock_on_plugin,
        "lock_config": self.lock_config,
        "lock_busy": self.lock_busy,
        "last_message": self.last_message,
        "vendor": self.vendor,
        "model": self.model,
        "connected": self.connected,
        "last_seen": self.last_seen,
        "connectors": {str(k): v for k, v in sorted(self.connectors.items())},
        "last_rfid": self.last_rfid,
        "last_rfid_at": self.last_rfid_at,
    }


registry: dict[str, ChargerState] = {}


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


# --- Database ---------------------------------------------------------------
# The in-memory `registry` and `tags` are loaded once at startup and every
# change is written through to SQLite, so reads never touch the disk.

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


def load_registry() -> None:
  with db() as conn:
    rows = conn.execute("SELECT * FROM chargers").fetchall()
  for row in rows:
    registry[row["id"]] = ChargerState(
        id=row["id"],
        desired_locked=bool(row["desired_locked"]),
        lock_on_plugin=bool(row["lock_on_plugin"]),
        vendor=row["vendor"],
        model=row["model"],
        last_seen=row["last_seen"],
        last_rfid=row["last_rfid"],
        last_rfid_at=row["last_rfid_at"],
    )


def save_registry() -> None:
  rows = [
      (s.id, s.desired_locked, s.lock_on_plugin, s.vendor, s.model, s.last_seen,
       s.last_rfid, s.last_rfid_at)
      for s in registry.values()
  ]
  try:
    with db() as conn:
      conn.executemany(
          "INSERT OR REPLACE INTO chargers (id, desired_locked, lock_on_plugin,"
          " vendor, model, last_seen, last_rfid, last_rfid_at)"
          " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          rows,
      )
  except sqlite3.Error as e:
    logging.error(f"Could not save chargers to {DB_FILE}: {e}")


def get_state(cp_id: str) -> ChargerState:
  if cp_id not in registry:
    registry[cp_id] = ChargerState(id=cp_id)
    save_registry()
  return registry[cp_id]


# --- RFID tags --------------------------------------------------------------

# OCPP id tags are case-insensitive, so they are keyed in upper case.
tags: dict[str, TagRecord] = {}


def tag_key(id_tag: str | None) -> str:
  return (id_tag or "").strip().upper()


def load_tags() -> None:
  """Loads tags and groups."""
  with db() as conn:
    group_rows = conn.execute("SELECT * FROM groups").fetchall()
    rows = conn.execute("SELECT * FROM tags").fetchall()
  for row in group_rows:
    groups[row["key"]] = {"name": row["name"], "added": row["added"]}
  for row in rows:
    tags[row["key"]] = {
        "id_tag": row["id_tag"],
        "name": row["name"],
        "authorized": bool(row["authorized"]),
        "added": row["added"],
        "parent_id_tag": row["parent_id_tag"],
    }
  # Groups used to exist only as a value on tags; give those a groups row.
  for tag in tags.values():
    if tag["parent_id_tag"] and tag_key(tag["parent_id_tag"]) not in groups:
      add_group(tag["parent_id_tag"])


def save_tag(tag: TagRecord) -> None:
  with db() as conn:
    conn.execute(
        "INSERT OR REPLACE INTO tags (key, id_tag, name, authorized, added,"
        " parent_id_tag) VALUES (?, ?, ?, ?, ?, ?)",
        (tag_key(tag["id_tag"]), tag["id_tag"], tag["name"], tag["authorized"],
         tag["added"], tag["parent_id_tag"]),
    )


def tag_status(id_tag: str) -> AuthorizationStatus:
  """Only registered and authorized tags may charge."""
  tag = tags.get(tag_key(id_tag))
  if tag is None:
    return AuthorizationStatus.invalid
  return AuthorizationStatus.accepted if tag["authorized"] else AuthorizationStatus.blocked


def tag_parent(id_tag: str | None) -> str | None:
  """The group (OCPP parentIdTag) of a registered tag, or None."""
  tag = tags.get(tag_key(id_tag))
  return (tag["parent_id_tag"] or None) if tag else None


def group_name(name: str) -> str:
  """The exact name of an existing group ("" for none). Raises ValueError.

  Matching ignores case, like id tags, but the stored spelling is returned:
  chargers may compare parentIdTag values exactly.
  """
  if not name.strip():
    return ""
  group = groups.get(tag_key(name))
  if group is None:
    raise ValueError(f"Unknown group {name.strip()!r}")
  return group["name"]


def sorted_tags() -> list[TagRecord]:
  return sorted(tags.values(), key=lambda t: (t["name"].lower(), t["id_tag"]))


def add_tag(
    id_tag: str, name: str = "", authorized: bool = True, parent_id_tag: str = ""
) -> TagRecord:
  """Registers a tag.

  Raises KeyError if it is already registered, ValueError for an unknown group.
  """
  key = tag_key(id_tag)
  if key in tags:
    raise KeyError(id_tag)
  tag: TagRecord = {
      "id_tag": id_tag.strip(),
      "name": name.strip(),
      "authorized": authorized,
      "added": now_iso(),
      "parent_id_tag": group_name(parent_id_tag),
  }
  save_tag(tag)  # before caching, so a failed write leaves no phantom tag
  tags[key] = tag
  logging.info(f"Registered RFID {id_tag}: {tag}")
  return tag


def update_tag(
    id_tag: str, name: str | None = None, authorized: bool | None = None,
    parent_id_tag: str | None = None,
) -> tuple[TagRecord, bool]:
  """Changes a tag. Raises KeyError if unknown, ValueError for an unknown group.

  Returns (tag, stale) where stale means chargers may have cached outdated
  information about it (access revoked or group changed).
  """
  key = tag_key(id_tag)
  old = tags[key]
  tag = old.copy()
  if name is not None:
    tag["name"] = name.strip()
  if authorized is not None:
    tag["authorized"] = authorized
  if parent_id_tag is not None:
    tag["parent_id_tag"] = group_name(parent_id_tag)
  save_tag(tag)
  tags[key] = tag
  logging.info(f"Updated RFID {tag['id_tag']}: {tag}")
  revoked = old["authorized"] and not tag["authorized"]
  return tag, revoked or old["parent_id_tag"] != tag["parent_id_tag"]


def remove_tag(id_tag: str) -> TagRecord:
  """Forgets a tag. Raises KeyError if unknown."""
  key = tag_key(id_tag)
  tag = tags[key]
  with db() as conn:
    conn.execute("DELETE FROM tags WHERE key = ?", (key,))
  del tags[key]
  logging.info(f"Removed RFID {tag['id_tag']}")
  return tag


# --- Tag groups ---------------------------------------------------------------
# A group is just a name that member tags share as their parentIdTag.

groups: dict[str, GroupRecord] = {}  # keyed by tag_key(name)


def sorted_groups() -> list[GroupRecord]:
  return sorted(groups.values(), key=lambda g: g["name"].lower())


def add_group(name: str) -> GroupRecord:
  """Creates a group. Raises KeyError if one with that name exists."""
  key = tag_key(name)
  if key in groups:
    raise KeyError(name)
  group: GroupRecord = {"name": name.strip(), "added": now_iso()}
  with db() as conn:
    conn.execute(
        "INSERT INTO groups (key, name, added) VALUES (?, ?, ?)",
        (key, group["name"], group["added"]),
    )
  groups[key] = group
  logging.info(f"Added tag group {group['name']}")
  return group


def remove_group(name: str) -> list[TagRecord]:
  """Deletes a group and takes its tags out of it. Returns those tags.

  Raises KeyError if there is no such group.
  """
  key = tag_key(name)
  group = groups[key]
  members = [k for k, t in tags.items() if t["parent_id_tag"] == group["name"]]
  with db() as conn:  # one transaction: either both happen or neither
    conn.execute(
        "UPDATE tags SET parent_id_tag = '' WHERE parent_id_tag = ?", (group["name"],)
    )
    conn.execute("DELETE FROM groups WHERE key = ?", (key,))
  del groups[key]
  for k in members:
    tags[k] = {**tags[k], "parent_id_tag": ""}
  logging.info(f"Removed tag group {group['name']} ({len(members)} tags)")
  return [tags[k] for k in members]


# --- Charge sessions --------------------------------------------------------
# Not cached in memory: they are only written by OCPP messages and read by the
# web interface, and the table grows without bound.


class SessionRecord(TypedDict):
  id: int  # OCPP transaction ID
  charger_id: str
  connector_id: int
  id_tag: str
  tag_name: str
  accepted: bool
  meter_start_wh: int
  meter_stop_wh: int | None
  energy_wh: int | None  # None while charging
  started_at: str
  stopped_at: str | None
  stop_reason: str | None
  stop_id_tag: str | None


def start_session(
    charger_id: str, connector_id: int, id_tag: str, meter_start_wh: int,
    started_at: str, accepted: bool,
) -> int:
  """Records a new session and returns its transaction ID."""
  tag = tags.get(tag_key(id_tag))
  with db() as conn:
    cursor = conn.execute(
        "INSERT INTO sessions (charger_id, connector_id, id_tag, tag_name,"
        " accepted, meter_start_wh, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (charger_id, connector_id, id_tag, tag["name"] if tag else "",
         accepted, meter_start_wh, started_at),
    )
  assert cursor.lastrowid is not None
  return cursor.lastrowid


def stop_session(
    charger_id: str, transaction_id: int, meter_stop_wh: int, stopped_at: str,
    reason: str | None, stop_id_tag: str | None = None,
) -> bool:
  """Completes a session. Returns False if the charger has no such session."""
  with db() as conn:
    cursor = conn.execute(
        "UPDATE sessions SET meter_stop_wh = ?, stopped_at = ?, stop_reason = ?,"
        " stop_id_tag = ? WHERE id = ? AND charger_id = ?",
        (meter_stop_wh, stopped_at, reason, stop_id_tag, transaction_id,
         charger_id),
    )
  return cursor.rowcount > 0


def session_filter(
    charger_id: str | None, since: datetime | None, until: datetime | None
) -> tuple[str, list[object]]:
  """A WHERE clause (or "") selecting sessions by charger and start time.

  Start times are the charger's own ISO timestamps, which may use "Z" or an
  offset, so they are compared with julianday() rather than as strings.
  """
  conditions: list[str] = []
  params: list[object] = []
  if charger_id is not None:
    conditions.append("charger_id = ?")
    params.append(charger_id)
  if since is not None:
    conditions.append("julianday(started_at) >= julianday(?)")
    params.append(since.isoformat())
  if until is not None:
    conditions.append("julianday(started_at) < julianday(?)")
    params.append(until.isoformat())
  return (" WHERE " + " AND ".join(conditions) if conditions else ""), params


def count_sessions(
    charger_id: str | None = None, since: datetime | None = None,
    until: datetime | None = None,
) -> int:
  where, params = session_filter(charger_id, since, until)
  with db() as conn:
    return conn.execute(f"SELECT COUNT(*) FROM sessions{where}", params).fetchone()[0]


def list_sessions(
    charger_id: str | None = None, limit: int = 100, offset: int = 0,
    since: datetime | None = None, until: datetime | None = None,
) -> list[SessionRecord]:
  """Newest sessions first; `since` is inclusive, `until` exclusive."""
  where, params = session_filter(charger_id, since, until)
  query = f"SELECT * FROM sessions{where} ORDER BY id DESC LIMIT ? OFFSET ?"
  params += [limit, offset]
  with db() as conn:
    rows = conn.execute(query, params).fetchall()
  return [
      {
          "id": row["id"],
          "charger_id": row["charger_id"],
          "connector_id": row["connector_id"],
          "id_tag": row["id_tag"],
          "tag_name": row["tag_name"],
          "accepted": bool(row["accepted"]),
          "meter_start_wh": row["meter_start_wh"],
          "meter_stop_wh": row["meter_stop_wh"],
          "energy_wh": (
              None if row["meter_stop_wh"] is None
              else row["meter_stop_wh"] - row["meter_start_wh"]
          ),
          "started_at": row["started_at"],
          "stopped_at": row["stopped_at"],
          "stop_reason": row["stop_reason"],
          "stop_id_tag": row["stop_id_tag"],
      }
      for row in rows
  ]


# --- Energy -------------------------------------------------------------------

EnergyPeriod = Literal["day", "month", "year"]
PERIOD_LENGTH: dict[EnergyPeriod, int] = {"day": 10, "month": 7}


class EnergyUsage(TypedDict):
  period: str  # "2026-11-22", "2026-11" or "2026"
  energy_wh: float


def record_energy(
    charger_id: str, connector_id: int, energy_wh: float, today: date | None = None
) -> None:
  """Stores a meter register reading as the latest one of today."""
  day = (today or datetime.now().astimezone().date()).isoformat()
  try:
    with db() as conn:
      conn.execute(
          "INSERT OR REPLACE INTO energy_readings (charger_id, connector_id, day,"
          " energy_wh, updated) VALUES (?, ?, ?, ?, ?)",
          (charger_id, connector_id, day, energy_wh, now_iso()),
      )
  except sqlite3.Error as e:  # never let this break the charger's request
    logging.error(f"Could not save energy reading for {charger_id}: {e}")


def fiscal_year(day: date, start: tuple[int, int]) -> int:
  """The calendar year in which the fiscal year containing `day` starts."""
  return day.year if (day.month, day.day) >= start else day.year - 1


def energy_usage(
    period: EnergyPeriod, charger_id: str | None = None
) -> list[EnergyUsage]:
  """Energy used per day, month or fiscal year, oldest first.

  A day's usage is its last reading minus the previous reading of the same
  meter, so days the server was not running are counted on the next day it
  was. A register that went down (meter replaced or reset) counts as zero
  for that day. Connector 0 (the whole charger) is only used for chargers
  that report no individual connectors, to avoid counting energy twice.

  An initial energy reading entered on the settings page counts as a reading
  at the start of its day, on the charger's lowest-numbered meter, so the
  energy used between then and the first real reading is included.

  Years are fiscal years, keyed by the calendar year they start in.
  """
  query = "SELECT charger_id, connector_id, day, energy_wh FROM energy_readings"
  params: list[object] = []
  if charger_id is not None:
    query += " WHERE charger_id = ?"
    params.append(charger_id)
  with db() as conn:
    rows = conn.execute(query, params).fetchall()

  # meter -> [(day, order, energy_wh)]; order 0 puts an initial reading
  # before a real reading of the same day.
  meters: dict[tuple[str, int], list[tuple[str, int, float]]] = {}
  with_connectors = {r["charger_id"] for r in rows if r["connector_id"] > 0}
  for r in rows:
    if r["connector_id"] == 0 and r["charger_id"] in with_connectors:
      continue
    meters.setdefault((r["charger_id"], r["connector_id"]), []).append(
        (r["day"], 1, r["energy_wh"])
    )
  for cp_id, initial in initial_energy().items():
    if charger_id is not None and cp_id != charger_id:
      continue
    own = sorted(c for (c_id, c) in meters if c_id == cp_id)
    meter = (cp_id, own[0] if own else 1)
    meters.setdefault(meter, []).append((initial["day"], 0, initial["energy_wh"]))

  start = fiscal_year_start()
  totals: dict[str, float] = {}
  for readings in meters.values():
    previous: float | None = None
    for day, _, energy_wh in sorted(readings):
      if period == "year":
        key = str(fiscal_year(date.fromisoformat(day), start))
      else:
        key = day[: PERIOD_LENGTH[period]]
      used = 0.0 if previous is None else max(0.0, energy_wh - previous)
      totals[key] = totals.get(key, 0.0) + used
      previous = energy_wh
  return [{"period": k, "energy_wh": v} for k, v in sorted(totals.items())]


def session_connector(charger_id: str, transaction_id: int) -> int | None:
  with db() as conn:
    row = conn.execute(
        "SELECT connector_id FROM sessions WHERE id = ? AND charger_id = ?",
        (transaction_id, charger_id),
    ).fetchone()
  return row["connector_id"] if row else None


class InitialEnergy(TypedDict):
  day: str  # YYYY-MM-DD
  energy_wh: float


def initial_energy() -> dict[str, InitialEnergy]:
  """Initial energy readings by charger id."""
  with db() as conn:
    rows = conn.execute("SELECT * FROM initial_energy").fetchall()
  return {r["charger_id"]: {"day": r["day"], "energy_wh": r["energy_wh"]} for r in rows}


def set_initial_energy(charger_id: str, day: date, energy_wh: float) -> InitialEnergy:
  with db() as conn:
    conn.execute(
        "INSERT OR REPLACE INTO initial_energy (charger_id, day, energy_wh)"
        " VALUES (?, ?, ?)",
        (charger_id, day.isoformat(), energy_wh),
    )
  logging.info(f"Initial energy of {charger_id}: {energy_wh} Wh on {day}")
  return {"day": day.isoformat(), "energy_wh": energy_wh}


def clear_initial_energy(charger_id: str) -> bool:
  with db() as conn:
    cursor = conn.execute("DELETE FROM initial_energy WHERE charger_id = ?", (charger_id,))
  return cursor.rowcount > 0


# --- Settings -----------------------------------------------------------------
# Key/value pairs, stored as JSON. Not cached: they are rarely read.


def get_setting(key: str) -> object:
  """The stored value, or None if it was never set."""
  with db() as conn:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
  return json.loads(row["value"]) if row else None


def set_setting(key: str, value: object) -> None:
  with db() as conn:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )


class NetworkSettings(TypedDict):
  ocpp_host: str
  ocpp_port: int
  http_host: str
  http_port: int


# What the running server listens on; set by main() at startup.
active_network: NetworkSettings | None = None


def saved_network_settings() -> NetworkSettings:
  """As saved on the settings page (defaults for anything never saved)."""
  saved = get_setting("network")
  saved = cast(dict[str, object], saved) if isinstance(saved, dict) else {}
  values: dict[str, object] = {}
  for name, (default, _) in NETWORK_DEFAULTS.items():
    value = saved.get(name)
    values[name] = value if type(value) is type(default) else default
  return cast(NetworkSettings, values)


def network_overrides() -> dict[str, str]:
  """Settings set by an environment variable: name -> variable name."""
  return {name: var for name, (_, var) in NETWORK_DEFAULTS.items() if os.environ.get(var)}


def network_settings() -> NetworkSettings:
  """What to listen on: saved settings, overridden by environment variables."""
  values = dict(saved_network_settings())
  for name, var in network_overrides().items():
    raw = os.environ[var]
    if name.endswith("_port"):
      if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise SystemExit(f"{var}={raw!r} is not a port number (1-65535)")
      values[name] = int(raw)
    else:
      values[name] = raw
  return cast(NetworkSettings, values)


def set_network_settings(values: NetworkSettings) -> None:
  set_setting("network", dict(values))
  logging.info(f"Network settings saved: {values}")


def fiscal_year_start() -> tuple[int, int]:
  """(month, day) on which a fiscal year starts; 1 January by default."""
  saved = get_setting("fiscal_year_start")
  if isinstance(saved, dict):
    saved = cast(dict[str, object], saved)
    month, day = saved.get("month"), saved.get("day")
    if isinstance(month, int) and isinstance(day, int):
      return month, day
  return 1, 1


def set_fiscal_year_start(month: int, day: int) -> None:
  """Raises ValueError for a date that doesn't exist every year."""
  if not 1 <= month <= 12 or not 1 <= day <= calendar.monthrange(2001, month)[1]:
    raise ValueError(f"{day}/{month} is not a date that exists every year")
  set_setting("fiscal_year_start", {"month": month, "day": day})
