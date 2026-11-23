# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

"""The web pages and the files they share."""

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ..config import STATIC_DIR

router = APIRouter(include_in_schema=False)


def static_file(name: str) -> FileResponse:
  return FileResponse(STATIC_DIR / name, headers={"Cache-Control": "no-store"})


@router.get("/", include_in_schema=False)
@router.get("/index.html", include_in_schema=False)
async def index() -> FileResponse:
  return static_file("index.html")


# Other pages, and the files they share: URL path -> file in STATIC_DIR.
STATIC_PAGES = {
    "/tags": "tags.html",
    "/energy": "energy.html",
    "/sessions": "sessions.html",
    "/prices": "prices.html",
    "/logs": "logs.html",
    "/settings": "settings.html",
    "/login": "login.html",
    "/style.css": "style.css",
    "/common.js": "common.js",
    "/app.js": "app.js",
}


def add_static_route(path: str, name: str) -> None:
  async def serve() -> FileResponse:
    return static_file(name)

  router.get(path, include_in_schema=False)(serve)


for _path, _name in STATIC_PAGES.items():
  add_static_route(_path, _name)
