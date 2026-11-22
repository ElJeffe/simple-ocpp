"""HTTP API and web page (FastAPI). Docs are served at /docs.

All endpoints are async so they run on the same event loop as the OCPP
connections and can safely share state with them (no threads).
"""

import asyncio
import ipaddress
import re
from datetime import date, datetime

from fastapi import FastAPI, HTTPException, Query, Response
from ocpp.exceptions import OCPPError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import store
from .charger import clear_charger_caches, connected_chargers, set_desired_lock
from .config import STATIC_DIR

app = FastAPI(title="simple-ocpp")


class Model(BaseModel):
  model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class TagCreate(Model):
  id_tag: str = Field(min_length=1, max_length=20)  # OCPP 1.6 IdToken limit
  name: str = Field("", max_length=100)
  authorized: bool = True
  parent_id_tag: str = Field("", max_length=20)  # group; "" = none


class TagUpdate(Model):
  name: str | None = Field(None, max_length=100)
  authorized: bool | None = None
  parent_id_tag: str | None = Field(None, max_length=20)


class GroupCreate(Model):
  name: str = Field(min_length=1, max_length=20)  # sent as parentIdTag


HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$")


def check_host(value: str) -> str:
  """An IP address (0.0.0.0 = all IPv4 interfaces, :: = all) or a host name."""
  try:
    return str(ipaddress.ip_address(value))
  except ValueError:
    if HOSTNAME.match(value):
      return value
    raise ValueError("must be an IP address such as 0.0.0.0, or a host name")


class NetworkUpdate(Model):
  ocpp_host: str
  ocpp_port: int = Field(ge=1, le=65535)
  http_host: str
  http_port: int = Field(ge=1, le=65535)

  @field_validator("ocpp_host", "http_host")
  @classmethod
  def valid_host(cls, value: str) -> str:
    return check_host(value)

  @model_validator(mode="after")
  def different_ports(self) -> "NetworkUpdate":
    if self.ocpp_port == self.http_port:
      raise ValueError("the charger port and the web interface port must differ")
    return self


class FiscalYearUpdate(Model):
  month: int = Field(ge=1, le=12)
  day: int = Field(ge=1, le=31)


class InitialEnergyUpdate(Model):
  day: date
  energy_wh: float = Field(ge=0)

  @field_validator("day")
  @classmethod
  def not_in_future(cls, value: date) -> date:
    if value > date.today():
      raise ValueError("must not be in the future")
    return value


class ChargerUpdate(Model):
  lock_on_plugin: bool | None = None


class LockResult(BaseModel):
  ok: bool
  message: str


# --- Web page ---------------------------------------------------------------


