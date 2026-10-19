# Coach MQTT command API

Status: implemented, opt-in. Nothing in this document enables writes by itself.

For the underlying entity model, YAML format, transport bindings, and local
execution boundaries, see [Coach architecture and configuration](COACH.md).
For evidence-led mapping of a new vehicle, see
[RV-C discovery and coach authoring](RVC-DISCOVERY.md).

## Setup and command-line testing

Copy the `[coach_commands]` section from `config-example.ini` and customize
`config-example-controllers.yaml` as a local controller policy. Paths in that
section are relative to the INI file, not the current working directory.
`journal_file` defaults to `data/coach-commands.sqlite3`; its directory must be
writable by the daemon. `journal_max_entries` defaults to 1024 (range 1–10000).
Only one daemon may own a journal. Preserve it across restarts; do not delete it
to retry an uncertain command.

For monitoring only, set `queries_enabled = true` and leave `enabled = false`
and all write flags false. For physical control, additionally set `enabled = true`,
the configured MQTT target's `write_enabled = true`, and each affected source's
`write_enabled = true`. The policy must grant both a group and every expanded
member action. Restart `rvcontrol --config config.ini run` after configuration
or policy changes. Keep broker command topics restricted to trusted backends;
these options do not configure broker authentication or ACLs.

### Startup error: policy references unknown target

Every target under a controller's `actions` must exist in the loaded coach's
entities or command groups, even when only queries are enabled. Invalid grants
stop startup before broker connection or source startup; they are never ignored.
The error names the missing target.

The factory Thor definition no longer includes the installation-specific
`patio_light` entity or `evening_scene` group. If an older local controller policy
still grants either target, load the installation YAML defining it with
`[coach] overlay` (paths are relative to the INI), or remove those action grants
from every controller in `[coach_commands] controllers_file` if no longer needed.
For the shareable patio definitions, use
`overlay = src/rv_control/coaches/wled-overlay-example.yml` when the INI is in the
repository root. A transport binding such as `wled = wled_patio` does not load
entity definitions by itself.

Run `rvcontrol --config config.ini coach-list` to inspect the effective entity
and group names without starting collectors, then restart the service after
correcting the overlay or policy. `coach-list` checks the coach definition, not
the controller policy.

The installed CLI and standalone tool use the MQTT target selected by the
configuration (host, port, credentials and base topic). They do not invoke the
coach executor locally or bypass the daemon's policy:

```sh
# Read-only tests: no physical write flags are necessary.
rvcontrol --config config.ini mqtt-query health
rvcontrol --config config.ini mqtt-query capabilities
rvcontrol --config config.ini mqtt-query snapshot
rvcontrol --config config.ini mqtt-query snapshot --entity kitchen_light
rvcontrol --config config.ini mqtt-query refresh_states --entity kitchen_light --refresh-timeout 5
rvcontrol --config config.ini mqtt-query republish_states

# Equivalent standalone tool, without using the installed console entry point.
.venv/bin/python tools/coach_mqtt.py --config config.ini mqtt-query health

# Physical actions: explicitly enable all write gates first.
rvcontrol --config config.ini mqtt-command kitchen_light on
rvcontrol --config config.ini mqtt-command kitchen_light off
rvcontrol --config config.ini mqtt-command living_ceiling_light brightness --param level 30
rvcontrol --config config.ini mqtt-command all_fans off
rvcontrol --config config.ini mqtt-command kitchen_fan_lid open --confirm
rvcontrol --config config.ini mqtt-command generator start --confirm

# Retrieve a prior result without executing its action again.
rvcontrol --config config.ini mqtt-query request_status --lookup-request-id PREVIOUS_REQUEST_ID
```

The default policy identity is `rv-cli`; use `--client-id` for another explicitly
granted controller. `--request-id`, `--ttl` and `--timeout` control correlation,
request lifetime and the local result wait. Requests and results are always
non-retained QoS 1 regardless of telemetry publishing settings. The client waits
for the result subscription acknowledgment before publishing once; it never
automatically retries a physical action. A timeout means outcome unknown, not
proof of failure. Inspect telemetry or use `request_status` before deciding
whether to send a new command. UI backends use the same MQTT protocol.

The Thor mapping includes explicit `action_schemas`; custom coach actions must
declare one too (an empty mapping for actions without parameters). Template
inheritance merges these alongside actions. For example:

```yaml
action_schemas:
  on: {}
  brightness:
    level: {type: number, min: 0, max: 100}
```

