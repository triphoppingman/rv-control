# Runtime variables: configuration and implementation

## 1. Intent and scope

Runtime variables connect telemetry to consumers without making one source know
about another. For example, GPSD publishes a position report, configuration binds
its coordinates, and Open-Meteo receives coordinates for its next request.
Neither source needs to know the other's protocol or variable names.

The shared runtime supports three kinds of values:

| Kind | Defined by | Available when | Expires? |
| --- | --- | --- | --- |
| Telemetry binding | `topic`, `fields`, optional `require` and `max_age` | A complete, acceptable report arrives | Yes |
| Constant | `constants` JSON object | Configuration is loaded | No |
| Derived value | Arithmetic or optional telemetry-window functions in a `[derived]` section | Every dependency is valid and calculation succeeds | With its earliest expiring input |

Optional publication makes these values observable through the existing targets,
including MQTT. Publication is not required for one source to consume another's
telemetry. This is an in-process facility, not an MQTT subscription or persistent
variable store. Opt-in moving-window calculations and read-only event rules use
the same runtime without changing existing bindings or consumers.
Event rules are state transitions, not another variable kind.

**Current consumer scope:** only Open-Meteo's `latitude` and `longitude` settings
opt into runtime references. All existing sources can supply telemetry to
bindings. This does not enable interpolation in arbitrary INI settings, coach
YAML/action parameters, BLE addresses, or CAN interfaces. Write permissions and
hardware command validation are unchanged.

## 2. Configuration

Examples use synthetic coordinates. Merge sections into your configuration
rather than defining the same INI section twice.

### 2.1 Bind a telemetry report

```ini
[source]
enabled-sources = position, weather

[position]
type = gpsd
host = localhost
port = 2947
topic = gpsd

[variables.location]
topic = gpsd/TPV
fields = latitude:lat, longitude:lon
require = mode >= 2
max_age = 300

[weather]
type = open_meteo
latitude = ${location.latitude}
longitude = ${location.longitude}
current = temperature_2m
poll_interval = 900
timeout = 10
retry_delay = 30
max_retry_delay = 900
topic = weather
```

This is the variable/source portion of a configuration; retain your target and
service sections. **Enabling weather sends the resolved coordinates to
Open-Meteo over HTTPS.**

| Binding option | Meaning |
| --- | --- |
| `topic` | Exact internal telemetry topic, before MQTT's `base_topic`; no wildcards or references |
| `fields` | Comma-separated `variable:payload.path` pairs; dotted paths traverse JSON objects, not arrays |
| `require` | Optional single comparison of a payload field with a JSON scalar |
| `max_age` | Positive finite seconds since local receipt; default `300` |

`require` supports `==`, `!=`, `<`, `<=`, `>`, `>=`. Examples are `mode >= 2`
and `status == "ready"`. It is not a Python expression. Numeric comparisons
accept integers/floats together, but booleans are not numbers for this purpose;
other comparisons require matching scalar types.

For a report such as:

```json
{"class": "TPV", "mode": 3, "lat": 12.5, "lon": -45.5}
```

both coordinates become available together. Missing fields, null/non-scalar
values, non-finite floats or a failed predicate invalidate the **whole telemetry
group**, removing the previous good report. There is no stale-value fallback.
Constants in the same namespace remain available independently.

### 2.2 Bind literals as constants

```ini
[variables.fixed]
constants = {"latitude": 12.5, "longitude": -45.5, "label": "RV", "enabled": true}
```

Use `${fixed.latitude}` and `${fixed.longitude}` in the weather settings to avoid
depending on GPS. Alternatively, put literal coordinates directly in those
settings; that existing configuration still works.

`constants` must be a JSON object with identifier keys and non-null finite JSON
scalar values: strings, numbers and booleans. Objects and arrays as values are
not supported. Constants retain their JSON types and are not recursively
expanded.

A constants-only section needs no `topic`. Do not add `fields`, `require` or
`max_age` without a telemetry topic. To mix constants and telemetry, add
`constants` to an ordinary binding section with its `topic` and `fields`.

### 2.3 Define derived values

```ini
[variables.temperature]
topic = weather
fields = celsius:current.temperature_2m
max_age = 1800

[derived.temperature]
fahrenheit = ${temperature.celsius} * 9 / 5 + 32

[derived.display]
rounded_step = (${temperature.fahrenheit} + 1) / 2
```

The last expression is simply arithmetic; it does not round the result.
Expressions may refer to constants, telemetry or other derived values, including
values in another namespace.

Allowed syntax:

