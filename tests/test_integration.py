# pyright: standard
"""End to end: a simulated charger talks to the real OCPP server over a
websocket on a random local port, while the test drives the HTTP API."""

import asyncio

import httpx
import pytest
import websockets
from ocpp.exceptions import NotSupportedError
from ocpp.routing import on
from ocpp.v16 import ChargePoint, call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    ClearCacheStatus,
    ConfigurationStatus,
    Reason,
    RemoteStartStopStatus,
    UnlockStatus,
)
from websockets.typing import Subprotocol

from chargepoint import api, charger, store

OCPP16 = [Subprotocol("ocpp1.6")]


class SimulatedCharger(ChargePoint):
  """Answers the commands the server sends and records them."""

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.config = {"UnlockConnectorOnEVSideDisconnect": "true"}
    self.received = []
    self.refuse_configuration = False

  @on(Action.change_configuration)
  def on_change_configuration(self, key, value):
    if self.refuse_configuration:
      raise NotSupportedError(description="Not on this firmware")
    self.received.append(("ChangeConfiguration", key, value))
    self.config[key] = value
    return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)

  @on(Action.get_configuration)
  def on_get_configuration(self, key=None):
    return call_result.GetConfiguration(configuration_key=[
        {"key": k, "readonly": False, "value": v}
        for k, v in self.config.items() if not key or k in key
    ])

  @on(Action.unlock_connector)
  def on_unlock_connector(self, connector_id):
    self.received.append(("UnlockConnector", connector_id))
    return call_result.UnlockConnector(status=UnlockStatus.unlocked)

  @on(Action.remote_stop_transaction)
  def on_remote_stop(self, transaction_id):
    self.received.append(("RemoteStopTransaction", transaction_id))
    return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)

  @on(Action.clear_cache)
  def on_clear_cache(self):
    self.received.append(("ClearCache",))
    return call_result.ClearCache(status=ClearCacheStatus.accepted)


@pytest.fixture
async def server():
  srv = await websockets.serve(
      charger.on_connect, "127.0.0.1", 0, subprotocols=OCPP16
  )
  yield srv.sockets[0].getsockname()[1]
  srv.close()
  await srv.wait_closed()


@pytest.fixture
async def sim(server):
  async with websockets.connect(
      f"ws://127.0.0.1:{server}/CP_SIM", subprotocols=OCPP16
  ) as ws:
    cp = SimulatedCharger("CP_SIM", ws)
    task = asyncio.create_task(cp.start())
    yield cp
    task.cancel()


@pytest.fixture
async def client():
  transport = httpx.ASGITransport(app=api.app)
  async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
    yield c


async def wait_for(condition, timeout=5):
  async with asyncio.timeout(timeout):
    while not condition():
      await asyncio.sleep(0.05)


async def test_connect_and_disconnect_tracked(server):
  async with websockets.connect(
      f"ws://127.0.0.1:{server}/CP_X", subprotocols=OCPP16
  ) as ws:
    task = asyncio.create_task(SimulatedCharger("CP_X", ws).start())
    await wait_for(lambda: "CP_X" in charger.connected_chargers)
    assert store.registry["CP_X"].connected
    task.cancel()
  await wait_for(lambda: "CP_X" not in charger.connected_chargers)
  assert not store.registry["CP_X"].connected


async def test_boot_sets_stop_on_invalid_id_but_not_lock(sim):
  await sim.call(call.BootNotification(charge_point_vendor="V", charge_point_model="M"))
  await wait_for(lambda: ("ChangeConfiguration", "StopTransactionOnInvalidId", "true") in sim.received)
  assert not any(r[:2] == ("ChangeConfiguration", "UnlockConnectorOnEVSideDisconnect")
                 for r in sim.received)


async def plug_in(sim, status=ChargePointStatus.preparing):
  await sim.call(call.StatusNotification(
      connector_id=1, error_code=ChargePointErrorCode.no_error, status=status,
  ))


async def test_plugging_in_locks_cable(sim):
  store.get_state("CP_SIM").desired_locked = False
  await plug_in(sim, ChargePointStatus.available)
  await plug_in(sim)
  await wait_for(lambda: store.registry["CP_SIM"].lock_config == "locked")
  assert ("ChangeConfiguration", "UnlockConnectorOnEVSideDisconnect", "false") in sim.received
  assert store.registry["CP_SIM"].desired_locked is True


async def test_connecting_locks_cable_when_enabled(server, monkeypatch):
  monkeypatch.setattr(charger, "CONNECT_LOCK_DELAY", 0)
  store.get_state("CP_ON").desired_locked = False
  store.get_state("CP_OFF").lock_on_plugin = False
  sims = {}
  for cp_id in ("CP_ON", "CP_OFF"):
    ws = await websockets.connect(f"ws://127.0.0.1:{server}/{cp_id}", subprotocols=OCPP16)
    sims[cp_id] = (SimulatedCharger(cp_id, ws), ws)
    asyncio.create_task(sims[cp_id][0].start())
  await wait_for(lambda: store.registry["CP_ON"].lock_config == "locked")
  assert store.registry["CP_ON"].desired_locked is True
  await asyncio.sleep(0.2)
  assert sims["CP_OFF"][0].received == []
  for _, ws in sims.values():
    await ws.close()


