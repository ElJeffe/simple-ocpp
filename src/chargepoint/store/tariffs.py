"""Electricity prices, used to put a cost on sessions and energy.

A tariff is a price per kWh valid from a date until the next tariff's date.
The earliest tariff also applies to everything before its date, so adding a
price never leaves older energy without a cost.
"""

import bisect
import logging
from datetime import date, datetime
from typing import TypedDict, cast

from .settings import get_setting, set_setting

DEFAULT_CURRENCY = "€"


class Tariff(TypedDict):
  valid_from: str  # YYYY-MM-DD
  price: float  # per kWh


class Prices:
  """Looks up the price per kWh on a day. Load once, use for many lookups."""

  def __init__(self, tariffs: list[Tariff]):
    ordered = sorted(tariffs, key=lambda t: t["valid_from"])
    self.days = [t["valid_from"] for t in ordered]
    self.prices = [t["price"] for t in ordered]

  def __bool__(self) -> bool:
    return bool(self.prices)

  def on(self, day: date) -> float | None:
    """Price per kWh on `day`, or None if no tariff is set at all."""
    if not self.prices:
      return None
    i = bisect.bisect_right(self.days, day.isoformat()) - 1
    return self.prices[max(i, 0)]

  def cost(self, energy_wh: float | None, day: date | None) -> float | None:
    """Cost of `energy_wh` used on `day`; None without a tariff or energy."""
    if energy_wh is None or day is None or (price := self.on(day)) is None:
      return None
    return energy_wh / 1000 * price


def tariffs() -> list[Tariff]:
  saved = get_setting("tariffs")
  result: list[Tariff] = []
  if isinstance(saved, list):
    for item in cast(list[object], saved):
      if isinstance(item, dict):
        item = cast(dict[str, object], item)
        valid_from, price = item.get("valid_from"), item.get("price")
        if isinstance(valid_from, str) and isinstance(price, (int, float)):
          result.append({"valid_from": valid_from, "price": float(price)})
  return sorted(result, key=lambda t: t["valid_from"])


def prices() -> Prices:
  return Prices(tariffs())


def currency() -> str:
  saved = get_setting("currency")
  return saved if isinstance(saved, str) and saved else DEFAULT_CURRENCY


def _save(new: list[Tariff]) -> None:
  days = [t["valid_from"] for t in new]
  if len(days) != len(set(days)):
    raise ValueError("Another price already starts on that date")
  set_setting("tariffs", sorted(new, key=lambda t: t["valid_from"]))


def set_tariffs(new: list[Tariff], new_currency: str) -> None:
  """Replaces all tariffs. Raises ValueError for two tariffs on one date."""
  _save(new)
  set_currency(new_currency)


def add_tariff(valid_from: date, price: float) -> Tariff:
  """Raises ValueError if a price already starts on that date."""
  tariff: Tariff = {"valid_from": valid_from.isoformat(), "price": price}
  _save([*tariffs(), tariff])
  logging.info(f"Price added: {tariff}")
  return tariff


def update_tariff(old_valid_from: date, valid_from: date, price: float) -> Tariff:
  """Changes the price starting on old_valid_from, possibly moving its date.

  Raises KeyError if there is no such price, ValueError if the new date is
  taken by another price.
  """
  current = tariffs()
  rest = [t for t in current if t["valid_from"] != old_valid_from.isoformat()]
  if len(rest) == len(current):
    raise KeyError(old_valid_from.isoformat())
  tariff: Tariff = {"valid_from": valid_from.isoformat(), "price": price}
  _save([*rest, tariff])
  logging.info(f"Price from {old_valid_from} changed to {tariff}")
  return tariff


def remove_tariff(valid_from: date) -> None:
  """Raises KeyError if no price starts on that date."""
  current = tariffs()
  rest = [t for t in current if t["valid_from"] != valid_from.isoformat()]
  if len(rest) == len(current):
    raise KeyError(valid_from.isoformat())
  _save(rest)
  logging.info(f"Price from {valid_from} removed")


def set_currency(new_currency: str) -> None:
  set_setting("currency", new_currency)


def price_url() -> str:
  """A link to where the user can look up current prices; never fetched."""
  saved = get_setting("price_url")
  return saved if isinstance(saved, str) else ""


def set_price_url(url: str) -> None:
  set_setting("price_url", url)


def local_day(timestamp: str | None) -> date | None:
  """The server-local date of an ISO timestamp from a charger, if valid."""
  if not timestamp:
    return None
  try:
    return datetime.fromisoformat(timestamp).astimezone().date()
  except ValueError:
    return None
