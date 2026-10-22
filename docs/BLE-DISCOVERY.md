# Unknown BLE device discovery job aid

Use this procedure to investigate an unfamiliar **Bluetooth Low Energy** sensor
before proposing an RV-Control source or claiming protocol support. It is for
human operators and AI agents working with an operator.

The utility is [tools/bt_discovery.py](../tools/bt_discovery.py). It does not
load runtime configuration, publish MQTT, or start collectors. Existing
invocations still scan for likely Renogy/Hughes names. No packages beyond the
project's existing Click and Bleak dependencies are required.

## Safety and scope

- Investigate only devices you own or are authorized to inspect. Scans also
  reveal nearby unrelated equipment; do not connect to it.
- Begin with advertisements. Connect only to an explicitly identified device.
- Close the vendor phone app and arrange for the relevant collector to stop
  using the device before connecting. Do not stop shared services without the
  operator's approval. Many sensors accept just one BLE connection.
- Do not power-cycle RV electrical equipment or move a vehicle solely to
  identify a sensor. Only power-cycle or gently tilt a standalone sensor when
  the operator considers that safe.
- No command performs explicit pairing, arbitrary characteristic writes,
  calibration, resets, firmware updates, or protection bypasses. The utility
  does not forcibly disconnect another application's connection.
- `inspect` retrieves GATT metadata, not characteristic/descriptor values.
  `read` reads exactly one selected readable characteristic. A device may still
  have read-side effects or require authentication; advertised properties are
  not proof of safety.
- `listen` subscribes to one selected notification/indication characteristic.
  Subscription normally writes the BLE client-configuration descriptor. It is
  **not zero-write at the BLE level**, but sends no application command.
- A connection or subscription is not evidence that measurements were decoded.
  Unknown bytes must remain unknown until validated.
- Captures contain device addresses and can include identifying or sensitive
  data. Store locally, review/redact before sharing, and do not commit raw
  installation captures or upload them to external services without approval.

This utility scans **BLE only**. Bluetooth Classic RFCOMM devices, such as many
ELM327 adapters, require a different workflow.

## Command reference

Run from the repository root using the configured virtual environment:

```sh
.venv/bin/python tools/bt_discovery.py --help
.venv/bin/python tools/bt_discovery.py inspect --help
.venv/bin/python tools/bt_discovery.py read --help
.venv/bin/python tools/bt_discovery.py listen --help
```

Shared `--adapter` and `--timeout` options go **before** the subcommand:

```text
bt_discovery.py [SHARED OPTIONS] inspect ADDRESS [OPTIONS]
bt_discovery.py [SHARED OPTIONS] read ADDRESS --characteristic UUID_OR_HANDLE
bt_discovery.py [SHARED OPTIONS] listen ADDRESS --characteristic UUID_OR_HANDLE
```

`--adapter` defaults to `hci0`. `--timeout` defaults to 10 seconds, accepts finite
values from 1 through 120, and applies separately to scanning, connection, GATT
operations and disconnect cleanup. Address resolution has a five-second
allowance beyond its scan timeout. These are per-phase bounds, not a single
total-command deadline.

Characteristic selectors accept a UUID, decimal handle (for example `37`), or
hexadecimal handle (`0x25`). UUIDs may occur more than once; use a handle from
the current inspection when Bleak reports ambiguity. Handles can change across
devices or firmware versions and are not portable protocol identifiers.

`--all`, `--details`, `--json`, and the top-level `--output` apply to scanning
only. Each subcommand has its own `--output`.

## 1. Discover and identify a candidate

Preserve the existing known-family scan:

```sh
.venv/bin/python tools/bt_discovery.py --adapter hci0 --timeout 15
```

Include unknown/unnamed devices and raw advertisement fields:

```sh
.venv/bin/python tools/bt_discovery.py --adapter hci0 --timeout 15 --all --details
.venv/bin/python tools/bt_discovery.py --adapter hci0 --timeout 15 --all --output scan-on.json
```

