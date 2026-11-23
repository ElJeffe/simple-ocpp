"""Known chargers. Kept in memory (`registry`) and written through to the
database; live connection state is never stored."""

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from . import database
from .database import db


LockConfig = Literal["locked", "unlocked", "unknown"]


class ConnectorInfo(TypedDict, total=False):
  """Latest known state of one connector (0 = the charger as a whole)."""

  status: str
  error_code: str
  updated: str
  power_w: float
  energy_wh: float
  meter_updated: str


class ChargerJSON(TypedDict):
  """A charger as the web interface sees it."""

  id: str
  name: str
  desired_locked: bool
  lock_on_plugin: bool
  lock_config: LockConfig
  lock_busy: bool
  last_message: str
  vendor: str
  model: str
  serial_number: str
  connected: bool
  last_seen: str
  connectors: dict[str, ConnectorInfo]
  last_rfid: str
  last_rfid_at: str


@dataclass
class ChargerState:
  """Everything we know about a charger, kept across reconnects."""

  id: str
  name: str = ""  # friendly name for the web interface; "" shows the id
  desired_locked: bool = True  # what the operator wants (persisted)
  lock_on_plugin: bool = True  # lock the cable whenever one is plugged in
  lock_config: LockConfig = "unknown"  # as reported by the charger
  lock_busy: bool = False
  last_message: str = ""
  vendor: str = ""
  model: str = ""
  serial_number: str = ""  # as reported at boot, if any
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
        "name": self.name,
        "desired_locked": self.desired_locked,
        "lock_on_plugin": self.lock_on_plugin,
        "lock_config": self.lock_config,
        "lock_busy": self.lock_busy,
        "last_message": self.last_message,
        "vendor": self.vendor,
        "model": self.model,
        "serial_number": self.serial_number,
        "connected": self.connected,
        "last_seen": self.last_seen,
        "connectors": {str(k): v for k, v in sorted(self.connectors.items())},
        "last_rfid": self.last_rfid,
        "last_rfid_at": self.last_rfid_at,
    }


registry: dict[str, ChargerState] = {}


def load_registry() -> None:
  with db() as conn:
    rows = conn.execute("SELECT * FROM chargers").fetchall()
  for row in rows:
    registry[row["id"]] = ChargerState(
        id=row["id"],
        name=row["name"],
        desired_locked=bool(row["desired_locked"]),
        lock_on_plugin=bool(row["lock_on_plugin"]),
        vendor=row["vendor"],
        model=row["model"],
        serial_number=row["serial_number"],
        last_seen=row["last_seen"],
        last_rfid=row["last_rfid"],
        last_rfid_at=row["last_rfid_at"],
    )


def save_registry() -> None:
  rows = [
      (s.id, s.name, s.desired_locked, s.lock_on_plugin, s.vendor, s.model, s.serial_number, s.last_seen,
       s.last_rfid, s.last_rfid_at)
      for s in registry.values()
  ]
  try:
    with db() as conn:
      conn.executemany(
          "INSERT OR REPLACE INTO chargers (id, name, desired_locked, lock_on_plugin,"
          " vendor, model, serial_number, last_seen, last_rfid, last_rfid_at)"
          " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          rows,
      )
  except sqlite3.Error as e:
    logging.error(f"Could not save chargers to {database.DB_FILE}: {e}")


def get_state(cp_id: str) -> ChargerState:
  if cp_id not in registry:
    registry[cp_id] = ChargerState(id=cp_id)
    save_registry()
  return registry[cp_id]


def delete_charger(cp_id: str, delete_history: bool = False) -> None:
  """Forgets a charger. Raises KeyError if unknown.

  With delete_history, its charge sessions, energy readings and initial
  energy are deleted too; otherwise they stay and still count in totals.
  """
  if cp_id not in registry:
    raise KeyError(cp_id)
  with db() as conn:  # one transaction
    conn.execute("DELETE FROM chargers WHERE id = ?", (cp_id,))
    if delete_history:
      for table in ("sessions", "energy_readings", "initial_energy"):
        conn.execute(f"DELETE FROM {table} WHERE charger_id = ?", (cp_id,))
  del registry[cp_id]
  logging.info(f"Removed charger {cp_id}" + (" and its history" if delete_history else ""))
