"""WiFi-connected Jura machine device model.

Wraps WifiClient and provides polling + update-callback infrastructure
compatible with the JuraWifiEntity base class.

Machine state word (@TM:08) bit definitions:
    Bit 0  (0x00000001): standby / idle
    Bit 1  (0x00000002): ready
    Bit 2  (0x00000004): machine ready (heating done, brew-ready)
    Bit 3  (0x00000008): water tank missing / empty
    Bit 4  (0x00000010): grinder empty (no beans)
    Bit 5  (0x00000020): drip tray full
    Bit 6  (0x00000040): grounds container full
    Bit 7  (0x00000080): brewing in progress
    Bit 8  (0x00000100): grinding in progress
    Bit 9  (0x00000200): heating in progress
    Bit 10 (0x00000400): rinsing in progress
    Bit 11 (0x00000800): cleaning in progress
    Bit 12 (0x00001000): error state
"""

from collections.abc import Callable
import logging
import time

from .wifi_client import WifiClient

_LOGGER = logging.getLogger(__name__)

# Bit masks for the @TM:08 state word
BIT_STANDBY = 0x00000001
BIT_READY = 0x00000002
BIT_MACHINE_READY = 0x00000004
BIT_WATER_MISSING = 0x00000008
BIT_GRINDER_EMPTY = 0x00000010
BIT_DRIP_TRAY_FULL = 0x00000020
BIT_GROUNDS_FULL = 0x00000040
BIT_BREWING = 0x00000080
BIT_GRINDING = 0x00000100
BIT_HEATING = 0x00000200
BIT_RINSING = 0x00000400
BIT_CLEANING = 0x00000800
BIT_ERROR = 0x00001000

# Statistics re-fetch interval (10 minutes)
_STATS_INTERVAL = 600.0

# Default product code → name mapping (Jura universal product codes)
DEFAULT_PRODUCTS: dict[int, str] = {
    0x01: "Ristretto",
    0x02: "Espresso",
    0x03: "Coffee",
    0x04: "Cappuccino",
    0x05: "Latte Macchiato",
    0x06: "Flat White",
    0x07: "Espresso Macchiato",
    0x08: "Milk Coffee",
    0x09: "Hot Water",
    0x0A: "Hot Milk",
    0x0B: "Milk Foam",
}

# Brew parameter definitions: attr → (position_in_brew_hex, display_name, min, max, step)
WIFI_BREW_PARAMS: list[dict] = [
    {
        "attr": "coffee_strength",
        "display_name": "Coffee Strength",
        "position": 3,
        "min": 0,
        "max": 5,
        "step": 1,
        "default": 0,
        "icon": "mdi:coffee-to-go",
    },
    {
        "attr": "water_amount",
        "display_name": "Water Amount",
        "position": 4,
        "min": 0,
        "max": 255,
        "step": 1,
        "default": 0,
        "icon": "mdi:water",
    },
    {
        "attr": "milk_amount",
        "display_name": "Milk Amount",
        "position": 5,
        "min": 0,
        "max": 255,
        "step": 1,
        "default": 0,
        "icon": "mdi:cup",
    },
    {
        "attr": "milk_foam_amount",
        "display_name": "Milk Foam Amount",
        "position": 6,
        "min": 0,
        "max": 255,
        "step": 1,
        "default": 0,
        "icon": "mdi:cup-outline",
    },
    {
        "attr": "temperature_setting",
        "display_name": "Temperature Setting",
        "position": 7,
        "min": 0,
        "max": 3,
        "step": 1,
        "default": 0,
        "icon": "mdi:thermometer",
    },
]


