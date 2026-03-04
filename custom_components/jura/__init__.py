from datetime import timedelta
import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .core import DOMAIN
from .core.device import Device, EmptyModel, UnsupportedModel, get_machine
from .core.wifi_device import WifiDevice
from .error_reporting import async_init_error_reporting

_LOGGER = logging.getLogger(__name__)

# Platforms loaded for BLE entries
BLE_PLATFORMS = ["binary_sensor", "button", "number", "select", "switch", "sensor"]
# Platforms loaded for WiFi entries
WIFI_PLATFORMS = ["binary_sensor", "sensor", "button", "select", "number"]

WIFI_POLL_INTERVAL = 30  # seconds


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    devices = hass.data.setdefault(DOMAIN, {})

    connection_type = entry.data.get("connection_type", "ble")
    tags: dict[str, str] = {
        "integration": DOMAIN,
        "integration_version": "1.3.0",
        "connection_type": connection_type,
        "ha_version": __version__,
    }
    if connection_type == "wifi":
        host = entry.data.get("host", "")
        # Only mask IPv4 addresses; skip IPv6 and hostnames
        if host and "." in host and ":" not in host:
            parts = host.split(".")
            if len(parts) == 4 and all(
                p.isdigit() and 0 <= int(p) <= 255 for p in parts
            ):
                last_octet = parts[-1]
                tags["machine_host"] = f"*.*.*.{last_octet}"

    # Error reporting: enabled by default, opt-out via integration options
    error_reporting_enabled = entry.options.get("error_reporting", True)
    await async_init_error_reporting(
        hass, tags=tags, entry_id=entry.entry_id, enabled=error_reporting_enabled
    )

    # Listen for options updates (e.g. user toggles error reporting)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    if connection_type == "wifi":
        return await _setup_wifi_entry(hass, entry, devices)

    return await _setup_ble_entry(hass, entry, devices)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload integration to apply changes."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _setup_wifi_entry(
    hass: HomeAssistant, entry: ConfigEntry, devices: dict
) -> bool:
    """Set up a WiFi-connected Jura machine."""
    data = entry.data
    device = WifiDevice(
        name=entry.title,
        host=data["host"],
        port=data.get("port", 51515),
        pin=data.get("pin", ""),
        device_name=data.get("device_name", "HomeAssistant"),
        auth_hash=data.get("auth_hash", ""),
    )
    devices[entry.entry_id] = device

    # Initial poll (best-effort; machine may be off)
    await device.async_update()

    await hass.config_entries.async_forward_entry_setups(entry, WIFI_PLATFORMS)

    async def _poll(_=None):
        await device.async_update()

    entry.async_on_unload(
        async_track_time_interval(hass, _poll, timedelta(seconds=WIFI_POLL_INTERVAL))
    )

    def _unload():
        hass.async_create_task(device.disconnect())

    entry.async_on_unload(_unload)
    return True


async def _setup_ble_entry(
    hass: HomeAssistant, entry: ConfigEntry, devices: dict
) -> bool:
    """Set up a BLE-connected Jura machine (original behaviour)."""

    @callback
    def update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        _LOGGER.debug(f"{change} {service_info.advertisement}")

        if device := devices.get(entry.entry_id):
            device.update_ble(service_info.advertisement)
            return

        try:
            machine = get_machine(service_info.advertisement.manufacturer_data[171])
        except EmptyModel:
            return
        except UnsupportedModel as e:
            _LOGGER.error("Unsupported model: %s", *e.args)
            return

        devices[entry.entry_id] = device = Device(
            entry.title,
            machine["model"],
            machine["products"],
            machine["alerts"],
            machine["key"],
            service_info.device,
        )
        device.update_ble(service_info.advertisement)

        hass.create_task(
            hass.config_entries.async_forward_entry_setups(entry, BLE_PLATFORMS)
        )

    # https://developers.home-assistant.io/docs/core/bluetooth/api/
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            update_ble,
            {"address": entry.data["mac"], "manufacturer_id": 171, "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    if entry.entry_id in hass.data[DOMAIN]:
        platforms = (
            WIFI_PLATFORMS
            if entry.data.get("connection_type") == "wifi"
            else BLE_PLATFORMS
        )
        await hass.config_entries.async_unload_platforms(entry, platforms)
    return True