async def test_plugging_in_does_nothing_when_disabled(sim, client):
  await plug_in(sim, ChargePointStatus.available)
  r = await client.post("/api/chargers/CP_SIM", json={"lock_on_plugin": False})
  assert r.json()["lock_on_plugin"] is False
  await plug_in(sim)
  await asyncio.sleep(0.2)
  assert sim.received == []


async def test_rfid_registration_flow(sim, client):
  r = await sim.call(call.Authorize(id_tag="card1"))
  assert r.id_tag_info["status"] == AuthorizationStatus.invalid

  # The operator sees the tag in the UI and registers it
  chargers = (await client.get("/api/chargers")).json()
  assert chargers[0]["last_rfid"] == "card1"
  await client.post("/api/tags", json={"id_tag": "card1", "name": "Car"})

  r = await sim.call(call.Authorize(id_tag="CARD1"))
  assert r.id_tag_info["status"] == AuthorizationStatus.accepted

  await client.post("/api/tags/card1", json={"authorized": False})
  await wait_for(lambda: ("ClearCache",) in sim.received)
  r = await sim.call(call.Authorize(id_tag="card1"))
  assert r.id_tag_info["status"] == AuthorizationStatus.blocked


async def test_refused_transaction_is_stopped_remotely(sim):
  r = await sim.call(call.StartTransaction(
      connector_id=1, id_tag="stranger", meter_start=0, timestamp="2026-01-01T00:00:00Z"
  ))
  assert r.id_tag_info["status"] == AuthorizationStatus.invalid
  await wait_for(lambda: ("RemoteStopTransaction", r.transaction_id) in sim.received)


async def test_unlock_via_api_releases_cable(sim, client):
  await sim.call(call.StatusNotification(
      connector_id=1,
      error_code=ChargePointErrorCode.no_error,
      status=ChargePointStatus.available,
  ))
  r = await client.post("/api/chargers/CP_SIM/unlock")
  assert r.status_code == 200, r.text
  assert "released" in r.json()["message"]
  assert ("ChangeConfiguration", "UnlockConnectorOnEVSideDisconnect", "true") in sim.received
  assert ("UnlockConnector", 1) in sim.received
  assert store.registry["CP_SIM"].lock_config == "unlocked"
  assert store.registry["CP_SIM"].desired_locked is False


async def test_charger_error_reply_gives_502_not_crash(sim, client):
  sim.refuse_configuration = True
  r = await client.post("/api/chargers/CP_SIM/lock")
  assert r.status_code == 502
  assert r.json() == {
      "ok": False,
      "message": "Charger refused lock request: Not on this firmware",
  }
  assert store.registry["CP_SIM"].lock_busy is False


async def test_charge_session_is_recorded(sim, client):
  await client.post("/api/tags", json={"id_tag": "card1", "name": "Car"})
  r = await sim.call(call.StartTransaction(
      connector_id=1, id_tag="card1", meter_start=100, timestamp="2026-01-01T00:00:00Z"
  ))
  assert r.id_tag_info["status"] == AuthorizationStatus.accepted
  await sim.call(call.StopTransaction(
      meter_stop=5100, timestamp="2026-01-01T01:00:00Z",
      transaction_id=r.transaction_id, reason=Reason.ev_disconnected,
  ))
  [s] = (await client.get("/api/sessions")).json()
  assert s["id"] == r.transaction_id
  assert (s["charger_id"], s["tag_name"], s["energy_wh"]) == ("CP_SIM", "Car", 5000)


async def test_group_member_stops_session(sim, client):
  await client.post("/api/groups", json={"name": "Family"})
  await client.post("/api/tags", json={"id_tag": "dad", "parent_id_tag": "Family"})
  await client.post("/api/tags", json={"id_tag": "mum", "parent_id_tag": "family"})
  r = await sim.call(call.StartTransaction(
      connector_id=1, id_tag="dad", meter_start=0, timestamp="2026-01-01T00:00:00Z"
  ))
  assert r.id_tag_info["parent_id_tag"] == "Family"
  # A compliant charger compares this with the parentIdTag from the start
  auth = await sim.call(call.Authorize(id_tag="mum"))
  assert auth.id_tag_info["parent_id_tag"] == "Family"
  stop = await sim.call(call.StopTransaction(
      meter_stop=1000, timestamp="2026-01-01T01:00:00Z",
      transaction_id=r.transaction_id, id_tag="mum", reason=Reason.local,
  ))
  assert stop.id_tag_info["parent_id_tag"] == "Family"
  [s] = (await client.get("/api/sessions")).json()
  assert (s["id_tag"], s["stop_id_tag"], s["stop_reason"]) == ("dad", "mum", "Local")
