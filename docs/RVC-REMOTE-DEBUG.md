# Remote RV-C message debugging with Kiro

Use this runbook to let another Kiro instance inspect live CAN traffic through
the Pi's temporary authenticated TCP monitor. Both raw CAN evidence and decoded
RV-C values are included. The server is strictly receive-only: it has no CAN
send command, no AT interface, and no unsafe-write option.

## Start the server on the Pi

From the repository directory, with SocketCAN already configured:

```sh
PYTHONPATH=src .venv/bin/python tools/rvc_monitor.py --interface can0 \
  server --host 0.0.0.0 --port 35001
```

The default host is `127.0.0.1` and default port is `35001`, distinct from the
OBD server's port. Use the Pi's LAN IPv4 address instead of `0.0.0.0` to restrict
the listener to that interface. Interface, `--specfile`, and
`--parameterized/--no-parameterized` options go **before** `server`.
The specification is loaded once at startup.

The server prints a newly generated token locally. To supply a reusable token,
use `--auth-token` or `RVC_SERVER_AUTH_TOKEN`. Tokens must contain 1 to 200
printable ASCII characters with no whitespace. Give the client the actual LAN
address, port, and token separately; do not commit or include the token in reports.
This is unencrypted plain TCP for temporary trusted-LAN debugging, not Telnet.
Do not expose it to the internet. Stop it with `Ctrl+C` after use.

Unlike the ELM327, SocketCAN supports independent receive sockets. The normal
collector can stay running while this receive-only monitor subscribes; this
server does not publish MQTT, reconfigure CAN, or transmit frames.

## Connect and collect

```sh
nc <pi-address> 35001
```

After `AUTH required`, type `AUTH <token>` and press Enter within ten seconds.
The server sends `OK authenticated; streaming JSON lines`, then one JSON
record per received CAN frame. Authentication precedes opening CAN.
If CAN cannot be opened, an `ERROR session failed: ...` line follows and the
connection closes; authentication success alone does not prove CAN is working.

Only one client is serviced at a time. Send `QUIT` followed by Enter or close
the socket to release that client's CAN subscription. The listener remains
available for the next client. Other commands are rejected and close the
session; there is no remote frame transmission. CR or LF terminates input.

Each JSON record includes:

| Field | Meaning |
| --- | --- |
| `timestamp` | CAN message timestamp in seconds, from python-can. |
| `can_id` | Uppercase hexadecimal arbitration ID, eight digits for extended frames or three for standard frames. |
| `data` | Original payload bytes as space-separated uppercase hex, including unknown DGNs and non-RV-C traffic. |
| `dlc` | Frame DLC reported by python-can. |
| `is_extended_id`, `is_remote_frame`, `is_error_frame`, `is_fd` | Original frame flags. |
| `bitrate_switch`, `error_state_indicator` | CAN FD flags. |
| `channel` | Received channel as text, or `null` if unavailable. |
| `decoded` | Canonical RV-C decoding with DGN/source/name and available values, or `null` for standard, remote, error, or FD frames. |

Unknown extended DGNs retain their raw data and an `UNKNOWN-...` name in
`decoded`. Missing decoded fields do not necessarily imply absent wire data:
decoding depends on the specification and the payload. Inspect `data` directly.
These are JSON records of the original CAN fields, not a binary packet capture
or a TCP stream of payload bytes without framing.

Socket clients must buffer through LF and handle split or combined TCP reads.
The auth greeting and status/error lines are text, not JSON. Do not try to parse
them as frame records. There is no OBD-style `>` prompt.
Use a bounded capture period or frame count; silence can mean the bus has no
traffic and is not by itself a server failure. `--write-timeout` defaults to
ten seconds per socket write; slow readers are disconnected with an explicit
server-side error. There is no frame replay or delivery guarantee, so preserve
timestamps and note gaps during reconnections or slow-client failures.

## Handoff instructions for Kiro

Provide the endpoint and token separately along with target DGN names/IDs,
source addresses if known, symptoms, the selected specification, and coach
operating state. Then give Kiro this instruction:

> Follow `docs/RVC-REMOTE-DEBUG.md`. Authenticate to the supplied TCP monitor
> and capture a bounded sample of raw and decoded frames for the requested
> RV-C issue. Preserve timestamps, CAN IDs, payload bytes, frame flags, and
> decoded DGN/source fields. Compare the wire data with the project-owned
> specification and canonical helpers in `src/rv_control/rvc_util.py`.
> Do not transmit CAN frames, change bus configuration, or assume missing decoded
> fields mean missing payload bytes. Filter locally by DGN/source after capture.
> Explain what the evidence establishes, any timing or specification uncertainty,
> and the next targeted check. Redact the token and send `QUIT` when finished.

Record the interface, relevant frame timestamps, DGN/source, raw bytes, decoded
values, expected behavior, and any observed gaps or error frames. If a coach
control must change to reproduce the issue, have the operator make that change
and record its time; the monitor cannot issue control commands.

See [README](../README.md) for the existing local monitor and other RV-C tools.
