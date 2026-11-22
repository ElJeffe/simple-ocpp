"""The optional password: login, logout, setting it, and the check that
guards every other page and API call while one is set."""

import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import quote

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
async def login(body: Login, response: Response) -> dict[str, bool]:
  if not store.auth_enabled():
    return {"ok": True}  # nothing to log in to
  if not store.check_password(body.password):
    await asyncio.sleep(FAILED_LOGIN_DELAY)
    raise HTTPException(401, "Wrong password")
  set_login_cookie(response)
  return {"ok": True}


@router.post("/api/logout")
async def logout(response: Response) -> dict[str, bool]:
  response.delete_cookie(COOKIE)
  return {"ok": True}


async def check_current(password: str) -> None:
  # 403, not 401: the browser is logged in, it just typed the wrong password.
  if store.auth_enabled() and not store.check_password(password):
    await asyncio.sleep(FAILED_LOGIN_DELAY)
    raise HTTPException(403, "The current password is wrong")


@router.post("/api/settings/password")
async def change_password(body: PasswordChange, response: Response) -> AuthStatus:
  """Sets or changes the password. Other browsers are logged out; this one
  stays logged in."""
  await check_current(body.current_password)
  store.set_password(body.new_password)
  set_login_cookie(response)
  return AuthStatus(enabled=True, logged_in=True)


@router.post("/api/settings/password/remove")
async def remove_password(body: PasswordRemoval, response: Response) -> AuthStatus:
  """Removes the password: the web interface is open to anyone again."""
  await check_current(body.current_password)
  store.clear_password()
  response.delete_cookie(COOKIE)
  return AuthStatus(enabled=False, logged_in=False)
