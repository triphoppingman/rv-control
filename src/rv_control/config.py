import logging
from collections.abc import Callable
from configparser import ConfigParser, Error
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def load_config(path: str) -> ConfigParser:
    """Load a configuration file and reject missing required sections."""
    return parse_config(Path(path).read_text(encoding="utf-8"), path)


def parse_config(text: str, path: str) -> ConfigParser:
    """Parse a single configuration snapshot and preserve its path for relative files."""
    config = ConfigParser(inline_comment_prefixes=("#",))
    config.read_string(text)
    required = ("source", "service") if config.has_section("target") else ("mqtt", "source", "service")
    missing = [section for section in required if not config.has_section(section)]
    if missing:
        raise ValueError(f"Missing config sections: {', '.join(missing)}")
    config["_meta"] = {"path": str(Path(path).resolve())}
    return config


class ConfigReloader:
    """Detect timestamp and byte changes without mutating the active configuration."""

    def __init__(self, path: str, validate: Callable[[ConfigParser], None]) -> None:
        """Capture the initial file and a hardware-free candidate validator."""
        self.path = Path(path)
        self.validate = validate
        self._mtime = self.path.stat().st_mtime_ns
        self._contents = self.path.read_bytes()
        self.initial_config = parse_config(self._contents.decode("utf-8"), str(self.path))
        self._observed = self._contents
        self._read_failed = False

    def poll(self) -> ConfigParser | None:
        """Return a stable validated changed snapshot, logging rejected edits once."""
        try:
            before = self.path.stat()
            if before.st_mtime_ns == self._mtime:
                self._read_failed = False
                return None
            contents = self.path.read_bytes()
            after = self.path.stat()
            if (before.st_mtime_ns, before.st_ino, before.st_size) != (
                after.st_mtime_ns, after.st_ino, after.st_size
            ):
                return None
            self._mtime = after.st_mtime_ns
            self._read_failed = False
        except OSError:
            if not self._read_failed:
                LOGGER.exception("Unable to check configuration for reload; keeping active configuration")
            self._read_failed = True
            return None
        if contents == self._observed:
            return None
        self._observed = contents
        if contents == self._contents:
            return None
        try:
            candidate = parse_config(contents.decode("utf-8"), str(self.path))
            self.validate(candidate)
        except (UnicodeError, Error, ValueError, KeyError, OSError) as error:
            LOGGER.error(
                "Configuration reload rejected (%s); keeping active configuration. "
                "Run 'rvcontrol --config <path> check' to diagnose the edited file.",
                type(error).__name__,
            )
            return None
        LOGGER.info("Configuration changed and validated; restarting collectors and targets")
        self._contents = contents
        return candidate