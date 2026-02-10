# meshcore-pathbot

MeshCore path resolver bot with a web dashboard.

Connects to a MeshCore companion radio, listens for `trace` and `ping` commands on a channel, resolves hex path prefixes to repeater names using a persistent database, and replies on the channel. Includes a full web dashboard for live monitoring, repeater management, path visualization, and configuration.

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
