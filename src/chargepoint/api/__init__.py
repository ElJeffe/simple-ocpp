"""HTTP API and web pages (FastAPI). Docs are served at /docs.

All endpoints are async so they run on the same event loop as the OCPP
connections and can safely share state with them (no threads).
"""

from fastapi import FastAPI

from . import auth, chargers, energy, pages, prices, sessions, settings, tags

app = FastAPI(title="simple-ocpp")
app.middleware("http")(auth.require_login)
for module in (pages, auth, chargers, tags, settings, prices, energy, sessions):
  app.include_router(module.router)

__all__ = ["app"]
