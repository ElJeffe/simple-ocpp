"""Starts the OCPP server (for chargers) and the web interface (for people)."""

import asyncio
import contextlib
import logging
import argparse
import errno
import os
import signal
import socket
import sys
from pathlib import Path
from collections.abc import Generator
from typing import override

import uvicorn
import websockets
from websockets.typing import Subprotocol

from chargepoint import lifecycle, store
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


def listen_failure(
    what: str, host: str, port: int, host_var: str, port_var: str, error: OSError
) -> SystemExit:
  """A clear, traceback-free exit for an address the server can't listen on."""
  if error.errno == errno.EADDRINUSE:
    problem = (
        f"port {port} is already in use on {host}.\n"
        "Another program, or another copy of simple-ocpp, is listening on it."
        f" To see which: ss -ltnp | grep :{port}"
    )
  elif error.errno == errno.EACCES:
    problem = f"no permission to use port {port} on {host}."
  elif error.errno == errno.EADDRNOTAVAIL:
    problem = f"this computer has no address {host}."
  else:
    problem = f"could not listen on {host}:{port} ({error.strerror or error})."
  return SystemExit(
      f"Cannot start the {what}: {problem}\n"
      f"Stop that program, or start with another address or port by setting"
      f" {host_var} / {port_var}."
  )


def open_web_sockets(host: str, port: int) -> list[socket.socket]:
  """Binds the web interface's address(es), so failures are ours to report."""
  sockets: list[socket.socket] = []
  try:
    for family, kind, proto, _, address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    ):
      sock = socket.socket(family, kind, proto)
      sockets.append(sock)
      sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
      sock.bind(address)
  except OSError:
    for sock in sockets:
      sock.close()
    raise
  return sockets


async def main() -> bool:
  """Runs until stopped. Returns True if a restart was requested."""
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
  restart = False

  def on_signal() -> None:
    if stop.is_set() and web is not None:
      web.force_exit = True
    stop.set()

  def on_restart() -> None:  # from the settings page
    nonlocal restart
    logging.info("Restart requested from the web interface")
    restart = True
    stop.set()

  lifecycle.set_restart_handler(on_restart)

  loop = asyncio.get_running_loop()
  for sig in (signal.SIGINT, signal.SIGTERM):
    loop.add_signal_handler(sig, on_signal)

  try:
    web_sockets = open_web_sockets(net["http_host"], net["http_port"])
  except OSError as e:
    raise listen_failure(
        "web interface", net["http_host"], net["http_port"],
        "OCPP_HTTP_HOST", "OCPP_HTTP_PORT", e,
    ) from None

  try:
    ocpp_server = await websockets.serve(
        on_connect, net["ocpp_host"], net["ocpp_port"],
        subprotocols=[Subprotocol("ocpp1.6")],
    )
  except OSError as e:
    for sock in web_sockets:
      sock.close()
    raise listen_failure(
        "charger server (OCPP)", net["ocpp_host"], net["ocpp_port"],
        "OCPP_HOST", "OCPP_PORT", e,
    ) from None
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
  web_task = asyncio.create_task(web.serve(sockets=web_sockets))

  # Run until a signal arrives, or until the web server stops on its own
  # (for example because its port is in use).
  stop_task = asyncio.create_task(stop.wait())
  await asyncio.wait({stop_task, web_task}, return_when=asyncio.FIRST_COMPLETED)

  logging.info("Restarting..." if restart else "Shutting down...")
  lifecycle.set_restart_handler(None)
  stop_task.cancel()
  web.should_exit = True
  ocpp_server.close()  # also closes charger connections ("going away")
  try:
    async with asyncio.timeout(SHUTDOWN_TIMEOUT):
      await asyncio.gather(web_task, ocpp_server.wait_closed())
  except TimeoutError:
    logging.warning("Connections did not close in time; exiting anyway")
  logging.info("Stopped")
  return restart


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
  elif asyncio.run(main()):
    # Start again in this same process (same PID, so systemd keeps tracking
    # it), with the same arguments and environment: it reads the saved
    # network settings afresh.
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.argv])
