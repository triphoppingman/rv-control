# rv-control guidance

## Project purpose

This is a Python 3.10+ service that collects RV telemetry from:

- RV-C over SocketCAN, normally `can0` through an MCP2515 interface.
- Renogy devices over BLE.
- Hughes Power Watchdog devices over BLE.

Telemetry is published as JSON to a local MQTT broker such as Mosquitto.

## Repository structure

- `src/rv_control/cli.py`: Click command-line interface.
- `src/rv_control/source.py`: `Source` ABC, source registry, lifecycle, supervision, communication checks, and aggregate interrogation.
- `src/rv_control/rvc.py`: RV-C decoding, CAN reads, interrogation, and optional writes.
- `src/rv_control/renogy.py`: Renogy adapter, self-contained configuration mapping, register inventory, interrogation, and optional writes.
- `src/rv_control/hughes.py`: Hughes BLE protocols, attribute inventory, interrogation, and daemon reconnects.
- `src/rv_control/renogybt/`: Project-owned Renogy client implementation.
- `src/rv_control/data/rvc-spec.yml`: Project-owned RV-C DGN specification.
- `src/rv_control/mqtt.py`: MQTT publishing and optional command subscription.
- `config.ini`: Local runtime configuration; treat it as machine-specific and do not expose credentials.
- `config-example.ini`: Shareable configuration template.
- `tests/`: pytest regression tests.

The old `vendor/` directory has been removed. Do not recreate runtime dependencies on it. Use the project-owned copies under `src/rv_control`.

## Implementation rules

- Use type hints on every function and method in `src`, including private methods and callbacks.
- Add an expressive docstring to every function and method, including private methods and nested callbacks.
- Preserve the `Source` ABC and register new sources through its registry mechanism. A source must provide `source_name`, `config_section`, `run`, and `comms_check`.
- Keep protocol-specific behavior inside its source module. Do not put device logic in the CLI.
- Keep shared RV-C utility functionality in `src/rv_control/rvc.py`; tools under `tools/` should call its canonical specification, CAN-frame, DGN, and protocol helper functions rather than duplicating them.
- Keep `comms-check` and `interrogate` one-shot operations. They must not inherit daemon persistence or run indefinitely.
- `run` is the long-lived daemon path. Avoid rereading static specifications or rebuilding static client configuration inside poll loops.
- Keep write-back disabled by default. MQTT global `write_enabled` and source-level write flags must both be required.
- Validate incoming MQTT commands before touching CAN or BLE hardware. Never accept unchecked CAN IDs, oversized CAN payloads, or malformed hexadecimal data.
- Handle temporary hardware failures with bounded or configurable retry behavior and clear logs. Do not silently swallow failures that make a source unavailable.
- Keep MQTT cleanup and source cleanup in `finally` blocks. Initialize cleanup variables before entering `try` blocks.
- Do not add cloud services or external telemetry destinations without an explicit request.
- Avoid unrelated refactors and preserve the existing public CLI/configuration contract.

## Configuration conventions

- Renogy configuration is self-contained in `[renogy]`; do not add a `configfile` reference to an external Renogy config.
- Renogy and Hughes Bluetooth adapters default to `hci0` and are configurable per source.
- `persistent_connection` may be blank to inherit `[service] daemon_mode` for daemon operation.
- `comms-check` must perform fresh discovery/checks even when daemon mode is enabled.
- Keep real device addresses and credentials out of documentation and new tests.

## Commands and validation

Use the configured virtual environment:

```sh
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
.venv/bin/rvcontrol --config config.ini check
.venv/bin/rvcontrol --config config.ini comms-check
.venv/bin/rvcontrol --config config.ini interrogate
```

Hardware commands may fail when `can0`, Bluetooth devices, or Mosquitto are unavailable. Always run hardware-free tests and compilation after edits. Do not claim a live hardware check passed unless it was actually run.

## Attribution

The project incorporates or follows work from these upstream repositories:

- https://github.com/linuxkidd/rvc-monitor-py, Apache License 2.0.
- https://github.com/cyrils/renogy-bt, GNU GPL v3.0.
- https://github.com/IAmTheMitchell/Hughes-Power-Watchdog, MIT License.

Preserve the applicable license notices in the project-owned copied or derived components.
