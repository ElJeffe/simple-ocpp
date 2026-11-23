# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

"""The optional web interface password and login tokens.

Without a password, the web interface is open to anyone who can reach it.
With one, a browser needs a login token: "<expiry>.<signature>", signed with
a random secret kept in the database. The signature also covers the password
hash, so changing or removing the password logs everyone out. Tokens are not
stored, so logins survive a server restart.
"""

import hashlib
import hmac
import logging
import secrets
import time
from typing import cast

from .settings import get_setting, set_setting

LOGIN_DAYS = 30


def _hash(password: str, salt: bytes) -> bytes:
  return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _stored() -> dict[str, str] | None:
  saved = get_setting("password")
  if isinstance(saved, dict):
    saved = cast(dict[str, object], saved)
    salt, digest = saved.get("salt"), saved.get("hash")
    if isinstance(salt, str) and isinstance(digest, str):
      return {"salt": salt, "hash": digest}
  return None


def auth_enabled() -> bool:
  """Whether a password is set, so the web interface needs a login."""
  return _stored() is not None


def set_password(password: str) -> None:
  salt = secrets.token_bytes(16)
  set_setting("password", {"salt": salt.hex(), "hash": _hash(password, salt).hex()})
  logging.info("Web interface password set")


def clear_password() -> None:
  set_setting("password", None)
  logging.warning("Web interface password removed; it is open to anyone who can reach it")


def check_password(password: str) -> bool:
  stored = _stored()
  if stored is None:
    return False
  digest = _hash(password, bytes.fromhex(stored["salt"]))
  return hmac.compare_digest(digest.hex(), stored["hash"])


def _secret() -> bytes:
  secret = get_setting("login_secret")
  if not isinstance(secret, str):
    secret = secrets.token_hex(32)
    set_setting("login_secret", secret)
  return bytes.fromhex(secret)


def _signature(expiry: int, password_hash: str) -> str:
  message = f"{expiry}:{password_hash}".encode()
  return hmac.new(_secret(), message, hashlib.sha256).hexdigest()


def make_login_token() -> str:
  """A token for a browser that just logged in. Requires a password to be set."""
  stored = _stored()
  assert stored is not None
  expiry = int(time.time()) + LOGIN_DAYS * 86400
  return f"{expiry}.{_signature(expiry, stored['hash'])}"


def check_login_token(token: str | None) -> bool:
  stored = _stored()
  if stored is None or not token:
    return False
  expiry_text, _, signature = token.partition(".")
  # The cookie is untrusted: int() fails on digits like "²" and on very long
  # numbers, and compare_digest fails on non-ASCII text (but not on bytes).
  if not (expiry_text.isascii() and expiry_text.isdigit()) or len(expiry_text) > 20:
    return False
  if int(expiry_text) < time.time():
    return False
  expected = _signature(int(expiry_text), stored["hash"])
  return hmac.compare_digest(signature.encode(), expected.encode())
