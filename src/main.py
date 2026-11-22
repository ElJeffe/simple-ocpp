"""Starts the OCPP server (for chargers) and the web interface (for people)."""

import asyncio
import contextlib
import logging
import argparse
import signal
from pathlib import Path
from collections.abc import Generator
from typing import override

import uvicorn
import websockets
from websockets.typing import Subprotocol

from chargepoint import store
from chargepoint.api import app
from chargepoint.charger import on_connect
from chargepoint.config import DATA_DIR

logging.basicConfig(level=logging.INFO)

SHUTDOWN_TIMEOUT = 10  # seconds to wait for connections to close


class WebServer(uvicorn.Server):
  """Uvicorn without its own Ctrl-C handling; main() stops it instead.

  Otherwise uvicorn takes over SIGINT and stops only the web interface,
  leaving the OCPP server running.
  """

  @override
  @contextlib.contextmanager
  def capture_signals(self) -> Generator[None]:
    yield


async def main() -> None:
  store.ensure_data_dir()
  logging.info(f"Data directory: {DATA_DIR}")
  store.init_db()
  store.load_registry()
  store.load_tags()
  net = store.network_settings()
  store.set_active_network(net)  # shown on the settings page

  # Ctrl-C (SIGINT) and SIGTERM (e.g. systemctl stop) both shut down cleanly;
  # a second one during shutdown stops waiting for open connections.
  stop = asyncio.Event()
  web: WebServer | None = None

  def on_signal() -> None:
    if stop.is_set() and web is not None:
      web.force_exit = True
    stop.set()

  loop = asyncio.get_running_loop()
  for sig in (signal.SIGINT, signal.SIGTERM):
    loop.add_signal_handler(sig, on_signal)

  try:
    ocpp_server = await websockets.serve(
        on_connect, net["ocpp_host"], net["ocpp_port"],
        subprotocols=[Subprotocol("ocpp1.6")],
    )
  except OSError as e:
    raise SystemExit(
        f"Cannot listen for chargers on {net['ocpp_host']}:{net['ocpp_port']}:"
        f" {e.strerror}. Fix it on the settings page, or override it with the"
        " OCPP_HOST / OCPP_PORT environment variables."
    ) from e
  logging.info(
      f"OCPP 1.6-J Server listening on ws://{net['ocpp_host']}:{net['ocpp_port']}"
  )
  web = WebServer(
      uvicorn.Config(
          app, host=net["http_host"], port=net["http_port"], access_log=False,
          log_config=None,
      )
  )
  logging.info(f"Web interface on http://{net['http_host']}:{net['http_port']}")
  web_task = asyncio.create_task(web.serve())

  # Run until a signal arrives, or until the web server stops on its own
  # (for example because its port is in use).
  stop_task = asyncio.create_task(stop.wait())
  await asyncio.wait({stop_task, web_task}, return_when=asyncio.FIRST_COMPLETED)

  logging.info("Shutting down...")
  stop_task.cancel()
  web.should_exit = True
  ocpp_server.close()  # also closes charger connections ("going away")
  try:
    async with asyncio.timeout(SHUTDOWN_TIMEOUT):
      await asyncio.gather(web_task, ocpp_server.wait_closed())
  except TimeoutError:
    logging.warning("Connections did not close in time; exiting anyway")
  logging.info("Stopped")


def remove_password() -> None:
  """For a forgotten password: opens the web interface to anyone again."""
  store.ensure_data_dir()
  store.init_db()
  if not store.auth_enabled():
    print("No web interface password is set.")
    return
  store.clear_password()
  print("Web interface password removed; it is open to anyone who can reach it.")


def backup(target: Path) -> None:
  """Writes a consistent copy of the database, also while the server runs."""
  store.ensure_data_dir()
  store.init_db()
  store.backup(target)
  print(f"Database backed up to {target}")


if __name__ == "__main__":
  parser = argparse.ArgumentParser(
      description="OCPP 1.6-J central system with a web interface. Without"
      " options, starts the server."
  )
  actions = parser.add_mutually_exclusive_group()
  actions.add_argument(
      "--remove-password", action="store_true",
      help="remove the web interface password (if you forgot it) and exit",
  )
  actions.add_argument(
      "--backup", metavar="FILE", type=Path,
      help="write a backup of the database to FILE and exit; safe while the server runs",
  )
  args = parser.parse_args()
  if args.remove_password:
    remove_password()
  elif args.backup:
    backup(args.backup)
  else:
    asyncio.run(main())
