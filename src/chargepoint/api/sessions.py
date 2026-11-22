"""Charge sessions."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Response

from .. import store

router = APIRouter()


@router.get("/api/sessions")
async def list_sessions(
    response: Response,
    charger_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    since: datetime | None = Query(None, description="Started at or after"),
    until: datetime | None = Query(None, description="Started before"),
) -> list[store.SessionRecord]:
  """Newest sessions first. X-Total-Count gives the number matching the filter."""
  for name, value in (("since", since), ("until", until)):
    if value is not None and value.tzinfo is None:
      raise HTTPException(422, f"{name} needs a time zone, e.g. 2026-11-01T00:00:00Z")
  response.headers["X-Total-Count"] = str(store.count_sessions(charger_id, since, until))
  return store.list_sessions(charger_id, limit, offset, since, until)
