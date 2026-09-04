#!/usr/bin/env python3
"""Read and control a WLED controller through its configured JSON API."""

from __future__ import annotations

import json
from typing import Any

import click
import requests

from rv_control.config import load_config
from rv_control.wled import WledSource


@click.group()
@click.option("--config", "config_path", default="config.ini", type=click.Path(exists=True, dir_okay=False), show_default=True, help="INI configuration file.")
@click.option("--section", default="wled_patio", show_default=True, help="WLED configuration section.")
@click.pass_context
def cli(context: click.Context, config_path: str, section: str) -> None:
    """Read or control a configured WLED controller."""
    config = load_config(config_path)
    if not config.has_section(section):
        raise click.UsageError(f"Configuration section not found: [{section}]")
    if config[section].get("type", "").strip().lower() != "wled":
        raise click.UsageError(f"Configuration section [{section}] must have type = wled")
    context.ensure_object(dict)
    context.obj["source"] = WledSource(config, None, None, section)


def emit(value: dict[str, Any]) -> None:
    """Print a WLED JSON response in a readable stable format."""
    click.echo(json.dumps(value, indent=2, sort_keys=True, default=str))


def source_from(context: click.Context) -> WledSource:
    """Return the configured WLED source stored by the command group."""
    return context.obj["source"]


def run_request(operation: Any) -> None:
    """Run one WLED operation and convert expected failures into CLI errors."""
    try:
        emit(operation())
    except (OSError, PermissionError, requests.RequestException, ValueError) as error:
        raise click.ClickException(str(error)) from error


@cli.command()
@click.pass_context
def status(context: click.Context) -> None:
    """Display the current WLED light state."""
    run_request(source_from(context).status)


@cli.command()
@click.pass_context
def info(context: click.Context) -> None:
    """Display WLED controller identity and firmware information."""
    run_request(source_from(context).info)


@cli.command()
@click.pass_context
def on(context: click.Context) -> None:
    """Turn the WLED output on."""
    run_request(lambda: source_from(context).set_state({"on": True}))


@cli.command()
@click.pass_context
def off(context: click.Context) -> None:
    """Turn the WLED output off."""
    run_request(lambda: source_from(context).set_state({"on": False}))


@cli.command()
@click.argument("level", type=click.IntRange(0, 255))
@click.pass_context
def brightness(context: click.Context, level: int) -> None:
    """Set WLED brightness from 0 through 255."""
    run_request(lambda: source_from(context).set_state({"bri": level}))


@cli.command("state")
@click.argument("payload", type=str)
@click.pass_context
def state(context: click.Context, payload: str) -> None:
    """Send a raw JSON state object for advanced WLED controls."""
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise click.BadParameter(f"invalid JSON object: {error.msg}") from error
    if not isinstance(value, dict) or not value:
        raise click.BadParameter("state payload must be a non-empty JSON object")
    run_request(lambda: source_from(context).set_state(value))


if __name__ == "__main__":
    cli()
