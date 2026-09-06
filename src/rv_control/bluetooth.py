"""Coordinate contended Bluetooth adapter operations across source threads."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator


class BluetoothAdapterCoordinator:
    """Serialize scan and connection setup work for one Bluetooth adapter."""

    def __init__(self, adapter: str) -> None:
        """Create independent locks for scans and connection establishment."""
        self.adapter = adapter
        self._scan_lock = threading.Lock()
        self._connect_lock = threading.Lock()

    @asynccontextmanager
    async def scan_slot(self) -> AsyncIterator[None]:
        """Reserve the adapter while a source performs device discovery."""
        await asyncio.to_thread(self._scan_lock.acquire)
        try:
            yield
        finally:
            self._scan_lock.release()

    @asynccontextmanager
    async def connect_slot(self) -> AsyncIterator[None]:
        """Reserve the adapter while a source establishes a device connection."""
        await asyncio.to_thread(self._connect_lock.acquire)
        try:
            yield
        finally:
            self._connect_lock.release()


class BluetoothAdapterRegistry:
    """Provide process-wide coordinators, one for each named adapter."""

    _coordinators: dict[str, BluetoothAdapterCoordinator] = {}
    _lock = threading.Lock()

    @classmethod
    def for_adapter(cls, adapter: str) -> BluetoothAdapterCoordinator:
        """Return the shared coordinator for an adapter name."""
        normalized_adapter = adapter.strip() or "hci0"
        with cls._lock:
            coordinator = cls._coordinators.get(normalized_adapter)
            if coordinator is None:
                coordinator = BluetoothAdapterCoordinator(normalized_adapter)
                cls._coordinators[normalized_adapter] = coordinator
            return coordinator