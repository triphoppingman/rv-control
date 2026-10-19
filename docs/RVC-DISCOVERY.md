# RV-C discovery and coach authoring

This runbook turns observed bus traffic into a reviewable coach definition.
It is intended for both a human operator and an AI agent assisting that operator.
Read [Coach architecture and configuration](COACH.md) for the YAML contract.
The output is an evidence-backed mapping, not an automatically generated guess.

## Safety and evidence rules

1. Begin receive-only. Keep MQTT and source actuator writes disabled.
2. Change one known, safe physical control at a time, with an operator present.
   Do not experiment with slides, leveling, ignition, generators, heaters, or
   powered covers merely because an instance appears on the bus.
3. Distinguish **observed state**, **captured panel command**, **inferred command**,
   and **command verified from RV-Control**. These are different evidence levels.
4. An address identifies a sender; an instance identifies a channel within a
   message family. Neither alone establishes a physical device's function.
5. Preserve raw CAN ID, payload, timestamp, flags, DGN, source, and decoded
   fields. A decoder omission is not evidence that a byte was absent.
6. Never mark an entity verified because YAML loads, a mock test passes, a
   transition ranking is high, or a CAN send succeeds.

Do not transmit unknown instance sweeps, replay a whole capture, impersonate a
panel address, or grant an AI agent unsupervised actuator access. Standalone
send tools bypass the coach's reserved list and application write gates.

## What the existing Thor work established

The maintained record is the
[Thor Magnitude BH35 2020 YAML](../src/rv_control/coaches/thor-magnitude-bh35-2020.yml),
its comments/notes and tests, plus the
[MQTT probe](../tools/mqtt_rvc_probe.py) and
[canonical RV-C helpers](../src/rv_control/rvc_util.py). This account summarizes
that record; it does not imply a complete raw capture archive is checked in.
The same source addresses and instances must not be assumed on another coach.

### Identify senders, then correlate loads

The Thor observations identified `9F` as the Spyder monitor/status controller,
`9E` as the Spyder touchscreen, and `FC` as the climate panel/gateway. Status
from the first controller must not be confused with commands from the others.
The useful dimmer pair was `DC_DIMMER_COMMAND_2` (`0x1FEDB`) and
`DC_DIMMER_STATUS_3` (`0x1FEDA`); captured dimmer commands used group `FF`.

The MQTT probe first ranked changing fields during repeated on/off cycles.
That alone was insufficient: continuous background activity and interleaved
instances on the same DGN topic could look like operator actions. The `mark`
workflow instead compares persistent before/after values around operator-marked
presses, split by instance/group. Raw captures remain the authority for exact
sender, bytes, sequence, and timing.

### Classify behavior instead of treating every channel as a switch

| Observation in the maintained Thor mapping | Modeling consequence |
| --- | --- |
| Named lights and fans correlate with dimmer instances. | Reusable `dimmer_switch` and `dimmable_light` templates. Only living ceiling, kitchen fan, and mid-bath fan currently carry `verified: true`. |
| Opposing channels operate the fan lid and generator relays. | `timed_pair`: opposite direction off first, then a timed activation. |
| Awning controls are momentary inputs to a separate controller. | `pulse_pair`: one-second pulse with an explicit later off frame; no invented position telemetry. |
| Climate actions span thermostat instances 0, 1, and 2. | Capture full ordered actions; the front furnace follows the front A/C's heat/cool behavior rather than an assumed one-to-one zone model. |
| Tank status includes relative level and resolution. | Compute percent from both fields rather than assuming the raw value is percent. |
| Touchscreen repeatedly locks/unlocks instances 32, 43, 44, 53-56. | Reserve those command instances; their suspected slide role is not proven. |
| Tank input 18 appears unused. | Record it in `reserved`, without claiming reserved entries suppress telemetry. |

The file deliberately preserves uncertainty. Water pump and tank-heat notes say
the Bluetooth app produced status changes without an observed command frame.
Front A/C `off` is annotated as inferred, not captured. Cargo light and fan-lid
notes preserve timing quirks; the lid's template is not a claim that its sequence
is a byte-for-byte replay of the panel. Captured, inferred, and verified behavior
must remain distinguishable during future edits.

Status mappings were then added for dimmers, tanks, thermostats, ambient sensors,
and generator feedback. An initial status request was needed because off loads
could remain unknown until changed. Finally, transport bindings separated
vehicle semantics from local source sections, and the MQTT layer added explicit
schemas, controller grants, and correlated results.

## Phase 1: prepare a bounded investigation

