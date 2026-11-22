# pyright: standard
"""HTTP API, called in-process (no network)."""

import httpx
import pytest

from chargepoint import api, store


@pytest.fixture
async def client():
  transport = httpx.ASGITransport(app=api.app)
  async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
    yield c


@pytest.fixture
def cache_clears(monkeypatch):
  calls = []
  monkeypatch.setattr(api.tags, "clear_charger_caches", lambda: calls.append(1))
  return calls


async def test_index_page(client):
  r = await client.get("/")
  assert r.status_code == 200
  assert "<title>Chargers</title>" in r.text
  assert r.headers["cache-control"] == "no-store"


async def test_list_chargers(client):
  store.get_state("B")
  store.get_state("A").last_rfid = "abc"
  r = await client.get("/api/chargers")
  assert [c["id"] for c in r.json()] == ["A", "B"]
  assert r.json()[0]["last_rfid"] == "abc"


async def test_register_list_update_delete_tag(client, cache_clears):
  r = await client.post("/api/tags", json={"id_tag": " abc ", "name": "Car"})
  assert r.status_code == 200
  assert r.json()["id_tag"] == "abc" and r.json()["authorized"] is True

  assert [t["name"] for t in (await client.get("/api/tags")).json()] == ["Car"]

  r = await client.post("/api/tags/ABC", json={"name": "Van"})
  assert r.json()["name"] == "Van"
  assert cache_clears == []

  r = await client.post("/api/tags/abc", json={"authorized": False})
  assert r.json()["authorized"] is False
  assert cache_clears == [1]

  r = await client.delete("/api/tags/abc")
  assert r.json() == {"ok": True}
  assert cache_clears == [1, 1]
  assert (await client.get("/api/tags")).json() == []


async def test_duplicate_tag(client):
  await client.post("/api/tags", json={"id_tag": "abc"})
  r = await client.post("/api/tags", json={"id_tag": "ABC"})
  assert r.status_code == 400
  assert r.json()["detail"] == "Tag is already registered"


@pytest.mark.parametrize("body", [
    {},
    {"id_tag": ""},
    {"id_tag": "   "},
    {"id_tag": "x" * 21},
    {"id_tag": "abc", "bogus": 1},
    {"id_tag": "abc", "authorized": "maybe"},
])
async def test_invalid_tag_body(client, body):
  r = await client.post("/api/tags", json=body)
  assert r.status_code == 422
  assert store.tags == {}


async def test_unknown_tag(client):
  assert (await client.post("/api/tags/nope", json={"name": "x"})).status_code == 404
  assert (await client.delete("/api/tags/nope")).status_code == 404


async def test_lock_unknown_charger(client):
  r = await client.post("/api/chargers/NOPE/lock")
  assert r.status_code == 404


async def test_lock_offline_charger_saves_choice(client):
  store.get_state("CP_1")
  r = await client.post("/api/chargers/CP_1/unlock")
  assert r.status_code == 200
  assert r.json()["ok"] is True
  assert store.registry["CP_1"].desired_locked is False


async def test_lock_requires_post(client):
  store.get_state("CP_1")
  assert (await client.get("/api/chargers/CP_1/lock")).status_code == 405


async def test_list_sessions(client):
  store.start_session("A", 1, "abc", 0, "t1", True)
  store.start_session("B", 1, "abc", 0, "t2", True)
  r = await client.get("/api/sessions")
  assert [s["charger_id"] for s in r.json()] == ["B", "A"]
  r = await client.get("/api/sessions", params={"charger_id": "A"})
  assert [s["started_at"] for s in r.json()] == ["t1"]
  assert (await client.get("/api/sessions", params={"limit": 0})).status_code == 422


async def test_update_charger_setting(client):
  store.get_state("CP_1")
  r = await client.post("/api/chargers/CP_1", json={"lock_on_plugin": False})
  assert r.status_code == 200 and r.json()["lock_on_plugin"] is False
  store.registry.clear()
  store.load_registry()
  assert store.registry["CP_1"].lock_on_plugin is False
  assert (await client.post("/api/chargers/nope", json={})).status_code == 404
  assert (await client.post("/api/chargers/CP_1", json={"x": 1})).status_code == 422


