# rv-control

`rv-control` collects telemetry from an RV-C CAN bus, Renogy BLE devices, and a Hughes Power Watchdog, publishing each reading as JSON to Mosquitto.

The runtime is self-contained and does not import code or configuration from external checkout directories:

- `src/rv_control/data/rvc-spec.yml` is the project-owned RV-C DGN specification.
- `src/rv_control/renogybt` contains the project-owned Renogy client implementation.
- `src/rv_control/hughes.py` contains the standalone Hughes BLE implementation.

## Install

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

The editable install provides both `rv-control` and `rvcontrol` commands. The equivalent module form is `python -m rv_control`.

## Commands

Copy the anonymized example configuration to create your local configuration:

```sh
cp config-example.ini config.ini
$EDITOR config.ini
```

Show the available global options and commands:

```sh
rv-control --help
rv-control check --help
rv-control comms-check --help
rv-control interrogate --help
rv-control run --help
```

Validate the configuration without opening CAN, Bluetooth, or MQTT connections:

```sh
rv-control --config config.ini check
```

The check command prints the resolved config path and whether each source is enabled. With the supplied example configuration, the expected output is similar to:

```text
Configuration OK: /path/to/config.ini
rv_c: disabled
renogy: disabled
hughes: enabled
```

Check communications for every configured source without starting the service:

```sh
rv-control --config config.ini comms-check
```

Enabled RV-C checks open and close the configured SocketCAN interface and verify the DGN spec file. Enabled Bluetooth checks contact the local Bluetooth stack, scan for the configured device address, and verify its advertised name when configured. The command exits with status `1` if an enabled source fails.

Renogy's communication check also lists the register blocks exposed by the configured client type, including read/write access, register address, word count, and the decoded attribute or operation name. Hughes checks list the telemetry attributes exposed by the detected device generation.

Interrogate every enabled source to perform live reads and print all returned attributes as formatted JSON:

```sh
rv-control --config config.ini interrogate
```

Renogy interrogation connects once, reads every register block defined by the configured Renogy client type, and prints the decoded state. Hughes connects once, waits for a complete notification packet, and prints its decoded power attributes. RV-C listens for `interrogate_seconds` (10 by default), decodes received DGN frames, and prints the latest state for each DGN/source combination. The command exits with status `1` if an enabled source cannot be interrogated.

Run the collectors in the foreground:

```sh
rv-control --config config.ini run
```

Enable diagnostic logging while troubleshooting hardware or MQTT connections:

```sh
rv-control --verbose --config config.ini run
```

Stop a foreground process with `Ctrl+C`. The process stops source threads, disconnects MQTT, and exits cleanly.

## Bluetooth discovery

Use the lightweight discovery tool to find nearby devices that advertise names associated with Renogy or Hughes Power Watchdog hardware:

```sh
.venv/bin/python tools/bt_discovery.py
```

Choose a different adapter or scan duration when needed:

```sh
.venv/bin/python tools/bt_discovery.py --adapter hci0 --timeout 15
```

The tool prints each matching device's family, Bluetooth address, advertised name, and signal strength. It does not connect to or modify any device.

## MQTT check

Use the MQTT check tool to verify the broker connection and observe project topics:

```sh
.venv/bin/python tools/mqtt_check.py
```

Choose a different broker, credentials, topic prefix, or wait time when needed:

```sh
.venv/bin/python tools/mqtt_check.py \
	--host localhost --port 1883 --base-topic rv --timeout 5
```

The tool subscribes to `<base-topic>/#` and prints retained or live topics received during the check. MQTT does not provide a portable command to list empty topics, so a topic with no retained message and no activity during the timeout cannot be reported by this tool.

## Configuration

Edit only the sections for hardware installed in the RV. List enabled source sections in `[source]`; each listed section selects its implementation with `type`.

```ini
[mqtt]
host = localhost
port = 1883
base_topic = rv
username =
password =
write_enabled = false

[source]
enabled-sources = renogy_controller

[rv_c]
interface = can0
specfile = src/rv_control/data/rvc-spec.yml
parameterized_strings = true
write_enabled = false

[renogy_controller]
type = renogy
device-type = RNG_CTRL
adapter = hci0
mac_addr = AA:BB:CC:DD:EE:FF
alias = BT-TH-EXAMPLE
device_id = 255
max_retry = 3
persistent_connection =
enable_polling = true
poll_interval = 60
temperature_unit = F
fields =
topic = renogy
write_enabled = false
```

