from __future__ import annotations

import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def stub_bluez_link_release(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Keep source tests off the system D-Bus while recording stale-link releases."""
    calls: list[tuple[str, str]] = []

    async def release(adapter: str, address: str, timeout: float = 10) -> bool:
        """Record a BlueZ link release request without touching hardware."""
        calls.append((adapter, address))
        return False

    import importlib

    from rv_control.sources import hughes

    ble_manager_module = importlib.import_module("rv_control.sources.renogybt.BLEManager")
    monkeypatch.setattr(hughes, "release_stale_connection", release)
    monkeypatch.setattr(ble_manager_module, "release_stale_connection", release)
    return calls


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """Create and return a temporary configuration with every required section."""
    path = tmp_path / "config.ini"
    path.write_text(
        """[mqtt]
host = localhost
port = 1883
base_topic = rv
write_enabled = true

[source]
enabled-sources = renogy

[rv_c]
interface = can0
specfile = src/rv_control/data/rvc-spec.yml

[renogy]
type = renogy
device-type = RNG_CTRL
adapter = hci0
mac_addr = 10:CA:BF:AA:96:EC
alias = BT-TH-BFAA96EC
device_id = 255
max_retry = 3
persistent_connection =
enable_polling = false
poll_interval = 60
temperature_unit = F
fields =

[hughes]
type = hughes
adapter = hci0
connect_settle_delay = 0
address =
name =
persistent_connection =

[wled]
type = wled
base_url = http://wled.local
timeout = 1
poll_interval = 1
topic = wled
write_enabled = false

[coach]
specfile = src/rv_control/coaches/thor-magnitude-bh35-2020.yml
rvc = rv_c_bus
wled = wled_patio

[service]
daemon_mode = true
reconnect_delay = 1
max_retry = 0
max_reconnect_delay = 4
"""
    )
    return path