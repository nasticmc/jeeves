# meshcore-pathbot — v1.0.0

MeshCore path resolver bot with a web dashboard.

Connects to a MeshCore companion radio, listens for `trace` and `ping` commands on a channel, resolves hex path prefixes to repeater names using a persistent database, and replies on the channel. Includes a full web dashboard for live monitoring, repeater management, path visualization, and configuration.

### Features

- **Path resolution** — resolves 1-byte and multibyte (2-byte hash) hex path prefixes to repeater names with geographic disambiguation
- **Web dashboard** (port 8075) — live message feed, path visualizer, repeater management, overlap analysis, statistics, and full settings UI
- **Guest dashboard** (port 8076, optional) — read-only view of ping history, paths, and repeater info
- **Weather & forecasts** — per-channel `weather` and `forecast` commands via Open-Meteo
- **Lightning alerts** — scheduled thunderstorm detection with automatic channel broadcasts
- **Radio management** — read and update radio name, frequency, TX power, and channel keys directly from the web UI
- **Systemd & Docker** — production-ready deployment with auto-restart

## Quick Start

```bash
pip install -e .
meshcore-pathbot --serial /dev/ttyUSB0 --config config.toml
```

Then open http://localhost:8075 for the web dashboard.

## Usage

```
meshcore-pathbot [OPTIONS]

Connection (pick one):
  -s, --serial PORT     Serial port (e.g. /dev/ttyUSB0, COM3)
  -t, --tcp HOST        TCP host (e.g. 192.168.1.100)
  -b, --ble [ADDR]      BLE address (or empty to scan)

Options:
  -C, --config FILE     TOML config file
  -c, --channel N       Channel to listen on (default: 2)
  -r, --repeaters-file  Repeaters DB path (.db preferred)
  -i, --ignore NAME     Node names to ignore
  -d, --debug           Enable debug logging
  --no-web              Disable web dashboard
  --web-port PORT       Web dashboard port (default: 8075)
```

## Configuration

Copy `config.example.toml` to `config.toml` and edit to your setup. CLI arguments override config file values.

## Docker

```bash
docker compose up -d
```

Mount your serial device by uncommenting the `devices` section in `docker-compose.yml`.

## Systemd

```bash
sudo cp meshcore-pathbot.service /etc/systemd/system/
sudo systemctl enable --now meshcore-pathbot
```