def static_file(name: str) -> FileResponse:
  return FileResponse(STATIC_DIR / name, headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def index() -> FileResponse:
  return static_file("index.html")


# Other pages, and the files they share: URL path -> file in STATIC_DIR.
STATIC_PAGES = {
    "/tags": "tags.html",
    "/energy": "energy.html",
    "/sessions": "sessions.html",
    "/settings": "settings.html",
    "/style.css": "style.css",
    "/common.js": "common.js",
    "/app.js": "app.js",
}


def add_static_route(path: str, name: str) -> None:
  async def serve() -> FileResponse:
    return static_file(name)

  app.get(path, include_in_schema=False)(serve)


for _path, _name in STATIC_PAGES.items():
  add_static_route(_path, _name)


# --- Chargers ---------------------------------------------------------------


@app.get("/api/chargers")
async def list_chargers() -> list[store.ChargerJSON]:
  return [s.to_json() for s in sorted(store.registry.values(), key=lambda s: s.id)]


@app.post("/api/chargers/{cp_id}")
async def update_charger(cp_id: str, body: ChargerUpdate) -> store.ChargerJSON:
  s = store.registry.get(cp_id)
  if s is None:
    raise HTTPException(404, "Unknown charger")
  if body.lock_on_plugin is not None:
    s.lock_on_plugin = body.lock_on_plugin
  store.save_registry()
  return s.to_json()


@app.delete("/api/chargers/{cp_id}")
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


@app.post("/api/chargers/{cp_id}/lock", response_model=LockResult)
async def lock_charger(cp_id: str) -> LockResult | JSONResponse:
  return await _apply_lock(cp_id, True)


@app.post("/api/chargers/{cp_id}/unlock", response_model=LockResult)
async def unlock_charger(cp_id: str) -> LockResult | JSONResponse:
  return await _apply_lock(cp_id, False)


# --- RFID tags --------------------------------------------------------------


@app.get("/api/tags")
async def list_tags() -> list[store.TagRecord]:
  return store.sorted_tags()


@app.post("/api/tags")
async def register_tag(body: TagCreate) -> store.TagRecord:
  try:
    return store.add_tag(body.id_tag, body.name, body.authorized, body.parent_id_tag)
  except KeyError:
    raise HTTPException(400, "Tag is already registered")
  except ValueError as e:
    raise HTTPException(400, str(e))


@app.post("/api/tags/{id_tag}")
async def update_tag(id_tag: str, body: TagUpdate) -> store.TagRecord:
  try:
    tag, stale = store.update_tag(
        id_tag, body.name, body.authorized, body.parent_id_tag
    )
  except KeyError:
    raise HTTPException(404, "Unknown tag")
  except ValueError as e:
    raise HTTPException(400, str(e))
  if stale:
    clear_charger_caches()
  return tag


@app.delete("/api/tags/{id_tag}")
async def delete_tag(id_tag: str) -> dict[str, bool]:
  try:
    store.remove_tag(id_tag)
  except KeyError:
    raise HTTPException(404, "Unknown tag")
  clear_charger_caches()
  return {"ok": True}


# --- Tag groups ---------------------------------------------------------------


@app.get("/api/groups")
async def list_groups() -> list[store.GroupRecord]:
  return store.sorted_groups()


@app.post("/api/groups")
async def add_group(body: GroupCreate) -> store.GroupRecord:
  try:
    return store.add_group(body.name)
  except KeyError:
    raise HTTPException(400, "A group with that name already exists")


@app.delete("/api/groups/{name}")
async def delete_group(name: str) -> dict[str, int]:
  """Deletes a group; its tags are left without a group."""
  try:
    members = store.remove_group(name)
  except KeyError:
    raise HTTPException(404, "Unknown group")
  if members:
    clear_charger_caches()  # they may have cached the old parentIdTag
  return {"tags_ungrouped": len(members)}


# --- Settings -------------------------------------------------------------------


class NetworkInfo(BaseModel):
  saved: store.NetworkSettings  # as saved on the settings page
  active: store.NetworkSettings | None  # what the server listens on now
  next_start: store.NetworkSettings  # saved, with environment overrides
  overrides: dict[str, str]  # setting -> environment variable that sets it


class FiscalYearStart(BaseModel):
  month: int
  day: int


class SettingsInfo(BaseModel):
  network: NetworkInfo
  fiscal_year_start: FiscalYearStart


def settings_info() -> SettingsInfo:
  month, day = store.fiscal_year_start()
  return SettingsInfo(
      network=NetworkInfo(
          saved=store.saved_network_settings(),
          active=store.active_network,
          next_start=store.network_settings(),
          overrides=store.network_overrides(),
      ),
      fiscal_year_start=FiscalYearStart(month=month, day=day),
  )


@app.get("/api/settings")
async def get_settings() -> SettingsInfo:
  return settings_info()


@app.post("/api/settings/network")
async def update_network(body: NetworkUpdate) -> SettingsInfo:
  """Saved now, used from the next server start."""
  store.set_network_settings({
      "ocpp_host": body.ocpp_host, "ocpp_port": body.ocpp_port,
      "http_host": body.http_host, "http_port": body.http_port,
  })
  return settings_info()


@app.post("/api/settings/fiscal-year")
async def update_fiscal_year(body: FiscalYearUpdate) -> SettingsInfo:
  try:
    store.set_fiscal_year_start(body.month, body.day)
  except ValueError as e:
    raise HTTPException(400, str(e))
  return settings_info()


@app.get("/api/initial-energy")
async def list_initial_energy() -> dict[str, store.InitialEnergy]:
  return store.initial_energy()


@app.post("/api/chargers/{cp_id}/initial-energy")
async def set_initial_energy(cp_id: str, body: InitialEnergyUpdate) -> store.InitialEnergy:
  if cp_id not in store.registry:
    raise HTTPException(404, "Unknown charger")
  return store.set_initial_energy(cp_id, body.day, body.energy_wh)


@app.delete("/api/chargers/{cp_id}/initial-energy")
async def clear_initial_energy(cp_id: str) -> dict[str, bool]:
  if not store.clear_initial_energy(cp_id):
    raise HTTPException(404, "No initial energy set for this charger")
  return {"ok": True}


# --- Energy -------------------------------------------------------------------


@app.get("/api/energy")
async def energy(
    period: store.EnergyPeriod = "day", charger_id: str | None = None
) -> list[store.EnergyUsage]:
  """Energy used per day, month or year, by one charger or all of them."""
  return store.energy_usage(period, charger_id)


# --- Charge sessions --------------------------------------------------------


@app.get("/api/sessions")
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
