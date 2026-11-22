# pyright: standard
import sqlite3

import pytest
from ocpp.v16.enums import AuthorizationStatus

from chargepoint import store


def test_tag_key_is_case_and_whitespace_insensitive():
  assert store.tag_key(" abc123 ") == store.tag_key("ABC123") == "ABC123"
  assert store.tag_key(None) == ""


def test_unknown_tag_is_invalid():
  assert store.tag_status("nope") == AuthorizationStatus.invalid
  assert store.tag_status("") == AuthorizationStatus.invalid


def test_authorized_and_blocked_tags():
  store.add_tag("abc", "Car")
  assert store.tag_status("ABC") == AuthorizationStatus.accepted
  store.update_tag("abc", authorized=False)
  assert store.tag_status("abc") == AuthorizationStatus.blocked


def test_add_tag_strips_and_rejects_duplicates():
  tag = store.add_tag("  abc ", " Car ")
  assert tag["id_tag"] == "abc"
  assert tag["name"] == "Car"
  assert tag["authorized"] is True
  with pytest.raises(KeyError):
    store.add_tag("ABC")


def test_update_tag_reports_revocation_only_when_access_is_removed():
  store.add_tag("abc")
  _, revoked = store.update_tag("abc", name="New")
  assert not revoked
  _, revoked = store.update_tag("abc", authorized=False)
  assert revoked
  _, revoked = store.update_tag("abc", authorized=False)
  assert not revoked  # was already blocked
  _, revoked = store.update_tag("abc", authorized=True)
  assert not revoked


def test_update_and_remove_unknown_tag_raise():
  with pytest.raises(KeyError):
    store.update_tag("nope", name="x")
  with pytest.raises(KeyError):
    store.remove_tag("nope")


def test_remove_tag():
  store.add_tag("abc")
  store.remove_tag("ABC")
  assert store.tags == {}


def test_sorted_tags_by_name_then_id():
  store.add_tag("b", "beta")
  store.add_tag("a", "Alpha")
  store.add_tag("c", "alpha")
  assert [t["id_tag"] for t in store.sorted_tags()] == ["a", "c", "b"]


def test_tags_survive_reload():
  store.add_tag("abc", "Car")
  store.update_tag("abc", authorized=False)
  store.tags.clear()
  store.load_tags()
  assert store.tags["ABC"]["name"] == "Car"
  assert store.tags["ABC"]["authorized"] is False


def test_registry_survives_reload():
  s = store.get_state("CP_1")
  s.desired_locked = False
  s.lock_on_plugin = False
  s.vendor = "V"
  s.last_rfid = "abc"
  s.connected = True  # runtime only, must not be persisted
  store.save_registry()
  store.registry.clear()
  store.load_registry()
  loaded = store.registry["CP_1"]
  assert (loaded.desired_locked, loaded.vendor, loaded.last_rfid) == (False, "V", "abc")
  assert loaded.lock_on_plugin is False
  assert loaded.connected is False


def test_get_state_creates_and_saves_new_charger():
  store.get_state("CP_NEW")
  with store.db() as conn:
    ids = [r["id"] for r in conn.execute("SELECT id FROM chargers")]
  assert ids == ["CP_NEW"]


def test_removed_tag_stays_removed_after_reload():
  store.add_tag("abc")
  store.add_tag("def")
  store.remove_tag("abc")
  store.tags.clear()
  store.load_tags()
  assert set(store.tags) == {"DEF"}


def test_init_db_keeps_existing_data():
  store.add_tag("abc", "Car")
  store.init_db()
  store.tags.clear()
  store.load_tags()
  assert store.tags["ABC"]["name"] == "Car"


def test_failed_write_leaves_cache_unchanged(monkeypatch, tmp_path):
  store.add_tag("abc", "Car")
  monkeypatch.setattr(store.database, "DB_FILE", tmp_path / "missing" / "x.db")
  with pytest.raises(sqlite3.Error):
    store.update_tag("abc", name="Van")
  with pytest.raises(sqlite3.Error):
    store.add_tag("new")
  assert store.tags["ABC"]["name"] == "Car"
  assert "NEW" not in store.tags


def test_ensure_data_dir_creates_directory(tmp_path, monkeypatch):
  target = tmp_path / "nested" / "data"
  monkeypatch.setattr(store.database, "DATA_DIR", target)
  store.ensure_data_dir()
  assert target.is_dir() and list(target.iterdir()) == []


