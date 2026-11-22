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
- **Costs and export**: electricity prices that can change over time give every session and period a cost. Sessions and energy can be downloaded as CSV.
- **Settings page**: password, listening addresses and ports, the start of the fiscal year, and a charger's meter reading from before it was connected here.

## Requirements

- Python 3.14
- A charger that speaks OCPP 1.6-J (JSON over websocket)

## Installation

```sh
git clone https://github.com/ElJeffe/simple-ocpp.git simple-ocpp
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
- serves the web interface on `http://<this-host>:8090`;
- keeps its database in **`/var/lib/simple-ocpp/simple-ocpp.db`**.

That directory must exist and be writable by the user running the server. If it isn't, the server stops with a message saying so. To keep the data somewhere else, set `OCPP_DATA_DIR`:

```sh
OCPP_DATA_DIR=~/simple-ocpp-data venv/bin/python src/main.py
```

Press **Ctrl-C** (or send SIGTERM, e.g. with `systemctl stop`) to stop. Chargers are disconnected cleanly and will reconnect when the server is back.

## Running as a service

`deploy/simple-ocpp.service` is a systemd unit that starts simple-ocpp at boot and restarts it if it stops unexpectedly. It runs as its own system user, which can write only to the data directory `/var/lib/simple-ocpp`.

1. Install the project in `/opt/simple-ocpp` and create its venv:

   ```sh
   sudo git clone https://github.com/ElJeffe/simple-ocpp.git /opt/simple-ocpp
   cd /opt/simple-ocpp
   sudo python3 -m venv venv
   sudo venv/bin/pip install -r requirements.txt
   ```

2. Create the system user it runs as:

   ```sh
   sudo useradd --system --home-dir /var/lib/simple-ocpp --shell /usr/sbin/nologin simple-ocpp
   ```

3. Install and start the service. systemd creates `/var/lib/simple-ocpp` for it.

   ```sh
   sudo cp deploy/simple-ocpp.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now simple-ocpp
   ```

Useful commands:

```sh
systemctl status simple-ocpp          # running?
journalctl -u simple-ocpp -f          # follow the log
sudo systemctl restart simple-ocpp    # or Settings → Restart server
```

To update, pull the new version and restart:

```sh
cd /opt/simple-ocpp
sudo git pull
sudo venv/bin/pip install -r requirements.txt
sudo systemctl restart simple-ocpp
```

Commands that work on the database, such as `--remove-password`, must run as the service user, so that the database keeps the right owner:

```sh
sudo -u simple-ocpp OCPP_DATA_DIR=/var/lib/simple-ocpp /opt/simple-ocpp/venv/bin/python /opt/simple-ocpp/src/main.py --remove-password
```

## Running in Docker

The `Dockerfile` builds an image that runs simple-ocpp as an unprivileged user, with its database in a volume at `/var/lib/simple-ocpp`. The easiest way to run it is with `compose.yaml`:

```sh
docker compose up -d --build
```

Before starting, set `TZ` in `compose.yaml` to your time zone. Energy is logged per local day, and without it the container uses UTC.

The same with plain Docker:

```sh
docker build -t simple-ocpp .
docker run -d --name simple-ocpp --restart unless-stopped \
  -p 9000:9000 -p 8090:8090 \
  -v simple-ocpp-data:/var/lib/simple-ocpp \
  -e TZ=Europe/Amsterdam \
  simple-ocpp
```

Instead of building it yourself, you can use the image published on Docker Hub: in `compose.yaml`, replace `build: .` with `image: jefsteelant/simple-ocpp:latest`. `:latest` follows the newest release, and a version tag such as `:1.2.0` stays on one exact version. Then `docker compose pull && docker compose up -d` updates to the latest one.

Some things work differently in the container:

- **Ports:** inside the container, the server always listens on 9000 (chargers) and 8090 (web interface). Choose other ports on the host with `-p`, e.g. `-p 80:8090`. The network fields on the Settings page are locked for this reason, since changing them would break the port mapping.
- **Data:** everything is in the volume, so the container can be removed and recreated without losing anything.
  - To use a host directory instead of a named volume, make it writable for the container's user (uid 10001): `sudo chown 10001:10001 /srv/simple-ocpp`.