Supported parameter types are `number`, `integer`, `boolean`, and `string`,
with optional numeric `min`/`max` and scalar `enum`. All declared parameters are
required; policy restrictions may narrow, never widen, the action declaration.
Unknown parameters and transport-setting overrides are rejected.

Kitchen light instance 16 is mapped but not marked verified in the coach file.
Test deliberately on the installation; hardware-free tests do not establish
that a particular coach accepts its mapped commands.

## Purpose and boundary

Expose the existing coach entity and command-group actions through a stable,
transport-independent MQTT API. A dashboard should request "kitchen fan on",
not construct CAN frames, invoke the CLI, or load RV-Control's coach files.
RV-Control remains responsible for transport selection, parameter validation,
reserved-instance checks, frame ordering, timing, and hardware access.

Existing entity telemetry on `<base_topic>/coach/entity/<entity>` is unchanged.
Existing source-level `<base_topic>/<source-section>/set` commands and
`coach-exec` remain compatible. The new endpoint is an additional, explicitly
enabled command receiver, not a replacement.

## Implementation boundary

- `MqttTarget` restores raw and independently enabled semantic subscriptions
  on every successful connection. Semantic routing preserves the original
  bytes and retain flag for strict validation.
- `CoachCommandService` authorizes and preflights a whole expanded group using
  the coach's canonical frame builders, without constructing extra sources.
- A bounded worker dispatches through the running sources' reliable
  `execute_command` interface, not legacy handlers that may log and return.
- Existing raw handlers and local `coach-exec` retain their compatibility
  contract; only the semantic API provides the journaled result guarantees.

## Topics and delivery

With `base_topic = rv`:

| Topic | Direction | QoS | Retain |
| --- | --- | --- | --- |
| `rv/coach/command` | Controller to RV-Control | 1 | false |
| `rv/coach/query` | Controller to RV-Control, monitoring only | 1 | false |
| `rv/coach/result/<client_id>/<request_id>` | RV-Control to controller | 1 | false |

Subscribe to results before submitting a request. There is no client-supplied
reply topic. Derive the result topic from strictly validated identifiers.
Identifiers use `[A-Za-z0-9_-]{1,64}`; `/`, `+`, `#`, and empty identifiers
are forbidden. These are application identifiers, not MQTT connection IDs.

Never retain commands or results. Reject messages marked retained. Broker ACLs
must prohibit retained command publication where supported; with some MQTT
subscription settings, a live retained publication is delivered without its
retain flag. Use a clean session with no offline command backlog, enforce
expiry independently, and never replay commands after reconnect.

## Request envelope, version 1

```json
{
  "version": 1,
  "client_id": "rv-dashboard",
  "request_id": "f7d7055e-2cd3-4e1f-9839-b1543f665686",
  "issued_at": "2026-10-17T18:00:00Z",
  "expires_at": "2026-10-17T18:00:10Z",
  "target": "kitchen_fan",
  "action": "on",
  "params": {},
  "confirmed": false
}
```

All fields above are required. Reject unknown envelope keys, unsupported
versions, non-object payloads, duplicate JSON keys, non-finite numbers, and
payloads larger than 8192 bytes. Boolean fields must be JSON booleans; booleans
are not accepted as numbers.

Timestamps are UTC RFC 3339 instants. Require `issued_at < expires_at`, a
maximum lifetime of 15 seconds, and no issue time more than two seconds in
the future. Expired requests must not execute. Recheck expiry immediately
before the first physical write. Hosts need synchronized clocks.

On the command topic, targets and actions must exactly match loaded coach definitions and the
controller's explicit allow-list. Groups are supported only when their full
expanded action plan is authorized. Reject cycles, excessive expansion, missing
members, unavailable sources, and any disallowed member before writing.

For physical actions, `params` contains only declared scalar parameters. No transport names, source
sections, bus objects, CAN IDs, raw frames, URLs, payload overrides, or arbitrary
Python objects may be supplied. Reject unknown parameters. Resolve parameters
through a typed action schema, not through unrestricted template substitution.

## Monitoring and state refresh commands

Use the same versioned envelope and correlated result topic on
`<base_topic>/coach/query`, with the reserved service target `coach`.
Service operations are not coach entity actions and never call `Coach.execute`.
Reject service operations on the physical command topic and physical actions
on the query topic. Require `confirmed: false` for monitoring requests.