def test_ensure_data_dir_fails_clearly_when_not_writable(tmp_path, monkeypatch):
  blocker = tmp_path / "file"
  blocker.write_text("")  # a file where the directory should be
  monkeypatch.setattr(store.database, "DATA_DIR", blocker / "data")
  with pytest.raises(SystemExit, match="OCPP_DATA_DIR"):
    store.ensure_data_dir()


def test_session_start_and_stop():
  store.add_tag("abc", "Car")
  tid = store.start_session("CP_1", 1, "ABC", 1000, "2026-01-01T10:00:00Z", True)
  [s] = store.list_sessions()
  assert (s["id"], s["tag_name"], s["accepted"]) == (tid, "Car", True)
  assert s["meter_stop_wh"] is None and s["energy_wh"] is None
  assert store.stop_session("CP_1", tid, 3500, "2026-01-01T12:00:00Z", "EVDisconnected")
  [s] = store.list_sessions()
  assert (s["energy_wh"], s["stopped_at"], s["stop_reason"]) == (
      2500, "2026-01-01T12:00:00Z", "EVDisconnected"
  )


def test_session_ids_are_unique_and_survive_deletion():
  first = store.start_session("CP_1", 1, "a", 0, "t", True)
  with store.db() as conn:
    conn.execute("DELETE FROM sessions")
  assert store.start_session("CP_1", 1, "a", 0, "t", True) > first


def test_stop_session_needs_matching_charger():
  tid = store.start_session("CP_1", 1, "a", 0, "t", True)
  assert not store.stop_session("CP_2", tid, 10, "t", None)
  assert not store.stop_session("CP_1", tid + 1, 10, "t", None)


def test_list_sessions_newest_first_filtered_and_limited():
  for cp in ("A", "B", "A", "A"):
    store.start_session(cp, 1, "a", 0, "t", True)
  assert [s["id"] for s in store.list_sessions()] == [4, 3, 2, 1]
  assert [s["id"] for s in store.list_sessions("A", limit=2)] == [4, 3]


def test_init_db_adds_missing_columns_to_old_database():
  with store.db() as conn:
    conn.execute("DROP TABLE chargers")
    conn.execute("CREATE TABLE chargers (id TEXT PRIMARY KEY, desired_locked INTEGER"
                 " NOT NULL DEFAULT 1, vendor TEXT NOT NULL DEFAULT '', model TEXT NOT"
                 " NULL DEFAULT '', last_seen TEXT NOT NULL DEFAULT '', last_rfid TEXT"
                 " NOT NULL DEFAULT '', last_rfid_at TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO chargers (id) VALUES ('OLD')")
  store.init_db()
  store.load_registry()
  assert store.registry["OLD"].lock_on_plugin is True


def test_tag_groups_share_one_spelling():
  store.add_group(" Family ")
  store.add_tag("a", parent_id_tag="family ")
  store.add_tag("b", parent_id_tag="FAMILY")
  store.add_tag("c")
  assert store.tag_parent("A") == store.tag_parent("b") == "Family"
  assert store.tag_parent("c") is None
  assert store.tag_parent("unknown") is None


def test_changing_group_marks_tag_stale():
  store.add_group("Family")
  store.add_tag("a")
  _, stale = store.update_tag("a", parent_id_tag="Family")
  assert stale
  _, stale = store.update_tag("a", parent_id_tag="family")  # same group
  assert not stale
  _, stale = store.update_tag("a", parent_id_tag="")
  assert stale and store.tag_parent("a") is None


def test_groups_survive_reload():
  store.add_group("Family")
  store.add_group("Empty")
  store.add_tag("a", parent_id_tag="Family")
  store.tags.clear()
  store.groups.clear()
  store.load_tags()
  assert store.tags["A"]["parent_id_tag"] == "Family"
  assert [g["name"] for g in store.sorted_groups()] == ["Empty", "Family"]


def test_unknown_group_is_rejected():
  with pytest.raises(ValueError):
    store.add_tag("a", parent_id_tag="Nope")
  assert store.tags == {}
  store.add_tag("a")
  with pytest.raises(ValueError):
    store.update_tag("a", parent_id_tag="Nope")
  assert store.tags["A"]["parent_id_tag"] == ""


def test_add_group_rejects_duplicates():
  store.add_group("Family")
  with pytest.raises(KeyError):
    store.add_group("FAMILY ")


