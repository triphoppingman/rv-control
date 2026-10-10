# Remote OBD PID debugging with Kiro

This runbook lets another Kiro instance interrogate the vehicle's ELM327 adapter
through the Raspberry Pi's temporary TCP bridge. It does not require this chat's
history. The goal is to collect evidence explaining a failed PID, not blindly
scan identifiers or change ECU state.

## Operator setup on the Pi

1. Park the vehicle safely and turn the ignition on. Record whether the engine
   is running; do not start it automatically as part of a diagnostic script.
2. Stop the normal collector and any phone OBD apps. The adapter accepts one
   RFCOMM connection at a time.
3. From the repository directory, start the bridge:

   ```sh
   PYTHONPATH=src .venv/bin/python tools/obd_tool.py \
     --config config.ini --section obd_engine \
     server --host 0.0.0.0 --port 35000 --allow-unsafe
   ```

   Use the Pi's LAN IPv4 address instead of `0.0.0.0` to bind only that interface.
   The server prints its listening address and a freshly generated token locally.
   It runs in the foreground until `Ctrl+C`.
4. Give Kiro the actual Pi LAN address, port, and token through the approved local
   debug channel. Also provide vehicle model year, engine, ignition/engine state,
   relevant PID definitions, configured `header`, `protocol`, `fallback_protocol`,
   and `read_timeout`. Do not share the entire configuration: it can contain
   unrelated credentials and device addresses.

This is unencrypted plain TCP for a temporary trusted-LAN session. Do not expose
it to the internet. Do not commit the token or put it in the diagnostic report.
`--allow-unsafe` is needed for diagnostic adapter configuration such as `ATSH`
and `ATH`; it also permits ECU writes, which this runbook does **not** authorize.

## Handoff text for the other Kiro instance

Supply the connection details separately, then give Kiro this instruction:

> Follow `docs/OBD-REMOTE-DEBUG.md` to investigate the supplied target PID names
> and definitions, or the failing entries in a `check-pids` report. Establish
> each target's mode, identifier, ECU header, decode expression, and units before
> testing it. Connect to the supplied TCP endpoint, authenticate with the supplied
> token, and capture the actual responses. Use bounded read-only ECU queries and
> adapter diagnostics only. Do not clear DTCs, write identifiers, change ECU
> sessions, unlock security, reset ECUs, or scan arbitrary PID/header ranges.
> Establish standard OBD controls and compare known manufacturer requests on
> verified headers. Keep commands sequential and record all adapter setting
> changes. Explain what the evidence proves, what remains uncertain, and the
> next targeted check. Do not claim a replacement PID or decode is correct
> without vehicle-specific evidence. End with `QUIT` and redact the auth token.

## TCP client protocol

Use `nc <pi-address> <port>` interactively, or a plain TCP socket client with
bounded connection/read deadlines. This is not Telnet and has no negotiation.
Do not assume one TCP read equals one response: reads can split or combine bytes.

1. Read the greeting `AUTH required\r\n`.
2. Send `AUTH <token>\n` within 10 seconds. Authentication is case-sensitive.
3. Read `OK authenticated; initializing adapter\r\n`, then wait for `>`.
   Initialization may take several command timeouts, including ECU protocol
   discovery; use a sufficiently long, bounded startup deadline (for example
   120 seconds with the default source timeouts). `OK authenticated` alone
   does not mean the adapter is ready. Authentication/session errors or EOF
   must be reported, not treated as a successful connection.
4. Send one ASCII command followed by LF or CR. Read through the next `>` before
   sending another. Preserve the entire reply, including echo, whitespace,
   `SEARCHING...`, and error text. The prompt need not end in a newline.
5. Send `QUIT\n` and close the socket when done. `QUIT` closes the client session,
   not the listening server.

Commands are limited to 256 bytes. Blank lines are ignored, not treated as
repeat-command requests. Only one client is serviced at a time; do not open
parallel connections. The default client idle timeout is 300 seconds per
complete command; adapter requests use the configured `read_timeout`.
Choose a client response deadline longer than that adapter timeout.

A bridge error starts with `ERROR`; it is not an ECU response. A command timeout
can return `ERROR ...` followed by `>`, allowing the next command. Authentication
failure or a fatal session error ends the connection. Reconnect only with bounded
attempts after reporting the failure. Each new authenticated connection resets
and initializes the adapter using the INI settings; ECU-offline status alone
does not block AT diagnostics. Adapter initialization replies are not forwarded.

## Bounded PID investigation

Build an explicit target list from the supplied `pid.*` definitions or diagnostic
report. For each target, record its name, mode, identifier, effective ECU header
(per-PID override or source default), decode expression, units, and known operating
prerequisites. Concatenate the hexadecimal mode and identifier to form the
request; for example, mode `22` and identifier `F478` become `22F478`.
Do not assume all targets belong to the engine ECU or use mode 22.

Start by recording these baseline replies without changing settings:

```text
ATI
ATRV
ATDP
ATDPN
0100
010C
```

- `ATI`, `ATRV`, `ATDP`, and `ATDPN` establish firmware, voltage, and protocol.
- `0100` and `010C` are standard OBD controls. Zero RPM can be a valid response
  with the engine stopped; failure of RPM alone does not establish a bad link.