- **Stopping:** `docker stop` disconnects chargers cleanly.
- **Health:** the image has a health check (`docker ps` shows "healthy").
- **Commands** run through `docker exec`:

  ```sh
  docker exec simple-ocpp python src/main.py --remove-password
  docker exec simple-ocpp python src/main.py --backup /var/lib/simple-ocpp/backup.db
  docker cp simple-ocpp:/var/lib/simple-ocpp/backup.db .
  ```

- **Updating:** pull the new version and rebuild with `docker compose up -d --build`. The data in the volume is kept.

## Backup and restore

Everything is in one SQLite file, `simple-ocpp.db` in the data directory. Don't back it up by copying that file while the server runs: a copy taken mid-write can be broken. Use one of these instead; both are safe while chargers are connected.

- **Settings → Backup → Download backup** in the web interface.
- From the command line, e.g. from cron or a systemd timer:

  ```sh
  venv/bin/python src/main.py --backup /path/to/simple-ocpp-backup.db
  ```

  As a service, run it as the service user, for example in the `simple-ocpp` user's crontab (`sudo crontab -u simple-ocpp -e`). This writes a backup every night at 3:00 and keeps one per weekday (the `%` signs are escaped because cron treats them specially):

  ```
  0 3 * * * OCPP_DATA_DIR=/var/lib/simple-ocpp /opt/simple-ocpp/venv/bin/python /opt/simple-ocpp/src/main.py --backup /var/lib/simple-ocpp/backup-$(date +\%a).db
  ```

  Copy the backups off the machine as well.

A backup also holds the password hash, so keep it private.

To restore a backup (as a service; in Docker, stop the container, `docker cp` the backup to `/var/lib/simple-ocpp/simple-ocpp.db` and start it again):

```sh
sudo systemctl stop simple-ocpp
sudo cp simple-ocpp-backup.db /var/lib/simple-ocpp/simple-ocpp.db
sudo chown simple-ocpp:simple-ocpp /var/lib/simple-ocpp/simple-ocpp.db
sudo systemctl start simple-ocpp
```

## Connecting a charger

In the charger's OCPP settings (usually in its installer app or local web page), set:

| Setting | Value |
|---|---|
| Central system / backend URL | `ws://<server-address>:9000/<charger-id>` |
| Protocol | OCPP 1.6-J (JSON) |

`<charger-id>` is the name the charger shows up under, often its serial number. Once the charger connects, it appears on the Chargers page.

## The web interface

Open `http://<server-address>:8090`. The navigation bar at the top has seven pages:

- **Chargers**: a card per charger.
  - **Lock / Unlock**: changes the cable lock. Unlock also releases a cable that is currently held.
  - **Lock cable when plugged in**: when ticked, the cable is locked each time one is plugged in and each time the charger connects. Untick it for chargers or simulators that don't support changing their configuration.
  - **Last RFID**: the last card presented. Click **Register** next to an unknown card to allow it.
  - **Session in progress**: while a car is charging, the card shows who is charging, how long the session has been running, the energy and cost so far, and the current power. Energy and power are unknown while the charger is offline.
  - **Remove charger** (offline chargers only): removes the charger, either keeping its sessions and energy history (they still count in the totals) or deleting them.
