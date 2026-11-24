# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

"""OCPP 1.6 protocol handling: one ChargePoint instance per connected charger."""

import asyncio
import logging
import time
import math
from typing import Any, cast
from urllib.parse import unquote

import websockets
from ocpp.exceptions import OCPPError
from ocpp.routing import on  # pyright: ignore[reportUnknownVariableType]
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result, datatypes
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    ClearCacheStatus,
    ConfigurationStatus,
    RegistrationStatus,
    RemoteStartStopStatus,
    UnlockStatus,
)

from .logbuffer import TRACE
from .store import (
    ChargerState,
    ConnectorInfo,
    LockConfig,
    get_state,
    close_open_sessions,
    find_started_session,
    now_iso,
    record_energy,
    registry,
    resume_session,
    save_registry,
    session_connector,
    start_session,
    stop_session,
    tag_parent,
    tag_status,
    tag_toggles_lock,
)

LOCK_KEY = "UnlockConnectorOnEVSideDisconnect"

# Connector states in which a cable is plugged in (OCPP 1.6 ChargePointStatus).
PLUGGED_IN = {"Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing"}

# Seconds to wait after a charger connects before sending the lock setting,
# so a booting charger can send its BootNotification first.
CONNECT_LOCK_DELAY = 5.0

# Seconds to wait before acting on a toggle tag, so the charger gets its
# Authorize answer first.
TOGGLE_DELAY = 1.0
# Seconds after a toggle in which another read of a card is ignored, and in
# which automatic locking is off: many chargers report Preparing right after a
# card read, which looks like a cable being plugged in.
TOGGLE_COOLDOWN = 5.0

connected_chargers: dict[str, "ChargePoint"] = {}  # id -> live connection


def id_tag_info(id_tag: str) -> datatypes.IdTagInfo:
  """The authorization answer for a tag, including its group.

  The charger remembers the parentIdTag of the tag that started a session,
  and per OCPP 1.6 lets any tag with the same parentIdTag stop it.
  """
  return datatypes.IdTagInfo(status=tag_status(id_tag), parent_id_tag=tag_parent(id_tag))


def clear_charger_caches() -> None:
  """Makes chargers forget tags they may have cached as authorized."""
  for charger in list(connected_chargers.values()):
    asyncio.create_task(charger.clear_auth_cache())