def test_remove_group_ungroups_its_tags():
  store.add_group("Family")
  store.add_group("Work")
  store.add_tag("a", parent_id_tag="Family")
  store.add_tag("b", parent_id_tag="Work")
  removed = store.remove_group("family")
  assert [t["id_tag"] for t in removed] == ["a"]
  assert store.tag_parent("a") is None and store.tag_parent("b") == "Work"
  store.tags.clear()
  store.groups.clear()
  store.load_tags()  # and the database agrees
  assert store.tag_parent("a") is None
  assert [g["name"] for g in store.sorted_groups()] == ["Work"]
  with pytest.raises(KeyError):
    store.remove_group("Family")


def test_groups_only_stored_on_tags_get_created_on_load():
  with store.db() as conn:
    conn.execute("INSERT INTO tags (key, id_tag, parent_id_tag) VALUES ('A', 'a', 'Old')")
  store.load_tags()
  assert [g["name"] for g in store.sorted_groups()] == ["Old"]


def test_stop_session_records_stopping_tag():
  tid = store.start_session("CP_1", 1, "a", 0, "t", True)
  store.stop_session("CP_1", tid, 10, "t", "Local", "b")
  assert store.list_sessions()[0]["stop_id_tag"] == "b"


def test_init_db_adds_group_and_stop_tag_columns():
  with store.db() as conn:
    conn.execute("DROP TABLE tags")
    conn.execute("CREATE TABLE tags (key TEXT PRIMARY KEY, id_tag TEXT NOT NULL,"
                 " name TEXT NOT NULL DEFAULT '', authorized INTEGER NOT NULL"
                 " DEFAULT 0, added TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO tags (key, id_tag) VALUES ('OLD', 'old')")
    conn.execute("ALTER TABLE sessions DROP COLUMN stop_id_tag")
  store.init_db()
  store.load_tags()
  assert store.tags["OLD"]["parent_id_tag"] == ""
  tid = store.start_session("CP_1", 1, "a", 0, "t", True)
  assert store.stop_session("CP_1", tid, 10, "t", None, "b")


def day(s):
  from datetime import date
  return date.fromisoformat(s)


def test_energy_keeps_last_reading_of_each_day():
  store.record_energy("CP_1", 1, 1000, day("2026-11-01"))
  store.record_energy("CP_1", 1, 1500, day("2026-11-01"))
  store.record_energy("CP_1", 1, 4000, day("2026-11-02"))
  with store.db() as conn:
    rows = [tuple(r) for r in conn.execute(
        "SELECT day, energy_wh FROM energy_readings ORDER BY day")]
  assert rows == [("2026-11-01", 1500), ("2026-11-02", 4000)]
  assert store.energy_usage("day") == [
      {"period": "2026-11-01", "energy_wh": 0},  # first reading: nothing to compare
      {"period": "2026-11-02", "energy_wh": 2500},
  ]


def test_energy_usage_per_month_and_year_and_charger():
  for d, wh in [("2025-12-31", 0), ("2026-01-01", 1000), ("2026-01-15", 3000),
                ("2026-02-01", 3500)]:
    store.record_energy("A", 1, wh, day(d))
  store.record_energy("B", 1, 500, day("2026-01-01"))
  store.record_energy("B", 1, 700, day("2026-01-02"))
  assert store.energy_usage("month") == [
      {"period": "2025-12", "energy_wh": 0},
      {"period": "2026-01", "energy_wh": 3200},
      {"period": "2026-02", "energy_wh": 500},
  ]
  assert store.energy_usage("year") == [
      {"period": "2025", "energy_wh": 0}, {"period": "2026", "energy_wh": 3700},
  ]
  assert store.energy_usage("year", "B") == [{"period": "2026", "energy_wh": 200}]


def test_energy_gap_days_count_on_next_reading_and_resets_count_zero():
  store.record_energy("A", 1, 1000, day("2026-03-01"))
  store.record_energy("A", 1, 4000, day("2026-03-05"))  # server off for 3 days
  store.record_energy("A", 1, 100, day("2026-03-06"))   # meter replaced
  store.record_energy("A", 1, 600, day("2026-03-07"))
  assert [u["energy_wh"] for u in store.energy_usage("day")] == [0, 3000, 0, 500]