- **Tags**: the registered cards. Rename, authorize, block or remove them, and pick each card's group. Below that, add and delete groups. Deleting a group leaves its cards without a group.
- **Energy**: energy used per day (one month at a time), per month (one fiscal year) or per fiscal year, for all chargers or one. Hover a bar for its value, or open **Show as table**. With a price set, costs are shown next to the energy. **Download CSV** saves the range shown.
- **Sessions**: 20 sessions per page, newest first, with a period filter (today, last 7 days, this month, last month, this year, or a custom range). Above the table are the number of sessions, their energy and their cost for the whole filter. **Download CSV** saves all sessions in the filter, not just the page shown. A session whose stop never arrived (for example because the charger rebooted) is ended automatically when a new session starts on that connector or the charger boots. It is shown as "no stop received", with estimated energy. If the stop still arrives later, it replaces the estimate.
- **Prices**: the electricity price per kWh, used for all costs.
  - Add a price in cents per kWh (for example 30.5 for 30.5 c€/kWh), with the date it's valid from. Each price lasts until the next one starts, so when the price changes, add a new one and older energy keeps its old price.
  - The table lists every price, with its validity period and which one applies today. Each row can be edited (price and start date) or removed.
  - The earliest price also applies to everything before its date. A session's cost uses the price on the day it started; energy per day, month or year uses each day's own price. Without a price, no costs are shown.
  - **Find current prices** is a saved link to the page where you look up prices. Click **Edit** next to it to change it, or clear it to remove it. Prices are not retrieved from it automatically.
  - The currency symbol or code is set at the bottom of the page.
- **Logs**: recent log lines, newest first, updated live. Filter by source (`server`, `ocpp`, `websocket`, `web`) and minimum level: TRACE, DEBUG, INFO (the default), WARNING or ERROR. ERROR needs action (data not saved, a charger fault, a refused session that may keep charging), WARNING is a failure or denied card that the system survives, INFO is a readable timeline of events, DEBUG holds protocol steps and raw OCPP messages, and TRACE holds repeating messages such as heartbeats, meter values and unchanged statuses. The console and service logs show INFO and up; DEBUG and TRACE appear only on this page. The server keeps the latest 2,000 TRACE lines and 2,000 other lines in memory, so the list starts empty after a restart, and the page shows the newest 500 that match the filters. Logs contain charger IDs and RFID tags.
- **Settings**: see below.

The pages refresh every 3 seconds.

### How energy is counted

Once a day, the server logs each meter's total reading. During the day, the row for today is overwritten with the latest reading. A day's usage is that reading minus the meter's previous one. Some consequences:

- Days the server wasn't running are counted on the next day it ran.
- A meter reading that goes down (meter replaced or reset) counts as zero.
- Energy used before the charger was first connected isn't counted. To include it, enter the charger's meter reading from an earlier date under **Settings → Initial energy**.

## Configuration

Most settings are on the **Settings** page and are stored in the database:

- **Password**: optional. See [Security](#security).
- **Network**: the address and port for charger connections and for the web interface. Changes take effect when the server restarts: click **Restart server** on the same page. The restart is refused if a new address or port can't be opened, and the page then takes you to the new address. `0.0.0.0` means all IPv4 interfaces, `::` all interfaces, and `127.0.0.1` only this computer.
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

**Web interface password.** By default there is no password: anyone who can reach the web interface can unlock cables, register cards and change settings. To require a login, set a password under **Settings → Password**, at least 8 characters.

- Every page then asks for the password first, and the API answers 401 until you log in.
- A login lasts 30 days, also across server restarts, until you click **Log out** in the navigation bar.
- Changing the password logs out every other browser.
- Changing or removing the password requires the current one.
- Logins, logouts and failed attempts appear on the **Logs** page with the client's address. Behind a reverse proxy or Docker's network, that can be the address of the proxy or gateway.
- If you forget it, run the following (as a service, as the service user; see [Running as a service](#running-as-a-service)):

  ```sh
  venv/bin/python src/main.py --remove-password
  ```

**What is not protected:**

- **There is no encryption.** The password and everything else travel over plain HTTP, and chargers use plain `ws://`. For access from outside your own network, let simple-ocpp listen on `127.0.0.1` only and put a reverse proxy with HTTPS in front of it.
- **Any device that can reach the charger port can connect as a charger.** Only expose that port to the network your chargers are on.

## HTTP API

Everything the web interface does goes through a JSON API. Its interactive documentation is at `http://<server-address>:8090/docs`.

## Development

```sh
venv/bin/pip install -r requirements-dev.txt
venv/bin/pytest          # tests, including end-to-end tests with a simulated charger
venv/bin/pyright         # type checking (strict)
```

`start_test.sh` starts a test instance on port 9999, with its data in `/tmp/simple-ocpp`.
