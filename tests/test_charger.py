# pyright: standard
"""OCPP message handlers, called directly without a network connection."""

import asyncio

from ocpp.v16.enums import AuthorizationStatus

from chargepoint import charger, store


async def test_authorize_unknown_tag_is_rejected_and_recorded(charge_point):
  result = await charge_point.on_authorize(id_tag="abc")
  assert result.id_tag_info.status == AuthorizationStatus.invalid
  assert store.registry["CP_1"].last_rfid == "abc"
  assert store.registry["CP_1"].last_rfid_at


async def test_authorize_registered_tag(charge_point):
  store.add_tag("ABC")
  result = await charge_point.on_authorize(id_tag="abc")
  assert result.id_tag_info.status == AuthorizationStatus.accepted


async def test_authorize_blocked_tag(charge_point):
  store.add_tag("abc", authorized=False)
  result = await charge_point.on_authorize(id_tag="abc")
  assert result.id_tag_info.status == AuthorizationStatus.blocked


async def start(charge_point, id_tag):
  return await charge_point.on_start_transaction(
      connector_id=1, id_tag=id_tag, meter_start=0, timestamp="2026-01-01T00:00:00Z"
  )


async def test_start_with_unknown_or_empty_tag_is_refused_and_stopped(
    charge_point, monkeypatch
):
  stopped = []

  async def fake_remote_stop(transaction_id):
    stopped.append(transaction_id)

  monkeypatch.setattr(charge_point, "remote_stop", fake_remote_stop)
  for id_tag in ("unknown", ""):
    result = await start(charge_point, id_tag)
    assert result.id_tag_info.status == AuthorizationStatus.invalid
  await asyncio.sleep(0)  # let the scheduled stops run
  assert len(stopped) == 2


async def test_start_with_authorized_tag(charge_point, monkeypatch):
  stopped = []

  async def fake_remote_stop(transaction_id):
    stopped.append(transaction_id)

  monkeypatch.setattr(charge_point, "remote_stop", fake_remote_stop)
  store.add_tag("abc")
  result = await start(charge_point, "ABC")
  assert result.id_tag_info.status == AuthorizationStatus.accepted
  await asyncio.sleep(0)
  assert stopped == []


async def test_transaction_ids_are_unique(charge_point):
  store.add_tag("abc")
  ids = {(await start(charge_point, "abc")).transaction_id for _ in range(3)}
  assert len(ids) == 3


async def test_transactions_are_stored_as_sessions(charge_point, monkeypatch):
  monkeypatch.setattr(charge_point, "remote_stop", lambda tid: asyncio.sleep(0))
  store.add_tag("abc", "Car")
  ok = await start(charge_point, "abc")
  refused = await start(charge_point, "stranger")
  await charge_point.on_stop_transaction(
      meter_stop=7000, timestamp="2026-01-01T02:00:00Z",
      transaction_id=ok.transaction_id, reason="Local",
  )
  newest, oldest = store.list_sessions()
  assert (oldest["id"], oldest["accepted"], oldest["energy_wh"]) == (
      ok.transaction_id, True, 7000
  )
  assert oldest["stop_reason"] == "Local"
  assert (newest["id"], newest["accepted"], newest["energy_wh"]) == (
      refused.transaction_id, False, None
  )


async def test_stop_transaction_records_tag(charge_point):
  await charge_point.on_stop_transaction(
      meter_stop=10, timestamp="t", transaction_id=1, id_tag="xyz"
  )
  assert store.registry["CP_1"].last_rfid == "xyz"


async def test_stop_transaction_without_tag_keeps_previous(charge_point):
  await charge_point.on_authorize(id_tag="abc")
  await charge_point.on_stop_transaction(meter_stop=10, timestamp="t", transaction_id=1)
  assert store.registry["CP_1"].last_rfid == "abc"


async def test_meter_values_keeps_totals_and_converts_units(charge_point):
  await charge_point.on_meter_values(
      connector_id=1,
      meter_value=[{
          "timestamp": "t",
          "sampled_value": [
              {"value": " 2.5", "measurand": "Power.Active.Import", "unit": "kW"},
              {"value": "161.9", "measurand": "Energy.Active.Import.Register", "unit": "kWh"},
              {"value": "999", "measurand": "Power.Active.Import", "unit": "W", "phase": "L1"},
              {"value": "16", "measurand": "Current.Import", "unit": "A"},
              {"value": "garbage", "measurand": "Power.Active.Import", "unit": "W"},
          ],
      }],
  )
  c = store.registry["CP_1"].connectors[1]
  assert c.get("power_w") == 2500
  assert c.get("energy_wh") == 161900


async def test_meter_value_without_measurand_is_energy(charge_point):
  await charge_point.on_meter_values(
      connector_id=1, meter_value=[{"timestamp": "t", "sampled_value": [{"value": "5"}]}]
  )
  assert store.registry["CP_1"].connectors[1].get("energy_wh") == 5


async def test_status_and_meter_values_do_not_overwrite_each_other(charge_point):
  await charge_point.on_meter_values(
      connector_id=1,
      meter_value=[{"timestamp": "t", "sampled_value": [
          {"value": "100", "measurand": "Power.Active.Import", "unit": "W"}]}],
  )
  await charge_point.on_status_notification(
      connector_id=1, error_code="NoError", status="Charging"
  )
  c = store.registry["CP_1"].connectors[1]
  assert c.get("status") == "Charging" and c.get("power_w") == 100


