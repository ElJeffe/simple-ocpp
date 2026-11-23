# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

"""Energy: the daily meter log, usage per period and initial readings."""

import logging
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Literal, TypedDict

from .database import db
from .tariffs import prices


EnergyPeriod = Literal["day", "month", "quarter", "year"]

# How old a charger's queued reading can plausibly be.
MAX_QUEUED_AGE = timedelta(days=31)


PERIOD_LENGTH: dict[EnergyPeriod, int] = {"day": 10, "month": 7, "year": 4}


class EnergyUsage(TypedDict):
  period: str  # "2026-11-22", "2026-11", "2026-Q1" or "2026"
  energy_wh: float
  cost: float | None  # each day at that day's tariff; None without tariffs


def reading_time(timestamp: str | None) -> datetime:
  """When a charger took a reading, or now if its timestamp can't be right.

  A charger whose clock isn't set may report a date far in the past or in the
  future; such a reading is treated as taken on arrival.
  """
  now = datetime.now(timezone.utc)
  if not timestamp:
    return now
  try:
    when = datetime.fromisoformat(timestamp).astimezone(timezone.utc)
  except (ValueError, OverflowError):
    return now
  return when if now - MAX_QUEUED_AGE <= when <= now else now


def record_energy(
    charger_id: str, connector_id: int, energy_wh: float, today: date | None = None,
    at: str | None = None,
) -> None:
  """Stores a meter register reading as the latest one of its day.

  `at` is the charger's timestamp. A charger that was offline delivers its
  queued messages late: they count on the day they happened, and never
  replace a reading taken after them.
  """
  when = reading_time(at)
  day = (today or when.astimezone().date()).isoformat()
  try:
    with db() as conn:
      conn.execute(
          "INSERT INTO energy_readings (charger_id, connector_id, day, energy_wh,"
          " updated) VALUES (?, ?, ?, ?, ?)"
          " ON CONFLICT (charger_id, connector_id, day) DO UPDATE SET"
          " energy_wh = excluded.energy_wh, updated = excluded.updated"
          " WHERE julianday(excluded.updated) >= julianday(energy_readings.updated)",
          (charger_id, connector_id, day, energy_wh, when.isoformat()),
      )
  except sqlite3.Error as e:  # never let this break the charger's request
    logging.error(f"Could not save energy reading for {charger_id}: {e}")


def energy_usage(
    period: EnergyPeriod, charger_id: str | None = None,
    first_day: date | None = None, last_day: date | None = None,
) -> list[EnergyUsage]:
  """Energy used per day, month, quarter or year, oldest first.

  first_day and last_day (both included) limit which days are counted.

  A day's usage is its last reading minus the previous reading of the same
  meter, so days the server was not running are counted on the next day it
  was. A register that went down (meter replaced or reset) counts as zero
  for that day. Connector 0 (the whole charger) is only used for chargers
  that report no individual connectors, to avoid counting energy twice.

  An initial energy reading entered on the settings page counts as a reading
  at the start of its day, on the charger's lowest-numbered meter, so the
  energy used between then and the first real reading is included.

  Years and quarters follow the calendar.
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
    if not math.isfinite(r["energy_wh"]):  # stored by an older version
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

  price_list = prices()
  totals: dict[str, float] = {}
  costs: dict[str, float] = {}
  for readings in meters.values():
    previous: float | None = None
    for day, _, energy_wh in sorted(readings):
      used = 0.0 if previous is None else max(0.0, energy_wh - previous)
      previous = energy_wh
      when = date.fromisoformat(day)
      if (first_day and when < first_day) or (last_day and when > last_day):
        continue
      if period == "quarter":
        key = f"{when.year}-Q{(when.month + 2) // 3}"
      else:
        key = day[: PERIOD_LENGTH[period]]
      totals[key] = totals.get(key, 0.0) + used
      costs[key] = costs.get(key, 0.0) + (price_list.cost(used, when) or 0.0)
  return [
      {"period": k, "energy_wh": v, "cost": costs[k] if price_list else None}
      for k, v in sorted(totals.items())
  ]


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
