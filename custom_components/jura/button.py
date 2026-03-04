import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .core import DOMAIN
from .core.entity import JuraEntity, JuraWifiEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    device = hass.data[DOMAIN][config_entry.entry_id]

    if config_entry.data.get("connection_type") == "wifi":
        add_entities(
            [
                JuraWifiBrewButton(device),
                JuraWifiCancelBrewButton(device),
                JuraWifiUpdateStatisticsButton(device),
            ]
        )
        return

    add_entities(
        [
            JuraMakeButton(device, "make"),
            JuraRefreshStatsButton(device),
        ]
    )


# ---------------------------------------------------------------------------
# BLE button classes (unchanged)
# ---------------------------------------------------------------------------


class JuraMakeButton(JuraEntity, ButtonEntity):
    def internal_update(self):
        self._attr_available = self.device.product is not None

        if self.hass:
            self._async_write_ha_state()

    async def async_press(self) -> None:
        self.device.start_product()


class JuraRefreshStatsButton(JuraEntity, ButtonEntity):
    """Button to refresh statistics from the Jura machine."""

    def __init__(self, device):
        super().__init__(device, "refresh_stats")
        self._attr_icon = "mdi:refresh"
        self._attr_name = f"{device.name} Refresh Statistics"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_available = True  # Always make the button available

    def internal_update(self):
        if self.hass:
            self._async_write_ha_state()

    async def async_press(self) -> None:
        """Handle the button press."""
        _LOGGER.info("Manually refreshing Jura statistics and alerts")
        try:
            await self.device.read_statistics(force_update=True)
            await self.device.read_alerts()
            _LOGGER.info("Successfully refreshed Jura statistics and alerts")
        except Exception as e:
            _LOGGER.error(f"Error refreshing Jura statistics: {e}")


# ---------------------------------------------------------------------------
# WiFi button classes
# ---------------------------------------------------------------------------


class JuraWifiBrewButton(JuraWifiEntity, ButtonEntity):
    """Button that sends the @TP: brew command for the currently selected product."""

    def __init__(self, device):
        super().__init__(device, "brew")
        self._attr_icon = "mdi:coffee-maker"
        self._attr_name = f"{device.name} Brew"

    def internal_update(self):
        # Available when the machine is connected
        self._attr_available = self.device.connected
        if self.hass:
            self._async_write_ha_state()

    async def async_press(self) -> None:
        try:
            resp = await self.device.start_product()
            _LOGGER.info("Brew response: %r", resp)
        except Exception as e:
            _LOGGER.error("Brew failed: %s", e)


class JuraWifiCancelBrewButton(JuraWifiEntity, ButtonEntity):
    """Button that sends @TG:FF to cancel an in-progress brew."""

    def __init__(self, device):
        super().__init__(device, "cancel_brew")
        self._attr_icon = "mdi:coffee-maker-off"
        self._attr_name = f"{device.name} Cancel Brew"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def internal_update(self):
        self._attr_available = self.device.connected
        if self.hass:
            self._async_write_ha_state()

    async def async_press(self) -> None:
        try:
            resp = await self.device.cancel_product()
            _LOGGER.info("Cancel brew response: %r", resp)
        except Exception as e:
            _LOGGER.error("Cancel brew failed: %s", e)


class JuraWifiUpdateStatisticsButton(JuraWifiEntity, ButtonEntity):
    """Button that manually triggers a statistics refresh from the machine."""

    def __init__(self, device):
        super().__init__(device, "update_statistics")
        self._attr_icon = "mdi:refresh"
        self._attr_name = f"{device.name} Update Statistics"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def internal_update(self):
        self._attr_available = True
        if self.hass:
            self._async_write_ha_state()

    async def async_press(self) -> None:
        _LOGGER.info("Manually refreshing WiFi statistics for %s", self.device.name)
        try:
            await self.device.refresh_statistics()
        except Exception as e:
            _LOGGER.error("Statistics refresh failed: %s", e)
