# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Add new entries under `[Unreleased]` in the same commit as the code change.

## [Unreleased]

### Added

- Pluggable telemetry targets: `Target` ABC ([src/rv_control/target.py](src/rv_control/target.py)) with a self-registering registry, mirroring `Source`. Targets are enabled via `[target] enabled-targets` (section names, each with a `type`); several targets fan out through `MultiTarget`, where one target's failure does not affect the others.
- `store` target ([src/rv_control/store.py](src/rv_control/store.py)) that keeps the latest payload per full topic (e.g. `rv/renogy`) in a local JSON file (`directory`, `filename`, `flush_interval`; default `data/state.json`), written atomically from a background thread.
- `[target]` and `[store]` sections in `config-example.ini`, README "Targets" documentation, and `tests/test_target.py`.
- ELM327 Bluetooth OBD-II source (`type = obd`, [src/rv_control/obd.py](src/rv_control/obd.py)) that polls configured PIDs read-only over a raw Bluetooth RFCOMM socket (no `rfcomm bind`, no `pyserial`) and publishes them to MQTT.
  - PIDs are configured in the source's INI section as `pid.<name> = mode, pid, decode, unit`; decode expressions over `b0`, `b1`, … are validated against a safe arithmetic subset and compiled once at startup.
  - Only read-only OBD modes (01, 02, 03, 05, 06, 07, 09, 0A, 21, 22) are accepted; the source does not accept MQTT commands.
  - Adapter initialization (`ATZ`, `ATE0`, `ATL0`, `ATS0`, `ATH0`, `ATSP<protocol>`, `ATSH<header>`), ECU probe with `0100`, and optional `fallback_protocol` when automatic detection fails.
  - Responses are read until the ELM327 `>` prompt; `SEARCHING...`, multi-frame (`0:`/`1:`) and multi-ECU replies, error strings, and negative responses are handled, and the echoed mode/PID is verified before decoding.
  - Per-cycle flat snapshots on `<base_topic>/<topic>` (failed PIDs are `null`) and availability changes on `<base_topic>/<topic>/status` with adapter version, battery voltage, and protocol.
  - When the source goes offline (engine off or adapter lost), an all-`null` snapshot is published to `<base_topic>/<topic>` so snapshot-only consumers such as rv-control-ui clear stale engine values.
  - Engine on/off resilience: an unreachable adapter or a silent ECU (`max_failed_cycles`) is treated as offline, the socket is closed, and reconnects use exponential backoff (`reconnect_delay`, `max_reconnect_delay`, `max_retry`, inheriting `[service]` when blank). State transitions are logged once.
  - Fixed-rate polling at `poll_hz` with a one-time warning when a cycle overruns.
  - `adapter` selects the local Bluetooth controller by `hciN` name or controller MAC, and the RFCOMM socket is bound to it.
  - `comms-check` passes when the adapter answers (even with the engine off) and lists configured PIDs and units; `interrogate` returns one reading of every PID.
- `tools/obd_tool.py` with `info`, `supported`, `read`, `monitor`, and `query` commands for direct, read-only adapter access using the same INI section.
- `[obd_engine]` example section in `config-example.ini`.
- Synchronous `connect_slot_sync()` on the Bluetooth adapter coordinator so RFCOMM connects serialize with BLE connection setup on a shared adapter.
- README documentation for the OBD source and a Raspberry Pi guide for ELM327 adapter selection, controller selection, discovery, pairing, RFCOMM channel lookup, permissions, and troubleshooting.
- `tests/test_obd.py` covering PID parsing, decode safety, response parsing, adapter I/O, interrogation, fallback protocol, comms-check, daemon reconnects, retry limits, and adapter resolution.
- This changelog, and a steering rule requiring changelog updates with code commits.

### Changed

- MQTT publishing is now `MqttTarget` ([src/rv_control/mqtt.py](src/rv_control/mqtt.py)), with unchanged topics, payloads, and write gating; `MqttPublisher` remains as an alias. `run` builds publishers with `Target.build`, and command routing locates the MQTT section by `type`.
- `[mqtt]` is no longer required when a `[target]` section is present. Without `[target]`, the legacy `[service] targets` list (default `mqtt`) applies, so existing configs behave as before.
- Configuration files are read as UTF-8 so unit strings such as `°F` load consistently.
