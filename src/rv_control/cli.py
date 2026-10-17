from __future__ import annotations

import logging
import json
import sqlite3
import threading
from typing import Any

import click

from .coach import Coach
from .coach_commands import CoachCommandService, api_enabled
from .coach_mqtt_client import mqtt_command, mqtt_query
from .coach_state import CoachStatePublisher, wrap_publisher
from .config import load_config
from .sources.base import Source
from .sources.hughes import HughesSource
from .sources.obd import ObdSource
from .sources.renogy import RenogySource
from .sources.rvc import RvcSource
from .sources.wled import WledSource
from .targets import log as _log, mqtt as _mqtt, store as _store  # noqa: F401  (register targets)
from .targets.base import MultiTarget, Target
from .targets.mqtt import MqttTarget


@click.group()
@click.option("--config", "config_path", default="config.ini", type=click.Path(exists=True, dir_okay=False))
@click.option("--verbose", is_flag=True)
@click.pass_context
def cli(context: click.Context, config_path: str, verbose: bool) -> None:
    """Collect RV telemetry and publish it to MQTT."""
    config = load_config(config_path)
    configured_level = config["service"].get("log_level", "INFO").upper()
    log_level = logging.getLevelName(configured_level)
    if not isinstance(log_level, int):
        raise click.BadParameter(
            f"Invalid [service] log_level {configured_level!r}; use DEBUG, INFO, WARNING, ERROR, or CRITICAL.",
            param_hint="--config",
        )
    logging.basicConfig(
        level=logging.DEBUG if verbose else log_level,
        format="%(asctime)s %(levelname)s %(name)s [%(filename)s:%(lineno)d %(funcName)s]: %(message)s",
    )
    context.obj = config


@cli.command()
@click.pass_obj
def check(config: Any) -> None:
    """Validate configuration without opening hardware connections."""
    click.echo(f"Configuration OK: {config['_meta']['path']}")
    for section in config["source"].get("enabled-sources", "").split(","):
        section = section.strip()
        if section:
            click.echo(f"{section}: {config[section].get('type', 'missing type')}")


@cli.command("comms-check")
@click.pass_obj
def comms_check(config: Any) -> None:
    """Check local source connectivity and configured devices."""
    results = Source.check_communications(config)
    failed = False
    for name, result in sorted(results.items()):
        status = "OK" if result["ok"] else "FAIL"
        click.echo(f"{name}: {status} - {result['message']}")
        for register in result.get("registers", []):
            access = register.get("access", "read")
            words = f" words={register['words']}" if register.get("words") is not None else ""
            click.echo(f"  {access} register={register['register']}{words} attribute={register['attribute']}")
        for attribute in result.get("attributes", []):
            unit = f" unit={attribute['unit']}" if attribute.get("unit") else ""
            click.echo(f"  {attribute.get('access', 'read')} attribute={attribute['attribute']}{unit}")
        failed = failed or not result["ok"]
    if failed:
        raise click.exceptions.Exit(1)


@cli.command("interrogate")
@click.pass_obj
def interrogate(config: Any) -> None:
    """Read and print current attributes for every enabled source."""
    results = Source.interrogate_enabled(config)
    if not results:
        raise click.ClickException("No sources are enabled")
    click.echo(json.dumps(results, indent=2, sort_keys=True, default=str))
    if any(not result["ok"] for result in results.values()):
        raise click.exceptions.Exit(1)


@cli.command("coach-list")
@click.option("--coach-spec", default=None, help="Coach specification file path or model stem.")
@click.pass_obj
def coach_list(config: Any, coach_spec: str | None) -> None:
    """List all coach entities and command groups."""
    try:
        coach = Coach.load(config, coach_spec)
        result = {
            "coach": coach.info,
            "entities": coach.list_entities(),
            "command_groups": coach.list_command_groups(),
        }
        click.echo(json.dumps(result, indent=2, sort_keys=True, default=str))
    except (FileNotFoundError, ValueError, KeyError) as error:
        raise click.ClickException(str(error)) from error


