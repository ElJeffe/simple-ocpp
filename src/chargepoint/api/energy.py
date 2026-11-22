"""Energy use per period."""

from datetime import date

from fastapi import APIRouter, Query, Response

from .. import store
from .export import csv_response, number

router = APIRouter()

FIRST = Query(None, description="First day to count (included)")
LAST = Query(None, description="Last day to count (included)")


@router.get("/api/energy")
async def energy(
    period: store.EnergyPeriod = "day", charger_id: str | None = None,
    first_day: date | None = FIRST, last_day: date | None = LAST,
) -> list[store.EnergyUsage]:
  """Energy used and its cost per day, month or fiscal year, by one charger
  or all of them."""
  return store.energy_usage(period, charger_id, first_day, last_day)


@router.get("/api/energy.csv")
async def energy_csv(
    period: store.EnergyPeriod = "day", charger_id: str | None = None,
    first_day: date | None = FIRST, last_day: date | None = LAST,
) -> Response:
  """The same, as a CSV download. Fiscal years are labelled by the calendar
  year they start in."""
  usage = store.energy_usage(period, charger_id, first_day, last_day)
  label = {"day": "Day", "month": "Month", "year": "Fiscal year"}[period]
  return csv_response(
      f"energy-per-{period}.csv",
      [label, "Energy (kWh)", f"Cost ({store.currency()})"],
      ([u["period"], number(u["energy_wh"] / 1000, 3), number(u["cost"], 2)] for u in usage),
  )