class ChargePoint(cp):
  id: str  # set by the (untyped) ocpp base class
  last_toggle: float = -TOGGLE_COOLDOWN  # time.monotonic() of the last lock toggle

  async def request[R](self, payload: object, result_type: type[R]) -> R:
    """Sends a command and returns the charger's typed answer.

    Raises OCPPError if the charger answers with an error (for example
    NotSupported) and asyncio.TimeoutError if it does not answer at all.
    """
    response: object = await self.call(payload, suppress=False)
    if not isinstance(response, result_type):
      raise TypeError(f"Expected {result_type.__name__}, got {response!r}")
    return response

  @property
  def state(self) -> ChargerState:
    return get_state(self.id)

  def touch(self) -> None:
    self.state.last_seen = now_iso()

  def record_rfid(self, id_tag: str | None) -> None:
    if not id_tag:
      return
    self.state.last_rfid = id_tag
    self.state.last_rfid_at = now_iso()
    save_registry()

  @on(Action.boot_notification)
  async def on_boot_notification(
      self, charge_point_vendor: str, charge_point_model: str, **kwargs: Any
  ):
    logging.info(
        f"[{self.id}] Booted: {charge_point_vendor} - {charge_point_model}"
    )
    self.state.vendor = charge_point_vendor
    self.state.model = charge_point_model
    serial = kwargs.get("charge_point_serial_number") or kwargs.get("charge_box_serial_number")
    if serial:  # optional in OCPP; keep what we know if a later boot omits it
      self.state.serial_number = serial
    self.touch()
    save_registry()
    # A reboot ends any session; queued StopTransactions may still follow.
    if closed := close_open_sessions(self.id, now_iso()):
      logging.warning(f"[{self.id}] Rebooted; closed sessions without a stop: {closed}")

    asyncio.create_task(self.apply_settings_after_boot())

    return call_result.BootNotification(
        current_time=now_iso(),
        interval=60,
        status=RegistrationStatus.accepted,
    )

  @on(Action.heartbeat)
  async def on_heartbeat(self):
    logging.log(TRACE, f"[{self.id}] Heartbeat received")
    self.touch()
    return call_result.Heartbeat(current_time=now_iso())

  @on(Action.status_notification)
  async def on_status_notification(
      self, connector_id: int, error_code: str, status: str, **kwargs: Any
  ):
    connector = self.state.connectors.setdefault(connector_id, {})
    unchanged = (connector.get("status"), connector.get("error_code")) == (status, error_code)
    if unchanged:
      level = TRACE  # some chargers keep repeating their status
    elif status == "Faulted":
      level = logging.ERROR
    elif error_code != "NoError":
      level = logging.WARNING
    else:
      level = logging.INFO
    logging.log(
        level,
        f"[{self.id}] Connector {connector_id} status: {status} (Error: {error_code})",
    )
    was_plugged_in = connector.get("status") in PLUGGED_IN
    connector.update(status=status, error_code=error_code, updated=now_iso())
    self.touch()
    if connector_id > 0 and status in PLUGGED_IN and not was_plugged_in:
      self.on_cable_plugged_in(connector_id)
    return call_result.StatusNotification()

  def on_cable_plugged_in(self, connector_id: int) -> None:
    if self.should_auto_lock():
      asyncio.create_task(self.auto_lock(f"cable plugged into connector {connector_id}"))

  async def lock_after_connect(self) -> None:
    """Re-sends the lock setting to a charger that (re)connected."""
    await asyncio.sleep(CONNECT_LOCK_DELAY)
    if connected_chargers.get(self.id) is not self:
      return
    if self.state.lock_release_pending:
      await self.release_lock_setting()
    elif self.should_auto_lock():
      await self.auto_lock("charger connected")

  def should_auto_lock(self) -> bool:
    """Whether "Lock cable when plugged in" is on and no lock request is running."""
    return (
        self.state.cable_lock and self.state.lock_on_plugin and not self.state.lock_busy
        and not self.toggled_recently()
    )

  def toggled_recently(self) -> bool:
    return time.monotonic() - self.last_toggle < TOGGLE_COOLDOWN

  def no_cable_plugged_in(self) -> bool:
    """Only "Available" is sure: Faulted or Unavailable can still hold a cable."""
    connectors = [c for connector_id, c in self.state.connectors.items() if connector_id > 0]
    return bool(connectors) and all(c.get("status") == "Available" for c in connectors)

  def on_toggle_tag(self, id_tag: str) -> None:
    if not self.no_cable_plugged_in():
      logging.debug(f"[{self.id}] RFID {id_tag}: not toggling the lock, a cable may be plugged in")
    elif self.state.lock_busy or self.toggled_recently():
      logging.debug(f"[{self.id}] RFID {id_tag}: not toggling the lock, just changed")
    else:
      self.last_toggle = time.monotonic()
      asyncio.create_task(self.toggle_lock(id_tag))

  async def toggle_lock(self, id_tag: str) -> None:
    await asyncio.sleep(TOGGLE_DELAY)
    state = self.state
    if connected_chargers.get(self.id) is not self or state.lock_busy or not state.cable_lock:
      return
    # What the charger reported, as on the Chargers page.
    locked = state.desired_locked if state.lock_config == "unknown" else state.lock_config == "locked"
    logging.info(f"[{self.id}] RFID {id_tag}: {'unlocking' if locked else 'locking'} the cable")
    try:
      await set_desired_lock(self.id, not locked)
    except (asyncio.TimeoutError, OCPPError) as e:  # set_cable_lock logged it
      logging.debug(f"[{self.id}] Toggling the lock failed: {e!r}")
    except Exception:
      logging.exception(f"[{self.id}] Toggling the lock failed")

  async def auto_lock(self, reason: str) -> None:
    logging.info(f"[{self.id}] Locking cable: {reason}")
    self.state.desired_locked = True
    save_registry()
    try:
      await self.set_cable_lock(True)
    except (asyncio.TimeoutError, OCPPError) as e:  # set_cable_lock logged it
      logging.debug(f"[{self.id}] Locking cable failed ({reason}): {e!r}")
    except Exception:
      logging.exception(f"[{self.id}] Locking cable failed ({reason})")

  @on(Action.meter_values)
  async def on_meter_values(
      self, connector_id: int, meter_value: list[dict[str, Any]], **kwargs: Any
  ):
    """Keeps the latest power (W) and energy register (Wh) per connector."""
    readings = ConnectorInfo()
    energy_at: str | None = None
    for mv in meter_value:
      for sv in mv.get("sampled_value", []):
        measurand = sv.get("measurand", "Energy.Active.Import.Register")
        if sv.get("phase"):  # only the totals, not per-phase values
          continue
        try:
          value = float(str(sv.get("value", "")).strip())
        except ValueError:
          continue
        if not math.isfinite(value):  # float() also accepts "inf" and "nan"
          logging.warning(f"[{self.id}] Ignored meter value {sv.get('value')!r}")
          continue
        unit = sv.get("unit", "")
        if measurand == "Power.Active.Import":
          readings["power_w"] = value * 1000 if unit == "kW" else value
        elif measurand == "Energy.Active.Import.Register":
          readings["energy_wh"] = value * 1000 if unit == "kWh" else value
          energy_at = mv.get("timestamp")
    if readings:
      readings["meter_updated"] = now_iso()
      self.state.connectors.setdefault(connector_id, {}).update(readings)
    if "energy_wh" in readings:
      record_energy(self.id, connector_id, readings["energy_wh"], at=energy_at)
    if (transaction_id := kwargs.get("transaction_id")) is not None:
      self.resume(transaction_id)
    logging.log(TRACE, f"[{self.id}] Meter values connector {connector_id}: {readings}")
    self.touch()
    return call_result.MeterValues()

  @on(Action.authorize)
  async def on_authorize(self, id_tag: str):
    logging.debug(f"[{self.id}] Authorizing RFID: {id_tag}")
    self.touch()
    self.record_rfid(id_tag)
    info = id_tag_info(id_tag)
    group = f" (group {info.parent_id_tag})" if info.parent_id_tag else ""
    level = logging.INFO if info.status == AuthorizationStatus.accepted else logging.WARNING
    logging.log(level, f"[{self.id}] RFID {id_tag}: {info.status.value}{group}")
    if (info.status == AuthorizationStatus.accepted and self.state.cable_lock
        and tag_toggles_lock(id_tag)):
      self.on_toggle_tag(id_tag)
    return call_result.Authorize(id_tag_info=info)

  def resume(self, transaction_id: int) -> None:
    """Reopens a session closed at a boot when the charger still reports it."""
    if resume_session(self.id, transaction_id):
      logging.warning(f"[{self.id}] Transaction {transaction_id} is still running; reopened it")

  @on(Action.start_transaction)
  async def on_start_transaction(
      self,
      connector_id: int,
      id_tag: str,
      meter_start: int,
      timestamp: str,
      **kwargs: Any,
  ):
    self.touch()
    self.record_rfid(id_tag)

    record_energy(self.id, connector_id, meter_start, at=timestamp)
    info = id_tag_info(id_tag)
    status = info.status
    transaction_id = find_started_session(
        self.id, connector_id, id_tag, meter_start, timestamp
    )
    if transaction_id is not None:
      logging.info(f"[{self.id}] Repeated start of transaction {transaction_id}")
      self.resume(transaction_id)
    else:
      if closed := close_open_sessions(self.id, timestamp, connector_id, meter_start):
        logging.warning(
            f"[{self.id}] New session on connector {connector_id}; closed"
            f" sessions without a stop: {closed}"
        )
      transaction_id = start_session(
          self.id, connector_id, id_tag, meter_start, timestamp,
          accepted=status == AuthorizationStatus.accepted,
      )
    if status != AuthorizationStatus.accepted:
      logging.warning(
          f"[{self.id}] Refusing transaction {transaction_id}: RFID"
          f" {id_tag!r} is {status.value}"
      )
      # The charger should stop on its own (StopTransactionOnInvalidId), but
      # send an explicit stop as well in case it doesn't.
      asyncio.create_task(self.remote_stop(transaction_id))
    else:
      logging.info(
          f"[{self.id}] Transaction {transaction_id} started on connector"
          f" {connector_id} (RFID: {id_tag})"
      )

    return call_result.StartTransaction(
        transaction_id=transaction_id,
        id_tag_info=info,
    )

  @on(Action.stop_transaction)
  async def on_stop_transaction(
      self,
      meter_stop: int,
      timestamp: str,
      transaction_id: int,
      id_tag: str | None = None,
      **kwargs: Any,
  ):
    logging.info(
        f"[{self.id}] Transaction {transaction_id} stopped"
        + (f" (RFID: {id_tag})" if id_tag else "")
    )
    self.touch()
    self.record_rfid(id_tag)
    if not stop_session(
        self.id, transaction_id, meter_stop, timestamp, kwargs.get("reason"),
        id_tag or None,
    ):
      logging.warning(f"[{self.id}] Stop for unknown transaction {transaction_id}")
    elif (connector_id := session_connector(self.id, transaction_id)) is not None:
      record_energy(self.id, connector_id, meter_stop, at=timestamp)
    # The session has ended either way; this only refreshes the charger's
    # cached information about the tag. No tag, no idTagInfo.
    return call_result.StopTransaction(
        id_tag_info=id_tag_info(id_tag) if id_tag else None
    )

  async def apply_settings_after_boot(self) -> None:
    """The cable lock is set elsewhere: see lock_after_connect and on_cable_plugged_in."""
    await asyncio.sleep(2)  # Give the charger a moment to finish boot sequence
    try:
      # Make the charger end a session itself when we reject its tag.
      response = await self.request(
          call.ChangeConfiguration(key="StopTransactionOnInvalidId", value="true"),
          call_result.ChangeConfiguration,
      )
      logging.debug(
          f"[{self.id}] StopTransactionOnInvalidId=true: {response.status}"
      )
    except Exception as e:
      # Not a problem by itself: refused sessions also get a RemoteStopTransaction.
      logging.info(f"[{self.id}] Could not set StopTransactionOnInvalidId: {e}")

  async def remote_stop(self, transaction_id: int) -> None:
    await asyncio.sleep(1)  # Let the StartTransaction response arrive first
    try:
      response = await self.request(
          call.RemoteStopTransaction(transaction_id=transaction_id),
          call_result.RemoteStopTransaction,
      )
      level = logging.INFO if response.status == RemoteStartStopStatus.accepted else logging.ERROR
      logging.log(
          level,
          f"[{self.id}] RemoteStopTransaction {transaction_id}: {response.status}",
      )
    except Exception as e:
      logging.error(
          f"[{self.id}] Could not stop refused transaction {transaction_id};"
          f" it may keep charging: {e}"
      )

  async def clear_auth_cache(self) -> None:
    try:
      response = await self.request(call.ClearCache(), call_result.ClearCache)
      level = logging.DEBUG if response.status == ClearCacheStatus.accepted else logging.WARNING
      logging.log(level, f"[{self.id}] ClearCache: {response.status}")
    except Exception as e:
      logging.warning(f"[{self.id}] ClearCache failed: {e}")

  async def read_lock_config(self) -> None:
    """Asks the charger for the current value of the lock setting."""
    response = await self.request(
        call.GetConfiguration(key=[LOCK_KEY]), call_result.GetConfiguration
    )
    entries = cast(list[dict[str, Any]], response.configuration_key or [])
    for entry in entries:
      if entry.get("key") == LOCK_KEY:
        value = str(entry.get("value", "")).lower()
        states: dict[str, LockConfig] = {"false": "locked", "true": "unlocked"}
        self.state.lock_config = states.get(value, "unknown")
        return
    self.state.lock_config = "unknown"

  async def set_cable_lock(self, locked: bool) -> str:
    """Locks or unlocks the cable. Returns a human-readable result.

    Locking sets UnlockConnectorOnEVSideDisconnect=false so the cable stays
    locked. Unlocking sets it back to true and also sends UnlockConnector so
    a cable that is currently held gets released right away.
    """
    s = self.state
    s.lock_busy = True
    action = "lock" if locked else "unlock"
    try:
      logging.debug(f"[{self.id}] Applying cable {action} setting...")
      response = await self.request(
          call.ChangeConfiguration(
              key=LOCK_KEY, value="false" if locked else "true"
          ),
          call_result.ChangeConfiguration,
      )

      if response.status == ConfigurationStatus.accepted:
        msg = f"Cable {action} setting accepted."
      elif response.status == ConfigurationStatus.reboot_required:
        msg = f"Cable {action} setting accepted; charger needs a reboot."
      else:
        msg = f"Charger rejected cable {action}: {response.status}"
      level = logging.INFO if response.status == ConfigurationStatus.accepted else logging.WARNING
      logging.log(level, f"[{self.id}] {msg}")

      if not locked:
        msg += " " + await self.release_connectors()

      try:
        await self.read_lock_config()
      except Exception as e:
        logging.warning(f"[{self.id}] Could not read back lock setting: {e}")

      s.last_message = msg
      return msg
    except asyncio.TimeoutError:
      s.last_message = f"Charger did not respond to {action} request."
      logging.warning(f"[{self.id}] {s.last_message}")
      raise
    except OCPPError as e:
      s.last_message = f"Charger refused {action} request: {e.description}"
      logging.warning(f"[{self.id}] {s.last_message}")
      raise
    finally:
      s.lock_busy = False

  async def sync_lock_state(self) -> None:
    """After the cable lock is turned on: show what the charger really does."""
    try:
      await self.read_lock_config()
    except Exception as e:
      logging.info(f"[{self.id}] Could not read the lock setting: {e}")
      return
    if self.state.lock_config != "unknown":
      self.state.desired_locked = self.state.lock_config == "locked"
      save_registry()

  async def release_lock_setting(self) -> None:
    """After the cable lock is turned off: stop holding the cable after unplugging.

    Deliberately no UnlockConnector, which would end a session in progress.
    """
    state = self.state
    if not state.lock_release_pending:
      return
    try:
      response = await self.request(
          call.ChangeConfiguration(key=LOCK_KEY, value="true"),
          call_result.ChangeConfiguration,
      )
    except asyncio.TimeoutError:  # tried again when the charger next connects
      logging.warning(f"[{self.id}] Charger did not answer the cable lock release")
      return
    except OCPPError as e:  # e.g. a fixed cable: there is no such setting
      logging.info(f"[{self.id}] Charger has no cable lock to release: {e.description}")
      response = None
    except Exception:
      logging.exception(f"[{self.id}] Releasing the cable lock failed")
      return
    if response is not None:
      logging.info(f"[{self.id}] Cable lock released: {response.status}")
      if response.status != ConfigurationStatus.rejected:
        state.lock_config = "unlocked"
    state.lock_release_pending = False
    save_registry()

  async def release_connectors(self) -> str:
    connector_ids = [c for c in self.state.connectors if c > 0] or [1]
    results: list[str] = []
    for connector_id in connector_ids:
      results.append(await self.manual_unlock(connector_id))
    return " ".join(results)

  async def manual_unlock(self, connector_id: int = 1) -> str:
    """Sends UnlockConnector to release a cable the connector is holding."""
    logging.debug(
        f"[{self.id}] Sending UnlockConnector for connector {connector_id}..."
    )
    response = await self.request(
        call.UnlockConnector(connector_id=connector_id),
        call_result.UnlockConnector,
    )
    level = logging.INFO if response.status == UnlockStatus.unlocked else logging.WARNING
    logging.log(level, f"[{self.id}] Connector {connector_id} unlock: {response.status}")
    if response.status == UnlockStatus.unlocked:
      return f"Connector {connector_id} released."
    return f"Connector {connector_id} release: {response.status}."


