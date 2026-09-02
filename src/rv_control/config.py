from configparser import ConfigParser
from pathlib import Path


def load_config(path: str) -> ConfigParser:
    """Load a configuration file and reject missing required sections."""
    config = ConfigParser(inline_comment_prefixes=("#",))
    config.read(path)
    required = ("mqtt", "source", "service")
    missing = [section for section in required if not config.has_section(section)]
    if missing:
        raise ValueError(f"Missing config sections: {', '.join(missing)}")
    config["_meta"] = {"path": str(Path(path).resolve())}
    return config