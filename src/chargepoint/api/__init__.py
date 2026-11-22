"""HTTP API and web pages (FastAPI). Docs are served at /docs.

All endpoints are async so they run on the same event loop as the OCPP
connections and can safely share state with them (no threads).
"""

from fastapi import FastAPI

from . import chargers, energy, pages, sessions, settings, tags

app = FastAPI(title="simple-ocpp")
for module in (pages, chargers, tags, settings, energy, sessions):
  app.include_router(module.router)

__all__ = ["app"]
