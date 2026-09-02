from __future__ import annotations

import pytest
from pathlib import Path


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

[rv_c]
enabled = false
interface = can0
specfile = src/rv_control/data/rvc-spec.yml

[renogy]
enabled = true
adapter = hci0
mac_addr = 10:CA:BF:AA:96:EC
alias = BT-TH-BFAA96EC
type = RNG_CTRL
device_id = 255
max_retry = 3
persistent_connection =
enable_polling = false
poll_interval = 60
temperature_unit = F
fields =

[hughes]
enabled = false
adapter = hci0
address =
name =
persistent_connection =

[service]
daemon_mode = true
reconnect_delay = 1
max_retry = 0
max_reconnect_delay = 4
"""
    )
    return path