def test_energy_ignores_connector_0_when_connectors_report():
  store.record_energy("A", 0, 10000, day("2026-03-01"))
  store.record_energy("A", 0, 12000, day("2026-03-02"))
  store.record_energy("A", 1, 100, day("2026-03-01"))
  store.record_energy("A", 1, 1100, day("2026-03-02"))
  store.record_energy("B", 0, 50, day("2026-03-01"))  # only reports connector 0
  store.record_energy("B", 0, 80, day("2026-03-02"))
  assert store.energy_usage("day")[-1] == {"period": "2026-03-02", "energy_wh": 1030}


# --- Settings ---


def test_network_settings_defaults_saved_and_environment(monkeypatch):
  assert store.network_settings() == {
      "ocpp_host": "0.0.0.0", "ocpp_port": 9000, "http_host": "0.0.0.0", "http_port": 8090,
  }
  store.set_network_settings({
      "ocpp_host": "192.168.1.5", "ocpp_port": 9100, "http_host": "127.0.0.1", "http_port": 8100,
  })
  monkeypatch.setenv("OCPP_PORT", "9999")
  assert store.network_overrides() == {"ocpp_port": "OCPP_PORT"}
  assert store.saved_network_settings()["ocpp_port"] == 9100
  assert store.network_settings() == {
      "ocpp_host": "192.168.1.5", "ocpp_port": 9999, "http_host": "127.0.0.1", "http_port": 8100,
  }


def test_bad_port_in_environment_stops_startup(monkeypatch):
  monkeypatch.setenv("OCPP_HTTP_PORT", "http")
  with pytest.raises(SystemExit, match="OCPP_HTTP_PORT"):
    store.network_settings()


def test_malformed_saved_network_settings_fall_back_to_defaults():
  store.set_setting("network", {"ocpp_port": "9100", "http_host": 5, "extra": 1})
  assert store.saved_network_settings() == {
      "ocpp_host": "0.0.0.0", "ocpp_port": 9000, "http_host": "0.0.0.0", "http_port": 8090,
  }


def test_fiscal_year_start():
  assert store.fiscal_year_start() == (1, 1)
  store.set_fiscal_year_start(8, 15)
  assert store.fiscal_year_start() == (8, 15)
  for month, d in ((2, 29), (4, 31), (13, 1), (1, 0)):
    with pytest.raises(ValueError):
      store.set_fiscal_year_start(month, d)
  assert store.fiscal_year_start() == (8, 15)


def test_fiscal_year_of_a_day():
  assert store.fiscal_year(day("2026-08-14"), (8, 15)) == 2025
  assert store.fiscal_year(day("2026-08-15"), (8, 15)) == 2026
  assert store.fiscal_year(day("2026-01-01"), (1, 1)) == 2026


def test_yearly_energy_uses_fiscal_years():
  store.set_fiscal_year_start(7, 1)
  for d, wh in [("2025-06-01", 0), ("2025-06-30", 1000), ("2025-07-01", 1500),
                ("2026-06-30", 4500), ("2026-07-01", 5000)]:
    store.record_energy("A", 1, wh, day(d))
  assert store.energy_usage("year") == [
      {"period": "2024", "energy_wh": 1000},   # 2024-25: until 30 June 2025
      {"period": "2025", "energy_wh": 3500},
      {"period": "2026", "energy_wh": 500},
  ]
  assert store.energy_usage("month")[0] == {"period": "2025-06", "energy_wh": 1000}


# --- Initial energy ---


def test_initial_energy_counts_usage_until_first_reading():
  store.set_initial_energy("A", day("2026-08-10"), 100_000)
  store.record_energy("A", 1, 160_000, day("2026-11-20"))
  store.record_energy("A", 1, 165_000, day("2026-11-21"))
  assert store.energy_usage("day") == [
      {"period": "2026-08-10", "energy_wh": 0},
      {"period": "2026-11-20", "energy_wh": 60_000},
      {"period": "2026-11-21", "energy_wh": 5_000},
  ]
  assert store.energy_usage("year") == [{"period": "2026", "energy_wh": 65_000}]
  assert store.clear_initial_energy("A") and not store.clear_initial_energy("A")
  assert store.energy_usage("year") == [{"period": "2026", "energy_wh": 5_000}]


def test_initial_energy_on_a_reading_day_counts_before_that_reading():
  store.set_initial_energy("A", day("2026-11-20"), 1000)
  store.record_energy("A", 1, 1600, day("2026-11-20"))
  assert store.energy_usage("day") == [{"period": "2026-11-20", "energy_wh": 600}]


