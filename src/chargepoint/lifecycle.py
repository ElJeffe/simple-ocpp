# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

"""Lets the web interface ask the running server to restart itself.

main() registers a handler at startup; without one (for example in tests,
or when the API runs some other way) a restart can't be requested.
"""

from collections.abc import Callable

_restart: Callable[[], None] | None = None


def set_restart_handler(handler: Callable[[], None] | None) -> None:
  global _restart
  _restart = handler


def can_restart() -> bool:
  return _restart is not None


def request_restart() -> None:
  """Starts a clean shutdown followed by a fresh start. Requires a handler."""
  assert _restart is not None
  _restart()
