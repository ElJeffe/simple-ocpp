"""Starts the OCPP server (for chargers) and the web interface (for people)."""

import asyncio
import contextlib
import logging
import signal
import sys
import threading
from collections.abc import Generator
from typing import override

import uvicorn
import websockets
from websockets.typing import Subprotocol

from chargepoint import store
from chargepoint.api import app
from chargepoint.charger import connected_chargers, on_connect
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


def read_console(loop: asyncio.AbstractEventLoop, lines: asyncio.Queue[str | None]) -> None:
  """Reads stdin in a daemon thread, so waiting for a line never blocks exit.

  Uses sys.stdin.readline() rather than input(): in a terminal, input() goes
  through GNU readline, and a thread waiting in it keeps Python from exiting.
  """
  while True:
    print("\nType 'unlock <CP_ID>' to release cable:", flush=True)
    try:
      line = sys.stdin.readline()
    except (OSError, ValueError):  # stdin closed or unusable
      line = ""
    if not line:  # end of input: no interactive terminal, e.g. as a service
      loop.call_soon_threadsafe(lines.put_nowait, None)
      return
    loop.call_soon_threadsafe(lines.put_nowait, line)


async def console_input() -> None:
  """Simple CLI to trigger manual unlocks while the server is running."""
  lines: asyncio.Queue[str | None] = asyncio.Queue()
  threading.Thread(
      target=read_console, args=(asyncio.get_running_loop(), lines), daemon=True
  ).start()
  while (cmd := await lines.get()) is not None:
    parts = cmd.strip().split()
    if len(parts) == 2 and parts[0].lower() == "unlock":
      cp_id = parts[1]
      if cp_id in connected_chargers:
        try:
          print(await connected_chargers[cp_id].manual_unlock(connector_id=1))
        except Exception as e:  # keep the console (and server) alive
          print(f"Unlock failed: {e!r}")
      else:
        print(
            f"Charger '{cp_id}' not connected. Connected:"
            f" {list(connected_chargers.keys())}"
        )


async def main() -> None:
  store.ensure_data_dir()
  logging.info(f"Data directory: {DATA_DIR}")
  store.init_db()
  store.load_registry()
  store.load_tags()
  net = store.network_settings()
  store.active_network = net  # shown on the settings page

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
  console_task = asyncio.create_task(console_input())

  # Run until a signal arrives, or until the web server stops on its own
  # (for example because its port is in use).
  stop_task = asyncio.create_task(stop.wait())
  await asyncio.wait({stop_task, web_task}, return_when=asyncio.FIRST_COMPLETED)

  logging.info("Shutting down...")
  console_task.cancel()
  stop_task.cancel()
  web.should_exit = True
  ocpp_server.close()  # also closes charger connections ("going away")
  try:
    async with asyncio.timeout(SHUTDOWN_TIMEOUT):
      await asyncio.gather(web_task, ocpp_server.wait_closed())
  except TimeoutError:
    logging.warning("Connections did not close in time; exiting anyway")
  logging.info("Stopped")


if __name__ == "__main__":
  asyncio.run(main())