def test_initial_energy_applies_to_lowest_connector_and_respects_filter():
  store.set_initial_energy("A", day("2026-01-01"), 500)
  store.set_initial_energy("B", day("2026-01-01"), 0)
  store.record_energy("A", 2, 900, day("2026-02-01"))
  store.record_energy("A", 3, 7000, day("2026-02-01"))
  assert store.energy_usage("year", "A") == [{"period": "2026", "energy_wh": 400}]
  assert store.energy_usage("year") == [{"period": "2026", "energy_wh": 400}]
  assert store.initial_energy()["B"] == {"day": "2026-01-01", "energy_wh": 0}


def test_list_sessions_pages_and_period_filter():
  from datetime import datetime, timezone
  for started in ("2026-10-31T23:59:59Z", "2026-11-01T00:00:00+00:00",
                  "2026-11-15T10:00:00.123Z", "2026-11-30T23:00:00-02:00",  # = Dec 1 01:00 UTC
                  "2026-12-01T00:00:00Z"):
    store.start_session("A", 1, "a", 0, started, True)
  nov = (datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 12, 1, tzinfo=timezone.utc))
  assert [s["id"] for s in store.list_sessions(since=nov[0], until=nov[1])] == [3, 2]
  assert store.count_sessions(since=nov[0], until=nov[1]) == 2
  assert store.count_sessions(since=nov[0]) == 4 and store.count_sessions() == 5
  assert [s["id"] for s in store.list_sessions(limit=2)] == [5, 4]
  assert [s["id"] for s in store.list_sessions(limit=2, offset=2)] == [3, 2]
  assert [s["id"] for s in store.list_sessions(limit=2, offset=4)] == [1]
  assert store.count_sessions("B") == 0


# --- Removing chargers and closing sessions without a stop ---


def test_delete_charger_keeps_or_deletes_history():
  for cp in ("A", "B"):
    store.get_state(cp)
    store.start_session(cp, 1, "a", 0, "2026-11-01T10:00:00Z", True)
    store.record_energy(cp, 1, 1000, day("2026-11-01"))
    store.set_initial_energy(cp, day("2026-10-01"), 500)
  store.delete_charger("A")
  store.delete_charger("B", delete_history=True)
  assert store.registry == {}
  store.load_registry()
  assert store.registry == {}
  assert [s["charger_id"] for s in store.list_sessions()] == ["A"]
  assert set(store.initial_energy()) == {"A"}
  assert store.energy_usage("day", "B") == []
  with pytest.raises(KeyError):
    store.delete_charger("A")


def test_new_session_closes_open_one_on_same_connector():
  old = store.start_session("A", 1, "a", 1000, "2026-11-01T10:00:00Z", True)
  other = store.start_session("A", 2, "a", 0, "2026-11-01T10:00:00Z", True)
  assert store.close_open_sessions("A", "2026-11-02T08:00:00Z", 1, 4000) == [old]
  by_id = {s["id"]: s for s in store.list_sessions()}
  assert by_id[old]["energy_wh"] == 3000
  assert by_id[old]["stop_reason"] == store.NO_STOP_RECEIVED
  assert by_id[old]["stopped_at"] == "2026-11-02T08:00:00Z"
  assert by_id[other]["stopped_at"] is None
  assert store.close_open_sessions("A", "t", 1, 5000) == []  # nothing left open there


def test_boot_close_uses_last_meter_reading_and_real_stop_wins():
  tid = store.start_session("A", 1, "a", 1000, "2026-11-01T10:00:00Z", True)
  store.record_energy("A", 1, 2500, day("2026-11-01"))
  no_reading = store.start_session("A", 2, "a", 700, "2026-11-01T10:00:00Z", True)
  assert store.close_open_sessions("A", "2026-11-01T12:00:00Z") == [tid, no_reading]
  by_id = {s["id"]: s for s in store.list_sessions()}
  assert by_id[tid]["energy_wh"] == 1500 and by_id[no_reading]["energy_wh"] == 0
  store.stop_session("A", tid, 2600, "2026-11-01T11:59:00Z", "Reboot")  # queued stop arrives
  s = store.list_sessions()[-1]
  assert (s["energy_wh"], s["stop_reason"]) == (1600, "Reboot")