async def test_tag_groups(client, cache_clears):
  assert (await client.post("/api/tags", json={"id_tag": "a", "parent_id_tag": "Family"})).status_code == 400
  await client.post("/api/groups", json={"name": "Family"})
  r = await client.post("/api/tags", json={"id_tag": "a", "parent_id_tag": "Family"})
  assert r.json()["parent_id_tag"] == "Family"
  r = await client.post("/api/tags", json={"id_tag": "b"})
  assert r.json()["parent_id_tag"] == ""
  r = await client.post("/api/tags/b", json={"name": "Van"})
  assert cache_clears == []
  r = await client.post("/api/tags/b", json={"parent_id_tag": "FAMILY"})
  assert r.json()["parent_id_tag"] == "Family"
  assert cache_clears == [1]  # chargers may have cached the old group
  r = await client.post("/api/tags/b", json={"parent_id_tag": "x" * 21})
  assert r.status_code == 422
  r = await client.post("/api/tags/b", json={"parent_id_tag": "Nope"})
  assert r.status_code == 400 and "Unknown group" in r.json()["detail"]


async def test_add_list_delete_groups(client, cache_clears):
  assert (await client.post("/api/groups", json={"name": "Work"})).status_code == 200
  assert (await client.post("/api/groups", json={"name": "Family"})).status_code == 200
  assert (await client.post("/api/groups", json={"name": "family"})).status_code == 400
  assert (await client.post("/api/groups", json={"name": " "})).status_code == 422
  assert (await client.post("/api/groups", json={"name": "x" * 21})).status_code == 422
  assert [g["name"] for g in (await client.get("/api/groups")).json()] == ["Family", "Work"]

  await client.post("/api/tags", json={"id_tag": "a", "parent_id_tag": "Family"})
  r = await client.delete("/api/groups/Work")
  assert r.json() == {"tags_ungrouped": 0} and cache_clears == []
  r = await client.delete("/api/groups/FAMILY")
  assert r.json() == {"tags_ungrouped": 1} and cache_clears == [1]
  assert (await client.get("/api/tags")).json()[0]["parent_id_tag"] == ""
  assert (await client.get("/api/groups")).json() == []
  assert (await client.delete("/api/groups/Family")).status_code == 404


async def test_energy_endpoint(client):
  from datetime import date
  store.record_energy("A", 1, 0, date(2026, 1, 1))
  store.record_energy("A", 1, 2000, date(2026, 1, 2))
  store.record_energy("B", 1, 0, date(2026, 1, 1))
  store.record_energy("B", 1, 500, date(2026, 2, 1))
  r = await client.get("/api/energy", params={"period": "month"})
  assert r.json() == [{"period": "2026-01", "energy_wh": 2000, "cost": None},
                      {"period": "2026-02", "energy_wh": 500, "cost": None}]
  r = await client.get("/api/energy", params={"charger_id": "A"})
  assert r.json()[-1] == {"period": "2026-01-02", "energy_wh": 2000, "cost": None}
  assert (await client.get("/api/energy", params={"period": "week"})).status_code == 422


async def test_pages_and_shared_files(client):
  for path, text in (("/tags", "<title>Tags</title>"), ("/energy", "<title>Energy</title>"),
                     ("/sessions", "<title>Sessions</title>"), ("/settings", "<title>Settings</title>"),
                     ("/style.css", "--accent"), ("/common.js", "function renderNav"),
                     ("/app.js", "async function refresh")):
    r = await client.get(path)
    assert r.status_code == 200 and text in r.text, path


