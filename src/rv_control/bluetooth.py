"""Coordinate contended Bluetooth adapter operations across source threads."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

LOGGER = logging.getLogger(__name__)

BLUEZ_SERVICE = "org.bluez"
BLUEZ_DEVICE_INTERFACE = "org.bluez.Device1"
DBUS_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"


def bluez_device_path(adapter: str, address: str) -> str:
    """Return the BlueZ D-Bus object path for a device address on an adapter."""
    normalized_adapter = adapter.strip() or "hci0"
    return f"/org/bluez/{normalized_adapter}/dev_{address.strip().upper().replace(':', '_')}"


async def release_stale_connection(adapter: str, address: str, timeout: float = 10) -> bool:
    """Ask BlueZ to drop a link to a device that this process no longer owns.

    A BLE peripheral that BlueZ still considers connected stops advertising, so it
    disappears from discovery and refuses other centrals such as a phone app until
    it is power cycled. Calling this before each connection attempt and after each
    teardown releases such orphaned links. Returns True when a disconnect was issued.
    Failures are logged and never raised so callers can always proceed.
    """
    if not address or not address.strip():
        return False
    try:
        from dbus_fast import BusType, Message, MessageType, Variant
        from dbus_fast.aio import MessageBus
    except ImportError:
        return False
    path = bluez_device_path(adapter, address)
    bus = None
    try:
        bus = await asyncio.wait_for(MessageBus(bus_type=BusType.SYSTEM).connect(), timeout)
        reply = await asyncio.wait_for(bus.call(Message(
            destination=BLUEZ_SERVICE,
            path=path,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="Get",
            signature="ss",
            body=[BLUEZ_DEVICE_INTERFACE, "Connected"],
        )), timeout)
        if reply is None or reply.message_type != MessageType.METHOD_RETURN:
            return False
        connected = reply.body[0]
        if isinstance(connected, Variant):
            connected = connected.value
        if not connected:
            return False
        LOGGER.warning("BlueZ still holds a connection to %s on %s; disconnecting it", address, adapter)
        reply = await asyncio.wait_for(bus.call(Message(
            destination=BLUEZ_SERVICE,
            path=path,
            interface=BLUEZ_DEVICE_INTERFACE,
            member="Disconnect",
        )), timeout)
        if reply is not None and reply.message_type == MessageType.ERROR:
            LOGGER.warning("BlueZ refused to disconnect %s: %s %s", address, reply.error_name, reply.body)
            return False
        return True
    except Exception as error:
        LOGGER.warning("Unable to verify BlueZ link state for %s on %s: %r", address, adapter, error)
        return False
    finally:
        if bus is not None:
            bus.disconnect()


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

    @contextmanager
    def connect_slot_sync(self) -> Iterator[None]:
        """Reserve the adapter from blocking code, such as RFCOMM socket connects."""
        with self._connect_lock:
            yield


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