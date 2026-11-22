"""The settings page: network, fiscal year and initial energy."""

import asyncio
import ipaddress
import re
import socket
from datetime import date

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from .. import lifecycle, store
from .models import Model

router = APIRouter()


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
  currency: str
  tariffs: list[store.Tariff]


def settings_info() -> SettingsInfo:
  month, day = store.fiscal_year_start()
  return SettingsInfo(
      network=NetworkInfo(
          saved=store.saved_network_settings(),
          active=store.active_network(),
          next_start=store.network_settings(),
          overrides=store.network_overrides(),
      ),
      fiscal_year_start=FiscalYearStart(month=month, day=day),
      currency=store.currency(),
      tariffs=store.tariffs(),
  )


class LocalisationUpdate(Model):
  time_format: store.TimeFormat
  decimal_separator: store.DecimalSeparator
  currency: str = Field(min_length=1, max_length=5)


@router.get("/api/localisation")
async def get_localisation() -> store.Localisation:
  return store.localisation()


@router.post("/api/localisation")
async def update_localisation(body: LocalisationUpdate) -> store.Localisation:
  store.set_localisation({
      "time_format": body.time_format,
      "decimal_separator": body.decimal_separator,
      "currency": body.currency,
  })
  return store.localisation()


@router.get("/api/settings")
async def get_settings() -> SettingsInfo:
  return settings_info()


RESTART_DELAY = 0.5  # seconds, so the answer reaches the browser first


def check_can_listen(host: str, port: int) -> str | None:
  """Why the server could not listen on host:port, or None if it can."""
  try:
    for family, kind, proto, _, address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    ):
      with socket.socket(family, kind, proto) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(address)
  except OSError as e:
    return f"{host}:{port} is not available ({e.strerror or e})"
  return None


@router.post("/api/restart")
async def restart() -> NetworkInfo:
  """Restarts the server to apply saved network settings.

  Refused when a new address or port can't be opened, so a mistake doesn't
  leave the server unable to start. Returns where it will listen.
  """
  if not lifecycle.can_restart():
    raise HTTPException(409, "This server can't restart itself; restart it by hand")
  info = settings_info().network
  active, upcoming = info.active, info.next_start
  listeners = [  # (new address, address in use now)
      ((upcoming["ocpp_host"], upcoming["ocpp_port"]),
       (active["ocpp_host"], active["ocpp_port"]) if active else None),
      ((upcoming["http_host"], upcoming["http_port"]),
       (active["http_host"], active["http_port"]) if active else None),
  ]
  for new, now in listeners:
    if new == now:
      continue  # in use by this server, and free again after the restart
    if problem := check_can_listen(*new):
      raise HTTPException(409, f"Not restarted: {problem}")
  asyncio.get_running_loop().call_later(RESTART_DELAY, lifecycle.request_restart)
  return info


@router.get("/api/backup", response_class=Response)
async def download_backup() -> Response:
  """A consistent copy of the whole database, as a file download."""
  name = f"simple-ocpp-{date.today().isoformat()}.db"
  return Response(
      store.backup_bytes(), media_type="application/vnd.sqlite3",
      headers={"Content-Disposition": f'attachment; filename="{name}"'},
  )


@router.post("/api/settings/network")
async def update_network(body: NetworkUpdate) -> SettingsInfo:
  """Saved now, used from the next server start."""
  store.set_network_settings({
      "ocpp_host": body.ocpp_host, "ocpp_port": body.ocpp_port,
      "http_host": body.http_host, "http_port": body.http_port,
  })
  return settings_info()


@router.post("/api/settings/fiscal-year")
async def update_fiscal_year(body: FiscalYearUpdate) -> SettingsInfo:
  try:
    store.set_fiscal_year_start(body.month, body.day)
  except ValueError as e:
    raise HTTPException(400, str(e))
  return settings_info()


@router.get("/api/initial-energy")
async def list_initial_energy() -> dict[str, store.InitialEnergy]:
  return store.initial_energy()


@router.post("/api/chargers/{cp_id}/initial-energy")
async def set_initial_energy(cp_id: str, body: InitialEnergyUpdate) -> store.InitialEnergy:
  if cp_id not in store.registry:
    raise HTTPException(404, "Unknown charger")
  return store.set_initial_energy(cp_id, body.day, body.energy_wh)


@router.delete("/api/chargers/{cp_id}/initial-energy")
async def clear_initial_energy(cp_id: str) -> dict[str, bool]:
  if not store.clear_initial_energy(cp_id):
    raise HTTPException(404, "No initial energy set for this charger")
  return {"ok": True}
