# pyright: standard
"""Shared fixtures. Every test gets empty state and its own database."""

import pytest

from chargepoint import charger, store


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
  monkeypatch.setattr(store, "DB_FILE", tmp_path / "test.db")
  for var in ("OCPP_HOST", "OCPP_PORT", "OCPP_HTTP_HOST", "OCPP_HTTP_PORT"):
    monkeypatch.delenv(var, raising=False)
  monkeypatch.setattr(store, "active_network", None)
  store.init_db()
  store.registry.clear()
  store.tags.clear()
  store.groups.clear()
  charger.connected_chargers.clear()
  yield tmp_path
  store.registry.clear()
  store.tags.clear()
  store.groups.clear()
  charger.connected_chargers.clear()


class FakeConnection:
  """Stands in for a websocket when handlers are called directly."""

  def __init__(self):
    self.sent = []

  async def send(self, message):
    self.sent.append(message)

  async def recv(self):
    raise AssertionError("not used in unit tests")


@pytest.fixture
def charge_point():
  return charger.ChargePoint("CP_1", FakeConnection())
