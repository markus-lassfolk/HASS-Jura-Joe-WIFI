from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN
from .core.entity import JuraEntity, JuraWifiEntity
from .core.wifi_device import WIFI_BREW_PARAMS


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    device = hass.data[DOMAIN][config_entry.entry_id]

    if config_entry.data.get("connection_type") == "wifi":
        add_entities([JuraWifiNumber(device, p) for p in WIFI_BREW_PARAMS])
        return

    add_entities([JuraNumber(device, select) for select in device.numbers()])


# ---------------------------------------------------------------------------
# BLE number class (unchanged)
# ---------------------------------------------------------------------------


class JuraNumber(JuraEntity, NumberEntity):
    def internal_update(self):
        attribute = self.device.attribute(self.attr)

        self._attr_available = "value" in attribute
        self._attr_native_min_value = attribute.get("min", 0)
        self._attr_native_max_value = attribute.get("max", 0)
        self._attr_native_step = attribute.get("step")
        self._attr_native_value = attribute.get("value")

        if self.hass:
            self._async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        self.device.set_value(self.attr, int(value))
        self._attr_native_value = int(value)
        self._async_write_ha_state()


# ---------------------------------------------------------------------------
# WiFi number class
# ---------------------------------------------------------------------------


class JuraWifiNumber(JuraWifiEntity, NumberEntity):
    """Number entity for a single WiFi brew parameter (F3-F7).

    A value of 0 means 'use machine default' for that parameter.
    """

    def __init__(self, device, param_def: dict):
        self._param_attr = param_def["attr"]
        super().__init__(device, param_def["attr"])
        self._attr_name = f"{device.name} {param_def['display_name']}"
        self._attr_icon = param_def["icon"]
        self._attr_native_min_value = float(param_def["min"])
        self._attr_native_max_value = float(param_def["max"])
        self._attr_native_step = float(param_def["step"])
        self._attr_native_value = float(param_def["default"])

    def internal_update(self):
        self._attr_native_value = float(self.device.get_brew_param(self._param_attr))
        if self.hass:
            self._async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        self.device.set_brew_param(self._param_attr, int(value))
        self._attr_native_value = float(int(value))
        self._async_write_ha_state()
