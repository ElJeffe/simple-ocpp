"""Fixed settings. Most settings live in the database (see store.py) and are
edited on the settings page; the environment variables here override them."""

import os
from pathlib import Path

APP_NAME = "simple-ocpp"

SRC_DIR = Path(__file__).resolve().parent.parent  # .../src
STATIC_DIR = SRC_DIR / "static"

# Persistent state goes in /var/lib/<app>, per the Filesystem Hierarchy
# Standard. Override with OCPP_DATA_DIR, e.g. for local development.
DATA_DIR = Path(os.environ.get("OCPP_DATA_DIR", f"/var/lib/{APP_NAME}"))
DB_FILE = DATA_DIR / f"{APP_NAME}.db"

# Listening addresses and ports: setting name -> (default, environment variable).
# A set environment variable wins over the settings page, so a wrong setting
# that locks you out of the web interface can be bypassed at startup.
NETWORK_DEFAULTS: dict[str, tuple[str | int, str]] = {
    "ocpp_host": ("0.0.0.0", "OCPP_HOST"),
    "ocpp_port": (9000, "OCPP_PORT"),
    "http_host": ("0.0.0.0", "OCPP_HTTP_HOST"),
    "http_port": (8090, "OCPP_HTTP_PORT"),
}
