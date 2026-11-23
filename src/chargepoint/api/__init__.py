# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

"""HTTP API and web pages (FastAPI). Docs are served at /docs.

All endpoints are async so they run on the same event loop as the OCPP
connections and can safely share state with them. The only exception is
password hashing, which touches no shared state and runs in a thread.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import auth, chargers, energy, logs, pages, prices, sessions, settings, tags

app = FastAPI(title="simple-ocpp")


@app.exception_handler(RequestValidationError)
async def invalid_request(_: Request, error: RequestValidationError) -> JSONResponse:
  """The usual 422 answer, without echoing the input: Infinity or NaN can't be
  written as JSON, which would turn the answer into a server error."""
  details = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in error.errors()]
  return JSONResponse({"detail": details}, status_code=422)


app.middleware("http")(auth.require_login)
app.middleware("http")(auth.reject_cross_site)  # added last, so it runs first
for module in (pages, auth, chargers, tags, settings, prices, energy, sessions, logs):
  app.include_router(module.router)

__all__ = ["app"]
