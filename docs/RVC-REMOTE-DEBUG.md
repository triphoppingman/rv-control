# Remote RV-C message debugging with Kiro

Use this runbook to let another Kiro instance inspect live CAN traffic through
the Pi's temporary authenticated TCP monitor. Both raw CAN evidence and decoded
RV-C values are included. The server is strictly receive-only: it has no CAN
send command, no AT interface, and no unsafe-write option.

For the complete workflow from captured traffic to a coach definition, including
operator-marked changes, uncertainty, and validation, see
[RV-C discovery and coach authoring](RVC-DISCOVERY.md). The
[coach reference](COACH.md) defines the resulting YAML format and configuration.

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
Add `-v` / `--verbose` after `server` to echo every raw byte sent (`TX`) and
received (`RX`) on the client socket to the Pi's stderr; the auth token is redacted.
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
operating state. For discovery, provide the physical entity labels and the
safe actions the operator is willing to perform; DGN and instance mappings may
not yet be known.

Discovery is collaborative: the operator manipulates an entity in the RV using
its normal physical controls, while the agent watches for corresponding
messages on the bus. The agent cannot establish a physical mapping from traffic
alone, and the receive-only monitor cannot operate equipment. Use this sequence:

1. Agree on one entity and one safe action, such as switching a known light
   on, and record its initial physical state.
2. Capture an idle baseline. The agent signals when capture is ready before
   the operator acts.
3. The operator performs the agreed action and reports its time, the exact
   control used, and what physically changed. Use synchronized timestamps or
   a shared event marker to correlate the observation with the capture.
4. The agent compares traffic before and after that event, looking for command
   and status messages, their senders, DGN, instance/group, payload changes,
   and timing. Do not mistake periodic background traffic for the action.
5. Repeat safe transitions, including restoring the initial state, to test the
   candidate mapping. If several candidates remain, agree on the next specific
   observation rather than guessing or transmitting test commands.
6. Record the physical observation alongside raw and decoded evidence. Label
   conclusions as observed state, captured panel command, or inference.
   Watching a panel command does not verify that RV-Control can control the
   entity.

Keep an operator present and manipulate only equipment explicitly agreed to be
safe. Do not experiment with unknown channels or hazardous actuators. For the
full mapping and coach-authoring workflow, follow
[RV-C discovery and coach authoring](RVC-DISCOVERY.md).

Then give Kiro this instruction:

> Follow `docs/RVC-REMOTE-DEBUG.md`. Authenticate to the supplied TCP monitor
> and capture a bounded sample of raw and decoded frames for the requested
> RV-C issue. Preserve timestamps, CAN IDs, payload bytes, frame flags, and
> decoded DGN/source fields. Compare the wire data with the project-owned
> specification and canonical helpers in `src/rv_control/rvc_util.py`.
> Treat discovery as a collaboration with the operator in the RV: agree on one
> safe entity/action, capture a baseline, signal readiness, and have the operator
> manipulate its normal control and report the event time and physical result.
> Correlate that event with command/status messages and repeat safe transitions
> to distinguish the entity's traffic from background activity. If the mapping
> is ambiguous, request the next specific operator observation; do not guess.
> Do not transmit CAN frames, change bus configuration, or assume missing decoded
> fields mean missing payload bytes. Filter locally by DGN/source after capture.
> Explain what the evidence establishes, any timing or specification uncertainty,
> and the next targeted check. Redact the token and send `QUIT` when finished.

Record the interface, relevant frame timestamps, DGN/source, raw bytes, decoded
values, expected behavior, and any observed gaps or error frames. If a coach
control must change to reproduce the issue, have the operator make that change
and record its time; the monitor cannot issue control commands.

See [README](../README.md) for the existing local monitor and other RV-C tools.