Record the vehicle model/year/options, controller names/firmware if known,
interface, specification version or repository revision, operating conditions,
target controls, and allowed physical actions. Agree on a capture duration and
stop condition. Keep credentials and the local INI private.

Check SocketCAN without changing its configuration:

```sh
ip -details link show can0
rvcontrol --config config.ini check
```

`check` validates the basic application configuration, not wire mappings.
Interface setup belongs to the [installation guide](../README.md#mcp2515-and-socketcan).
Do not change bitrate or termination during an agent investigation without
operator approval.

For local observation:

```sh
PYTHONPATH=src .venv/bin/python tools/rvc_monitor.py --interface can0
```

Capture an idle baseline for a fixed interval, then stop with `Ctrl+C`.
For remote raw-plus-decoded JSON records, follow the
[receive-only TCP monitor runbook](RVC-REMOTE-DEBUG.md). It defines authentication,
framing, bounded collection, and cleanup. The normal collector may keep running;
the monitor opens its own receive socket.

If an operator authorizes **read-request transmission**, inventory responding
devices using:

```sh
PYTHONPATH=src .venv/bin/python tools/rvc_send.py --interface can0 get-info --timeout 5
```

This sends an address-claim request; it is not passive discovery and does not
map every load. A controller may serve many instances, and silence is not proof
of absence. Addresses may be dynamic. Do not infer an unused transmit source
address solely from a short passive capture.

## Phase 2: correlate physical controls with traffic

For each load, record baseline state, the exact physical button/app used, press
times, resulting physical state, and restoration to the original state. Repeat
safe transitions to separate causation from periodic background traffic.

The daemon must already publish decoded RV-C traffic for MQTT-based discovery:

```sh
.venv/bin/python tools/mqtt_rvc_probe.py --config config.ini --topic 'rv/rvc/#' listen
.venv/bin/python tools/mqtt_rvc_probe.py --config config.ini --topic 'rv/rvc/#' \
  map --toggles 3 --window 20 --csv device-map.csv
```

Use the actual `<base_topic>/<RV-C topic>/#`. The probe reads broker settings
from `[mqtt]`; its automatic RV-C prefix lookup uses the legacy `[rv_c]`
section, not arbitrary named source sections. An explicit `--topic` avoids that
ambiguity. The probe subscribes only; the operator changes the device.

`map` ranks fields by transition counts and hints. For a busy multiplexed bus,
prefer marked comparisons:

```sh
.venv/bin/python tools/mqtt_rvc_probe.py --config config.ini --topic 'rv/rvc/#' \
  mark --settle 1.5 --csv device-map.csv
```

Let the baseline settle. Toggle the physical control and press Enter to mark
each press, allowing the new state to settle before the next transition.
Despite the prompt's wording, completion is detected by two marks less than
one second apart; the final quick Enter is removed. Leave enough time after
the final physical action for its settle sample before ending.

Select a candidate only after checking its instance/group against raw records.
The CSV stores labels, topics, fields, values, transition counts and a match
identity for later review; it is not a coach file or a complete capture.
The `map` heuristic does not fully demultiplex its transition series and its
identity attachment is limited; both workflows' displayed source metadata
can reflect the last message on a shared topic. Do not treat that source
column or a ranking score as proof of the controlling sender.

Maintain an evidence table for each proposed mapping:

| Item | Record |
| --- | --- |
| Identity | Entity key, physical label/area, DGN, instance, group, sender(s). |
| Before/action/after | Operator timestamps, control used, physical and decoded states. |
| Wire evidence | Raw IDs and bytes for command and response, sequence order and spacing. |
| State conversion | Exact decoder fields, units, scaling, invalid sentinels. |
| Confidence | Observed state / captured command / inferred / RV-Control verified. |
| Constraints | Confirmation, forbidden channels, timing, unresolved questions. |

Keep raw evidence outside shareable configuration where it contains private
information. Summarize sanitized facts in YAML `notes` and review material.

## Phase 3: determine the protocol behavior

Use [rvc-spec.yml](../src/rv_control/data/rvc-spec.yml) and
[rvc_util.py](../src/rv_control/rvc_util.py), not a second decoder written for
the investigation. The 29-bit ID contains priority in bits 28-26, the 17-bit DGN
in bits 25-8, and source in bits 7-0. Payload `instance` and `group` are separate
from this source address.

For each action:

- Separate request/command messages from status/acknowledging broadcasts.
- Compare repeated captures in both directions, including idle traffic.
- Preserve all frames, unchanged/sentinel bytes, local spacing, and protocol
  duration fields. One button press may command multiple channels or zones.
- Identify true brightness scaling rather than copying raw half-percent bytes
  into a percent-valued template.
- For motors/relay pairs, establish interlocks, opposite-channel-off behavior,
  pulse/timed semantics, and physical stop behavior before offering actions.
- Determine which status fields actually confirm the result. Relay-on is not
  generator-running, motor-active is not cover-position, and send-success is
  not hardware-success.

For unknown DGNs, retain raw evidence and document uncertainty. A protocol
specification change belongs in the canonical specification/helpers with tests;
a physical label-to-instance mapping belongs in the coach file. Never alter a
generic decoder just to make an unverified installation guess look correct.

## Phase 4: author the coach file

1. Create a model/year-named `.yml` under
   [coaches/](../src/rv_control/coaches/) or use a private external path.
2. Add coach identity. Copy template structure, not another coach's instance
   assignments, timing assumptions, or verification flags.
3. Start with read-only entities and confirmed status mappings. Add actions only
   when supported by sufficient command evidence and operator approval.
4. Use logical transports/targets and keep installation details in `[coach]`
   INI bindings. Do not add a new source type for a coach file.
5. Add explicit parameter schemas for actions intended for semantic MQTT.
6. Reserve channels that must not be commanded. Do not expose unknown channels
   in groups. Add confirmation requirements where appropriate.
7. Preserve inference notes and leave `verified` false/absent until tested from
   RV-Control on that installation. That flag is descriptive, not enforcement.
8. Add groups/scenes only after each member's behavior is independently known.
9. Prepare a least-privilege controller policy separately if MQTT control is
   wanted; discovery does not require enabling that endpoint.

See [the YAML reference](COACH.md#coach-yaml-reference) for merging, placeholders,
frames, state conversion, and reserved-instance limitations.

## Phase 5: validate without hardware first

```sh
rvcontrol --config config.ini coach-list --coach-spec /path/to/new-coach.yml
.venv/bin/pytest -q tests/test_coach.py tests/test_coach_source_dispatch.py tests/test_coach_state.py
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
```

The existing tests protect existing contracts; add tests for the new file.
Load it with `Coach.load`, compile `EntityStateTracker`, and use
`Coach.prepare_action(entity, action, params)` to inspect bytes/waits without
opening hardware. Use fake `Source` objects to verify dispatch, not live CAN.
Include exact captured payload assertions, malformed/out-of-range parameters,
reserved-instance rejection, sentinel handling, and representative status
messages. Test any MQTT schemas/grants in the corresponding API suites.

`coach-list` does not prepare every action or compile state mappings. It is one
check, not certification. Never call `coach-exec` as a dry run; there is no such
dry-run flag.

## Phase 6: supervised physical verification and handoff

Only after review, enable the minimum necessary source write gate for a
specifically approved action. For MQTT, the endpoint, MQTT write flag, policy,
and source gate are all additionally required. Keep an operator watching the
load and a known physical stop control available. Test one nonhazardous action
at a time, compare expected bytes and observed telemetry, and restore the coach.
Do not automate hazardous experiments.

Mark verified only the scope actually tested; add notes where the boolean alone
cannot express which actions remain untested. Restore write flags/policy to the
intended operating posture, stop temporary listeners, and redact capture tokens.

Acceptance checklist:

- Identity and every instance/group assignment have installation evidence.
- Unknown/inferred behavior is labeled and not accidentally granted for control.
- Actions preserve ordering, timing, parameter limits, and safety exclusions.
- State uses canonical fields/units and handles unavailable values.
- Hardware-free tests pass; any live results are reported separately.
- INI bindings and optional controller grants refer to actual sections/entities.
- Documentation explains deviations from reused templates and remaining gaps.

### AI-agent task contract

Give an assisting agent this bounded instruction, alongside sanitized evidence:

> Follow `docs/RVC-DISCOVERY.md` and `docs/COACH.md`. Work receive-only unless
> the operator explicitly authorizes a particular request or action. Correlate
> operator-marked physical changes with raw and decoded traffic; distinguish
> senders, DGNs, instances, and groups. Produce an evidence table, a proposed
> coach YAML, hardware-free regression tests, and a list of unresolved mappings.
> Reuse canonical RV-C helpers. Do not infer actuator safety, invent missing
> commands, enable writes, copy verification flags, or mark tests as live
> verification. Stop at the agreed capture bound or when evidence is insufficient
> and request the next specific operator observation.