The Renogy section is self-contained. `device-type` can be `RNG_CTRL`, `RNG_CTRL_HIST`, `RNG_BATT`, `RNG_INVT`, `RNG_INVT_HF`, `RNG_DCC`, or `RNG_SHNT`. Use the Bluetooth adapter's address format exactly as reported by discovery tools. An empty `persistent_connection` inherits `[service] daemon_mode`; it defaults to enabled for daemon operation. When enabled, the initial BLE discovery is reused across reconnects instead of scanning again. Persistent mode also keeps polling active, so use `enable_polling = true` for continuous daemon telemetry.

```ini
[hughes_power_watchdog]
type = hughes
adapter = hci0
address = AA:BB:CC:DD:EE:FF
name = PMD-EXAMPLE
persistent_connection =
topic = hughes
write_enabled = false
```

Hughes uses the same `persistent_connection` setting as Renogy. Runtime connects directly to the configured address without repeatedly scanning; when persistence is enabled, a dropped session reconnects to that address after `service.reconnect_delay` seconds. `comms-check` remains a one-shot scan and ignores daemon mode.

Daemon retry behavior is configured in the service section:

```ini
[service]
daemon_mode = true
reconnect_delay = 10
max_retry = 0
max_reconnect_delay = 300
```

`max_retry = 0` means unlimited retries. `max_reconnect_delay` caps the exponential backoff used by the Hughes source; source supervision also restarts a collector thread that exits unexpectedly.

The MQTT `write_enabled` option is a global safety switch. RV-C and Renogy also require their own source-level `write_enabled = true` before accepting commands. Hughes is telemetry-only.

Bluetooth access usually requires membership in the `bluetooth` group or appropriate Linux capabilities. RV-C requires a configured SocketCAN `can0` interface and the MCP2515 kernel overlay.

Topics are `<base_topic>/<source>` for source snapshots. RV-C additionally publishes each decoded DGN at `<base_topic>/rvc/<dgn-name>`.

## MQTT commands

Bidirectional commands are opt-in. Set `mqtt.write_enabled = true` and the relevant source's `write_enabled = true`, then publish JSON to the source command topic.

Send an extended RV-C CAN frame:

```sh
mosquitto_pub -h localhost -t rv/rv_c/set \
	-m '{"can_id":"0x19FEDB99","data":"02FFC803FF00FFFF"}'
```

Send a raw Renogy BLE request:

```sh
mosquitto_pub -h localhost -t rv/renogy_controller/set \
	-m '{"bytes":"FF0301000022D1F1"}'
```

Ask Renogy to generate a Modbus read request with its CRC:

```sh
mosquitto_pub -h localhost -t rv/renogy_controller/set \
	-m '{"register":256,"words":34,"function":3}'
```

The configured MQTT username and password can be passed to `mosquitto_pub` with `-u` and `-P`. Verify command traffic with:

```sh
mosquitto_sub -h localhost -t 'rv/#' -v
```

## Testing and troubleshooting

Run the included syntax and import checks without hardware:

```sh
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
PYTHONPATH=src .venv/bin/python -c \
	'from rv_control.config import load_config; load_config("config.ini"); print("config passed")'
```

Before enabling RV-C, confirm SocketCAN is available:

```sh
ip link show can0
```

Monitor RV-C traffic with decoded fields using:

```sh
PYTHONPATH=src .venv/bin/python tools/rvc_monitor.py
PYTHONPATH=src .venv/bin/python tools/rvc_monitor.py --interface can1
```

The monitor prints every CAN frame in a candump-style format. Extended RV-C frames include the DGN name, source address, and decoded values; non-RV-C frames are shown as raw frames.

Read the controller date and time with:

```sh
PYTHONPATH=src .venv/bin/python tools/rvc_datetime.py
```

Set the controller date and time from the host clock with:

```sh
PYTHONPATH=src .venv/bin/python tools/rvc_datetime.py -s
```

The utility uses `can0` by default. Select another interface with `--interface` or provide a custom RV-C specification file as the final argument:

```sh
PYTHONPATH=src .venv/bin/python tools/rvc_datetime.py --interface can1
PYTHONPATH=src .venv/bin/python tools/rvc_datetime.py /path/to/rvc-spec.yml
```

Before enabling Bluetooth sources, confirm the adapter and device are visible:

```sh
bluetoothctl list
bluetoothctl scan on
```

Run with `--verbose` to see connection and parsing failures. A source error is logged independently; check that the configured address, interface, device ID, and source settings are correct.

Install dependencies with `pip install -r requirements.txt`.

## systemd

Install the project and config under `/opt/rv-control`, create the `rv-control` service user, and adjust the user and paths in [deploy/rv-control.service](deploy/rv-control.service). Then install and start the unit:

```sh
sudo cp deploy/rv-control.service /etc/systemd/system/rv-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now rv-control
sudo systemctl status rv-control
```

Follow service logs with:

```sh
sudo journalctl -u rv-control -f
```

Stop or restart the daemon with:

