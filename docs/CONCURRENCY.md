# Concurrency and threading model

The daemon uses threads to isolate device polling from target delivery. It does
not create a separate consumer thread for each target: one shared dispatcher
delivers publications to all enabled targets in turn.

## Threads and responsibilities

- **Source threads:** Each enabled source is a `Source` thread. Its polling,
  reconnect, and device-specific work runs in that thread; sources share a
  stop event and publish through the common publisher interface. The supervisor
  restarts a source thread that exits unexpectedly.
- **Target dispatcher:** `QueuedTarget` owns one worker and a bounded FIFO
  queue. Its default capacity is 1024 messages and `[service]
  publish_queue_depth` configures it.
- **Runtime-variable maintenance:** When runtime-variable publication is
  enabled, a maintenance thread periodically checks for expired values. Normal
  runtime updates happen inline as telemetry is published.
- **Target-specific workers:** MQTT uses Paho's network loop; Store has a
  periodic file-flush thread. These support destination I/O and do not poll
  sources.
- **Inbound MQTT source commands:** When enabled, valid source commands are
  handled by one command worker rather than the Paho callback thread. Its
  separate queue has capacity 64; unlike outbound telemetry, commands are
  logged and dropped if that queue is full.

## Publication path

For each source publication, the shared wrappers synchronously update runtime
variables and coach/entity state before forwarding the raw message. The message
then enters the bounded target queue with a snapshot of its payload and source
provenance (source type and configured section). Generated coach-entity
messages are queued after their triggering raw telemetry.

The dispatcher consumes messages in FIFO order and calls each enabled target
sequentially. A slow target therefore delays later messages and other targets;
target delivery is not parallel. Destination-specific workers may continue
their own I/O after the dispatcher call returns.

When the queue is full, a publishing source blocks until capacity is available.
Queue saturation does not drop outbound telemetry, but blocking can also hold
the shared runtime-publish lock, delaying other sources and runtime-variable
maintenance. The queue is in memory, not durable storage. Target failures are
logged; they are not retried by the dispatcher.

## Shutdown

On shutdown, the daemon sets the shared stop event and waits for source threads
before closing the publisher. It allows a 30-second grace period, logs sources
that exceed it, and continues waiting for them so they cannot publish into
closed targets. The dispatcher then drains accepted messages before closing
targets. This is graceful in-process draining, not a guarantee against process
termination or downstream delivery failure.

`comms-check` and `interrogate` are one-shot operations; they do not start the
long-lived daemon polling lifecycle.