Alternatively, `--all --json` prints the JSON array to stdout. `--output` saves
JSON even when `--json` is omitted. Output files are created exclusively:
existing files are never overwritten, and parent directories must exist.

Scan records contain:

- Capture timestamp, address, advertised local name (or null), and likely family
  (or null). Family identification is only a name-prefix heuristic.
- RSSI, advertised transmit power (possibly null), and service UUIDs.
- Manufacturer data: decimal company-ID keys and lowercase hexadecimal bytes.
- Service data: UUID keys and lowercase hexadecimal bytes.

The scan is a **last-known snapshot per address**, not a timestamped stream of
every advertisement. Its timestamp is when the result record was assembled,
not the RF reception time. It cannot prove absence: a sleeping sensor, a device
connected to another client, or an address that rotates may not appear.

Identify your candidate using operator-confirmed labels, proximity and repeated
scans. Where safe, compare sensor-off and sensor-on snapshots:

```sh
.venv/bin/python tools/bt_discovery.py --all --output scan-off.json
.venv/bin/python tools/bt_discovery.py --all --output scan-on-repeat.json
```

RSSI and a suggestive name are clues, not proof. For Beech Lane, do not assume
the name, service UUIDs or protocol match LevelMate Pro or any other brand.

## 2. Inventory GATT without reading values

Replace `DEVICE_ADDRESS` with the operator-confirmed BLE address:

```sh
.venv/bin/python tools/bt_discovery.py --adapter hci0 --timeout 15 \
  inspect DEVICE_ADDRESS --output gatt.json
```

The tool resolves that address on the selected adapter, connects, enumerates
services/characteristics/descriptors, then disconnects. It reports UUIDs,
handles, descriptions and characteristic properties. It does not automatically
read even standard battery/device-information characteristics.

Record:

1. Sensor product/model, firmware if independently known, and discovery date.
2. Service UUIDs and candidate characteristics with `read`, `notify` or
   `indicate` properties.
3. Any evidence associating a characteristic with measurements.
4. Whether the device was awake and the vendor app/collector disconnected.

Descriptions such as "Unknown" are normal for vendor-specific UUIDs.
Writable properties do **not** authorize exploring writes.

## 3. Read a selected value

After selecting a characteristic from the inventory:

```sh
.venv/bin/python tools/bt_discovery.py --adapter hci0 \
  read DEVICE_ADDRESS --characteristic CHARACTERISTIC_UUID --output value.json
```

The JSON result includes UTC timestamp, monotonic timestamp in nanoseconds,
address, characteristic UUID/handle, byte length, and raw hex. Reads without
advertised `read` support are rejected. Authentication failures are surfaced;
the tool will not attempt pairing as a fallback.

Do not read all characteristics automatically. Start with a known harmless
characteristic or an explicitly reviewed candidate.

## 4. Capture notifications

Subscribe only to a selected `notify` or `indicate` characteristic:

```sh
.venv/bin/python tools/bt_discovery.py --adapter hci0 --timeout 15 \
  listen DEVICE_ADDRESS --characteristic CHARACTERISTIC_UUID \
  --duration 30 --max-packets 10000 --output level-baseline.jsonl
```

- Default duration: 30 seconds; range: 0.1 through 3600 seconds.
- Default packet limit: 10000; range: 1 through 100000.
- Capture ends at the duration or packet limit, whichever occurs first.
- Duration begins after subscription setup, not when scanning starts.
- Each packet is immediately written/flushed as one JSON object per line, with
  the same raw-byte fields as `read`. No unbounded packet buffer is retained.
- Without `--output`, JSONL goes to stdout. The success summary goes to stderr:
  `{"packets": 12, "stop_reason": "duration"}` or `"packet_limit"`.
- Link loss, output failure, failed subscription and zero received packets exit
  unsuccessfully. Partial files are retained as evidence, not marked complete.