- Numeric literals and explicit runtime references.
- Parentheses and unary `+` / `-`.
- Binary `+`, `-`, `*`, `/`, `%`.
- Optional moving-window functions over directly bound numeric telemetry:
  `avg`, `min`, `max`, and `rate`.

Window functions use the form `avg(${location.voltage}, 60)`: they include
numeric reports received during the preceding number of seconds, including the
current report. `avg`, `min` and `max` are sample-based, not time-weighted.
`rate` is `(newest - oldest) / elapsed_seconds` and needs two samples with
different receipt times. Windows must be greater than zero and no longer than
86,400 seconds. Runtime retains at most 1,024 samples per telemetry variable;
if that cap is reached, oldest samples are evicted. Window functions cannot
read constants or other derived values. Existing arithmetic expressions do
not use history and keep their prior behavior.

Other calls (including `round`), attribute access, indexing, powers,
comparisons, string concatenation, arbitrary Python and shell execution are
not allowed. Expressions are limited to 4096 characters and 64 AST nodes;
arithmetic integers are also bounded. Booleans and numeric strings are not
arithmetic operands. Division by zero, insufficient window samples,
oversized/non-finite results and invalid operand types make the value
unavailable and log a warning, rather than preserving an old result.

Derived values do not have their own configurable TTL. They inherit the earliest
dependency expiry. An expression using only constants/numeric literals has no
expiry. Unknown references and derived cycles fail at configuration load even
if the result has no consumer.

### 2.4 Names, references and types

| Form | Meaning |
| --- | --- |
| `[variables.location]` with field `latitude` | `${location.latitude}` |
| `[variables]` with field/constant `value` | `$value` or `${value}` |
| `[derived.temperature]` with key `fahrenheit` | `${temperature.fahrenheit}` |
| `[derived]` with key `result` | `$result` or `${result}` |
| `$$` in a setting template | Literal dollar sign |

Names use ASCII letters, digits and underscores, and cannot begin with a digit.
Namespaces can contain dot-separated identifier components. Use braced
references for namespaced values. All three value kinds share one name space:
a constant or expression cannot overwrite a telemetry field.

A whole-setting reference preserves the scalar type. An embedded reference
produces a string. A literal setting remains a string until the consumer performs
domain conversion. There is no environment-variable lookup, command
substitution, `${name:-default}` syntax or recursive template expansion.

### 2.5 Publish variables through targets

```ini
[runtime]
publish_variables = true
variables_topic = variables
variables_republish_seconds = 60
```

| Option | Default | Meaning |
| --- | --- | --- |
| `publish_variables` | `false` | Enable namespace publications through all configured targets |
| `variables_topic` | `variables` | Literal internal output prefix; must not overlap binding topics or enabled source roots |
| `variables_republish_seconds` | `60` | Finite, non-negative repeat interval; `0` disables periodic repeats only |

With MQTT `base_topic = rv`, namespace `temperature` appears at
`rv/variables/temperature`. A nested namespace `cabin.temperature` appears at
`rv/variables/cabin/temperature`; unnamespaced values appear at `rv/variables`.
Log and store targets receive the same internal publications without an
MQTT-specific implementation.

Example payload:

```json
{
  "namespace": "temperature",
  "values": {"celsius": 20, "fahrenheit": 68},
  "status": {
    "celsius": {
      "available": true,
      "reason": null,
      "updated_at": "2026-01-01T12:00:00+00:00",
      "expires_at": "2026-01-01T12:30:00+00:00"
    },
    "fahrenheit": {
      "available": true,
      "reason": null,
      "updated_at": "2026-01-01T12:00:00+00:00",
      "expires_at": "2026-01-01T12:30:00+00:00"
    }
  },
  "published_at": "2026-01-01T12:00:00+00:00"
}
```

Status is per variable, not just per namespace:

| `reason` | Meaning |
| --- | --- |
| `null` | Available |
| `missing` | No accepted telemetry report yet |
| `invalid` | Latest matching report was incomplete or rejected |
| `stale` | Receipt age reached `max_age` |
| `dependency_unavailable` | A derived input is unavailable |
| `calculation_error` | Inputs were available but arithmetic failed |

Unavailable values are JSON `null`. Constants have null `updated_at` and
`expires_at`. For derived values, `updated_at` is the oldest input receipt time,
not calculation time; `expires_at` is the earliest input expiry.

Startup emits constants and unavailable states. Updates, including timestamp or
availability changes, trigger new snapshots. A maintenance thread checks every
half-second for expiry and periodic repeats, even with no incoming traffic.
Republishing does not refresh measurement times or TTLs.