Then test each target on its verified effective header. Group targets by ECU
header to keep comparisons meaningful, while preserving any decode dependencies
when checking calculated values. Query one or two **known, configured** working
identifiers on the same ECU, preferably using the same service as the failing
target. A working control on one ECU does not establish communication with a
different module. Standard mode 01 support bitmaps can help with standard PIDs,
but do not enumerate manufacturer-specific mode 22 identifiers.

A successful standard control but failed manufacturer-specific requests narrows
the issue; it does not identify a replacement. Record a separate outcome for
each target rather than generalizing one PID's failure to the whole list.

If the responding ECU needs to be identified, enable response headers with
`ATH1`, repeat the relevant controls and target request, and save the raw replies.
The server passes these replies through directly; the project's normal decoder
expects headers off and should not be used blindly to interpret this capture.
Restore `ATH0` afterward. When interpreting CAN replies, distinguish arbitration
IDs and ISO-TP length/control bytes from application payload bytes.

Change the request header with `ATSH<verified-header>` only when the operator or
vehicle-specific evidence identifies the intended module. For example, the
repository's Ford configuration uses `7E0` for engine requests and per-PID `7E1`
for transmission requests; that is not permission to sweep all headers.
Check the `ATSH` reply for `OK` before sending requests, then restore the original
header. Do not change protocols unless a specific protocol mismatch is evidenced
and the operator approves that targeted test.

Use a small explicit command list, not a continuous poll or brute-force scan.
Stop if communications become unstable or replies indicate access/session
requirements. Report those requirements rather than attempting to bypass them.

## Interpret the evidence

The examples below refer to application payloads after removing echo, headers,
and transport framing where present. Match the response service and echoed
identifier to the actual target request: a positive response normally uses the
requested service plus `0x40` (for example, `01 0C` returns `41 0C ...`, and
`22 F478` returns `62 F4 78 ...`). A negative response has the form
`7F <requested-service> <code>`; the mode 22 examples below also illustrate
how to interpret the same negative codes for other applicable services.

| Response | Evidence and next step |
| --- | --- |
| `62 F4 78 ...` | Positive response to `22F478`. Check byte count and the configured decode separately; the identifier was not rejected in this exchange. |
| `7F 22 31` | Negative response: request out of range. Verify target ECU and vehicle-specific identifier definition; this does not reveal a replacement. |
| `7F 22 11` | Service not supported. Compare known mode 22 requests on the same ECU and protocol. |
| `7F 22 22` | Conditions not correct. Record ignition/engine state and documented operating prerequisites. |
| `7F 22 33` | Security access denied. Stop this line of testing; do not attempt an unlock. |
| `7F 22 7F` | Service not supported in active session. Record it; do not automatically change diagnostic sessions. |
| `7F 22 78` | Response pending, not final rejection. Preserve the full reply and inspect adapter timing/transport behavior rather than labeling the DID unsupported. |
| `NO DATA`, `UNABLE TO CONNECT`, or timeout | No usable reply. Check standard controls, protocol, header, power, and ignition state before concluding the PID is absent. |
| `?` | Adapter-level rejection, not proof of an ECU rejection. Check exact command formatting and adapter capabilities. |
| `ERROR ...` | Bridge validation, timeout, authentication, or session failure. Keep it separate from ELM/ECU replies. |

For each positive reply, validate the target's data length, byte order, signedness,
scale, offset, units, and dependencies against vehicle-specific information.
Apply the decode only to the data after the response service and echoed identifier,
not to CAN headers or transport framing.

### Worked example: EGT11

The current EGT11 example uses request `22F478` on the configured engine header.
A positive application response begins `62 F4 78`; `7F 22 31` instead indicates
a negative response to service `22`. Compare this request with known working
mode 22 identifiers on the same verified ECU before proposing a replacement.
This example definition is not universal to all Ford vehicles or firmware.

EGT11's configured expression is `((b0*256+b1)*0.18)-40`, in degrees Fahrenheit,
applied to data **after** `62 F4 78`. Validate the definition and units against
vehicle-specific information; a plausible number alone is not proof.

## Diagnostic report and cleanup

Record:

- Vehicle/engine details and ignition/engine state; avoid unnecessary VINs.
- Adapter firmware, voltage, protocol, and configured timeouts.
- Each command, active header/settings, full response, and observed duration.
- Standard control results and same-ECU manufacturer-request comparisons.
- Separate conclusions about transport, ECU rejection, response parsing, and
  decoding; list unresolved questions rather than guessing.
- A minimal recommended configuration change only if the evidence supports it.

Do not include the authentication token or unrelated configuration secrets.
Restore changed adapter settings, send `QUIT`, and have the operator stop the
server with `Ctrl+C`. Restart the normal collector only after remote clients
have disconnected and the bridge has stopped.

For a subsequent local regression capture, with the bridge stopped:

```sh
PYTHONPATH=src .venv/bin/python tools/obd_tool.py \
  --config config.ini --section obd_engine \
  check-pids --output obd-diagnostics.json
```

See the [README's TCP server documentation](../README.md#temporary-obd-tcp-debug-server)
for all server flags and defaults.
