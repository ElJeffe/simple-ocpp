"""Settings stored as JSON key/value pairs. Not cached: they are rarely read."""

import calendar
import json
import logging
import os
from typing import TypedDict, cast

from ..config import NETWORK_DEFAULTS
from .database import db


def get_setting(key: str) -> object:
  """The stored value, or None if it was never set."""
  with db() as conn:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
  return json.loads(row["value"]) if row else None


def set_setting(key: str, value: object) -> None:
  with db() as conn:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )


class NetworkSettings(TypedDict):
  ocpp_host: str
  ocpp_port: int
  http_host: str
  http_port: int


_active_network: NetworkSettings | None = None


def set_active_network(values: NetworkSettings) -> None:
  """Records what the running server listens on; called by main() at startup."""
  global _active_network
  _active_network = values


def active_network() -> NetworkSettings | None:
  """What the running server listens on, or None when not started by main()."""
  return _active_network


def saved_network_settings() -> NetworkSettings:
  """As saved on the settings page (defaults for anything never saved)."""
  saved = get_setting("network")
  saved = cast(dict[str, object], saved) if isinstance(saved, dict) else {}
  values: dict[str, object] = {}
  for name, (default, _) in NETWORK_DEFAULTS.items():
    value = saved.get(name)
    values[name] = value if type(value) is type(default) else default
  return cast(NetworkSettings, values)


def network_overrides() -> dict[str, str]:
  """Settings set by an environment variable: name -> variable name."""
  return {name: var for name, (_, var) in NETWORK_DEFAULTS.items() if os.environ.get(var)}


def network_settings() -> NetworkSettings:
  """What to listen on: saved settings, overridden by environment variables."""
  values = dict(saved_network_settings())
  for name, var in network_overrides().items():
    raw = os.environ[var]
    if name.endswith("_port"):
      if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise SystemExit(f"{var}={raw!r} is not a port number (1-65535)")
      values[name] = int(raw)
    else:
      values[name] = raw
  return cast(NetworkSettings, values)


def set_network_settings(values: NetworkSettings) -> None:
  set_setting("network", dict(values))
  logging.info(f"Network settings saved: {values}")


def fiscal_year_start() -> tuple[int, int]:
  """(month, day) on which a fiscal year starts; 1 January by default."""
  saved = get_setting("fiscal_year_start")
  if isinstance(saved, dict):
    saved = cast(dict[str, object], saved)
    month, day = saved.get("month"), saved.get("day")
    if isinstance(month, int) and isinstance(day, int):
      return month, day
  return 1, 1


def set_fiscal_year_start(month: int, day: int) -> None:
  """Raises ValueError for a date that doesn't exist every year."""
  if not 1 <= month <= 12 or not 1 <= day <= calendar.monthrange(2001, month)[1]:
    raise ValueError(f"{day}/{month} is not a date that exists every year")
  set_setting("fiscal_year_start", {"month": month, "day": day})
