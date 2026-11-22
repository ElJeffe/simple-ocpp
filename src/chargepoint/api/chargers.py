"""Chargers: list, settings, removal and the cable lock."""

import asyncio

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


@router.get("/api/chargers")
async def list_chargers() -> list[store.ChargerJSON]:
  return [s.to_json() for s in sorted(store.registry.values(), key=lambda s: s.id)]


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