| Action | Parameters | Purpose |
| --- | --- | --- |
| `refresh_states` | optional `entities`: unique entity-ID array; optional `timeout_s`: integer 1–10, default 5 | Ask devices for fresh state; omitting entities selects every monitored coach entity |
| `snapshot` | optional `entities`: unique entity-ID array | Return current cached entity states, without polling devices |
| `republish_states` | optional `entities`: unique entity-ID array | Republish cached states on their normal entity topics |
| `capabilities` | none | Discover available entities, typed actions, confirmation requirements, and monitoring support |
| `health` | none | Return service readiness and sanitized source/command availability |
| `request_status` | `request_id`: identifier | Retrieve this controller's latest journaled command/query result without retrying execution |

An entity list contains 1–200 known entity identifiers. An empty list is
invalid, not shorthand for all entities. Reject unknown identifiers and unknown
parameters before querying or publishing anything. Bound response size to
256 KiB; reject larger requested selections with `response_too_large` rather
than silently truncating. A controller may split its entity selection.

### Requery all states

```json
{
  "version": 1,
  "client_id": "rv-dashboard",
  "request_id": "refresh-20261017-180000",
  "issued_at": "2026-10-17T18:00:00Z",
  "expires_at": "2026-10-17T18:00:15Z",
  "target": "coach",
  "action": "refresh_states",
  "params": {"timeout_s": 5},
  "confirmed": false
}
```

Use the existing coach tracker's status-request mapping and each running
source's monitoring interface. The current startup status request path already
asks RV-C sources for the DGNs required by the coach state definitions.
Extract/reuse that behavior for runtime refresh; do not rerun startup,
reconnect sources, invoke one-shot interrogation, or construct arbitrary
client-supplied DGNs. Explicit refresh must not depend on
`request_status_on_start` being true.

Deduplicate requests by source and DGN so querying all lights does not flood
the bus with identical requests. Rate-limit refresh to one per controller
per 10 seconds and one per source per five seconds. Reject excess with
`rate_limited`; do not queue refreshes for later. Serialize source access with
physical actions and return `busy` on conflicts. Do not stop live collectors.
Sources without a safe monitoring interface are explicitly unsupported.

Record a per-entity/per-field observation baseline before dispatch. Count
unchanged post-query telemetry as a new observation even when normal change
suppression would skip publication. Publish resulting entity telemetry through
the normal state pipeline. Wait no longer than `timeout_s`, bounded by the
request expiry. Check expiry before dispatch and do not start a refresh that
cannot finish its observation window within the remaining lifetime.

Return `data.entities` entries with `entity`, `status`, `observed_at`, and
`observed_fields`. Per-entity statuses are `observed`, `partial`, `timed_out`,
`unsupported`, and `source_unavailable`. `observed` requires new observations
for all mapped state fields; partial entities list missing fields. Cached state
alone never satisfies refresh. Shared bus replies can update unselected
entities too; this is expected and must not be hidden from normal telemetry.

No response is not proof that a device is off or disconnected. RV-C does not
correlate replies to request IDs, so report "observed after refresh", not a
guaranteed device acknowledgment.

### Cache operations and discovery

`snapshot` returns `data.entities`, with each entry containing entity ID, cached
state, per-field last-observed timestamps, and explicit missing fields.
`republish_states` returns the selected published/missing entity IDs and sends
only states that exist. Neither operation changes observation timestamps.
Preserve the entity payload's original `updated` value; if the tracker cannot
recover an observation time, return null, not the current time.
Clients must not treat MQTT arrival time of republished state as proof of fresh
device data. Snapshot responses, rather than replaying cached telemetry, are
the recommended dashboard reconnect path when observation age matters.

`capabilities` returns only this controller's allowed targets/actions, parameter
schemas, confirmation requirements, and entity monitoring fields/support.
Include `available` and sanitized unavailability reasons for disabled writes
or unavailable sources. Do not expose transport frames, addresses, paths, or
credentials. Discovery does not grant permission to execute.

`health` returns uptime, readiness, broker connectivity as known by the service,
sanitized source health, and whether action execution or refresh is busy.
Receiving a response proves connectivity at that moment, not future
availability; no response means unknown/unreachable, not necessarily unhealthy.
It does not restart, probe, or reconfigure hardware.

`request_status` accepts only a request ID belonging to the requesting
controller and returns the recorded result in `data.result`, or
`request_not_found`. Its own envelope request ID must differ from the looked-up
ID. Do not reveal other controllers' requests.

