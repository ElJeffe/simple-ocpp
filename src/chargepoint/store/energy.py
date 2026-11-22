"""Energy: the daily meter log, usage per period and initial readings."""

import logging
import sqlite3
from datetime import date, datetime
from typing import Literal, TypedDict

from .database import db, now_iso
from .settings import fiscal_year_start


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