class WifiDevice:
    """Represents a Jura coffee machine reachable over TCP/IP (WiFi Connect)."""

    def __init__(
        self,
        name: str,
        host: str,
        port: int = 51515,
        pin: str = "",
        device_name: str = "HomeAssistant",
        auth_hash: str = "",
        model: str = "Jura WiFi",
        mac: str = "",
    ):
        self.name = name
        self.model = model
        self.connected = False
        self.conn_info: dict = {"host": host}

        # Use MAC if provided; otherwise derive a stable ID from the IP
        self._mac = mac if mac else host.replace(".", "")

        self._client = WifiClient(host, port, pin, device_name, auth_hash)

        # Basic state
        self._state_word: int = 0
        self._firmware_version: str = ""
        self._temperature: int | None = None

        # Brew parameters (0 = use machine default)
        self._selected_product: int = 0x02  # default: Espresso
        self._brew_params: dict[str, int] = {
            p["attr"]: p["default"] for p in WIFI_BREW_PARAMS
        }

        # Statistics
        self._product_counters: dict[int, int] = {}
        self._counters_loaded: bool = False
        self._last_stats_update: float = 0.0
        self._maintenance_hex: str = ""

        self._update_handlers: list[Callable] = []

    # --- Public properties ---------------------------------------------------

    @property
    def mac(self) -> str:
        """Stable device identifier (real MAC or IP-derived string)."""
        return self._mac

    @property
    def state_word(self) -> int:
        """Raw 32-bit machine state word from @TM:08."""
        return self._state_word

    @property
    def firmware_version(self) -> str:
        """WiFi firmware version string from @TG:C0."""
        return self._firmware_version

    @property
    def temperature(self) -> int | None:
        """Raw temperature value from @TM:0A (unit depends on machine)."""
        return self._temperature

    @property
    def selected_product_name(self) -> str:
        """Human-readable name of the currently selected product."""
        return DEFAULT_PRODUCTS.get(self._selected_product, "Unknown")

    @property
    def maintenance_hex(self) -> str:
        """Raw hex payload from @TG:43 maintenance counters."""
        return self._maintenance_hex

    # --- State-word helpers --------------------------------------------------

    def machine_ready(self) -> bool:
        return bool(self._state_word & BIT_MACHINE_READY)

    def water_missing(self) -> bool:
        return bool(self._state_word & BIT_WATER_MISSING)

    def grinder_empty(self) -> bool:
        return bool(self._state_word & BIT_GRINDER_EMPTY)

    def drip_tray_full(self) -> bool:
        return bool(self._state_word & BIT_DRIP_TRAY_FULL)

    def grounds_full(self) -> bool:
        return bool(self._state_word & BIT_GROUNDS_FULL)

    def brewing(self) -> bool:
        return bool(self._state_word & BIT_BREWING)

    def grinding(self) -> bool:
        return bool(self._state_word & BIT_GRINDING)

    def heating(self) -> bool:
        return bool(self._state_word & BIT_HEATING)

    def rinsing(self) -> bool:
        return bool(self._state_word & BIT_RINSING)

    def cleaning(self) -> bool:
        return bool(self._state_word & BIT_CLEANING)

    def error(self) -> bool:
        return bool(self._state_word & BIT_ERROR)

    # --- Maintenance helpers (parsed from @TG:43 raw hex) -------------------

    def maintenance_cleaning_needed(self) -> bool:
        """Byte 0, bit 0 of @TG:43 payload = cleaning needed."""
        return self._parse_maintenance_bit(byte_idx=0, bit=0x01)

    def maintenance_descaling_needed(self) -> bool:
        """Byte 0, bit 1 of @TG:43 payload = descaling needed."""
        return self._parse_maintenance_bit(byte_idx=0, bit=0x02)

    def maintenance_filter_needed(self) -> bool:
        """Byte 1, bit 0 of @TG:43 payload = filter replacement needed."""
        return self._parse_maintenance_bit(byte_idx=1, bit=0x01)

    def _parse_maintenance_bit(self, byte_idx: int, bit: int) -> bool:
        hex_start = byte_idx * 2
        hex_end = hex_start + 2
        if len(self._maintenance_hex) >= hex_end:
            try:
                return bool(int(self._maintenance_hex[hex_start:hex_end], 16) & bit)
            except ValueError:
                pass
        return False

    # --- Statistics helpers --------------------------------------------------

    def total_products(self) -> int:
        """Total cups made across all products."""
        return sum(self._product_counters.values())

    def product_count(self, product_code: int) -> int:
        """Cup count for a specific product code."""
        return self._product_counters.get(product_code, 0)

    # --- Brew control --------------------------------------------------------

    def select_product(self, name: str) -> None:
        """Select a product by name."""
        for code, pname in DEFAULT_PRODUCTS.items():
            if pname == name:
                self._selected_product = code
                return
        _LOGGER.warning("Unknown product name: %r", name)

    def set_brew_param(self, attr: str, value: int) -> None:
        """Set a brew parameter value (0 = use machine default)."""
        if attr in self._brew_params:
            self._brew_params[attr] = value
        else:
            _LOGGER.warning("Unknown brew parameter: %r", attr)

    def get_brew_param(self, attr: str) -> int:
        """Get the current value for a brew parameter."""
        return self._brew_params.get(attr, 0)

    def build_brew_hex(self) -> str:
        """Build the 34-char hex string for @TP: brew command.

        Layout (17 bytes):
            byte[0]   = product code
            byte[3]   = coffee_strength (F3)
            byte[4]   = water_amount   (F4)
            byte[5]   = milk_amount    (F5)
            byte[6]   = milk_foam_amount (F6)
            byte[7]   = temperature_setting (F7)
            byte[8]   = 0x01 (fixed flag)
            all others = 0x00
        """
        buf = bytearray(17)
        buf[0] = self._selected_product
        for param in WIFI_BREW_PARAMS:
            buf[param["position"]] = self._brew_params.get(param["attr"], 0)
        buf[8] = 0x01
        return buf.hex()

    async def start_product(self) -> str:
        """Build brew hex and send @TP: command. Returns machine response."""
        if not self._client.connected:
            ok = await self._client.connect()
            if not ok:
                raise ConnectionError(f"Cannot connect to {self.name}")
        if not self.machine_ready():
            _LOGGER.warning(
                "Brew requested but machine is not ready (state=0x%08X)",
                self._state_word,
            )
        product_hex = self.build_brew_hex()
        _LOGGER.info(
            "Starting product: %s  hex=%s", self.selected_product_name, product_hex
        )
        return await self._client.start_product(product_hex)

    async def cancel_product(self) -> str:
        """Send @TG:FF to cancel an in-progress brew."""
        if not self._client.connected:
            ok = await self._client.connect()
            if not ok:
                raise ConnectionError(f"Cannot connect to {self.name}")
        return await self._client.cancel_product()

    # --- Statistics ----------------------------------------------------------

    async def refresh_statistics(self) -> None:
        """Fetch product counters and maintenance data from the machine.

        Combines base counters (@TR:32) with overflow (@TR:33) so the true
        count = base + overflow * 65536.  Maintenance data is fetched via
        @TG:43.  Calls all update handlers when done.
        """
        if not self._client.connected:
            ok = await self._client.connect()
            if not ok:
                _LOGGER.warning(
                    "Cannot connect to %s for statistics refresh", self.name
                )
                return
        try:
            base = await self._client.read_product_counters()
            overflow = await self._client.read_product_counters_overflow()
            combined: dict[int, int] = {}
            for idx, count in base.items():
                combined[idx] = count + overflow.get(idx, 0) * 65536
            self._product_counters = combined
            self._counters_loaded = True

            self._maintenance_hex = await self._client.read_maintenance_counters()
            self._last_stats_update = time.monotonic()
            _LOGGER.debug(
                "Statistics refreshed: %d product entries, maintenance=%r",
                len(self._product_counters),
                self._maintenance_hex,
            )
        except Exception as e:
            _LOGGER.warning("Statistics refresh failed for %s: %s", self.name, e)
        finally:
            self._notify_updates()

    # --- Update handler registration -----------------------------------------

    def register_wifi_update(self, handler: Callable):
        """Register a callback invoked after each poll cycle completes."""
        self._update_handlers.append(handler)

    def _notify_updates(self):
        for handler in self._update_handlers:
            try:
                handler()
            except Exception as e:
                _LOGGER.warning("Update handler raised an exception: %s", e)

    # --- Polling -------------------------------------------------------------

    async def async_update(self):
        """Poll the machine for current state. Called by the HA coordinator."""
        try:
            if not self._client.connected:
                ok = await self._client.connect()
                if not ok:
                    self.connected = False
                    self._notify_updates()
                    return

            self.connected = True
            self._state_word = await self._client.get_machine_state()
            self.conn_info["state_word"] = hex(self._state_word)

            if not self._firmware_version:
                self._firmware_version = await self._client.get_firmware_version()

            temp = await self._client.get_temperature()
            if temp is not None:
                self._temperature = temp

            # Refresh statistics once on startup, then every 10 minutes
            now = time.monotonic()
            if (
                not self._counters_loaded
                or (now - self._last_stats_update) >= _STATS_INTERVAL
            ):
                await self.refresh_statistics()
                return  # refresh_statistics already called _notify_updates

        except Exception as e:
            _LOGGER.warning("WiFi poll error for %s: %s", self.name, e)
            self.connected = False
            await self._client.disconnect()

        self._notify_updates()

    async def disconnect(self):
        """Disconnect from the machine (called on HA unload)."""
        await self._client.disconnect()
        self.connected = False
