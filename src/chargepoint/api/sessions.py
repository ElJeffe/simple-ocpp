# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

"""Charge sessions."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Response

from .. import store
from .export import DECIMAL, CsvDecimal, csv_decimal, csv_response, local_time, number

router = APIRouter()


def check_zones(since: datetime | None, until: datetime | None) -> None:
  for name, value in (("since", since), ("until", until)):
    if value is not None and value.tzinfo is None:
      raise HTTPException(422, f"{name} needs a time zone, e.g. 2026-11-01T00:00:00Z")


SINCE = Query(None, description="Started at or after (with time zone)")
UNTIL = Query(None, description="Started before (with time zone)")
HIDE_EMPTY = Query(False, description="Leave out ended sessions that used no energy")


@router.get("/api/sessions")
async def list_sessions(
    response: Response,
    charger_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    since: datetime | None = SINCE,
    until: datetime | None = UNTIL,
    hide_empty: bool = HIDE_EMPTY,
) -> list[store.SessionRecord]:
  """Newest sessions first. X-Total-Count gives the number matching the filter."""
  check_zones(since, until)
  response.headers["X-Total-Count"] = str(
      store.count_sessions(charger_id, since, until, hide_empty)
  )
  return store.list_sessions(charger_id, limit, offset, since, until, hide_empty)


class Summary(store.SessionSummary):
  currency: str


@router.get("/api/sessions/summary")
async def sessions_summary(
    charger_id: str | None = None,
    since: datetime | None = SINCE,
    until: datetime | None = UNTIL,
    hide_empty: bool = HIDE_EMPTY,
) -> Summary:
  """Number, energy and cost of the sessions matching the filter."""
  check_zones(since, until)
  return {**store.session_summary(charger_id, since, until, hide_empty), "currency": store.currency()}


@router.get("/api/sessions.csv")
async def sessions_csv(
    charger_id: str | None = None,
    since: datetime | None = SINCE,
    until: datetime | None = UNTIL,
    decimal: CsvDecimal | None = DECIMAL,
    hide_empty: bool = HIDE_EMPTY,
) -> Response:
  """All sessions matching the filter, oldest first, as a CSV download.
  Times are in the server's time zone."""
  check_zones(since, until)
  sessions = reversed(store.list_sessions(charger_id, None, 0, since, until, hide_empty))
  currency = store.currency()
  separator = csv_decimal(decimal)

  def charger_name(charger_id: str) -> str:
    charger = store.registry.get(charger_id)
    return charger.name if charger else ""

  def status(s: store.SessionRecord) -> str:
    if not s["accepted"]:
      return "Refused"
    if s["stopped_at"] is None:
      return "Charging"
    if s["stop_reason"] == store.NO_STOP_RECEIVED:
      return "Ended (no stop received; energy estimated)"
    return s["stop_reason"] or "Ended"

  def minutes(s: store.SessionRecord) -> str:
    if not s["accepted"] or not s["stopped_at"] or s["stop_reason"] == store.NO_STOP_RECEIVED:
      return ""
    try:
      start, stop = datetime.fromisoformat(s["started_at"]), datetime.fromisoformat(s["stopped_at"])
    except ValueError:
      return ""
    return str(round((stop - start).total_seconds() / 60))

  rows = ([
      s["id"], s["charger_id"], s["connector_id"], s["id_tag"], s["tag_name"],
      local_time(s["started_at"]), local_time(s["stopped_at"]), minutes(s),
      number(None if s["energy_wh"] is None else s["energy_wh"] / 1000, 3, separator),
      number(s["cost"], 2, separator), status(s), s["stop_id_tag"] or "",
      charger_name(s["charger_id"]),
  ] for s in sessions)
  return csv_response("charge-sessions.csv", [
      "Transaction", "Charger", "Connector", "Tag", "Tag name", "Started", "Stopped",
      "Minutes", "Energy (kWh)", f"Cost ({currency})", "Status", "Stopped by tag",
      "Charger name",
  ], rows, separator)