### Monitoring authorization and results

Add a separate default-disabled monitoring switch:

```ini
[coach_commands]
enabled = false
queries_enabled = false
controllers_file = /etc/rv-control/coach-command-controllers.yaml
```

Each controller's policy may explicitly grant monitoring operations:

```yaml
controllers:
  rv-dashboard:
    queries:
      refresh_states: {entities: ["*"]}
      snapshot: {entities: ["*"]}
      republish_states: {entities: ["*"]}
      capabilities: {}
      health: {}
      request_status: {}
```

The literal `["*"]` grants all monitored entities; otherwise specify an explicit
entity list. Requests themselves never accept wildcards. Omitting a request's
entity selection expands to all monitored entities and requires permission
for all of them; do not silently narrow the selection. Broker ACLs restrict
the query topic to trusted controller backends just like physical commands.

Monitoring requires `queries_enabled` and a matching policy grant, but not
physical action enablement or write flags. A status-request bus frame is a
read request, consistent with the existing startup request behavior. Only
explicitly implemented safe source read operations qualify for this exception.
Actuation, raw frames, resets, reconnects, and configuration changes do not.
Subscribe to the query topic independently of physical command subscription.

Monitoring uses the same envelope validation, expiry, correlation, duplicate
protection, and bounded result journal as actions. Repeated refresh requests
with the same identity replay their result, not bus queries. Snapshot/health
with a new identity reads current information. Bound concurrent cache queries
and return `busy` instead of accumulating an unlimited queue.

Monitoring results add a typed `data` object and use `outcome: "not_started"`
to indicate no actuation. `completed` with `code: "observed"` means all requested
refresh observations arrived; `completed` with `code: "partial"` means some
were partial, unavailable, unsupported, or timed out. No observations yields
`failed` / `refresh_timeout`; wholly unsupported or unavailable selections use
`failed` / `unsupported` or `source_unavailable`. Cache/discovery queries use
`completed` / `queried`, while republishing uses `completed` / `republished`
with explicit missing states. Physical action `completed` retains its
transport-dispatch meaning; clients must interpret results by action.

The dashboard can expose Refresh all, Refresh entity, connection diagnostics,
and command-result recovery independently of control widgets. None requires
enabling light, fan, lid, or generator actuation.

Examples of initial schemas:

| Action | Parameters |
| --- | --- |
| Light/fan `on`, `off`, `toggle` | none |
| RV-C light `brightness` | `level`: number, 0 through 100 |
| WLED `brightness` | `bri`: integer, 0 through 255 |
| Kitchen lid `open`, `close` | none |
| Generator `start`, `stop` | none |

Schemas are associated with loaded actions, not inferred from widget type or
action name alone. Unknown/custom parameterized actions are unavailable over
MQTT until a schema is declared. Translate semantic parameters to the existing
executor only after schema validation. Do not silently clamp values.

## Opt-in configuration and authorization

Installation configuration, separate from transport mappings:

```ini
[coach_commands]
enabled = false
controllers_file = /etc/rv-control/coach-command-controllers.yaml
```

External policy file:

```yaml
controllers:
  rv-dashboard:
    actions:
      kitchen_fan:
        on: {}
        off: {}
      living_ceiling_light:
        on: {}
        off: {}
        brightness:
          params:
            level: {type: number, min: 0, max: 100}
      kitchen_fan_lid:
        open: {confirm: true}
        close: {confirm: true}
      generator:
        start: {confirm: true}
        stop: {confirm: true}
```

All physical writes require command API enablement, global MQTT `write_enabled`, and
every affected source's `write_enabled`. Unlisted controllers, targets, and
actions are denied. Policy bounds can narrow but never widen action schemas.
Fail startup on an invalid policy or unusable enabled command configuration.
Do not expose installation paths, credentials, raw frames, or source internals
in protocol responses.

`client_id` is a claimed identity, not authentication. The shared request topic
is suitable only when broker ACLs restrict publication to trusted controller
services. Never give browser clients direct broker access. Each controller
backend authenticates its users and authorizes individual actions.

If mutually untrusted controllers need different permissions, use a separately
specified identity-bound per-controller request topic and broker ACLs. Do not
claim per-controller isolation based solely on a JSON `client_id`.

`confirmed: true` records a controller assertion of deliberate user confirmation;
it is not authentication or a safety interlock. Coach confirmation requirements
and policy confirmation requirements combine with OR, and cannot be bypassed
through `params.confirm`. Generator start/stop and lid movement should require
confirmation in the initial dashboard policy.