async def test_network_settings_api(client, monkeypatch):
  store.set_active_network(store.network_settings())
  body = {"ocpp_host": " ::1 ", "ocpp_port": 9100, "http_host": "my-host.lan", "http_port": 8100}
  r = await client.post("/api/settings/network", json=body)
  assert r.status_code == 200, r.text
  net = r.json()["network"]
  assert net["saved"] == {"ocpp_host": "::1", "ocpp_port": 9100, "http_host": "my-host.lan", "http_port": 8100}
  assert net["active"]["ocpp_port"] == 9000  # applies after a restart
  assert net["next_start"] == net["saved"] and net["overrides"] == {}
  for bad in ({"ocpp_host": "not a host!"}, {"http_port": 0}, {"ocpp_port": 70000},
              {"http_port": 9100}, {"http_host": "-bad.example"}):
    r = await client.post("/api/settings/network", json={**body, **bad})
    assert r.status_code == 422, bad
  monkeypatch.setenv("OCPP_HTTP_PORT", "8888")
  net = (await client.get("/api/settings")).json()["network"]
  assert net["overrides"] == {"http_port": "OCPP_HTTP_PORT"}
  assert net["next_start"]["http_port"] == 8888 and net["saved"]["http_port"] == 8100


async def test_fiscal_year_api(client):
  assert (await client.get("/api/settings")).json()["fiscal_year_start"] == {"month": 1, "day": 1}
  r = await client.post("/api/settings/fiscal-year", json={"month": 8, "day": 1})
  assert r.json()["fiscal_year_start"] == {"month": 8, "day": 1}
  assert (await client.post("/api/settings/fiscal-year", json={"month": 2, "day": 30})).status_code == 400
  assert (await client.post("/api/settings/fiscal-year", json={"month": 0, "day": 1})).status_code == 422


async def test_initial_energy_api(client):
  from datetime import date, timedelta
  store.get_state("CP_1")
  path = "/api/chargers/CP_1/initial-energy"
  r = await client.post(path, json={"day": "2026-08-01", "energy_wh": 123400})
  assert r.json() == {"day": "2026-08-01", "energy_wh": 123400}
  assert (await client.get("/api/initial-energy")).json() == {"CP_1": r.json()}
  tomorrow = (date.today() + timedelta(days=1)).isoformat()
  assert (await client.post(path, json={"day": tomorrow, "energy_wh": 1})).status_code == 422
  assert (await client.post(path, json={"day": "2026-08-01", "energy_wh": -1})).status_code == 422
  assert (await client.post("/api/chargers/nope/initial-energy",
                            json={"day": "2026-08-01", "energy_wh": 1})).status_code == 404
  assert (await client.delete(path)).json() == {"ok": True}
  assert (await client.delete(path)).status_code == 404


async def test_sessions_paging_and_filter(client):
  for i in range(25):
    store.start_session("A", 1, "a", 0, f"2026-11-{i + 1:02d}T12:00:00Z", True)
  r = await client.get("/api/sessions", params={"limit": 20})
  assert r.headers["x-total-count"] == "25" and len(r.json()) == 20
  r = await client.get("/api/sessions", params={"limit": 20, "offset": 20})
  assert [s["started_at"][:10] for s in r.json()] == [f"2026-11-0{d}" for d in (5, 4, 3, 2, 1)]
  r = await client.get("/api/sessions", params={
      "since": "2026-11-10T00:00:00+01:00", "until": "2026-11-12T00:00:00Z"})
  assert r.headers["x-total-count"] == "2"
  assert [s["started_at"][:10] for s in r.json()] == ["2026-11-11", "2026-11-10"]
  r = await client.get("/api/sessions", params={"since": "2026-11-10T00:00:00"})
  assert r.status_code == 422 and "time zone" in r.json()["detail"]
  assert (await client.get("/api/sessions", params={"offset": -1})).status_code == 422


