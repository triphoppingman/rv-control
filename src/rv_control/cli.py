from __future__ import annotations

import logging
import json
import threading
from typing import Any

import click

from .config import load_config
from .mqtt import MqttPublisher
from .hughes import HughesSource
from .renogy import RenogySource
from .rvc import RvcSource
from .source import Source


@click.group()
@click.option("--config", "config_path", default="config.ini", type=click.Path(exists=True, dir_okay=False))
@click.option("--verbose", is_flag=True)
@click.pass_context
def cli(context: click.Context, config_path: str, verbose: bool) -> None:
    """Collect RV telemetry and publish it to MQTT."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    context.obj = load_config(config_path)


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


@cli.command()
@click.pass_obj
def run(config: Any) -> None:
    """Run collectors until interrupted."""
    stop_event = threading.Event()
    sources = []
    source_map = {}
    publisher = MqttPublisher(config, lambda topic, payload: _handle_command(config, source_map, topic, payload))
    try:
        publisher.connect()
        sources, source_map = Source.start_enabled(config, publisher, stop_event)
        if not sources:
            raise click.ClickException("No sources are enabled")
        Source.run_until_stopped(config, publisher, stop_event, sources, source_map)
    except OSError as error:
        raise click.ClickException(f"Unable to connect to MQTT: {error}") from error
    except KeyboardInterrupt:
        click.echo("Stopping")
    finally:
        stop_event.set()
        for source in sources:
            source.join(timeout=3)
        publisher.close()


def _handle_command(config: Any, source_map: dict[str, Any], topic: str, payload: dict[str, Any]) -> None:
    """Route an MQTT set-topic payload to its configured source."""
    prefix = config["mqtt"].get("base_topic", "rv").strip("/") + "/"
    relative = topic.removeprefix(prefix)
    source_name = relative.removesuffix("/set")
    source = source_map.get(source_name)
    if source and config["mqtt"].getboolean("write_enabled", fallback=False):
        source.handle_command(payload)