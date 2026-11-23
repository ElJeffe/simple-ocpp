"""Energy use per period."""

from datetime import date, datetime

from fastapi import APIRouter, Query, Response

from .. import store
from ..pdf import table_pdf
from .export import DECIMAL, CsvDecimal, csv_decimal, csv_response, number

router = APIRouter()

FIRST = Query(None, description="First day to count (included)")
LAST = Query(None, description="Last day to count (included)")
SELECTED = Query(None, description="Only these periods, e.g. 2026-Q1; all if left out")
PERIOD_LABELS: dict[store.EnergyPeriod, str] = {
    "day": "Day", "month": "Month", "quarter": "Quarter", "year": "Year",
}


def selected_usage(
    period: store.EnergyPeriod, charger_id: str | None, first_day: date | None,
    last_day: date | None, selected: list[str] | None,
) -> list[store.EnergyUsage]:
  usage = store.energy_usage(period, charger_id, first_day, last_day)
  if selected is None:
    return usage
  wanted = set(selected)
  return [u for u in usage if u["period"] in wanted]


@router.get("/api/energy")
async def energy(
    period: store.EnergyPeriod = "day", charger_id: str | None = None,
    first_day: date | None = FIRST, last_day: date | None = LAST,
) -> list[store.EnergyUsage]:
  """Energy used and its cost per day, month, quarter or year, by one charger
  or all of them."""
  return store.energy_usage(period, charger_id, first_day, last_day)


@router.get("/api/energy.csv")
async def energy_csv(
    period: store.EnergyPeriod = "day", charger_id: str | None = None,
    first_day: date | None = FIRST, last_day: date | None = LAST,
    decimal: CsvDecimal | None = DECIMAL, selected: list[str] | None = SELECTED,
) -> Response:
  """The same, as a CSV download."""
  usage = selected_usage(period, charger_id, first_day, last_day, selected)
  separator = csv_decimal(decimal)
  return csv_response(
      f"energy-per-{period}.csv",
      [PERIOD_LABELS[period], "Energy (kWh)", f"Cost ({store.currency()})"],
      ([u["period"], number(u["energy_wh"] / 1000, 3, separator),
        number(u["cost"], 2, separator)] for u in usage),
      separator,
  )


@router.get("/api/energy.pdf", response_class=Response)
async def energy_pdf(
    period: store.EnergyPeriod = "day", charger_id: str | None = None,
    first_day: date | None = FIRST, last_day: date | None = LAST,
    decimal: CsvDecimal | None = DECIMAL, selected: list[str] | None = SELECTED,
) -> Response:
  """The same, as a printable report with a total of the entries in it."""
  usage = selected_usage(period, charger_id, first_day, last_day, selected)
  separator = csv_decimal(decimal)
  currency = store.currency()
  with_cost = bool(store.prices())

  def cells(label: str, energy_wh: float, cost: float | None) -> list[str]:
    row = [label, number(energy_wh / 1000, 3, separator)]
    if with_cost:
      price = cost / energy_wh * 100_000 if cost is not None and energy_wh > 0 else None
      row += [number(price, 3, separator) or "—", number(cost, 2, separator) or "—"]
    return row

  def charger_lines() -> list[str]:
    """How the charger identifies itself; a report doesn't use our friendly name."""
    if not charger_id:
      return ["Charger: All chargers"]
    charger = store.registry.get(charger_id)  # gone if removed with its history kept
    if charger is None:
      return [f"ID: {charger_id}"]
    make = " ".join(part for part in (charger.vendor, charger.model) if part)
    identity = (
        f"Serial number: {charger.serial_number}" if charger.serial_number
        else f"ID: {charger.id}"
    )
    return [f"Charger: {make}", identity] if make else [identity]

  header = [PERIOD_LABELS[period], "Energy (kWh)"]
  if with_cost:
    header += [f"Price (c{currency}/kWh)", f"Cost ({currency})"]
  content = table_pdf(
      "Energy report",
      [*charger_lines(),
       f"Created: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"],
      header,
      [cells(u["period"], u["energy_wh"], u["cost"]) for u in usage],
      cells("Total", sum(u["energy_wh"] for u in usage),
            sum(u["cost"] or 0.0 for u in usage) if with_cost else None),
  )
  return Response(
      content, media_type="application/pdf",
      headers={"Content-Disposition": f'attachment; filename="energy-per-{period}.pdf"'},
  )
