"""Energy use per period."""

from fastapi import APIRouter

from .. import store

router = APIRouter()


@router.get("/api/energy")
async def energy(
    period: store.EnergyPeriod = "day", charger_id: str | None = None
) -> list[store.EnergyUsage]:
  """Energy used per day, month or year, by one charger or all of them."""
  return store.energy_usage(period, charger_id)