async def test_boot_notification_stores_vendor(charge_point, monkeypatch):
  async def no_settings():
    pass

  monkeypatch.setattr(charge_point, "apply_settings_after_boot", no_settings)
  result = await charge_point.on_boot_notification(
      charge_point_vendor="SolarEdge", charge_point_model="ONE"
  )
  assert result.status == "Accepted"
  assert store.registry["CP_1"].vendor == "SolarEdge"


async def test_set_desired_lock_offline_is_saved_for_later():
  msg = await charger.set_desired_lock("CP_OFF", False)
  assert "offline" in msg
  store.registry.clear()
  store.load_registry()
  assert store.registry["CP_OFF"].desired_locked is False


async def test_plug_in_locks_only_on_transition(charge_point, monkeypatch):
  locks = []

  async def fake_set_cable_lock(locked):
    locks.append(locked)
    return ""

  monkeypatch.setattr(charge_point, "set_cable_lock", fake_set_cable_lock)
  for status in ("Available", "Preparing", "Charging", "SuspendedEV", "Finishing",
                 "Available", "Charging"):
    await charge_point.on_status_notification(
        connector_id=1, error_code="NoError", status=status
    )
  await charge_point.on_status_notification(
      connector_id=0, error_code="NoError", status="Preparing"
  )  # the charger as a whole, not a connector
  await asyncio.sleep(0)
  assert locks == [True, True]  # Available->Preparing and Available->Charging


async def test_plug_in_respects_setting(charge_point, monkeypatch):
  locks = []

  async def fake_set_cable_lock(locked):
    locks.append(locked)
    return ""

  monkeypatch.setattr(charge_point, "set_cable_lock", fake_set_cable_lock)
  store.get_state("CP_1").lock_on_plugin = False
  await charge_point.on_status_notification(
      connector_id=1, error_code="NoError", status="Preparing"
  )
  await asyncio.sleep(0)
  assert locks == []


async def test_replies_include_group(charge_point, monkeypatch):
  monkeypatch.setattr(charge_point, "remote_stop", lambda tid: asyncio.sleep(0))
  store.add_group("Family")
  store.add_tag("a", parent_id_tag="Family")
  store.add_tag("b", parent_id_tag="family")
  store.add_tag("solo")
  auth = await charge_point.on_authorize(id_tag="b")
  assert auth.id_tag_info.parent_id_tag == "Family"
  started = await start(charge_point, "a")
  assert started.id_tag_info.parent_id_tag == "Family"
  assert (await charge_point.on_authorize(id_tag="solo")).id_tag_info.parent_id_tag is None
  stopped = await charge_point.on_stop_transaction(
      meter_stop=10, timestamp="t", transaction_id=started.transaction_id, id_tag="b"
  )
  assert stopped.id_tag_info.status == AuthorizationStatus.accepted
  assert stopped.id_tag_info.parent_id_tag == "Family"
  assert store.list_sessions()[0]["stop_id_tag"] == "b"


async def test_stop_without_tag_has_no_tag_info(charge_point):
  result = await charge_point.on_stop_transaction(
      meter_stop=10, timestamp="t", transaction_id=1
  )
  assert result.id_tag_info is None


async def test_meter_readings_are_logged_for_energy(charge_point, monkeypatch):
  monkeypatch.setattr(charge_point, "remote_stop", lambda tid: asyncio.sleep(0))
  store.add_tag("abc")
  started = await charge_point.on_start_transaction(
      connector_id=2, id_tag="abc", meter_start=1000, timestamp="t"
  )
  await charge_point.on_meter_values(connector_id=2, meter_value=[{
      "timestamp": "t", "sampled_value": [{"value": "1.5", "unit": "kWh"}]}])
  with store.db() as conn:
    assert conn.execute("SELECT energy_wh FROM energy_readings").fetchone()[0] == 1500
  await charge_point.on_stop_transaction(
      meter_stop=2500, timestamp="t", transaction_id=started.transaction_id
  )
  with store.db() as conn:
    rows = conn.execute("SELECT charger_id, connector_id, energy_wh FROM energy_readings").fetchall()
  assert [tuple(r) for r in rows] == [("CP_1", 2, 2500)]


async def test_power_only_meter_values_are_not_logged(charge_point):
  await charge_point.on_meter_values(connector_id=1, meter_value=[{
      "timestamp": "t", "sampled_value": [{"value": "100", "measurand": "Power.Active.Import"}]}])
  with store.db() as conn:
    assert conn.execute("SELECT COUNT(*) FROM energy_readings").fetchone()[0] == 0


async def test_start_and_boot_close_sessions_without_a_stop(charge_point, monkeypatch):
  async def nothing():
    pass

  monkeypatch.setattr(charge_point, "apply_settings_after_boot", nothing)
  store.add_tag("abc")
  first = await charge_point.on_start_transaction(
      connector_id=1, id_tag="abc", meter_start=1000, timestamp="2026-11-01T10:00:00Z")
  second = await charge_point.on_start_transaction(
      connector_id=1, id_tag="abc", meter_start=3000, timestamp="2026-11-01T14:00:00Z")
  by_id = {s["id"]: s for s in store.list_sessions()}
  assert by_id[first.transaction_id]["stop_reason"] == store.NO_STOP_RECEIVED
  assert by_id[first.transaction_id]["energy_wh"] == 2000
  assert by_id[second.transaction_id]["stopped_at"] is None
  await charge_point.on_boot_notification(charge_point_vendor="V", charge_point_model="M")
  assert store.list_sessions()[0]["stop_reason"] == store.NO_STOP_RECEIVED