```sh
sudo systemctl stop rv-control
sudo systemctl restart rv-control
```

## Third-party acknowledgements

This project explicitly acknowledges and incorporates work from the following open-source projects. The listed upstream repositories are the authoritative sources for the original code, documentation, specifications, and license terms:

- [linuxkidd/rvc-monitor-py](https://github.com/linuxkidd/rvc-monitor-py): RV-C protocol implementation and DGN specification. The project-owned specification copy is `src/rv_control/data/rvc-spec.yml`. The upstream project is licensed under the [Apache License 2.0](https://github.com/linuxkidd/rvc-monitor-py/blob/master/LICENSE).
- [cyrils/renogy-bt](https://github.com/cyrils/renogy-bt): Renogy BLE clients, register definitions, and parsers. The project-owned client copy is `src/rv_control/renogybt/`, with its license notice at `src/rv_control/renogybt/LICENSE`. The upstream project is licensed under the [GNU General Public License v3.0](https://github.com/cyrils/renogy-bt/blob/main/LICENSE).
- [IAmTheMitchell/Hughes-Power-Watchdog](https://github.com/IAmTheMitchell/Hughes-Power-Watchdog): Hughes Power Watchdog BLE protocol reference and device-generation behavior. The standalone implementation in `src/rv_control/hughes.py` follows that protocol documentation. The upstream project is licensed under the [MIT License](https://github.com/IAmTheMitchell/Hughes-Power-Watchdog/blob/main/LICENSE).

The copied and derived components under `src/rv_control` retain the applicable upstream license notices. When redistributing this project, preserve those notices and continue to provide the upstream license terms for the corresponding components.

## Raspberry Pi 4 addendum

The service is intended to run on Raspberry Pi OS 64-bit on a Raspberry Pi 4. The following host details are important:

### Operating-system packages

Install the system packages required by SocketCAN, Bluetooth, Mosquitto, and Python virtual environments:

```sh
sudo apt update
sudo apt install -y \
	can-utils bluez bluetooth mosquitto python3-venv python3-dev build-essential
```

Enable and start the local broker and Bluetooth service:

```sh
sudo systemctl enable --now mosquitto
sudo systemctl enable --now bluetooth
```

### MCP2515 and SocketCAN

An MCP2515 board needs a matching device-tree overlay, oscillator frequency, and interrupt GPIO for the specific CAN board. Do not copy those values blindly from another board. On current Raspberry Pi OS releases, edit `/boot/firmware/config.txt`; older releases may use `/boot/config.txt`.

For example, a board using a 16 MHz oscillator and GPIO 25 may require a line similar to:

```ini
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```

Reboot after changing the overlay, then bring up the interface with the bitrate required by the RV-C installation:

```sh
sudo reboot
sudo ip link set can0 up type can bitrate 250000
ip -details link show can0
```

The bitrate must match the RV-C network. Confirm the interface before starting the service:

```sh
ip link show can0
candump can0
```

If `can0` is missing, check the overlay name, oscillator value, interrupt GPIO wiring, SPI enablement, and `dmesg` output before troubleshooting this application.

### Bluetooth and permissions

The Renogy and Hughes sources use BlueZ through the `hci0` adapter by default. Confirm the adapter is powered and visible:

```sh
bluetoothctl list
bluetoothctl show
```

When running under systemd, the service account must be able to access the Bluetooth stack and SocketCAN device. Add the account to the relevant groups on the image, commonly `bluetooth`, `dialout`, and `gpio` where applicable, then log out and back in or restart the service. Keep the adapter setting explicit if more than one adapter is installed:

```ini
[renogy]
adapter = hci0

[hughes]
adapter = hci0
```

Run `comms-check` interactively first. It performs fresh discovery and is useful for confirming that the Pi sees the configured BLE addresses before enabling daemon mode.

### Installation location and service startup

For a system service, install the project at a stable path such as `/opt/rv-control`, use a virtual environment inside that directory, and ensure `deploy/rv-control.service` points to the same path. Copy `config-example.ini` to the service account's `config.ini`; do not place passwords or real device identifiers in the example file.

The Pi's onboard Bluetooth and SPI devices can take a moment to appear during boot. The supplied systemd unit starts after `bluetooth.target` and `network-online.target`; `Restart=always` and `RestartSec=10` allow the process to recover from early hardware or broker availability failures.

### Raspberry Pi troubleshooting

Use these checks before changing application settings:

```sh
systemctl status bluetooth mosquitto
rfkill list
ip -details link show can0
dmesg | grep -Ei 'mcp2515|can0|spi|bluetooth'
sudo journalctl -u rv-control -f
```

Avoid running multiple Bluetooth clients against the same Renogy or Hughes device at once. Mobile applications can hold the device connection and prevent the Pi from connecting.