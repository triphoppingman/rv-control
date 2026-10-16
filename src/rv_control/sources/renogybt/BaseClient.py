from __future__ import annotations

import asyncio
import configparser
import logging
import random
import traceback
from typing import Any, Callable
from .BLEManager import BLEManager
from .Utils import bytes_to_int, crc16_modbus, int_to_bytes

logger = logging.getLogger(__name__)

# Base class that works with all Renogy family devices
# Should be extended by each client with its own parsers and section definitions
# Section example: {'register': 5000, 'words': 8, 'parser': self.parser_func}

ALIAS_PREFIXES = ['BT-TH', 'RNGRBP', 'BTRIC', 'RTMShunt', 'RNGRIU']
WRITE_SERVICE_UUID = "0000ffd0-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID  = "0000ffd1-0000-1000-8000-00805f9b34fb"
READ_TIMEOUT = 15 # (seconds)
READ_SUCCESS = 3
READ_ERROR = 131

class BaseClient:
    def __init__(self, config: configparser.ConfigParser) -> None:
        """Initialize shared Renogy client state from device and data configuration."""
        self.config: configparser.ConfigParser = config
        self.ble_manager = None
        self.device = None
        self.poll_timer = None
        self.read_timeout = None
        self.data = {}
        self.device_id = self.config['device'].getint('device_id')
        self.sections = []
        self.section_index = 0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.future: asyncio.Future[str] | None = None
        self._stopping = False
        self.write_service_uuid = getattr(self, 'write_service_uuid', WRITE_SERVICE_UUID)
        self.write_char_uuid = getattr(self, 'write_char_uuid', WRITE_CHAR_UUID)
        self.notify_char_uuid = getattr(self, 'notify_char_uuid', NOTIFY_CHAR_UUID)
        self._retry_count = 0
        self._reconnecting = False
        self._connecting = False
        self._retry_reason: str | None = None
        # Incremented on each connection attempt so read chains from older sessions stop.
        self._generation = 0
        self.max_retry = self.config['device'].getint('max_retry', fallback=3)
        self.persistent_connection = self.config['device'].getboolean(
            'persistent_connection', fallback=False
        )
        logging.info(f"Init {self.__class__.__name__}: {self.config['device']['alias']} => {self.config['device']['mac_addr']}")

    def start(self) -> None:
        """Run one client session and close its owned event loop on every exit."""
        session = self._run_session()
        try:
            asyncio.run(session)
        except Exception as e:
            self.__on_error(e)
        except KeyboardInterrupt:
            self.__on_error("KeyboardInterrupt")
        finally:
            session.close()

    async def _run_session(self) -> None:
        """Collect until stopped, then release BLE resources before loop shutdown."""
        self.loop = asyncio.get_running_loop()
        self.future = self.loop.create_future()
        self._stopping = False
        self._retry_count = 0
        self._retry_reason = None
        try:
            await self.connect()
            await self.future
        finally:
            self._stopping = True
            if self.read_timeout:
                self.read_timeout.cancel()
            if self.poll_timer:
                self.poll_timer.cancel()
            try:
                pending = asyncio.all_tasks() - {asyncio.current_task()}
                for task in pending:
                    task.cancel()
                results = await asyncio.gather(*pending, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        logging.error("Renogy task failed during shutdown: %s", result)
                await self.disconnect()
            finally:
                self.ble_manager = None
                self.future = None
                self.loop = None

    async def connect(self) -> None:
        """Discover and connect the BLE manager, retrying according to client policy."""
        self._connecting = True
        try:
            await self._connect_once()
        finally:
            self._connecting = False
            if self._retry_reason is not None:
                self.__schedule_retry(self._retry_reason)

    async def _connect_once(self) -> None:
        """Complete one discovery and setup attempt before retry processing starts."""
        self._generation += 1
        self.section_index = 0
        self.data = {}
        if self.ble_manager is None:
            self.ble_manager = BLEManager(
                mac_address=self.config['device']['mac_addr'],
                alias=self.config['device']['alias'],
                adapter=self.config['device'].get('adapter', 'hci0'),
                discovery_timeout=self.config['device'].getfloat('discovery_timeout', fallback=5),
                write_settle_delay=self.config['data'].getfloat('write_settle_delay', fallback=0.5),
                on_data=self.on_data_received,
                on_connect_fail=self.__on_connect_fail,
                on_disconnect=self.__on_disconnect,
                notify_char_uuid=self.notify_char_uuid,
                write_char_uuid=self.write_char_uuid,
                write_service_uuid=self.write_service_uuid,
            )

        manager = self.ble_manager
        await manager.discover()
        if self.ble_manager is not manager:
            logging.debug("BLE manager replaced during discovery; abandoning stale connect attempt.")
            return

        if not manager.device:
            logging.error(f"Device not found: {self.config['device']['alias']} => {self.config['device']['mac_addr']}, please check the details provided.")
            for dev in manager.discovered_devices:
                if dev.name != None and dev.name.startswith(tuple(ALIAS_PREFIXES)):
                    logging.info(f"Possible device found! ====> {dev.name} > [{dev.address}]")
            self.__schedule_retry("Device not found during discovery")
            return
        else:
            await manager.connect()
            if self.ble_manager is not manager:
                logging.debug("BLE manager replaced during connect; abandoning stale connect attempt.")
                return
            if self._retry_reason is None and manager.client and manager.client.is_connected:
                self._retry_count = 0
                await self.read_section()

    async def disconnect(self) -> None:
        """Disconnect the BLE manager and release active client resources."""
        if self.ble_manager:
            await self.ble_manager.disconnect()

    async def on_data_received(self, response: bytes | bytearray) -> None:
        """Dispatch an incoming response to the matching parser and callback."""
        if self.read_timeout and not self.read_timeout.cancelled(): self.read_timeout.cancel()
        generation = self._generation
        operation = bytes_to_int(response, 1, 1)

        if operation == READ_SUCCESS or operation == READ_ERROR:
            if (operation == READ_SUCCESS and
                self.section_index < len(self.sections) and
                self.sections[self.section_index]['parser'] != None and
                self.sections[self.section_index]['words'] * 2 + 5 == len(response)):
                # call the parser and update data
                logging.info(f"on_data_received: read operation success")
                self.__safe_parser(self.sections[self.section_index]['parser'], response)
            else:
                logging.info(f"on_data_received: read operation failed: {response.hex()}")

            if self.section_index >= len(self.sections) - 1: # last section, read complete
                self.section_index = 0
                self.on_read_operation_complete()
                self.data = {}
                await self.check_polling()
            else:
                self.section_index += 1
                await asyncio.sleep(self.config['data'].getfloat('request_interval', fallback=0.5))
                if generation != self._generation or self._stopping:
                    return logging.debug("Discarding read chain from a replaced Renogy session.")
                await self.read_section()
        else:
            logging.warning("on_data_received: unknown operation={}".format(operation))

    def on_read_operation_complete(self) -> None:
        """Advance polling state after a register read completes successfully."""
        logging.info("on_read_operation_complete")
        self.data['__device'] = self.config['device']['alias']
        self.data['__client'] = self.__class__.__name__
        self.__safe_callback(self.on_data_callback, self.data)

    def on_read_timeout(self) -> None:
        """Record a read timeout and let the retry lifecycle handle it."""
        logging.error("on_read_timeout => Timed out! Please check your device_id!")
        self.__schedule_retry("Read timeout")

    async def check_polling(self) -> None:
        """Schedule exactly one next polling cycle, releasing the link when not persistent."""
        if self._stopping or not self.config['data'].getboolean('enable_polling'):
            return
        if self.poll_timer and not self.poll_timer.done():
            self.poll_timer.cancel()
        if not self.persistent_connection:
            # Free the single-connection BLE peripheral for other centrals between polls.
            manager = self.ble_manager
            self.ble_manager = None
            if manager is not None:
                try:
                    await manager.disconnect()
                except Exception as error:
                    logging.warning(f"Error disconnecting Renogy device between polls: {error!r}")
        self.poll_timer = self.loop.create_task(self._poll_after_interval(self._generation))

    async def _poll_after_interval(self, generation: int) -> None:
        """Wait one poll interval, then read again or reconnect for a new session."""
        await asyncio.sleep(self.config['data'].getfloat('poll_interval', fallback=60))
        if self._stopping or generation != self._generation or self._reconnecting:
            return
        # Past the wait, so a nested check_polling must not cancel this running cycle.
        self.poll_timer = None
        if self.persistent_connection and self.ble_manager is not None:
            await self.read_section()
        else:
            await self.connect()

    async def read_section(self) -> None:
        """Send the next configured register read request to the device."""
        index = self.section_index
        # check if device_id is set and if there are sections to read
        if self.device_id == None:
            return logging.error("BaseClient cannot be used directly")

        # shunt client is notification-driven, so no need to write anything
        if not getattr(self, 'write_char_uuid', None) or len(self.sections) == 0:
            return logging.info("Nothing to write, skipping operation")

        manager = self.ble_manager
        if manager is None:
            return logging.debug("No active BLE manager; skipping read request.")

        read_timeout = self.config['data'].getfloat('read_timeout', fallback=READ_TIMEOUT)
        self.read_timeout = self.loop.call_later(read_timeout, self.on_read_timeout)
        request = self.create_generic_read_request(self.device_id, 3, self.sections[index]['register'], self.sections[index]['words']) 
        await manager.characteristic_write_value(request)

    def create_generic_read_request(self, device_id: int, function: int, regAddr: int | None, readWrd: int | None) -> list[int] | None:
        """Build a Modbus-style read request from device, function, and register values."""
        data = None
        if regAddr != None and readWrd != None:
            data = []
            data.append(device_id)
            data.append(function)
            data.append(int_to_bytes(regAddr, 0))
            data.append(int_to_bytes(regAddr, 1))
            data.append(int_to_bytes(readWrd, 0))
            data.append(int_to_bytes(readWrd, 1))

            crc = crc16_modbus(bytes(data))
            data.append(crc[0])
            data.append(crc[1])
            logging.debug("{} {} => {}".format("create_request_payload", regAddr, data))
        return data

    def __on_error(self, error: Any = None) -> None:
        """Forward a client error to the configured error callback."""
        logging.error(f"Exception occured: {error}")
        self.__safe_callback(self.on_error_callback, error)
        self.stop()

    def __on_connect_fail(self, error: Any) -> None:
        """Handle failed BLE connection attempts and schedule retry processing."""
        reason = f"Connection failed: {error!r}"
        logger.error(
            "Renogy %s (%s) on adapter %s: %s",
            self.config['device']['alias'],
            self.config['device']['mac_addr'],
            self.config['device'].get('adapter', 'hci0'),
            reason,
        )
        self.__schedule_retry(reason)

    def __on_disconnect(self) -> None:
        """Handle an unexpected BLE disconnect during persistent operation."""
        self.__schedule_retry("Unexpected disconnect")

    def __schedule_retry(self, reason: str) -> None:
        """Retain retry triggers and let one worker handle them after setup completes."""
        if self._stopping or self.loop is None or not self.loop.is_running():
            return
        self._retry_reason = reason
        if self._connecting or self._reconnecting:
            return
        self._reconnecting = True
        self.loop.create_task(self.__handle_retry_async())

    async def __handle_retry_async(self) -> None:
        """Drain retry triggers without overlapping attempts or losing new failures."""
        try:
            while not self._stopping and self._retry_reason is not None:
                reason = self._retry_reason
                self._retry_reason = None
                if self._retry_count >= self.max_retry:
                    logging.error(f"Max retries ({self.max_retry}) reached. Reason: {reason}. Stopping client.")
                    self.__safe_callback(self.on_error_callback, f"Max retries reached: {reason}")
                    self.stop()
                    return
                self._retry_count += 1
                reconnect_delay = self.config['device'].getfloat('reconnect_delay', fallback=2)
                max_reconnect_delay = self.config['device'].getfloat('max_reconnect_delay', fallback=300)
                reconnect_jitter = self.config['device'].getfloat('reconnect_jitter', fallback=0.1)
                delay = min(reconnect_delay * (2 ** (self._retry_count - 1)), max_reconnect_delay)
                delay = min(delay * random.uniform(1 - reconnect_jitter, 1 + reconnect_jitter), max_reconnect_delay)
                logging.info(f"Retrying connection in {delay} seconds (Attempt {self._retry_count}/{self.max_retry}). Reason: {reason}")
                self._generation += 1
                if self.read_timeout and not self.read_timeout.cancelled():
                    self.read_timeout.cancel()
                if self.poll_timer:
                    self.poll_timer.cancel()
                    
                if self.ble_manager:
                    try:
                        await self.ble_manager.disconnect()
                        self.ble_manager = None
                    except Exception as e:
                        logging.warning(f"Error disconnecting manager client during retry setup: {e}")
                        
                await asyncio.sleep(delay)
                self._retry_reason = None
                await self.connect()
        except Exception as error:
            self.__on_error(error)
        finally:
            self._reconnecting = False

    def stop(self) -> None:
        """Request shutdown on the existing loop without allocating new resources."""
        if self.read_timeout and not self.read_timeout.cancelled(): self.read_timeout.cancel()
        loop = self.loop
        future = self.future
        if loop is None or not loop.is_running() or future is None or self._stopping:
            return

        def finish() -> None:
            """Complete the session once even when several callbacks request stop."""
            if not future.done():
                future.set_result('DONE')

        loop.call_soon_threadsafe(finish)

    def __safe_callback(self, calback: Callable[[BaseClient, Any], None] | None, param: Any) -> None:
        """Invoke an optional client callback while containing callback exceptions."""
        if calback is not None:
            try:
                calback(self, param)
            except Exception as e:
                logging.error(f"__safe_callback => exception in callback! {e}")
                traceback.print_exc()

    def __safe_parser(self, parser: Callable[[bytes | bytearray], None] | None, param: bytes | bytearray) -> None:
        """Invoke an optional response parser while containing parser exceptions."""
        if parser is not None:
            try:
                parser(param)
            except Exception as e:
                logging.error(f"exception in parser! {e}")
                traceback.print_exc()
