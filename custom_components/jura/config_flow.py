"""Config flow for the Jura integration.

Supports two connection types:
- BLE: existing Bluetooth flow (select MAC from discovered devices)
- WiFi: UDP auto-discovery + manual IP entry, then auth hash entry
- DHCP: passive discovery when HA detects an ESP32 on the network,
  confirmed via UDP probe before presenting to the user

Options flow for runtime settings (error reporting opt-out, etc.)
"""

import asyncio
import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
import voluptuous as vol

from .core import DOMAIN
from .core.discovery import (
    DISCOVERY_PORT,
    DISCOVERY_PROBE,
    MIN_RESPONSE_SIZE,
    discover_machines,
)

_LOGGER = logging.getLogger(__name__)

CONF_ERROR_REPORTING = "error_reporting"

CONNECTION_TYPE_BLE = "ble"
CONNECTION_TYPE_WIFI = "wifi"
_MANUAL_IP = "manual"


class FlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a Jura config flow."""

    VERSION = 1

    def __init__(self):
        self._wifi_host: str = ""
        self._discovered: list[dict] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return JuraOptionsFlowHandler(config_entry)

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> dict:
        """Handle DHCP discovery of a potential Jura WiFi machine.

        HA triggers this when a device with hostname 'espressif' appears.
        We send the Jura UDP probe to confirm it's actually a Jura machine
        before presenting the discovery to the user.
        """
        host = discovery_info.ip
        mac = discovery_info.macaddress
        _LOGGER.debug(
            "DHCP discovery: hostname=%s ip=%s mac=%s",
            discovery_info.hostname,
            host,
            mac,
        )

        # Check if we already have this device configured
        for entry in self._async_current_entries():
            if entry.data.get("host") == host:
                return self.async_abort(reason="already_configured")

        # Send UDP probe to confirm this is actually a Jura machine
        jura_info = await self._async_probe_jura(host)
        if jura_info is None:
            _LOGGER.debug("DHCP device at %s is not a Jura machine", host)
            return self.async_abort(reason="not_jura")

        # It's a Jura! Store info and present discovery to user
        self._wifi_host = host
        name = jura_info.get("name", "Jura Coffee Machine")
        model = jura_info.get("model", "")

        await self.async_set_unique_id(jura_info.get("mac", mac))
        self._abort_if_unique_id_configured(updates={"host": host})

        self.context["title_placeholders"] = {
            "name": name,
            "model": model,
            "host": host,
        }

        return await self.async_step_wifi_auth()

    async def _async_probe_jura(self, host: str) -> dict | None:
        """Send a UDP probe to check if the host is a Jura WiFi machine.

        Returns parsed beacon dict if it's a Jura, None otherwise.
        """
        loop = asyncio.get_event_loop()
        result: dict | None = None
        event = asyncio.Event()

        class _ProbeProtocol(asyncio.DatagramProtocol):
            def __init__(self):
                self.transport = None

            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                nonlocal result
                if data == DISCOVERY_PROBE:
                    return
                if len(data) >= MIN_RESPONSE_SIZE:
                    from .core.discovery import parse_beacon

                    parsed = parse_beacon(data, addr=addr[0])
                    if parsed:
                        result = parsed
                        event.set()

        try:
            transport, _protocol = await loop.create_datagram_endpoint(
                _ProbeProtocol,
                local_addr=("0.0.0.0", 0),
            )
        except OSError as err:
            _LOGGER.debug("Cannot create probe socket: %s", err)
            return None

        try:
            transport.sendto(DISCOVERY_PROBE, (host, DISCOVERY_PORT))
            async with asyncio.timeout(3.0):
                await event.wait()
        except TimeoutError:
            _LOGGER.debug("No Jura probe response from %s", host)
        finally:
            transport.close()

        return result

    async def async_step_user(self, user_input=None):
        """Step 1: choose BLE or WiFi."""
        if user_input is not None:
            conn_type = user_input.get("connection_type", CONNECTION_TYPE_BLE)
            if conn_type == CONNECTION_TYPE_WIFI:
                return await self.async_step_wifi_discover()
            return await self.async_step_ble()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "connection_type", default=CONNECTION_TYPE_BLE
                    ): vol.In([CONNECTION_TYPE_BLE, CONNECTION_TYPE_WIFI])
                }
            ),
        )

    # ------------------------------------------------------------------
    # BLE path (unchanged from original)
    # ------------------------------------------------------------------

    async def async_step_ble(self, user_input=None):
        """BLE: pick from discovered Bluetooth devices."""
        if user_input is not None:
            return self.async_create_entry(
                title=user_input["mac"],
                data={"connection_type": CONNECTION_TYPE_BLE, "mac": user_input["mac"]},
            )

        devices = bluetooth.async_get_scanner(self.hass).discovered_devices
        macs = [v.address for v in devices if v.name == "TT214H BlueFrog"]

        return self.async_show_form(
            step_id="ble",
            data_schema=vol.Schema({vol.Required("mac"): vol.In(macs)}),
        )

    # ------------------------------------------------------------------
    # WiFi path
    # ------------------------------------------------------------------

    async def async_step_wifi_discover(self, user_input=None):
        """WiFi step 1: run UDP discovery and let the user pick a machine."""
        if user_input is not None:
            host = user_input.get("host", _MANUAL_IP)
            if host == _MANUAL_IP:
                return await self.async_step_wifi_manual()
            self._wifi_host = host
            return await self.async_step_wifi_auth()

        try:
            self._discovered = await asyncio.wait_for(
                discover_machines(timeout=5.0), timeout=6.0
            )
            _LOGGER.debug("WiFi discovery found %d machine(s)", len(self._discovered))
        except Exception as e:
            _LOGGER.warning("WiFi discovery failed: %s", e)
            self._discovered = []

        host_options: dict[str, str] = {
            m["ip"]: f"{m.get('name', m['ip'])} ({m['ip']})" for m in self._discovered
        }
        host_options[_MANUAL_IP] = "Enter IP address manually"

        return self.async_show_form(
            step_id="wifi_discover",
            data_schema=vol.Schema({vol.Required("host"): vol.In(host_options)}),
        )

    async def async_step_wifi_manual(self, user_input=None):
        """WiFi step 1b: manually enter machine IP address."""
        if user_input is not None:
            self._wifi_host = user_input["host"]
            return await self.async_step_wifi_auth()

        return self.async_show_form(
            step_id="wifi_manual",
            data_schema=vol.Schema({vol.Required("host"): str}),
        )

    async def async_step_wifi_auth(self, user_input=None):
        """WiFi step 2: enter auth credentials (auth hash from J.O.E. app pairing)."""
        if user_input is not None:
            return self.async_create_entry(
                title=f"Jura WiFi ({self._wifi_host})",
                data={
                    "connection_type": CONNECTION_TYPE_WIFI,
                    "host": self._wifi_host,
                    "port": user_input.get("port", 51515),
                    "pin": user_input.get("pin", ""),
                    "auth_hash": user_input.get("auth_hash", ""),
                    "device_name": user_input.get("device_name", "HomeAssistant"),
                },
            )

        return self.async_show_form(
            step_id="wifi_auth",
            data_schema=vol.Schema(
                {
                    vol.Required("auth_hash"): str,
                    vol.Optional("pin", default=""): str,
                    vol.Optional("device_name", default="HomeAssistant"): str,
                    vol.Optional("port", default=51515): int,
                }
            ),
        )


class JuraOptionsFlowHandler(OptionsFlow):
    """Handle Jura integration options (error reporting, etc.)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage integration options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(CONF_ERROR_REPORTING, True)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_ERROR_REPORTING, default=current): bool,
                }
            ),
        )