@cli.command("coach-exec")
@click.argument("target", type=str)
@click.argument("action", type=str)
@click.option("--coach-spec", default=None, help="Coach specification file path or model stem.")
@click.option("--param", "-p", "params", multiple=True, type=(str, str), help="Extra key-value parameter pairs.")
@click.option("--confirm", is_flag=True, help="Confirm actions on entities marked confirm (generator, awning).")
@click.pass_obj
def coach_exec(
    config: Any,
    target: str,
    action: str,
    coach_spec: str | None,
    params: tuple[tuple[str, str], ...],
    confirm: bool,
) -> None:
    """Execute an action on a coach entity or command group."""
    kw: dict[str, Any] = {"confirm": True} if confirm else {}
    for key, val in params:
        try:
            kw[key] = int(val, 0)
        except ValueError:
            try:
                kw[key] = float(val)
            except ValueError:
                kw[key] = val

    try:
        coach = Coach.load(config, coach_spec)
        results = coach.execute(target, action, **kw)
        click.echo(json.dumps(results, indent=2, sort_keys=True, default=str))
    except (KeyError, ValueError, OSError, RuntimeError, PermissionError) as error:
        raise click.ClickException(str(error)) from error


@cli.command()
@click.pass_obj
def run(config: Any) -> None:
    """Run collectors until interrupted."""
    stop_event = threading.Event()
    sources = []
    source_map = {}
    publisher = Target.build(config, lambda topic, payload: _handle_command(config, source_map, topic, payload))
    command_service = None
    try:
        try:
            publisher = wrap_publisher(config, publisher)
            if api_enabled(config):
                if not isinstance(publisher, CoachStatePublisher):
                    raise ValueError("Coach MQTT API requires a configured coach tracker")
                raw_publisher = publisher.publisher
                targets = raw_publisher.targets if isinstance(raw_publisher, MultiTarget) else [raw_publisher]
                mqtt_targets = [target for target in targets if isinstance(target, MqttTarget)]
                if len(mqtt_targets) != 1:
                    raise ValueError("Coach MQTT API requires exactly one MQTT target")
                mqtt = mqtt_targets[0]
                command_service = CoachCommandService(
                    config, publisher.tracker.coach, publisher.tracker, source_map,
                    mqtt.publish_result, mqtt.connected.is_set,
                )
                mqtt.coach_service = command_service
        except (ValueError, KeyError, OSError, sqlite3.Error) as error:
            raise click.ClickException(f"Unable to load coach entity state: {error}") from error
        publisher.connect()
        sources, started_sources = Source.start_enabled(config, publisher, stop_event)
        source_map.update(started_sources)
        if not sources:
            raise click.ClickException("No sources are enabled")
        if isinstance(publisher, CoachStatePublisher):
            publisher.request_initial_status(source_map)
        Source.run_until_stopped(config, publisher, stop_event, sources, source_map)
    except OSError as error:
        raise click.ClickException(f"Unable to connect to target: {error}") from error
    except KeyboardInterrupt:
        click.echo("Stopping")
    finally:
        try:
            if command_service is not None:
                command_service.close()
        finally:
            stop_event.set()
            for source in sources:
                source.join(timeout=3)
            publisher.close()


def _handle_command(config: Any, source_map: dict[str, Any], topic: str, payload: dict[str, Any]) -> None:
    """Route an MQTT set-topic payload to its configured source."""
    section = config[Target.section_for_type(config, "mqtt") or "mqtt"]
    base_topic = section.get("base_topic", "rv").strip("/")
    prefix = base_topic + "/" if base_topic else ""
    relative = topic.removeprefix(prefix)
    source_name = relative.removesuffix("/set")
    source = source_map.get(source_name)
    if source and section.getboolean("write_enabled", fallback=False):
        source.handle_command(payload)


cli.add_command(mqtt_command)
cli.add_command(mqtt_query)