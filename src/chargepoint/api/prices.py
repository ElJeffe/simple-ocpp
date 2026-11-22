"""Electricity prices: a price per kWh valid from a date until the next one."""

from datetime import date
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import store
from .models import Model

router = APIRouter()


class Prices(BaseModel):
  currency: str
  tariffs: list[store.Tariff]  # oldest first
  price_url: str  # where the user can look up prices; "" if none


class PriceIn(Model):
  valid_from: date
  # Whole currency units per kWh (0.305 = 30.5 c€/kWh); the web page enters
  # and shows cents.
  price: float = Field(ge=0, le=100, description="Per kWh in currency units, e.g. 0.305 for 30.5 c€/kWh")


class PriceUrlIn(Model):
  url: str = Field(max_length=2000)  # "" removes it

  @field_validator("url")
  @classmethod
  def web_address(cls, value: str) -> str:
    """Only http(s), since the page shows this as a clickable link."""
    if not value:
      return value
    try:
      parts = urlsplit(value)
      valid = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
      valid = False
    if not valid or any(c.isspace() or ord(c) < 32 for c in value):
      raise ValueError("must be a web address starting with http:// or https://")
    return value


def prices() -> Prices:
  return Prices(
      currency=store.currency(), tariffs=store.tariffs(), price_url=store.price_url()
  )


@router.get("/api/prices")
async def list_prices() -> Prices:
  return prices()


@router.post("/api/prices")
async def add_price(body: PriceIn) -> Prices:
  try:
    store.add_tariff(body.valid_from, body.price)
  except ValueError as e:
    raise HTTPException(400, str(e))
  return prices()


@router.put("/api/prices/{valid_from}")
async def update_price(valid_from: date, body: PriceIn) -> Prices:
  """Changes the price that starts on `valid_from`; its date may change too."""
  try:
    store.update_tariff(valid_from, body.valid_from, body.price)
  except KeyError:
    raise HTTPException(404, "No price starts on that date")
  except ValueError as e:
    raise HTTPException(400, str(e))
  return prices()


@router.delete("/api/prices/{valid_from}")
async def remove_price(valid_from: date) -> Prices:
  try:
    store.remove_tariff(valid_from)
  except KeyError:
    raise HTTPException(404, "No price starts on that date")
  return prices()


@router.post("/api/prices/url")
async def set_price_url(body: PriceUrlIn) -> Prices:
  """Saves the link to where prices can be looked up. It is never fetched."""
  store.set_price_url(body.url)
  return prices()