async def test_delete_charger(client, monkeypatch):
  from chargepoint import charger
  store.get_state("CP_1")
  store.start_session("CP_1", 1, "a", 0, "t", True)
  monkeypatch.setitem(charger.connected_chargers, "CP_1", object())
  assert (await client.delete("/api/chargers/CP_1")).status_code == 409
  monkeypatch.delitem(charger.connected_chargers, "CP_1")
  r = await client.delete("/api/chargers/CP_1", params={"delete_history": "true"})
  assert r.json() == {"ok": True}
  assert (await client.get("/api/chargers")).json() == []
  assert (await client.get("/api/sessions")).json() == []
  assert (await client.delete("/api/chargers/CP_1")).status_code == 404


# --- Password ---


async def test_no_password_means_no_login(client):
  assert (await client.get("/api/auth")).json() == {"enabled": False, "logged_in": False}
  assert (await client.get("/")).status_code == 200
  assert (await client.get("/api/chargers")).status_code == 200


async def test_password_protects_pages_and_api(client):
  r = await client.post("/api/settings/password", json={"new_password": "secret123"})
  assert r.json() == {"enabled": True, "logged_in": True}  # this browser stays logged in
  assert (await client.get("/api/chargers")).status_code == 200

  await client.post("/api/logout")
  assert (await client.get("/api/auth")).json() == {"enabled": True, "logged_in": False}
  r = await client.get("/sessions?x=1")
  assert r.status_code == 303 and r.headers["location"] == "/login?next=/sessions%3Fx%3D1"
  for path in ("/api/chargers", "/api/settings", "/openapi.json", "/docs"):
    assert (await client.get(path)).status_code == 401, path
  assert (await client.post("/api/tags", json={"id_tag": "x"})).status_code == 401
  for path in ("/login", "/style.css", "/common.js"):
    assert (await client.get(path)).status_code == 200, path

  assert (await client.post("/api/login", json={"password": "nope"})).status_code == 401
  r = await client.post("/api/login", json={"password": "secret123"})
  assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
  assert (await client.get("/api/chargers")).status_code == 200


async def test_changing_and_removing_password_needs_current_one(client):
  await client.post("/api/settings/password", json={"new_password": "secret123"})
  r = await client.post("/api/settings/password",
                        json={"current_password": "wrong", "new_password": "other1234"})
  assert r.status_code == 403
  assert (await client.post("/api/settings/password",
                            json={"current_password": "secret123", "new_password": "short"})).status_code == 422
  r = await client.post("/api/settings/password",
                        json={"current_password": "secret123", "new_password": "other1234"})
  assert r.status_code == 200
  assert (await client.get("/api/chargers")).status_code == 200  # new cookie

  assert (await client.post("/api/settings/password/remove",
                            json={"current_password": "secret123"})).status_code == 403
  r = await client.post("/api/settings/password/remove", json={"current_password": "other1234"})
  assert r.json() == {"enabled": False, "logged_in": False}
  client.cookies.clear()
  assert (await client.get("/api/chargers")).status_code == 200  # open again


async def test_other_browsers_are_logged_out_by_a_password_change(client):
  import httpx
  await client.post("/api/settings/password", json={"new_password": "secret123"})
  transport = httpx.ASGITransport(app=api.app)
  async with httpx.AsyncClient(transport=transport, base_url="http://test") as other:
    await other.post("/api/login", json={"password": "secret123"})
    assert (await other.get("/api/chargers")).status_code == 200
    await client.post("/api/settings/password",
                      json={"current_password": "secret123", "new_password": "other1234"})
    assert (await other.get("/api/chargers")).status_code == 401


# --- Prices, summary and CSV ---


