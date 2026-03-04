"""Async TCP client for Jura WiFi Connect module (port 51515)."""

import asyncio
import contextlib
import logging

from .wifi_encryption import wifi_make_frame, wifi_parse_frame

_LOGGER = logging.getLogger(__name__)


class WifiClient:
    """Async WiFi client for Jura machines via plain TCP on port 51515."""

    def __init__(
        self,
        host: str,
        port: int = 51515,
        pin: str = "",
        device_name: str = "HomeAssistant",
        auth_hash: str = "",
    ):
        self.host = host
        self.port = port
        self.pin = pin
        self.device_name_hex = device_name.encode().hex()
        self.auth_hash = auth_hash
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.connected = False

    async def connect(self) -> bool:
        """Open TCP connection and authenticate with the machine.

        Returns True if auth succeeded (@hp4 response), False otherwise.
        """
        try:
            self.reader, self.writer = await asyncio.open_connection(
                self.host, self.port
            )
        except OSError as e:
            _LOGGER.warning("WiFi connect to %s:%s failed: %s", self.host, self.port, e)
            return False

        auth_cmd = f"@HP:{self.pin},{self.device_name_hex},{self.auth_hash}"
        await self._send_frame(auth_cmd)
        response = await self.read_response()
        _LOGGER.debug("Auth response: %r", response)
        self.connected = "@hp4" in response
        if not self.connected:
            _LOGGER.warning(
                "WiFi auth failed for %s, response: %r", self.host, response
            )
            await self.disconnect()
        return self.connected

    async def _send_frame(self, cmd: str):
        """Encode and write one framed command."""
        frame = wifi_make_frame(cmd)
        self.writer.write(frame)
        await self.writer.drain()

    async def send_command(self, cmd: str, timeout: float = 3.0) -> str:
        """Send command string and wait for one response frame."""
        await self._send_frame(cmd)
        return await self.read_response(timeout)

    async def read_response(self, timeout: float = 3.0) -> str:
        """Read and decode one response frame from the machine."""
        try:
            data = await asyncio.wait_for(self._read_until_frame(), timeout)
            return wifi_parse_frame(data)
        except TimeoutError:
            _LOGGER.debug("Read timeout waiting for response from %s", self.host)
            return ""

    async def _read_until_frame(self) -> bytes:
        """Read raw bytes until the 0x0D 0x0A frame terminator."""
        buf = bytearray()
        while True:
            byte = await self.reader.read(1)
            if not byte:
                break
            buf.extend(byte)
            if len(buf) >= 2 and buf[-2] == 0x0D and buf[-1] == 0x0A:
                break
        return bytes(buf)

    async def get_machine_state(self) -> int:
        """Return the 32-bit machine state word from @TM:08."""
        await self.send_command("@TS:01")
        resp = await self.send_command("@TM:08")
        await self.send_command("@TS:00")
        resp_lower = resp.lower()
        if resp_lower.startswith("@tm:08,"):
            try:
                return int(resp_lower[7:].strip(), 16)
            except ValueError:
                _LOGGER.debug("Could not parse state word from: %r", resp)
        return 0

    async def get_firmware_version(self) -> str:
        """Return firmware version string from @TG:C0."""
        resp = await self.send_command("@TG:C0")
        resp_lower = resp.lower()
        if resp_lower.startswith("@tg:"):
            return resp[4:].strip()
        return ""

    async def get_temperature(self) -> int | None:
        """Return raw temperature value from @TM:0A, or None on failure."""
        await self.send_command("@TS:01")
        resp = await self.send_command("@TM:0A")
        await self.send_command("@TS:00")
        resp_lower = resp.lower()
        if resp_lower.startswith("@tm:0a,"):
            try:
                return int(resp_lower[7:].strip(), 16)
            except ValueError:
                _LOGGER.debug("Could not parse temperature from: %r", resp)
        return None

    # -------------------------------------------------------------------------
    # Brew commands
    # -------------------------------------------------------------------------

    async def start_product(self, product_hex: str) -> str:
        """Start a product (brew).

        product_hex is the 34-char hex argument string (17 bytes):
        - position 0  = product code byte
        - positions 3-7 = F3-F7 parameter bytes (strength, water, milk, foam, temp)
        - position 8  = 0x01 (fixed flag)
        - all other positions = 0x00

        Returns the raw response string (@tp = started, @tp:XX = error code).
        """
        return await self.send_command(f"@TP:{product_hex}")

    async def cancel_product(self) -> str:
        """Cancel an in-progress product preparation. Sends @TG:FF."""
        return await self.send_command("@TG:FF")

    async def adjust_parameter_during_brew(self, param_hex: str) -> str:
        """Adjust a parameter during intake/brew. Sends @TD:<hex>."""
        return await self.send_command(f"@TD:{param_hex}")

    # -------------------------------------------------------------------------
    # Product counter statistics (@TR:32 / @TR:33)
    # -------------------------------------------------------------------------

    async def read_product_counters(self) -> dict[int, int]:
        """Read all product counter pages and return {product_index: count}.

        Sends @TR:32,<page> for pages 0x00-0x0F, concatenates hex responses,
        then splits into per-product counts (2 bytes each = big-endian uint16).
        """
        full_hex = await self._read_paged_data("@TR:32")
        return self._parse_counters(full_hex, bytes_per_entry=2)

    async def read_product_counters_overflow(self) -> dict[int, int]:
        """Read overflow counters (@TR:33) for products exceeding 65535 cups.

        Returns {product_index: overflow_multiplier} where each entry is 1 byte.
        Add overflow_count * 65536 to the base counter for the true total.
        """
        full_hex = await self._read_paged_data("@TR:33")
        return self._parse_counters(full_hex, bytes_per_entry=1)

    # -------------------------------------------------------------------------
    # Special counter statistics (@TR:52 / @TR:53)
    # -------------------------------------------------------------------------

    async def read_special_counters(self) -> str:
        """Read special/machine-level counter data (@TR:52). Returns raw hex."""
        return await self._read_paged_data("@TR:52")

    async def read_special_counters_overflow(self) -> str:
        """Read special counter overflow data (@TR:53). Returns raw hex."""
        return await self._read_paged_data("@TR:53")

    # -------------------------------------------------------------------------
    # Maintenance counters (@TG:43)
    # -------------------------------------------------------------------------

    async def read_maintenance_counters(self) -> str:
        """Read maintenance counter data (@TG:43). Returns raw hex payload."""
        resp = await self.send_command("@TG:43")
        if resp.startswith("@tg:43"):
            return resp[6:].strip()
        return ""

    # -------------------------------------------------------------------------
    # Machine function commands (@TF:XX)
    # -------------------------------------------------------------------------

    async def restart_machine(self) -> str:
        """Restart the coffee machine (@TF:02). Use with caution."""
        return await self.send_command("@TF:02")

    async def reset_daily_counter(self) -> str:
        """Reset the daily product counter (@TF:05)."""
        return await self.send_command("@TF:05")

    # -------------------------------------------------------------------------
    # Limit load (@TM:60)
    # -------------------------------------------------------------------------

    async def read_limit_load(self, product_code: str) -> str:
        """Read limit/capacity for a product (@TM:60,<code>). Returns raw hex."""
        resp = await self.send_command(f"@TM:60,{product_code}")
        if resp.startswith("@tm:60,"):
            return resp[7:].strip()
        return ""

    # -------------------------------------------------------------------------
    # P-Mode / Personal Mode (@TM:50, @TM:41, @TM:42)
    # -------------------------------------------------------------------------

    async def read_pmode_slots(self) -> int:
        """Read number of personal mode slots (@TM:50)."""
        resp = await self.send_command("@TM:50")
        if resp.startswith("@tm:50"):
            try:
                return int(resp[6:8].strip(), 16)
            except (ValueError, IndexError):
                pass
        return 0

    async def read_pmode_product(self, slot: int) -> str:
        """Read personal mode product definition for a slot (@TM:41,<slot>)."""
        resp = await self.send_command(f"@TM:41,{slot:02X}")
        if resp.startswith("@tm:41,"):
            return resp[7:].strip()
        return ""

    # -------------------------------------------------------------------------
    # Coffee timer (@TM:3C)
    # -------------------------------------------------------------------------

    async def get_coffee_timer(self) -> str:
        """Read coffee timer configuration (@TM:3C). Returns raw response."""
        resp = await self.send_command("@TM:3C")
        if resp.startswith("@tm"):
            return resp[4:].strip()
        return ""

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    async def _read_paged_data(self, cmd_prefix: str) -> str:
        """Read paginated data by sending <cmd_prefix>,<page> for pages 0-15.

        Concatenates hex payloads from all responses and returns the full string.
        Stops early if the machine returns @tr:00 (no more data).
        """
        full_hex = ""
        # cmd_prefix looks like "@TR:32"; extract the base for response matching
        for page in range(16):
            resp = await self.send_command(f"{cmd_prefix},{page:02X}")
            if not resp:
                break
            if resp.strip() in ("@tr:00", "@tr:00"):
                break
            # Response: @tr:32,XX,<hex_data>  (or similar for @tg etc.)
            parts = resp.split(",", 2)
            if len(parts) >= 3:
                full_hex += parts[2].strip()
            await asyncio.sleep(0.05)
        return full_hex

    @staticmethod
    def _parse_counters(full_hex: str, bytes_per_entry: int) -> dict[int, int]:
        """Parse a concatenated hex string into {index: count} mapping.

        bytes_per_entry=2 → uint16 big-endian per product
        bytes_per_entry=1 → uint8 per product
        """
        hex_per_entry = bytes_per_entry * 2
        counters: dict[int, int] = {}
        for i in range(0, len(full_hex), hex_per_entry):
            chunk = full_hex[i : i + hex_per_entry]
            if len(chunk) == hex_per_entry:
                with contextlib.suppress(ValueError):
                    counters[i // hex_per_entry] = int(chunk, 16)
        return counters

    async def disconnect(self):
        """Close the TCP connection cleanly."""
        self.connected = False
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None
            self.reader = None
