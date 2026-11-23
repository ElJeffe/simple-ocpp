# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

"""The optional password: login, logout, setting it, and the check that
guards every other page and API call while one is set."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .. import store
from .models import Model

router = APIRouter()

COOKIE = "simple_ocpp_login"
FAILED_LOGIN_DELAY = 1.0  # seconds; slows down password guessing

# Reachable without logging in: the login page, what it needs, and the login API.
PUBLIC_PATHS = {"/login", "/style.css", "/common.js", "/api/login", "/api/auth"}


def is_cross_site(request: Request) -> bool:
  """Whether a browser sent this request from a page of another site.

  Browsers only send Sec-Fetch-Site over HTTPS or to localhost, so plain HTTP
  relies on Origin. A page can't set or forge either header, nor add
  X-Forwarded-Host without a CORS approval this server never gives. Scripts
  send neither Sec-Fetch-Site nor Origin, and keep working.
  """
  fetch_site = request.headers.get("sec-fetch-site")
  if fetch_site is not None:
    return fetch_site not in ("same-origin", "none")
  origin = request.headers.get("origin")
  if origin is None:
    return False
  forwarded = request.headers.get("x-forwarded-host", "").split(",")
  hosts = {h.strip().lower() for h in [request.headers.get("host", ""), *forwarded]}
  return urlsplit(origin).netloc.lower() not in hosts - {""}


async def reject_cross_site(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
  """Middleware: another site's page must not be able to change anything here,
  for instance unlock a cable through the browser of someone on this network."""
  if request.method not in ("GET", "HEAD", "OPTIONS") and is_cross_site(request):
    logging.warning(
        f"Refused {request.method} {request.url.path} from another site"
        f" ({client_address(request)})"
    )
    return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
  return await call_next(request)


def logged_in(request: Request) -> bool:
  return store.check_login_token(request.cookies.get(COOKIE))


async def require_login(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
  """Middleware: with a password set, only logged-in browsers get through."""
  path = request.url.path
  if path in PUBLIC_PATHS or not store.auth_enabled() or logged_in(request):
    return await call_next(request)
  if path.startswith("/api/") or path in ("/docs", "/redoc", "/openapi.json"):
    return JSONResponse({"detail": "Not logged in"}, status_code=401)
  target = path + (f"?{request.url.query}" if request.url.query else "")
  return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


def client_address(request: Request) -> str:
  """The connecting address. Behind a proxy or Docker's NAT, that may be theirs."""
  return request.client.host if request.client else "unknown address"


def set_login_cookie(response: Response) -> None:
  response.set_cookie(
      COOKIE, store.make_login_token(), max_age=store.LOGIN_DAYS * 86400,
      httponly=True, samesite="lax",
  )


class AuthStatus(BaseModel):
  enabled: bool  # a password is set
  logged_in: bool


class Login(Model):
  password: str = Field(max_length=200)


class PasswordChange(Model):
  current_password: str = Field("", max_length=200)  # needed when one is set
  new_password: str = Field(min_length=8, max_length=200)


class PasswordRemoval(Model):
  current_password: str = Field(max_length=200)


@router.get("/api/auth")
async def auth_status(request: Request) -> AuthStatus:
  return AuthStatus(enabled=store.auth_enabled(), logged_in=logged_in(request))


@router.post("/api/login")
async def login(body: Login, request: Request, response: Response) -> dict[str, bool]:
  if not store.auth_enabled():
    return {"ok": True}  # nothing to log in to
  if not store.check_password(body.password):
    logging.warning(f"Failed login from {client_address(request)}: wrong password")
    await asyncio.sleep(FAILED_LOGIN_DELAY)
    raise HTTPException(401, "Wrong password")
  set_login_cookie(response)
  logging.info(f"Login from {client_address(request)}")
  return {"ok": True}


@router.post("/api/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
  if logged_in(request):
    logging.info(f"Logout from {client_address(request)}")
  response.delete_cookie(COOKIE)
  return {"ok": True}


async def check_current(password: str, request: Request) -> None:
  # 403, not 401: the browser is logged in, it just typed the wrong password.
  if store.auth_enabled() and not store.check_password(password):
    logging.warning(
        f"Password change or removal refused for {client_address(request)}:"
        " wrong current password"
    )
    await asyncio.sleep(FAILED_LOGIN_DELAY)
    raise HTTPException(403, "The current password is wrong")


@router.post("/api/settings/password")
async def change_password(
    body: PasswordChange, request: Request, response: Response
) -> AuthStatus:
  """Sets or changes the password. Other browsers are logged out; this one
  stays logged in."""
  await check_current(body.current_password, request)
  store.set_password(body.new_password)
  set_login_cookie(response)
  return AuthStatus(enabled=True, logged_in=True)


@router.post("/api/settings/password/remove")
async def remove_password(
    body: PasswordRemoval, request: Request, response: Response
) -> AuthStatus:
  """Removes the password: the web interface is open to anyone again."""
  await check_current(body.current_password, request)
  store.clear_password()
  response.delete_cookie(COOKIE)
  return AuthStatus(enabled=False, logged_in=False)