async def test_prices_api(client):
  assert (await client.get("/api/prices")).json() == {"currency": "€", "tariffs": [], "price_url": ""}
  r = await client.post("/api/prices", json={"valid_from": "2026-07-01", "price": 0.3})
  r = await client.post("/api/prices", json={"valid_from": "2026-01-01", "price": 0.25})
  assert r.json()["tariffs"] == [{"valid_from": "2026-01-01", "price": 0.25},
                                 {"valid_from": "2026-07-01", "price": 0.3}]
  r = await client.post("/api/prices", json={"valid_from": "2026-01-01", "price": 1})
  assert r.status_code == 400 and "already starts" in r.json()["detail"]
  for bad in ({"valid_from": "x", "price": 1}, {"valid_from": "2026-02-01", "price": -1},
              {"valid_from": "2026-02-01", "price": 101}, {"valid_from": "2026-02-01"}):
    assert (await client.post("/api/prices", json=bad)).status_code == 422, bad

  # change the price, then move its date
  r = await client.put("/api/prices/2026-07-01", json={"valid_from": "2026-07-01", "price": 0.32})
  assert r.json()["tariffs"][1] == {"valid_from": "2026-07-01", "price": 0.32}
  r = await client.put("/api/prices/2026-07-01", json={"valid_from": "2026-10-01", "price": 0.32})
  assert [t["valid_from"] for t in r.json()["tariffs"]] == ["2026-01-01", "2026-10-01"]
  r = await client.put("/api/prices/2026-10-01", json={"valid_from": "2026-01-01", "price": 1})
  assert r.status_code == 400  # date taken by the other price
  assert (await client.put("/api/prices/2026-07-01", json={"valid_from": "2026-07-01", "price": 1})).status_code == 404

  r = await client.delete("/api/prices/2026-01-01")
  assert r.json()["tariffs"] == [{"valid_from": "2026-10-01", "price": 0.32}]
  assert (await client.delete("/api/prices/2026-01-01")).status_code == 404

  assert (await client.get("/prices")).status_code == 200


async def test_session_summary_and_csv(client):
  store.set_tariffs([{"valid_from": "2026-01-01", "price": 0.25}], "€")
  store.add_tag("card1", "Car")
  tid = store.start_session("CP_1", 1, "card1", 1000, "2026-11-01T10:00:00Z", True)
  store.stop_session("CP_1", tid, 9000, "2026-11-01T12:30:00Z", "EVDisconnected")
  store.start_session("CP_1", 1, "stranger", 9000, "2026-11-02T10:00:00Z", False)
  r = await client.get("/api/sessions/summary", params={"since": "2026-11-01T00:00:00Z"})
  assert r.json() == {"count": 2, "energy_wh": 8000, "cost": 2.0, "currency": "€"}

  r = await client.get("/api/sessions.csv", params={"until": "2026-12-01T00:00:00Z"})
  assert r.headers["content-type"].startswith("text/csv")
  assert 'filename="charge-sessions.csv"' in r.headers["content-disposition"]
  assert r.text.startswith("﻿")
  import csv, io
  rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
  assert rows[0][-4:] == ["Energy (kWh)", "Cost (€)", "Status", "Stopped by tag"]
  first, second = rows[1], rows[2]  # oldest first
  assert first[:5] == [str(tid), "CP_1", "1", "card1", "Car"]
  assert (first[7], first[8], first[9], first[10]) == ("150", "8.000", "2.00", "EVDisconnected")
  assert (second[8], second[9], second[10]) == ("", "", "Refused")


async def test_energy_csv(client):
  from datetime import date
  store.set_tariffs([{"valid_from": "2026-01-01", "price": 0.5}], "€")
  store.record_energy("A", 1, 0, date(2026, 1, 1))
  store.record_energy("A", 1, 2000, date(2026, 1, 2))
  store.record_energy("A", 1, 3000, date(2026, 2, 1))
  r = await client.get("/api/energy.csv", params={"period": "month", "last_day": "2026-01-31"})
  assert r.text.lstrip("﻿").splitlines() == ["Month,Energy (kWh),Cost (€)", "2026-01,2.000,1.00"]
  r = await client.get("/api/energy", params={"period": "year"})
  assert r.json() == [{"period": "2026", "energy_wh": 3000, "cost": 1.5}]


