# simple-ocpp

A small OCPP 1.6-J central system for home and small-site EV chargers. Chargers connect to it over a websocket. You manage them from a web interface: lock or unlock the charging cable, decide which RFID cards may charge, and see charge sessions and energy use.

Everything is stored in one SQLite file. Apart from a few Python packages, nothing else needs to be installed.

## Features

- **Chargers**: live status per connector, power and meter reading, last RFID card read, online/offline.
- **Cable lock**: lock or unlock the cable from the web page. Optionally, the cable is locked automatically whenever it is plugged in, and again whenever the charger reconnects.
- **RFID cards**: register cards, give them a name, authorize or block them. Unknown cards are refused, and the charger is told to stop.
- **Tag groups**: any card in a group can stop a session that another card in the same group started (OCPP `parentIdTag`).
- **Charge sessions**: who charged where, when, for how long and how much energy, including refused attempts. You can page through them and filter by period.
- **Energy**: a daily log of each charger's meter, with charts per day, month and (fiscal) year.
- **Settings page**: listening addresses and ports, the start of the fiscal year, and a charger's meter reading from before it was connected here.

## Requirements

- Python 3.14
- A charger that speaks OCPP 1.6-J (JSON over websocket)

## Installation

```sh
git clone <this repository> simple-ocpp
cd simple-ocpp
```

## Running

The server runs from a Python virtual environment (venv) in the project directory. Create it once and install the dependencies into it:

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

A venv only works in the directory where it was created. If you move or rename the project directory, delete it and create it again:

```sh
rm -rf venv
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Then start the server:

```sh
venv/bin/python src/main.py
```

By default the server:

- accepts chargers on port **9000**, on all interfaces;
- serves the web interface on **http://<this-host>:8090**;
- keeps its database in **`/var/lib/simple-ocpp/simple-ocpp.db`**.

That directory must exist and be writable by the user running the server. If it isn't, the server stops with a message saying so. To keep the data somewhere else, set `OCPP_DATA_DIR`:

```sh
OCPP_DATA_DIR=~/simple-ocpp-data venv/bin/python src/main.py
```

Press **Ctrl-C** (or send SIGTERM, e.g. with `systemctl stop`) to stop. Chargers are disconnected cleanly and will reconnect when the server is back.

## Connecting a charger

In the charger's OCPP settings (usually in its installer app or local web page), set:

| Setting | Value |
|---|---|
| Central system / backend URL | `ws://<server-address>:9000/<charger-id>` |
| Protocol | OCPP 1.6-J (JSON) |

`<charger-id>` is the name the charger shows up under, often its serial number. Once the charger connects, it appears on the Chargers page.

## The web interface

Open `http://<server-address>:8090`. The navigation bar at the top has five pages:

- **Chargers**: a card per charger.
  - **Lock / Unlock**: changes the cable lock. Unlock also releases a cable that is currently held.
  - **Lock cable when plugged in**: when ticked, the cable is locked each time one is plugged in and each time the charger connects. Untick it for chargers or simulators that don't support changing their configuration.
  - **Last RFID**: the last card presented. Click **Register** next to an unknown card to allow it.
  - **Remove charger** (offline chargers only): removes the charger, either keeping its sessions and energy history (they still count in the totals) or deleting them.
- **Tags**: the registered cards. Rename, authorize, block or remove them, and pick each card's group. Below that, add and delete groups. Deleting a group leaves its cards without a group.
- **Energy**: energy used per day (one month at a time), per month (one fiscal year) or per fiscal year, for all chargers or one. Hover a bar for its value, or open **Show as table**.
- **Sessions**: 20 sessions per page, newest first, with a period filter (today, last 7 days, this month, last month, this year, or a custom range). A session whose stop never arrived (for example because the charger rebooted) is ended automatically when a new session starts on that connector or the charger boots. It is shown as "no stop received", with estimated energy. If the stop still arrives later, it replaces the estimate.
- **Settings**: see below.

The pages refresh every 3 seconds.

### How energy is counted

Once a day, the server logs each meter's total reading. During the day, the row for today is overwritten with the latest reading. A day's usage is that reading minus the meter's previous one. Some consequences:

- Days the server wasn't running are counted on the next day it ran.
- A meter reading that goes down (meter replaced or reset) counts as zero.
- Energy used before the charger was first connected isn't counted. To include it, enter the charger's meter reading from an earlier date under **Settings → Initial energy**.

## Configuration

Most settings are on the **Settings** page and are stored in the database:

- **Network**: the address and port for charger connections and for the web interface. Changes take effect when the server restarts. `0.0.0.0` means all IPv4 interfaces, `::` all interfaces, and `127.0.0.1` only this computer.
- **Fiscal year**: the day and month a year starts, used by the energy charts.
- **Initial energy**: a charger's total meter reading on a given date before it was connected here.

These environment variables override the settings page:

| Variable | Default | Meaning |
|---|---|---|
| `OCPP_DATA_DIR` | `/var/lib/simple-ocpp` | Directory for the database |
| `OCPP_HOST` | `0.0.0.0` | Address to accept chargers on |
| `OCPP_PORT` | `9000` | Port to accept chargers on |
| `OCPP_HTTP_HOST` | `0.0.0.0` | Address of the web interface |
| `OCPP_HTTP_PORT` | `8090` | Port of the web interface |

If you save web interface settings you can't reach, start the server with `OCPP_HTTP_HOST` and `OCPP_HTTP_PORT` to get back in.

## Security

simple-ocpp has **no login and no encryption**:

- Anyone who can reach the web interface can unlock cables, register cards and change settings.
- Any device that can reach the charger port can connect as a charger.

Only run it on a network you trust. Otherwise, let it listen on `127.0.0.1` only and put a reverse proxy with authentication and HTTPS in front of it.

## Other ways to control it

- **Console**: when started in a terminal, type `unlock <charger-id>` to release the cable on connector 1.
- **HTTP API**: everything the web interface does goes through a JSON API. Its interactive documentation is at `http://<server-address>:8090/docs`.

## Development

```sh
venv/bin/pip install -r requirements-dev.txt
venv/bin/pytest          # tests, including end-to-end tests with a simulated charger
venv/bin/pyright         # type checking (strict)
```

`start_test.sh` starts a test instance on port 9999, with its data in `/tmp/simple-ocpp`.

Layout:

```
src/main.py                  starts the OCPP server and the web interface
src/chargepoint/charger.py   OCPP message handling, one instance per connected charger
src/chargepoint/store.py     state and the SQLite database
src/chargepoint/api.py       HTTP API and pages (FastAPI)
src/chargepoint/config.py    paths, defaults and environment variables
src/static/                  web pages, styles and scripts
tests/                       pytest tests
```
