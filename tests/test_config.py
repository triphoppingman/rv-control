from __future__ import annotations

from pathlib import Path

from rv_control.config import load_config


def test_config_loads_required_sections(tmp_path: Path) -> None:
    """Verify configuration loading accepts all required sections."""
    path = tmp_path / "config.ini"
    path.write_text("""[mqtt]\nhost=localhost\n[rv_c]\nenabled=false\n[renogy]\nenabled=false\n[hughes]\nenabled=false\n[service]\nreconnect_delay=10\n""")
    config = load_config(str(path))
    assert config["mqtt"]["host"] == "localhost"
    assert config["_meta"]["path"] == str(path.resolve())