async def test_chargers_include_live_sessions(client):
  store.add_tariff(__import__("datetime").date(2026, 1, 1), 0.30)
  store.add_tag("card1", "Car")
  online = store.get_state("CP_ON")
  online.connected = True
  online.connectors = {1: {"status": "Charging", "energy_wh": 12_500, "power_w": 7200}}
  store.get_state("CP_OFF")
  tid = store.start_session("CP_ON", 1, "card1", 10_000, "2026-11-22T08:00:00Z", True)
  store.start_session("CP_OFF", 1, "card1", 500, "2026-11-22T08:00:00Z", True)
  done = store.start_session("CP_ON", 1, "card1", 0, "2026-11-21T08:00:00Z", True)
  store.stop_session("CP_ON", done, 10_000, "2026-11-21T09:00:00Z", "Local")

  chargers = {c["id"]: c for c in (await client.get("/api/chargers")).json()}
  [live] = chargers["CP_ON"]["active_sessions"]
  assert (live["id"], live["tag_name"], live["connector_id"]) == (tid, "Car", 1)
  assert (live["energy_wh"], live["power_w"], live["currency"]) == (2500, 7200, "€")
  assert live["cost"] == pytest.approx(0.75)
  [offline] = chargers["CP_OFF"]["active_sessions"]
  assert (offline["energy_wh"], offline["cost"], offline["power_w"]) == (None, None, None)


async def test_backup_download(client):
  store.add_tag("abc", "Car")
  r = await client.get("/api/backup")
  assert r.status_code == 200 and r.content.startswith(b"SQLite format 3\x00")
  assert 'filename="simple-ocpp-' in r.headers["content-disposition"]
  await client.post("/api/settings/password", json={"new_password": "secret123"})
  await client.post("/api/logout")
  assert (await client.get("/api/backup")).status_code == 401  # holds the password hash


# --- Restart ---


async def test_restart_needs_a_running_server(client):
  r = await client.post("/api/restart")
  assert r.status_code == 409 and "by hand" in r.json()["detail"]


async def test_restart_checks_ports_then_restarts(client, monkeypatch):
  import asyncio, socket
  from chargepoint import lifecycle
  from chargepoint.api import settings as settings_api
  calls = []
  monkeypatch.setattr(settings_api, "RESTART_DELAY", 0)
  lifecycle.set_restart_handler(lambda: calls.append(1))
  try:
    with socket.socket() as busy:  # a port something else listens on
      busy.bind(("127.0.0.1", 0))
      busy.listen()
      taken = busy.getsockname()[1]
      with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
      store.set_active_network(store.network_settings())
      store.set_network_settings({"ocpp_host": "127.0.0.1", "ocpp_port": taken,
                                  "http_host": "127.0.0.1", "http_port": free})
      r = await client.post("/api/restart")
      assert r.status_code == 409 and f"127.0.0.1:{taken} is not available" in r.json()["detail"]
      await asyncio.sleep(0.05)
      assert calls == []

    store.set_network_settings({"ocpp_host": "127.0.0.1", "ocpp_port": taken,
                                "http_host": "127.0.0.1", "http_port": free})
    r = await client.post("/api/restart")
    assert r.status_code == 200 and r.json()["next_start"]["http_port"] == free
    await asyncio.sleep(0.05)
    assert calls == [1]
  finally:
    lifecycle.set_restart_handler(None)


async def test_price_url(client):
  url = "https://example.com/prices?year=2026"
  r = await client.post("/api/prices/url", json={"url": f" {url} "})
  assert r.status_code == 200 and r.json()["price_url"] == url
  assert (await client.get("/api/prices")).json()["price_url"] == url
  for bad in ("javascript:alert(1)", "data:text/html,x", "example.com", "https://", "http://a b/"):
    assert (await client.post("/api/prices/url", json={"url": bad})).status_code == 422, bad
  assert (await client.get("/api/prices")).json()["price_url"] == url
  r = await client.post("/api/prices/url", json={"url": ""})
  assert r.json()["price_url"] == ""


