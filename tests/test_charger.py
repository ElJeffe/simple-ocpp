# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

# pyright: standard
"""OCPP message handlers, called directly without a network connection."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from ocpp.exceptions import NotSupportedError
from ocpp.v16 import call_result
from ocpp.v16.enums import AuthorizationStatus, ConfigurationStatus

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
  ids = set()
  for meter_start in range(3):
    result = await charge_point.on_start_transaction(
        connector_id=1, id_tag="abc", meter_start=meter_start,
        timestamp="2026-01-01T00:00:00Z",
    )
    ids.add(result.transaction_id)
  assert len(ids) == 3


async def test_start_after_a_real_stop_is_new_even_with_a_frozen_clock(charge_point):
  store.add_tag("abc")
  first = await start(charge_point, "abc")
  await charge_point.on_stop_transaction(
      meter_stop=0, timestamp="2026-01-01T00:00:00Z", transaction_id=first.transaction_id
  )
  second = await start(charge_point, "abc")
  assert second.transaction_id != first.transaction_id


async def test_repeated_start_after_a_reboot_keeps_its_transaction(charge_point):
  store.add_tag("abc")
  first = await start(charge_point, "abc")
  store.close_open_sessions("CP_1", "2026-01-01T00:01:00Z")  # as a boot does
  again = await start(charge_point, "abc")
  assert again.transaction_id == first.transaction_id
  assert [s["stopped_at"] for s in store.list_sessions("CP_1")] == [None]


async def test_meter_values_reopen_a_transaction_that_survived_a_boot(charge_point, monkeypatch):
  async def nothing():
    pass

  monkeypatch.setattr(charge_point, "apply_settings_after_boot", nothing)
  store.add_tag("abc")
  first = await start(charge_point, "abc")
  await charge_point.on_boot_notification(charge_point_vendor="V", charge_point_model="M")
  assert store.list_sessions()[0]["stop_reason"] == store.NO_STOP_RECEIVED
  await charge_point.on_meter_values(
      connector_id=1, transaction_id=first.transaction_id,
      meter_value=[{"timestamp": "2026-01-01T01:00:00Z", "sampled_value": [{"value": "1500"}]}],
  )
  s = store.list_sessions()[0]
  assert (s["stopped_at"], s["stop_reason"], s["energy_wh"]) == (None, None, None)
  await charge_point.on_stop_transaction(
      meter_stop=2000, timestamp="2026-01-01T02:00:00Z",
      transaction_id=first.transaction_id, reason="EVDisconnected",
  )
  assert store.list_sessions()[0]["energy_wh"] == 2000


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
  charge_point.state.cable_lock = True
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


def levels(caplog, text):
  return [r.levelname for r in caplog.records if text in r.getMessage()]


async def test_status_log_level_depends_on_the_change(charge_point, caplog):
  from chargepoint.logbuffer import TRACE
  caplog.set_level(TRACE)
  status = charge_point.on_status_notification
  await status(connector_id=2, error_code="NoError", status="Available")
  await status(connector_id=2, error_code="NoError", status="Available")
  await status(connector_id=2, error_code="HighTemperature", status="Available")
  await status(connector_id=2, error_code="GroundFailure", status="Faulted")
  assert levels(caplog, "Connector 2 status") == ["INFO", "TRACE", "WARNING", "ERROR"]


async def test_rfid_and_transaction_log_levels(charge_point, caplog, monkeypatch):
  import logging
  caplog.set_level(logging.DEBUG)
  monkeypatch.setattr(charge_point, "remote_stop", lambda tid: asyncio.sleep(0))
  store.add_tag("good")
  await charge_point.on_authorize(id_tag="good")
  await charge_point.on_authorize(id_tag="stranger")
  assert levels(caplog, "RFID good: Accepted") == ["INFO"]
  assert levels(caplog, "RFID stranger: Invalid") == ["WARNING"]
  await start(charge_point, "stranger")
  assert levels(caplog, "Refusing transaction") == ["WARNING"]
  assert levels(caplog, "started on connector") == []
  await start(charge_point, "good")
  assert levels(caplog, "started on connector") == ["INFO"]


async def test_failed_remote_stop_is_an_error(charge_point, caplog, monkeypatch):
  async def fail(*args):
    raise TimeoutError("no answer")

  monkeypatch.setattr(charger.asyncio, "sleep", lambda _: _no_wait())
  monkeypatch.setattr(charge_point, "request", fail)
  await charge_point.remote_stop(7)
  assert levels(caplog, "Could not stop refused transaction 7") == ["ERROR"]


async def _no_wait():
  pass


@pytest.mark.parametrize("path, expected", [
    ("/CP_1", "CP_1"),
    ("/CP_1/", "CP_1"),
    ("/CP_1?token=x", "CP_1"),
    ("/CP%201", "CP 1"),
    ("/a%2Fb", "a%2Fb"),        # would break the API's URLs
    ("/CP%0Afake", "CP%0Afake"),  # would forge log lines
    ("/", "UNKNOWN_CP"),
    ("/?x=1", "UNKNOWN_CP"),
    ("", "UNKNOWN_CP"),
])
def test_charger_id_from_path(path, expected):
  assert charger.charger_id_from_path(path) == expected


@pytest.mark.parametrize("known", ["CP%201", "CP_1?x=1"])
def test_known_charger_keeps_its_literal_id(known):
  store.get_state(known)
  assert charger.charger_id_from_path(f"/{known}") == known


@pytest.fixture
def toggle_setup(charge_point, monkeypatch):
  """A connected charger with an empty connector, recording lock requests."""
  calls = []

  async def fake_lock(locked):
    calls.append(locked)
    charge_point.state.lock_config = "locked" if locked else "unlocked"
    return "ok"

  monkeypatch.setattr(charge_point, "set_cable_lock", fake_lock)
  monkeypatch.setattr(charger, "TOGGLE_DELAY", 0)
  charger.connected_chargers["CP_1"] = charge_point
  charge_point.state.connectors = {0: {"status": "Available"}, 1: {"status": "Available"}}
  charge_point.state.lock_config = "locked"
  charge_point.state.cable_lock = True
  store.add_tag("toggle", toggle_lock=True)
  return charge_point, calls


async def present(charge_point, id_tag="toggle"):
  result = await charge_point.on_authorize(id_tag=id_tag)
  await asyncio.sleep(0.01)
  return result


async def test_toggle_tag_toggles_the_lock_both_ways(toggle_setup):
  charge_point, calls = toggle_setup
  result = await present(charge_point)
  assert result.id_tag_info.status == AuthorizationStatus.accepted
  assert calls == [False]
  charge_point.last_toggle = -charger.TOGGLE_COOLDOWN  # the cooldown has passed
  await present(charge_point)
  assert calls == [False, True]
  assert store.registry["CP_1"].desired_locked is True


async def test_cable_lock_off_stops_toggle_and_auto_lock(toggle_setup):
  charge_point, calls = toggle_setup
  charge_point.state.cable_lock = False
  result = await present(charge_point)
  assert result.id_tag_info.status == AuthorizationStatus.accepted
  assert calls == [] and store.tags["TOGGLE"]["toggle_lock"] is True
  assert not charge_point.should_auto_lock()
  charge_point.state.connectors[1] = {"status": "Available"}
  await charge_point.on_status_notification(1, "NoError", "Preparing")
  await asyncio.sleep(0.01)
  assert calls == []


def fake_config_answer(charge_point, monkeypatch, answer):
  sent = []

  async def fake_request(payload, result_type):
    sent.append(payload)
    if isinstance(answer, Exception):
      raise answer
    return call_result.ChangeConfiguration(status=answer)

  monkeypatch.setattr(charge_point, "request", fake_request)
  return sent


async def test_turning_cable_lock_off_releases_without_ending_the_session(
    toggle_setup, monkeypatch
):
  charge_point, _ = toggle_setup
  sent = fake_config_answer(charge_point, monkeypatch, ConfigurationStatus.accepted)
  charger.set_cable_lock_enabled("CP_1", False)
  await asyncio.sleep(0.01)
  assert [(p.key, p.value) for p in sent] == [(charger.LOCK_KEY, "true")]  # no UnlockConnector
  state = store.registry["CP_1"]
  assert not state.cable_lock and not state.desired_locked and not state.lock_release_pending
  store.registry.clear()
  store.load_registry()
  assert store.registry["CP_1"].cable_lock is False


@pytest.mark.parametrize("answer, pending", [
    (ConfigurationStatus.rejected, False),
    (ConfigurationStatus.not_supported, False),
    (NotSupportedError(description="no"), False),
    (asyncio.TimeoutError(), True),
])
async def test_release_is_only_retried_if_the_charger_did_not_answer(
    toggle_setup, monkeypatch, answer, pending
):
  charge_point, _ = toggle_setup
  fake_config_answer(charge_point, monkeypatch, answer)
  charger.set_cable_lock_enabled("CP_1", False)
  await asyncio.sleep(0.01)
  assert store.registry["CP_1"].lock_release_pending is pending


async def test_release_is_sent_when_an_offline_charger_connects(toggle_setup, monkeypatch):
  charge_point, _ = toggle_setup
  sent = fake_config_answer(charge_point, monkeypatch, ConfigurationStatus.accepted)
  del charger.connected_chargers["CP_1"]
  charger.set_cable_lock_enabled("CP_1", False)
  await asyncio.sleep(0.01)
  assert sent == [] and store.registry["CP_1"].lock_release_pending
  charger.connected_chargers["CP_1"] = charge_point
  monkeypatch.setattr(charger, "CONNECT_LOCK_DELAY", 0)
  await charge_point.lock_after_connect()
  assert len(sent) == 1 and not store.registry["CP_1"].lock_release_pending


async def test_new_charger_is_left_alone_until_cable_lock_is_on(charge_point, monkeypatch):
  sent = fake_config_answer(charge_point, monkeypatch, ConfigurationStatus.accepted)
  monkeypatch.setattr(charger, "CONNECT_LOCK_DELAY", 0)
  charger.connected_chargers["CP_1"] = charge_point
  assert charge_point.state.cable_lock is False
  await charge_point.lock_after_connect()
  await charge_point.on_status_notification(1, "NoError", "Preparing")
  await asyncio.sleep(0.01)
  assert sent == []
  store.save_registry()
  store.registry.clear()
  store.load_registry()
  assert store.registry["CP_1"].cable_lock is False


@pytest.mark.parametrize("value, locked", [("false", True), ("true", False)])
async def test_turning_cable_lock_on_shows_the_real_state(
    charge_point, monkeypatch, value, locked
):
  async def fake_request(payload, result_type):
    return call_result.GetConfiguration(
        configuration_key=[{"key": charger.LOCK_KEY, "value": value, "readonly": False}]
    )

  monkeypatch.setattr(charge_point, "request", fake_request)
  charger.connected_chargers["CP_1"] = charge_point
  charger.set_cable_lock_enabled("CP_1", True)
  await asyncio.sleep(0.01)
  assert store.registry["CP_1"].desired_locked is locked


async def test_turning_cable_lock_back_on_cancels_a_pending_release(toggle_setup):
  charge_point, _ = toggle_setup
  del charger.connected_chargers["CP_1"]
  charger.set_cable_lock_enabled("CP_1", False)
  charger.set_cable_lock_enabled("CP_1", True)
  state = store.registry["CP_1"]
  assert state.cable_lock and not state.lock_release_pending


async def test_second_read_of_a_card_is_ignored(toggle_setup):
  charge_point, calls = toggle_setup
  await present(charge_point)
  await present(charge_point)
  assert calls == [False]


@pytest.mark.parametrize("status", [
    "Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing",
    "Faulted", "Unavailable", "Reserved", None,
])
async def test_no_toggle_unless_sure_no_cable_is_plugged_in(toggle_setup, status):
  charge_point, calls = toggle_setup
  charge_point.state.connectors[1] = {"status": status} if status else {}
  await present(charge_point)
  assert calls == []


async def test_no_toggle_if_any_connector_or_none_is_known(toggle_setup):
  charge_point, calls = toggle_setup
  charge_point.state.connectors[2] = {"status": "Charging"}
  await present(charge_point)
  charge_point.state.connectors = {0: {"status": "Available"}}
  await present(charge_point)
  assert calls == []


async def test_only_authorized_toggle_tags_toggle(toggle_setup):
  charge_point, calls = toggle_setup
  store.add_tag("plain")
  store.add_tag("blocked", authorized=False, toggle_lock=True)
  for id_tag in ("plain", "blocked", "unknown"):
    await present(charge_point, id_tag)
  assert calls == []


async def test_automatic_lock_waits_after_a_toggle(toggle_setup):
  charge_point, _ = toggle_setup
  assert charge_point.should_auto_lock()
  await present(charge_point)
  assert not charge_point.should_auto_lock()
  charge_point.last_toggle = -charger.TOGGLE_COOLDOWN
  assert charge_point.should_auto_lock()


@pytest.mark.parametrize("reported, expected", [
    ({"charge_point_serial_number": "CP1", "charge_box_serial_number": "BOX"}, "CP1"),
    ({"charge_box_serial_number": "BOX"}, "BOX"),
    ({}, "KEPT"),
])
async def test_boot_stores_the_serial_number(charge_point, monkeypatch, reported, expected):
  monkeypatch.setattr(charge_point, "apply_settings_after_boot", lambda: asyncio.sleep(0))
  store.get_state("CP_1").serial_number = "KEPT"
  await charge_point.on_boot_notification(
      charge_point_vendor="Acme", charge_point_model="Mini", **reported
  )
  await asyncio.sleep(0)
  store.registry.clear()
  store.load_registry()
  assert store.registry["CP_1"].serial_number == expected


async def test_queued_stop_does_not_lower_todays_reading(charge_point):
  now = datetime.now(timezone.utc)
  await charge_point.on_meter_values(connector_id=1, meter_value=[{
      "timestamp": now.isoformat(),
      "sampled_value": [{"value": "9000", "unit": "Wh"}],
  }])
  store.add_tag("abc")
  started = now - timedelta(minutes=30)
  result = await charge_point.on_start_transaction(
      connector_id=1, id_tag="abc", meter_start=7000, timestamp=started.isoformat()
  )
  await charge_point.on_stop_transaction(
      meter_stop=8000, timestamp=(now - timedelta(minutes=10)).isoformat(),
      transaction_id=result.transaction_id, id_tag="abc",
  )
  with store.db() as conn:
    rows = conn.execute(
        "SELECT energy_wh FROM energy_readings WHERE day = ?",
        (now.astimezone().date().isoformat(),),
    ).fetchall()
  assert [r["energy_wh"] for r in rows] == [9000]


async def test_repeated_start_transaction_keeps_one_session(charge_point):
  store.add_tag("abc", authorized=True)
  first = await start(charge_point, "abc")
  again = await start(charge_point, "abc")
  assert again.transaction_id == first.transaction_id
  sessions = store.list_sessions("CP_1")
  assert [s["id"] for s in sessions] == [first.transaction_id]
  assert sessions[0]["stopped_at"] is None


async def test_a_different_start_is_a_new_session(charge_point):
  store.add_tag("abc", authorized=True)
  first = await start(charge_point, "abc")
  later = await charge_point.on_start_transaction(
      connector_id=1, id_tag="abc", meter_start=0, timestamp="2026-01-01T00:05:00Z"
  )
  assert later.transaction_id != first.transaction_id
  assert len(store.list_sessions("CP_1")) == 2


async def test_repeated_refused_start_is_stopped_again(charge_point, monkeypatch):
  stopped = []

  async def fake_remote_stop(transaction_id):
    stopped.append(transaction_id)

  monkeypatch.setattr(charge_point, "remote_stop", fake_remote_stop)
  first = await start(charge_point, "unknown")
  await start(charge_point, "unknown")
  await asyncio.sleep(0)
  assert stopped == [first.transaction_id] * 2
  assert len(store.list_sessions("CP_1")) == 1


@pytest.mark.parametrize("value", ["Infinity", "-inf", "NaN"])
async def test_non_finite_meter_values_are_ignored(charge_point, value):
  now = datetime.now(timezone.utc).isoformat()
  sample = [{"timestamp": now, "sampled_value": [{"value": "1000"}]}]
  await charge_point.on_meter_values(1, sample)
  sample[0]["sampled_value"] = [{"value": value}]
  await charge_point.on_meter_values(1, sample)
  assert store.registry["CP_1"].connectors[1].get("energy_wh") == 1000
  with store.db() as conn:
    assert conn.execute("SELECT energy_wh FROM energy_readings").fetchone()[0] == 1000
