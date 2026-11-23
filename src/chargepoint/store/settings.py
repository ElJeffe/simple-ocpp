"""Settings stored as JSON key/value pairs. Not cached: they are rarely read."""

import json
import logging
import os
from typing import Literal, TypedDict, cast, get_args

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


TimeFormat = Literal["browser", "24h", "12h"]
DecimalSeparator = Literal["browser", ".", ","]
DEFAULT_CURRENCY = "€"


class Localisation(TypedDict):
  time_format: TimeFormat
  decimal_separator: DecimalSeparator
  currency: str


def currency() -> str:
  saved = get_setting("currency")
  return saved if isinstance(saved, str) and saved else DEFAULT_CURRENCY


def set_currency(new_currency: str) -> None:
  set_setting("currency", new_currency)


def localisation() -> Localisation:
  """How the pages show times, decimals and money."""
  time_format, separator = get_setting("time_format"), get_setting("decimal_separator")
  return {
      "time_format": cast(TimeFormat, time_format) if time_format in get_args(TimeFormat) else "browser",
      "decimal_separator": (
          cast(DecimalSeparator, separator) if separator in get_args(DecimalSeparator) else "browser"
      ),
      "currency": currency(),
  }


def set_localisation(values: Localisation) -> None:
  set_setting("time_format", values["time_format"])
  set_setting("decimal_separator", values["decimal_separator"])
  set_currency(values["currency"])
  logging.info(f"Localisation saved: {values}")