async def test_logs_filter_by_level_and_source(client, monkeypatch):
  import logging
  from chargepoint import logbuffer
  buffer = logbuffer.BufferHandler(capacity=3)
  monkeypatch.setattr(logbuffer, "buffer", buffer)
  for name, level, text in (("root", logging.INFO, "dropped"), ("root", logging.INFO, "hello"),
                            ("ocpp", logging.WARNING, "careful"), ("uvicorn.error", logging.ERROR, "broken")):
    buffer.handle(logging.LogRecord(name, level, "x", 1, text, None, None))
  data = (await client.get("/api/logs")).json()
  assert [e["message"] for e in data["entries"]] == ["broken", "careful", "hello"]
  assert data["sources"] == ["ocpp", "server", "web"]
  data = (await client.get("/api/logs", params={"level": "WARNING"})).json()
  assert [e["message"] for e in data["entries"]] == ["broken", "careful"]
  data = (await client.get("/api/logs", params={"source": "ocpp"})).json()
  assert [e["level"] for e in data["entries"]] == ["WARNING"]
  assert len((await client.get("/api/logs", params={"limit": 1})).json()["entries"]) == 1
  assert (await client.get("/api/logs", params={"level": "LOUD"})).status_code == 422
  assert (await client.get("/logs")).status_code == 200
  await client.post("/api/settings/password", json={"new_password": "secret123"})
  await client.post("/api/logout")
  assert (await client.get("/api/logs")).status_code == 401


def test_repeating_ocpp_lines_become_trace_with_their_replies():
  import logging
  from chargepoint import logbuffer
  log_filter = logbuffer.OcppLevelFilter()
  def level(fmt, frame):
    record = logging.LogRecord("ocpp", logging.INFO, "x", 1, fmt, ("CP", frame), None)
    assert log_filter.filter(record)
    return record.levelname
  assert level("%s: receive message %s", '[2,"a","Heartbeat",{}]') == "TRACE"
  assert level("%s: send %s", '[3,"a",{"currentTime":"t"}]') == "TRACE"
  assert level("%s: send %s", '[3,"a",{"currentTime":"t"}]') == "DEBUG"  # id is used up
  assert level("%s: receive message %s", '[2,"b","MeterValues",{}]') == "TRACE"
  assert level("%s: receive message %s", '[2,"c","BootNotification",{}]') == "DEBUG"
  assert level("%s: send %s", '[3,"c",{"status":"Accepted"}]') == "DEBUG"
  assert level("%s: receive message %s", "not json") == "DEBUG"
  assert level("%s: receive message %s", '[2,"d",{"not":"an action"},{}]') == "DEBUG"
  assert level("%s: receive message %s", '[2,["bad id"],"Heartbeat",{}]') == "DEBUG"


async def test_trace_is_hidden_by_default_and_cannot_push_out_info(client, monkeypatch):
  import logging
  from chargepoint import logbuffer
  buffer = logbuffer.BufferHandler(capacity=2)
  monkeypatch.setattr(logbuffer, "buffer", buffer)
  buffer.handle(logging.LogRecord("root", logging.INFO, "x", 1, "important", None, None))
  for i in range(5):
    buffer.handle(logging.LogRecord("root", logbuffer.TRACE, "x", 1, f"beat {i}", None, None))
  info = (await client.get("/api/logs")).json()["entries"]
  assert [e["message"] for e in info] == ["important"]
  everything = (await client.get("/api/logs", params={"level": "TRACE"})).json()["entries"]
  assert [e["message"] for e in everything] == ["beat 4", "beat 3", "important"]
  assert everything[0]["level"] == "TRACE"


def test_library_lines_get_the_right_level():
  import logging
  from chargepoint import logbuffer
  def level(log_filter, msg, original=logging.INFO, args=()):
    record = logging.LogRecord("x", original, "x", 1, msg, args, None)
    assert log_filter.filter(record)
    return record.levelname
  server = logbuffer.ServerLibraryFilter()
  assert level(server, "connection open") == "DEBUG"
  assert level(server, "Application startup complete.") == "DEBUG"
  assert level(server, "connection rejected (%d %s)", args=(400, "Bad Request")) == "WARNING"
  assert level(server, "Invalid HTTP request received.", logging.WARNING) == "WARNING"
  assert level(logbuffer.OcppLevelFilter(), "Received a CALLError: %s'", logging.WARNING, ("x",)) == "DEBUG"
  assert level(logbuffer.OcppLevelFilter(), "Ignoring response with unknown unique id: %s", logging.ERROR, ("x",)) == "ERROR"


