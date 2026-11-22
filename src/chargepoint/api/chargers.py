"""Chargers: list, settings, removal and the cable lock."""

import asyncio
from typing import TypedDict

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from ocpp.exceptions import OCPPError
from pydantic import BaseModel

from .. import store
from ..charger import connected_chargers, set_desired_lock
from .models import Model

router = APIRouter()


class ChargerUpdate(Model):
  lock_on_plugin: bool | None = None


class LockResult(BaseModel):
  ok: bool
  message: str


class ActiveSession(TypedDict):
  """A session in progress, with what it has used so far."""

  id: int
  connector_id: int
  id_tag: str
  tag_name: str
  started_at: str
  energy_wh: float | None  # live meter minus start reading; None if unknown
  cost: float | None  # energy so far at the price of the start day
  power_w: float | None  # current power, if the charger reports it
  currency: str


class ChargerOut(store.ChargerJSON):
  active_sessions: list[ActiveSession]


def active_sessions(s: store.ChargerState, found: list[store.SessionRecord]) -> list[ActiveSession]:
  price_list, currency = store.prices(), store.currency()
  result: list[ActiveSession] = []
  for session in found:
    none = store.ConnectorInfo()
    connector = s.connectors.get(session["connector_id"], none) if s.connected else none
    meter = connector.get("energy_wh")
    energy = None if meter is None else max(0.0, meter - session["meter_start_wh"])
    result.append({
        "id": session["id"],
        "connector_id": session["connector_id"],
        "id_tag": session["id_tag"],
        "tag_name": session["tag_name"],
        "started_at": session["started_at"],
        "energy_wh": energy,
        "cost": price_list.cost(energy, store.local_day(session["started_at"])),
        "power_w": connector.get("power_w"),
        "currency": currency,
    })
  return result


@router.get("/api/chargers")
async def list_chargers() -> list[ChargerOut]:
  """All chargers, with any sessions in progress."""
  by_charger: dict[str, list[store.SessionRecord]] = {}
  for session in store.open_sessions():
    by_charger.setdefault(session["charger_id"], []).append(session)
  return [
      {**s.to_json(), "active_sessions": active_sessions(s, by_charger.get(s.id, []))}
      for s in sorted(store.registry.values(), key=lambda s: s.id)
  ]


@router.post("/api/chargers/{cp_id}")
async def update_charger(cp_id: str, body: ChargerUpdate) -> store.ChargerJSON:
  s = store.registry.get(cp_id)
  if s is None:
    raise HTTPException(404, "Unknown charger")
  if body.lock_on_plugin is not None:
    s.lock_on_plugin = body.lock_on_plugin
  store.save_registry()
  return s.to_json()


@router.delete("/api/chargers/{cp_id}")
async def delete_charger(cp_id: str, delete_history: bool = False) -> dict[str, bool]:
  """Removes an offline charger, optionally with its sessions and energy history."""
  if cp_id in connected_chargers:
    raise HTTPException(409, "The charger is connected; it would reappear at once")
  try:
    store.delete_charger(cp_id, delete_history)
  except KeyError:
    raise HTTPException(404, "Unknown charger")
  return {"ok": True}


async def _apply_lock(cp_id: str, locked: bool) -> LockResult | JSONResponse:
  if cp_id not in store.registry:
    raise HTTPException(404, "Unknown charger")
  try:
    return LockResult(ok=True, message=await set_desired_lock(cp_id, locked))
  except (asyncio.TimeoutError, OCPPError) as e:
    # 504: charger did not answer; 502: charger answered with an error
    return JSONResponse(
        status_code=504 if isinstance(e, asyncio.TimeoutError) else 502,
        content={"ok": False, "message": store.registry[cp_id].last_message},
    )


@router.post("/api/chargers/{cp_id}/lock", response_model=LockResult)
async def lock_charger(cp_id: str) -> LockResult | JSONResponse:
  return await _apply_lock(cp_id, True)


@router.post("/api/chargers/{cp_id}/unlock", response_model=LockResult)
async def unlock_charger(cp_id: str) -> LockResult | JSONResponse:
  return await _apply_lock(cp_id, False)