**Retention and privacy:** MQTT uses the target's existing QoS/retain settings.
Subscribers must check `expires_at` themselves: a retained message cannot
announce a later expiry while the daemon is stopped. `published_at` is not
measurement time. Runtime values are never restored from retained MQTT or disk.
Publication exposes all configured variables, potentially including location;
enable it only for trusted targets/subscribers.

### 2.6 Define read-only transition events

Event rules publish a message when a condition becomes true, and another when
it clears. They are opt-in, do not write to CAN/BLE/WLED or invoke coach
commands, and do not alter source telemetry or existing runtime-variable
resolution.

```ini
[runtime]
events_topic = events

[events.battery_low]
when = ${battery.voltage} < 12.0
clear_when = ${battery.voltage} > 12.5
for_seconds = 30
```

`[events.NAME]` defines one event. `when` is required and must compare one
runtime variable with a non-null JSON scalar using `==`, `!=`, `<`, `<=`, `>`
or `>=`. `clear_when` is optional; without it, the event clears when `when`
becomes false. A custom `clear_when` provides hysteresis. `for_seconds`
optionally delays activation while `when` remains true (default `0`, maximum
86,400 seconds); clearing is immediate. Invalid, missing, or stale inputs pause
the rule without clearing an already active event; if activation was pending,
its delay starts over after inputs become available again. Event state starts
inactive after daemon restart and is not restored from MQTT.

With `events_topic = events`, the example publishes to
`<base_topic>/events/battery_low`. Nested event names map dots to topic
separators. Each transition includes `event`, `state`, `previous_state`,
`variable`, its current `value` and `updated_at`, and `occurred_at`. Event rules
whose telemetry dependencies have no enabled producer fail startup validation.
The event topic must be literal, must not overlap enabled source telemetry
topics, and must be separate from the runtime-variable publication prefix.
Events are sent through the existing target configuration, so MQTT retain/QoS
settings apply as they do to other target publications.

## 3. Binding and resolution over time

There are three distinct events, not a one-time startup substitution:

1. **Compile:** load configuration, register constants/bindings/expressions and
   validate dependency structure. No producer data is available yet.
2. **Bind:** each producer publication updates matching telemetry bindings before
   forwarding the original telemetry to targets.
3. **Resolve:** immediately before a consumer operation, take one atomic snapshot,
   evaluate expressions and check required values for availability/freshness.

Example sequence:

```text
daemon starts -> location is missing -> weather waits, no HTTP request
valid TPV     -> latitude/longitude bound together
weather poll  -> fresh snapshot -> domain checks -> HTTP -> publish forecast
new TPV       -> new coordinates used on next weather poll
invalid TPV   -> previous coordinates discarded -> next poll waits
no more TPV   -> age reaches max_age -> next poll refuses stale inputs
```

Freshness uses a monotonic clock and expires at `age >= max_age`. UTC timestamps
are metadata for external subscribers, not the internal freshness clock.
Atomicity is per received group and per resolution snapshot; unrelated producer
reports need not represent the same measurement instant.

`check` validates configuration without starting producers or opening hardware.
Daemon startup runs the same validation. Source dependencies are inferred from
the longest matching declared telemetry root; absent/ambiguous producers and
source cycles fail for references consumed by enabled sources. Derived chains
are followed when constructing that graph. A binding used only for publication
need not have an enabled producer; it can remain explicitly unavailable.

`comms-check` and `interrogate` remain one-shot operations. They do not start a
GPS producer or wait for its first fix. Literal/constant settings can work in
one-shot mode; telemetry-backed settings fail promptly as unavailable.

