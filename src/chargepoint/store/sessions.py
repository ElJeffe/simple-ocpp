"""Charge sessions (OCPP transactions). Not cached in memory: they are only
written by OCPP messages and read by the web interface, and the table grows
without bound."""

import sqlite3
from datetime import datetime
from typing import TypedDict

from .chargers import registry
from .database import db
from .tags import tag_key, tags
from .tariffs import Prices, local_day, prices


class SessionRecord(TypedDict):
  id: int  # OCPP transaction ID
  charger_id: str
  connector_id: int
  several_connectors: bool  # the charger has more than one
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
  cost: float | None  # energy at the tariff of the start day; None without one


class SessionSummary(TypedDict):
  count: int
  energy_wh: float  # of the sessions that have ended
  cost: float | None  # None when no tariff is set


# Stop reason for sessions closed by the server because the charger never
# sent StopTransaction; their stop time and meter reading are estimates.
NO_STOP_RECEIVED = "NoStopReceived"


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
    charger_id: str | None = None, limit: int | None = 100, offset: int = 0,
    since: datetime | None = None, until: datetime | None = None,
) -> list[SessionRecord]:
  """Newest sessions first; `since` is inclusive, `until` exclusive.

  limit=None returns all of them (for exports and summaries).
  """
  where, params = session_filter(charger_id, since, until)
  query = f"SELECT * FROM sessions{where} ORDER BY id DESC LIMIT ? OFFSET ?"
  params += [-1 if limit is None else limit, offset]
  with db() as conn:
    rows = conn.execute(query, params).fetchall()
  price_list, several = prices(), chargers_with_several_connectors()
  return [session_record(row, price_list, several) for row in rows]


def chargers_with_several_connectors() -> set[str]:
  """Chargers known to have more than one connector.

  Live status is empty while a charger is offline, so its past sessions and
  meter readings count too. A connector number above 1 implies another one.
  """
  with db() as conn:
    rows = conn.execute(
        "SELECT charger_id FROM (SELECT charger_id, connector_id FROM sessions"
        " UNION SELECT charger_id, connector_id FROM energy_readings)"
        " WHERE connector_id > 0 GROUP BY charger_id"
        " HAVING COUNT(*) > 1 OR MAX(connector_id) > 1"
    ).fetchall()
  several = {row["charger_id"] for row in rows}
  for charger in registry.values():
    live = [c for c in charger.connectors if c > 0]
    if len(live) > 1 or any(c > 1 for c in live):
      several.add(charger.id)
  return several


def session_record(row: sqlite3.Row, price_list: Prices, several: set[str]) -> SessionRecord:
  energy_wh = None if row["meter_stop_wh"] is None else row["meter_stop_wh"] - row["meter_start_wh"]
  return {
    "id": row["id"],
    "charger_id": row["charger_id"],
    "connector_id": row["connector_id"],
    "several_connectors": row["charger_id"] in several,
    "id_tag": row["id_tag"],
    "tag_name": row["tag_name"],
    "accepted": bool(row["accepted"]),
    "meter_start_wh": row["meter_start_wh"],
    "meter_stop_wh": row["meter_stop_wh"],
    "energy_wh": energy_wh,
    "started_at": row["started_at"],
    "stopped_at": row["stopped_at"],
    "stop_reason": row["stop_reason"],
    "stop_id_tag": row["stop_id_tag"],
    "cost": price_list.cost(energy_wh, local_day(row["started_at"])),
  }


def open_sessions() -> list[SessionRecord]:
  """Accepted sessions that haven't ended yet, oldest first."""
  with db() as conn:
    rows = conn.execute(
        "SELECT * FROM sessions WHERE stopped_at IS NULL AND accepted = 1 ORDER BY id"
    ).fetchall()
  price_list, several = prices(), chargers_with_several_connectors()
  return [session_record(row, price_list, several) for row in rows]


def session_summary(
    charger_id: str | None = None, since: datetime | None = None,
    until: datetime | None = None,
) -> SessionSummary:
  """Number of sessions, their energy and their cost, for a filter."""
  found = list_sessions(charger_id, None, 0, since, until)
  costs = [s["cost"] for s in found if s["cost"] is not None]
  return {
      "count": len(found),
      "energy_wh": sum(s["energy_wh"] or 0 for s in found),
      "cost": sum(costs) if prices() else None,
  }


def session_connector(charger_id: str, transaction_id: int) -> int | None:
  with db() as conn:
    row = conn.execute(
        "SELECT connector_id FROM sessions WHERE id = ? AND charger_id = ?",
        (transaction_id, charger_id),
    ).fetchone()
  return row["connector_id"] if row else None


def close_open_sessions(
    charger_id: str, stopped_at: str, connector_id: int | None = None,
    meter_wh: float | None = None,
) -> list[int]:
  """Ends sessions that never got a StopTransaction. Returns their ids.

  Used when a new session starts on the same connector (meter_wh is then
  its start reading) or when the charger boots (all its connectors; the
  last logged meter reading is used). A StopTransaction that still arrives
  later overwrites these estimates.
  """
  query = "SELECT id, connector_id, meter_start_wh FROM sessions WHERE charger_id = ? AND stopped_at IS NULL"
  params: list[object] = [charger_id]
  if connector_id is not None:
    query += " AND connector_id = ?"
    params.append(connector_id)
  with db() as conn:
    closed: list[int] = []
    for row in conn.execute(query, params).fetchall():
      meter = meter_wh
      if meter is None:
        last = conn.execute(
            "SELECT energy_wh FROM energy_readings WHERE charger_id = ? AND connector_id = ?"
            " ORDER BY day DESC LIMIT 1",
            (charger_id, row["connector_id"]),
        ).fetchone()
        meter = last["energy_wh"] if last else row["meter_start_wh"]
      conn.execute(
          "UPDATE sessions SET meter_stop_wh = ?, stopped_at = ?, stop_reason = ? WHERE id = ?",
          (max(meter, row["meter_start_wh"]), stopped_at, NO_STOP_RECEIVED, row["id"]),
      )
      closed.append(row["id"])
  return closed