async def set_desired_lock(cp_id: str, locked: bool) -> str:
  """Stores the operator's choice and applies it if the charger is online."""
  s = get_state(cp_id)
  s.desired_locked = locked
  save_registry()
  charger = connected_chargers.get(cp_id)
  if charger is None:
    s.last_message = "Charger is offline; nothing was sent."
    return s.last_message
  return await charger.set_cable_lock(locked)


def charger_id_from_path(path: str) -> str:
  """The charger ID in a connection URL's path, e.g. /CP%201?x=1 -> CP 1.

  A charger already known under the literal text keeps that ID, so its
  history stays with it. An ID that would decode to a slash or a control
  character stays encoded: those break the API's URLs or could forge log lines.
  """
  if (literal := path.strip("/")) in registry and literal:
    return literal
  encoded = path.partition("?")[0].strip("/")
  if encoded in registry:
    return encoded
  decoded = unquote(encoded)
  charger_id = decoded if decoded.isprintable() and "/" not in decoded else encoded
  return charger_id or "UNKNOWN_CP"


def set_cable_lock_enabled(cp_id: str, enabled: bool) -> None:
  """Turns all cable lock features of a charger on or off.

  Turning them off also makes the charger stop holding the cable, because
  nothing on the web interface could release it afterwards.
  """
  s = get_state(cp_id)
  if s.cable_lock == enabled:
    return
  s.cable_lock = enabled
  s.lock_release_pending = not enabled
  if not enabled:
    s.desired_locked = False
  save_registry()
  logging.info(f"[{cp_id}] Cable lock {'enabled' if enabled else 'disabled'}")
  charger = connected_chargers.get(cp_id)
  if charger is not None:
    asyncio.create_task(
        charger.sync_lock_state() if enabled else charger.release_lock_setting()
    )


async def on_connect(websocket: websockets.ServerConnection) -> None:
  """Handles incoming WebSocket connections from chargers."""
  # The Charge Point ID is in the URL path (e.g., ws://localhost:9000/CP_001)
  charge_point_id = charger_id_from_path(
      websocket.request.path if websocket.request else ""
  )

  logging.info(f"New charger connected: {charge_point_id}")
  cp_instance = ChargePoint(charge_point_id, websocket)
  connected_chargers[charge_point_id] = cp_instance
  state = get_state(charge_point_id)
  state.connected = True
  state.connectors = {}
  cp_instance.touch()
  lock_task = asyncio.create_task(cp_instance.lock_after_connect())

  try:
    await cp_instance.start()
  except websockets.exceptions.ConnectionClosed:
    logging.info(f"Charger disconnected: {charge_point_id}")
  finally:
    lock_task.cancel()
    # Only clean up if a newer connection hasn't replaced this one
    if connected_chargers.get(charge_point_id) is cp_instance:
      connected_chargers.pop(charge_point_id, None)
      state.connected = False
      state.lock_busy = False
      state.connectors = {}
      save_registry()