The existing raw source write API can bypass this semantic allow-list. Broker
ACLs must deny dashboard credentials publication to raw `*/set` topics. This
feature must not imply that enabling the semantic policy secures raw endpoints.

## Admission, concurrency, expiry, and duplicates

1. Parse and validate the envelope, identity, expiry, policy, and parameters.
2. Expand and preflight the complete action plan, including write gates,
   confirmations, reserved instances, and transport availability.
3. Admit at most one physical action plan at a time initially. While busy,
   reject new requests with `busy`; do not create a delayed action queue.
4. Reserve the request identity and publish `accepted` before dispatch. If
   result publication fails, do not start physical execution.
5. Recheck expiry and write gates before the first physical write.
6. Execute the plan outside the MQTT callback thread. Preserve frame ordering
   and waits without blocking telemetry processing or MQTT keepalive.
7. Publish a terminal result and release admission.

The initial implementation uses one non-queued worker for actions and all
queries. Concurrent requests with new identities receive `busy`, including
cache and health queries while physical work or refresh is in progress. Thus a
successful health response reports no other active physical plan or refresh.
Plans are limited to 64 transport operations and a conservative 30-second
transport/wait budget; group expansion is limited to 64 entity actions and 16
levels. WLED request timeouts must be positive and at most 10 seconds.

Use `(client_id, request_id)` as the duplicate key and a canonical request
digest for collision detection. An identical duplicate gets the last result
without executing again. The same key with different content is rejected with
`request_id_conflict`. Deduplication lookup must precede `busy` rejection.

Use a bounded local durable journal for admitted requests. Persist an execution
intent before the first write; if persistence fails, reject without writing.
Retain identities through at least expiry plus 60 seconds. With at most
15-second lifetimes, older requests are expired even after journal cleanup.
If the journal is full, fail closed rather than evicting unexpired identities.

After restart, never replay an interrupted plan. An intent without a recorded
terminal result becomes `interrupted`, with physical outcome unknown.
Exactly-once physical execution cannot be guaranteed across crashes or
transport failures. Never automatically retry a physical action or compensate
by issuing an opposite action. User-initiated retries use a new request ID
after inspecting telemetry, especially for toggle and movement actions.

Once execution starts, expiry does not interrupt required motor-pair sequencing
or cleanup. Recheck write gates between steps; if they close, stop further
ordinary writes, report partial failure, and follow only explicitly documented
transport safety cleanup. Do not invent generic rollback behavior.

## Results

```json
{
  "version": 1,
  "client_id": "rv-dashboard",
  "request_id": "f7d7055e-2cd3-4e1f-9839-b1543f665686",
  "target": "kitchen_fan",
  "action": "on",
  "status": "completed",
  "code": "executed",
  "message": "Configured action sequence executed; physical state is not confirmed.",
  "timestamp": "2026-10-17T18:00:01Z",
  "outcome": "dispatched",
  "steps_completed": 1
}
```

Every correlatable result includes version, identity, target, action, status,
code, message, timestamp, outcome, and completed-step count.
Malformed requests without valid correlation identifiers are logged, never
reflected into an arbitrary result topic.

| Status | Meaning |
| --- | --- |
| `accepted` | Admitted and preflighted; not proof of a physical write |
| `completed` | All transport operations reported successful dispatch |
| `rejected` | Not admitted or blocked before any physical write |
| `failed` | Execution failed; inspect outcome and completed steps |

Outcomes are `not_started`, `dispatched`, `partial`, or `unknown`.
Transport calls without a reliable outcome must not produce `completed`.
An uncertain current step yields `unknown`, even when earlier steps completed.
Completed-step counts describe action-plan transport operations, not MQTT
packets or physical device movements. Group failures include sanitized
per-entity progress; preflight failure never executes earlier group members.

Stable codes include `invalid_request`, `unsupported_version`, `expired`,
`clock_skew`, `writes_disabled`, `forbidden`, `unknown_target`, `unknown_action`,
`invalid_params`, `confirmation_required`, `request_id_conflict`, `busy`,
`source_unavailable`, `execution_failed`, `interrupted`, `executed`,
`rate_limited`, `response_too_large`, `request_not_found`, `journal_full`, and
`service_error`.