async def test_login_activity_is_logged_without_the_password(client, caplog):
  import logging
  caplog.set_level(logging.INFO)
  def lines(text):
    return [(r.levelname, r.getMessage()) for r in caplog.records if text in r.getMessage()]
  await client.post("/api/logout")  # not logged in: nothing to report
  assert lines("Logout") == []
  await client.post("/api/settings/password", json={"new_password": "secret123"})
  await client.post("/api/logout")
  assert lines("Logout") == [("INFO", "Logout from 127.0.0.1")]
  await client.post("/api/login", json={"password": "wrong-guess"})
  assert lines("Failed login") == [("WARNING", "Failed login from 127.0.0.1: wrong password")]
  await client.post("/api/login", json={"password": "secret123"})
  assert lines("Login from") == [("INFO", "Login from 127.0.0.1")]
  await client.post("/api/settings/password",
                    json={"current_password": "nope-nope", "new_password": "other1234"})
  assert [level for level, _ in lines("wrong current password")] == ["WARNING"]
  logged = " ".join(r.getMessage() for r in caplog.records)
  assert not any(secret in logged for secret in ("secret123", "wrong-guess", "nope-nope", "other1234"))


async def test_localisation_settings(client):
  assert (await client.get("/api/localisation")).json() == {
      "time_format": "browser", "decimal_separator": "browser", "currency": "€"}
  body = {"time_format": "24h", "decimal_separator": ",", "currency": "EUR"}
  assert (await client.post("/api/localisation", json=body)).json() == body
  assert (await client.get("/api/prices")).json()["currency"] == "EUR"
  for bad in ({"time_format": "9h"}, {"decimal_separator": ";"}, {"currency": ""}, {"currency": "toolong"}):
    assert (await client.post("/api/localisation", json={**body, **bad})).status_code == 422, bad
  assert (await client.get("/api/localisation")).json() == body
  assert (await client.post("/api/prices/currency", json={"currency": "X"})).status_code in (404, 405)


async def test_csv_follows_decimal_separator(client):
  from datetime import date
  store.add_tariff(date(2026, 1, 1), 0.5)
  store.record_energy("A", 1, 0, date(2026, 1, 1))
  store.record_energy("A", 1, 2500, date(2026, 1, 2))
  params = {"period": "month"}
  r = await client.get("/api/energy.csv", params=params)
  assert r.text.lstrip("\ufeff").splitlines()[1] == "2026-01,2.500,1.25"
  await client.post("/api/localisation", json={
      "time_format": "browser", "decimal_separator": ",", "currency": "€"})
  r = await client.get("/api/energy.csv", params=params)
  assert r.text.lstrip("\ufeff").splitlines() == ["Month;Energy (kWh);Cost (€)", "2026-01;2,500;1,25"]
  r = await client.get("/api/energy.csv", params={**params, "decimal": "."})  # as the page shows it
  assert r.text.lstrip("\ufeff").splitlines()[1] == "2026-01,2.500,1.25"
  await client.post("/api/localisation", json={
      "time_format": "browser", "decimal_separator": "browser", "currency": "€"})
  r = await client.get("/api/energy.csv", params={**params, "decimal": ","})  # a comma browser
  assert r.text.lstrip("\ufeff").splitlines()[1] == "2026-01;2,500;1,25"
  r = await client.get("/api/sessions.csv", params={"decimal": ","})
  assert r.text.lstrip("\ufeff").startswith("Transaction;Charger")
  assert (await client.get("/api/energy.csv", params={"decimal": ";"})).status_code == 422