- Cleanup attempts bounded unsubscription and disconnect even after errors or
  interruption. A failed cleanup is an error; process interruption, OS failure
  or a forcibly killed process cannot guarantee hardware teardown.

For tilt sensors, collect separate labeled captures for stable baseline and
safe, controlled front/back/left/right sensor orientations. Prefer a stationary,
removable sensor; do not instruct vehicle or jack movement as part of automated
discovery. Record the vendor app's values separately if they cannot be observed
at the same time because the device permits only one connection.

Do not assume one notification is a complete message. It may contain fragments,
multiple messages, counters or checksums. Record actual packet lengths.

## 5. Establish the protocol from evidence

Before writing a decoder, compare repeated captures and explicitly test:

- Framing/header bytes, message types, and length consistency.
- Which bytes change with each controlled measurement.
- Signed versus unsigned values, byte order, scaling and units.
- Axis order, sign, mounting orientation and reference/zero offsets.
- Whether reported values are angles or vehicle-dimension-dependent lift
  distances. Do not label one as the other without evidence.
- Checksums/CRC only when supported by repeatable observations.
- Sleep behavior, disconnect/reconnect behavior and firmware/model differences.

Keep raw observations separate from hypotheses. A useful evidence table is:

| Observation | Capture and packet reference | Hypothesis | Independent check |
| --- | --- | --- | --- |
| Two bytes vary with one axis | Local file plus line number | Signed little-endian angle | Repeat opposite direction and compare displayed value |

If no packets arrive, check device wake state, ownership and characteristic
selection. Some devices require an application-level telemetry handshake.
**Do not guess or send arbitrary bytes.** This utility intentionally has no
write command. The next step may be an operator-approved Android Bluetooth HCI
capture of the official app; HCI capture and decryption are not implemented here.
Such captures can contain credentials and unrelated traffic.

## AI-agent execution and handoff checklist

1. Inspect the tool's current help and existing evidence before proposing
   commands; distinguish implemented options from hypothetical functionality.
2. Obtain operator confirmation of device ownership, candidate identity and
   whether a collector/app must release the connection. Do not scan or connect
   to hardware merely because this document is available.
3. Use finite scans/captures and explicitly selected characteristic UUIDs or
   handles. Avoid parallel connections, automatic retries and arbitrary writes.
4. Preserve captures locally with unique, meaningful filenames. Never overwrite
   evidence or commit identifying captures by default.
5. Report the command, exit status, device state, packet count/stop reason,
   relevant UUIDs, and limitations. Empty/no-data captures are not success.
6. Cite local evidence filenames and packet line numbers when explaining byte
   interpretations. Mark assumptions, unresolved fields and device compatibility
   as unverified until confirmed.
7. Stop when identification is uncertain, authentication is needed, or further
   progress would require unreviewed commands. Ask the operator rather than
   broadening hardware access.
8. Once a protocol is established, propose a read-only source with fixture-based
   decoder tests, bounded lifecycle behavior and explicit units. Discovery is
   not an instruction to implement a source or enable write actions.

## Troubleshooting

- **No devices:** verify the chosen adapter, radio state, sensor wake state and
  permissions; compare the device with its official app after the scan ends.
- **Device found but connection fails:** close other clients, wait for their
  disconnect, then make a fresh bounded attempt. Do not assume a stale address
  still identifies the same device.
- **Ambiguous characteristic UUID:** inspect again and select the current handle.
- **Permission/authentication error:** document it; do not automatically pair,
  change BlueZ permissions or restart services.
- **No notifications:** this may indicate the wrong characteristic, sleep,
  indication/firmware quirks, or an undiscovered handshake; it does not prove
  the sensor is unsupported.
- **Output already exists:** choose a new evidence filename.
- **Partial capture/error:** keep the partial file, report the failure, and do not
  present it as a complete duration capture.