Correlated results are best-effort MQTT delivery backed by the journal.
Controllers may resend the identical unexpired request to recover the latest
result; expired duplicates may return the recorded result but never execute.
No configurable reply topic is supported. Result lookup uses only the
validated monitoring `request_status` operation described above.

## Dashboard integration

The web backend maintains a configurable action registry; widgets reference
action IDs instead of supplying arbitrary MQTT topics or payloads. This supports
buttons, explicit On/Off pairs, numeric sliders, selects, and confirmed actions
without per-widget publishing code. A widget without configured controls
remains read-only.

The web backend constructs the envelope, subscribes to its result prefix,
enforces user permissions, tracks request IDs, and maps results to UI feedback.
The browser never receives MQTT credentials. Controls are disabled when the
broker is unavailable, during an in-flight action, or when required telemetry
is absent/stale according to action policy.

Display transport execution separately from physical confirmation. For example,
`completed` on generator start is not "Generator running"; that requires
fresh `state.status == "running"` telemetry. A confirmation timeout is
"state not confirmed", never an automatic retry. Freshness alone does not
prove causality; label a matching reading "desired state observed" unless the
device supplies an actual correlated acknowledgment.

Capability discovery is an authorized query, not an automatically retained
publication. Clients still use explicit configured controls, and RV-Control
is authoritative for rejecting unsupported requests. No runtime dependency
on coach files is introduced into clients.

## Lifecycle, observability, and compatibility

- Load coach definitions and policy once at startup; validate the full policy.
- Register semantic command routing separately from raw source routing.
- Subscribe only when the API and global MQTT writes are enabled, and repeat
  subscriptions on every successful connection.
- Reject new work during shutdown. Finish an admitted bounded sequence when
  possible; journal an interrupted outcome when execution cannot finish.
- Log request identity, claimed controller, target/action, admission decision,
  outcome, and duration. Do not log credentials or unfiltered payloads.
- Bound individual transport timeouts and complete plan duration. Reject
  unbounded or excessive waits during preflight; do not silently truncate a plan.
- Do not change existing telemetry schemas or claim that current CLI result
  dictionaries provide the new delivery guarantees without source changes.

## Acceptance criteria

Hardware-free tests with fake sources and a fake clock must demonstrate:

1. Disabled defaults perform no writes and add no semantic subscription.
2. Opted-in subscriptions recover after MQTT reconnect.
3. Fan On/Off, typed light brightness, kitchen lid open/close, generator
   start/stop, and an allow-listed group dispatch through the coach executor.
4. Malformed/oversized JSON, unknown fields, bad IDs, unknown targets/actions,
   invalid parameters, forbidden members, and reserved instances cause no writes.
5. All three write gates and required confirmation are enforced, including
   rechecks after admission. Payload parameters cannot override trusted settings.
6. Retained, expired, future-dated, offline-backlogged, and interrupted requests
   never replay physical operations.
7. Duplicate delivery, concurrent duplicate delivery, ID collisions, restart
   recovery, journal exhaustion, and journal failure behave as specified.
8. Whole-group preflight prevents partial writes for validation failures;
   transport failures report partial/unknown outcomes without automatic retries.
9. A slow action does not block MQTT keepalive or telemetry; concurrent new
   requests receive `busy`; shutdown does not replay unfinished work.
10. Source write-disabled and logged/returned hardware failures cannot become
    success-shaped results. Result publish failure before dispatch causes no writes.
11. Missing physical confirmation never becomes success or a retry; fresh
    generator telemetry is independent of relay activity.
12. Existing raw command topics, telemetry, and CLI behavior remain compatible.
13. Refresh all reuses status-request mappings, coalesces shared source/DGN
    requests, respects rate limits, and works with physical writes disabled.
14. Selected refresh rejects unauthorized/unknown entities before bus access,
    observes unchanged fresh messages, and distinguishes partial, unsupported,
    unavailable, and timed-out entities without inventing device state.
15. Snapshot and republish preserve observation timestamps and never poll
    devices or falsely claim freshness. Cache/discovery responses remain bounded.
16. Query ACLs and grants are enforced independently of actuation; physical
    actions cannot enter through query routing. Capability and health queries
    leak no secrets, and result lookup is confined to the requesting controller.

Broker integration tests should verify ACLs, retain rejection, reconnect
behavior, QoS duplicate handling, and command/result correlation. Live hardware
tests are a separate explicit operator step; unit tests must never send real
CAN, BLE, HTTP device writes, or MQTT commands to an installation.
