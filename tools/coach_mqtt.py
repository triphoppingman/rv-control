#!/usr/bin/env python3
"""Send one semantic Coach command or monitoring query through MQTT."""

from __future__ import annotations

import click

from rv_control.coach_mqtt_client import mqtt_command, mqtt_query
from rv_control.config import load_config


@click.group()
@click.option(
    "--config",
    "config_path",
    default="config.ini",
    type=click.Path(exists=True, dir_okay=False),
    show_default=True,
    help="INI configuration file.",
)
@click.pass_context
def cli(context: click.Context, config_path: str) -> None:
    """Send one Coach MQTT command or query using the configured MQTT target."""
    context.obj = load_config(config_path)


cli.add_command(mqtt_command)
cli.add_command(mqtt_query)


if __name__ == "__main__":
    cli()
