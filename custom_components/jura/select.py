from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN
from .core.entity import JuraEntity, JuraWifiEntity
from .core.wifi_device import DEFAULT_PRODUCTS


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, add_entities: AddEntitiesCallback
):
    device = hass.data[DOMAIN][config_entry.entry_id]

    if config_entry.data.get("connection_type") == "wifi":
        add_entities([JuraWifiProductSelect(device)])
        return

    add_entities([JuraSelect(device, select) for select in device.selects()])


# ---------------------------------------------------------------------------
# BLE select class (unchanged)
# ---------------------------------------------------------------------------


class JuraSelect(JuraEntity, SelectEntity):
    def internal_update(self):
        attribute = self.device.attribute(self.attr)

        self._attr_current_option = attribute.get("default")
        self._attr_options = attribute.get("options", [])
        self._attr_available = "default" in attribute

        if self.hass:
            self._async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        self.device.select_option(self.attr, option)
        self._attr_current_option = option
        self._async_write_ha_state()


# ---------------------------------------------------------------------------
# WiFi select class
# ---------------------------------------------------------------------------


class JuraWifiProductSelect(JuraWifiEntity, SelectEntity):
    """Select entity for choosing which product to brew on a WiFi Jura machine."""

    _attr_icon = "mdi:coffee-maker"
    _attr_options = list(DEFAULT_PRODUCTS.values())

    def __init__(self, device):
        super().__init__(device, "product")
        self._attr_name = f"{device.name} Product"
        self._attr_current_option = device.selected_product_name

    def internal_update(self):
        self._attr_current_option = self.device.selected_product_name
        if self.hass:
            self._async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        self.device.select_product(option)
        self._attr_current_option = option
        self._async_write_ha_state()
