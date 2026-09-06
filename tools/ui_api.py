#!/usr/bin/env python3
"""Call the local rv-control-ui configuration API without curl."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import requests


def _set_host(context: click.Context, parameter: click.Parameter, host: str | None) -> str | None:
    """Store the selected UI API host in the shared command context."""
    if host is not None:
        context.ensure_object(dict)
        context.obj["base_url"] = f"http://{host.strip('/')}"
    return host


host_option = click.option(
    "--host",
    default="192.168.8.72",
    show_default=True,
    callback=_set_host,
    expose_value=False,
    help="UI device hostname or IP address.",
)


def _request(method: str, url: str, timeout: float, *, print_response: bool = True, **kwargs: Any) -> requests.Response:
    """Send one API request and print its JSON or text response."""
    try:
        response = requests.request(method, url, timeout=timeout, **kwargs)
        response.raise_for_status()
    except requests.RequestException as error:
        raise click.ClickException(str(error)) from error
    if print_response:
        try:
            click.echo(json.dumps(response.json(), indent=2, sort_keys=True))
        except ValueError:
            click.echo(response.text)
    return response


@click.group()
@host_option
@click.option("--timeout", default=10.0, show_default=True, type=click.FloatRange(min=0.1), help="HTTP request timeout in seconds.")
@click.pass_context
def main(context: click.Context, timeout: float) -> None:
    """Manage a running rv-control-ui device through its local REST API."""
    context.ensure_object(dict)
    context.obj["timeout"] = timeout


@main.command("get-config")
@host_option
@click.option(
    "-o",
    "--output",
    default=Path("config.json"),
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Local file to receive the running configuration.",
)
@click.pass_obj
def get_config(options: dict[str, Any], output: Path) -> None:
    """Write the running device configuration to a local JSON file."""
    response = _request("GET", f"{options['base_url']}/api/config", options["timeout"], print_response=False)
    try:
        output.write_text(json.dumps(response.json(), indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError) as error:
        raise click.ClickException(f"Unable to write JSON configuration: {error}") from error
    click.echo(f"Configuration written to {output}")


@main.command()
@host_option
@click.pass_obj
def info(options: dict[str, Any]) -> None:
    """Print device and network diagnostic information."""
    _request("GET", f"{options['base_url']}/api/info", options["timeout"])


@main.command("set-config")
@host_option
@click.option(
    "-i",
    "--input",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Local JSON configuration file to send to the device.",
)
@click.pass_obj
def set_config(options: dict[str, Any], config_path: Path) -> None:
    """Replace the configuration with the complete JSON file at CONFIG_PATH."""
    try:
        payload = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise click.ClickException(f"Unable to read JSON configuration: {error}") from error
    _request("POST", f"{options['base_url']}/api/config", options["timeout"], json=payload)


@main.command()
@host_option
@click.pass_obj
def restart(options: dict[str, Any]) -> None:
    """Request a device restart without changing its configuration."""
    _request("POST", f"{options['base_url']}/api/restart", options["timeout"])


if __name__ == "__main__":
    main()