With opt-in `--reload-config`, a validated INI timestamp-and-content change
restarts the whole daemon session. New bindings/constants/expressions replace
the previous configuration, and telemetry observations start empty; consumers
wait for fresh reports again. A timestamp-only touch does not reset values.
Invalid edits leave the active context running. See
[configuration reload](../README.md#optional-configuration-reload) for scope and
failure behavior.

## 4. Implementation map

| File | Responsibility |
| --- | --- |
| [runtime.py](../src/rv_control/runtime.py) | Templates, bindings, constants, restricted arithmetic, snapshots, freshness, dependency validation and optional publications |
| [sources/base.py](../src/rv_control/sources/base.py) | Common source API, configured-setting resolution, literal validation, polling/wait/retry lifecycle |
| [sources/open_meteo.py](../src/rv_control/sources/open_meteo.py) | Coordinate/API validation and one HTTP forecast operation |
| [cli.py](../src/rv_control/cli.py) | Construct/validate runtime, wrap targets, start sources and clean up |
| [targets/](../src/rv_control/targets/) | Existing MQTT/store/log delivery; not variable binding |

### 4.1 Startup and shutdown wiring

The CLI's daemon path creates one `RuntimeContext`, validates it, builds the
target pipeline (including the coach-state wrapper when configured), then wraps
that pipeline in `RuntimePublisher`.

```text
source threads
    |
    v
RuntimePublisher.publish(topic, payload)
    |-- RuntimeContext.update(topic, payload)
    |-- wrapped publisher.publish(original telemetry)
    |-- optional variable snapshots -> wrapped publisher directly
    `-- optional event transitions -> wrapped publisher directly
                                      |
                                      v
                              coach-state/target pipeline
                                      |
                                      v
                                MQTT / store / log
```

The shared publisher exposes `runtime_context` to sources. Supervised source
restarts receive that same publisher/context rather than resetting other
sources' variable state. Target connection precedes initial variable publication
and maintenance-thread startup. The maintenance thread handles variable
expiry/republication and event activation delays even without incoming telemetry.
Shutdown stops sources, closes the runtime publisher (stopping/joining its
maintenance thread), then closes wrapped targets.

Generated snapshots bypass `RuntimePublisher.publish` and therefore never become
new binding inputs. The wrapper lock serializes source/publication operations;
the context lock protects bound state and snapshot evaluation.

### 4.2 RuntimeContext internals

- `parse_template` returns literal fragments and `Reference` objects; it never
  invokes a shell.
- `Binding` compiles one exact topic, field map, predicate and age limit.
  `extract` returns an entire acceptable group or invalidates it.
- `update` records receipt time and the full group under a lock. Invalid reports
  remove the stored group; warnings are suppressed until the binding recovers.
- `Expression` parses a restricted AST and evaluates validated nodes explicitly,
  without Python `eval`.
- Window functions use bounded per-variable sample history; ordinary arithmetic
  continues to evaluate current values only.
- `[events.NAME]` rules debounce optional activations and publish only
  active/cleared transitions. Unavailable inputs do not create false clears,
  and event evaluation never dispatches device commands.
- `dependencies` resolves transitive telemetry inputs and rejects derived cycles.
- `_snapshot` memoizes calculations within one evaluation, combining constants,
  telemetry and derived status. Derived calculation warnings are suppressed until
  a successful calculation permits a later failure to be reported again.
- `resolve` checks all required settings against one locked snapshot and raises
  `VariablesUnavailable` rather than returning partial/default values.
- `publication_snapshot` groups values and per-variable metadata by namespace.
- `validate_sources` checks declared runtime options, producer roots and source
  dependency cycles before daemon execution.

`RuntimePublisher` compares serialized namespace state with its last publication.
Measurement timestamps are part of that state, so a fresh report with unchanged
numbers still represents an update. `published_at` is added only when emitting,
so it does not itself cause a continuous change loop.

### 4.3 Common source behavior

`Source.runtime_options` is an explicit tuple of supported setting names, empty
by default. Its base helpers are:

- `configured_settings()`: read only those opted-in settings.
- `validate_literal_settings(validator)`: parse references centrally, reject empty
  required values, and validate literal settings immediately.
- `resolve_settings(settings)`: use the shared publisher context to resolve the
  whole mapping atomically. Without a shared publisher, create a fresh context;
  constants work, but telemetry is initially absent.
- `published_topic_root(...)`: declare the source's root for dependency checks.

`PollingSource` adds:

```text
interrogate()
    -> configured_settings()
    -> resolve_settings()
    -> collect(resolved_settings)
```

Its daemon `run()` calls `interrogate()` repeatedly:

| Outcome | Next step |
| --- | --- |
| Required variable unavailable | Log transition into waiting; retry after `min(5, poll_interval)` seconds |
| Declared `polling_errors` | Log failure; retry with exponential delay capped at `max_retry_delay` |
| Successful collection | Publish returned payload; reset retry delay; wait `poll_interval` |

Waits use `stop_event.wait`, so shutdown interrupts scheduling delays. Protocol
operations must still have their own bounded timeouts. `comms_check()` invokes
one interrogation and returns success/failure without entering the daemon loop.

### 4.4 Open-Meteo: the consumer implementation

`OpenMeteoSource` extends `PollingSource` and declares
`runtime_options = ("latitude", "longitude")`. Its constructor compiles static
HTTP options and asks the base to validate literal coordinates. Its
`validate_config` constructs an instance without performing HTTP.

The concrete `collect(settings)` method:

1. Reads the supplied resolved latitude/longitude.
2. Rejects booleans, nonnumeric/non-finite values and out-of-range coordinates.
3. Builds the current/daily forecast query.
4. Makes one timeout-bounded request in a managed HTTP session.
5. Validates response shape and requested fields.
6. Returns data plus `requested_location` and `fetched_at`.

It does not import runtime parsing or unavailable-variable handling. It cannot
tell whether coordinates originated in literals, constants, GPS telemetry or
arithmetic. Domain validation still occurs on every operation: a valid runtime
scalar is not necessarily a valid latitude.

### 4.5 Existing producers

All producers use the publisher provided through `Source`. Their normal
`publisher.publish(topic, payload)` calls are sufficient:

| Source | Existing publication shape |
| --- | --- |
| RV-C | Fixed `rvc/<decoded-name>` topics |
| GPSD | `<configured-topic>/<report-class>`, such as `gpsd/TPV` |
| Renogy | Configured topic with decoded device data |
| Hughes | Configured topic with measurements |
| WLED | Configured topic with controller state |
| OBD | Existing OBD telemetry topics/payloads |
| Open-Meteo | Configured topic with validated forecast data |

None needs a variable name, binding map, expression parser or dependency on a
consumer. Configure bindings against the actual published payload shape.
Except for Open-Meteo's opted-in coordinates, these adapters retain their
existing static settings and connection/reconnect loops. They were not migrated
wholesale to `PollingSource`.

## 5. Extending the system without duplicating binding logic

For a new **producer**, register a `Source` subclass normally, keep publishing
protocol telemetry, and ensure `published_topic_root` describes its actual topic
root. No runtime-specific extraction belongs in the adapter.

For a new **polling consumer**:

1. Derive from `PollingSource` and register through the existing source registry.
2. Declare only safe, intentionally dynamic `runtime_options`.
3. Compile static options once in the constructor. Call
   `validate_literal_settings` with a domain validator when appropriate.
4. Implement hardware-free `validate_config` for startup checking.
5. Implement typed, documented `collect(settings)` with domain validation and
   bounded protocol I/O. Return the normal telemetry payload.
6. Set the output `topic` and accurately describe its root.
7. Declare expected protocol failures in `polling_errors`; do not catch all
   exceptions or substitute success-shaped defaults.
8. Test literal, constant, missing, invalid, stale and changing inputs.

For an adapter with an existing persistent connection lifecycle, retain that
lifecycle unless a deliberate migration is required. Opt in safe settings and
use the common `Source` helpers at the operation boundary; do not cache resolved
dynamic values at construction, copy parsing logic, or silently make hardware
connection identities dynamic.

Keep binding/expression changes in the runtime, shared scheduling in the base,
and protocol/domain behavior in the concrete source.

## 6. Troubleshooting and regression evidence

| Symptom | Check |
| --- | --- |
| Unknown variable at startup | Namespace/field spelling; `$name` versus `${namespace.name}` |
| No enabled producer | Enabled-source list and declared topic roots; follow derived dependencies |
| Missing value indefinitely | Exact internal topic, field path and whether the producer actually publishes |
| Invalid after a good report | Latest predicate or missing/bad field; old reports are deliberately discarded |
| Stale value | Producer cadence versus `max_age`; dashboard publication time does not refresh inputs |
| Calculation error | Zero divisor, boolean/string input, arithmetic limits |
| One-shot weather fails but daemon works | One-shot commands do not run GPS to seed telemetry |
| No variable MQTT messages | Publication defaults off; check targets, prefix and daemon connection |
| Retained message looks valid after shutdown | Subscriber must compare UTC `expires_at`, not just `available` |

Relevant hardware-free regressions:

- [test_runtime.py](../tests/test_runtime.py): atomic binding, types, constants,
  arithmetic restrictions, dependency cycles, freshness, publication and expiry.
- [test_open_meteo.py](../tests/test_open_meteo.py): literal/dynamic coordinates,
  domain checks, HTTP behavior, one-shot semantics and polling backoff.
- [test_cli.py](../tests/test_cli.py): daemon/target wiring.

Run focused checks from the repository root:

```sh
.venv/bin/pytest -q tests/test_runtime.py tests/test_open_meteo.py tests/test_cli.py
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
.venv/bin/rvcontrol --config config-example.ini check
```

See also the [configuration example](../config-example.ini) and
[README overview](../README.md#runtime-variables-and-open-meteo-weather).
