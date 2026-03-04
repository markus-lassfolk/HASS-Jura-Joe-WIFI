"""UDP discovery for Jura WiFi Connect machines.

The J.O.E. app sends a 16-byte UDP probe to port 51515 (broadcast or unicast).
Machines respond with a ~142-byte beacon containing device name, model,
firmware version, and MAC address.

Probe payload: 0010A5F3000000000000000000000000
"""

import asyncio
import logging
import socket

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PORT = 51515
DISCOVERY_PROBE = bytes.fromhex("0010A5F3000000000000000000000000")
# Response size may vary by model; accept anything >= 80 bytes
MIN_RESPONSE_SIZE = 80


def parse_beacon(data: bytes, addr: str | None = None) -> dict | None:
    """Parse a Jura UDP discovery beacon response.

    The beacon contains null-terminated latin-1 strings for firmware,
    device name, and model. A 6-byte MAC address is embedded later in the
    payload.
    """
    if len(data) < MIN_RESPONSE_SIZE:
        return None
    # Ignore echo of our own probe
    if data == DISCOVERY_PROBE:
        return None
    try:
        text = data.decode("latin-1", errors="replace")
        fields = [f.strip() for f in text.split("\x00") if f.strip()]
        result: dict = {"raw": data.hex(), "beacon_size": len(data)}

        # First field contains firmware (after 3-byte header: 00 XX A5 F3)
        # Format: "??TT237W V06.11" — firmware string starts after header bytes
        if len(fields) >= 1:
            fw = fields[0]
            # Strip leading non-printable header bytes
            fw_clean = "".join(c for c in fw if c.isprintable())
            result["firmware"] = fw_clean
        if len(fields) >= 2:
            result["name"] = fields[1]
        if len(fields) >= 3:
            result["model"] = fields[2]

        # Find MAC address: 6 bytes matching XX:XX:XX pattern
        # In the 142-byte response, MAC is typically near the end
        # Known pattern: look for the OUI bytes we know (8C:4B:14 for Jura)
        # More generically, scan for a valid MAC in the latter half
        mac_found = False
        for offset in range(len(data) - 6, max(len(data) // 2, 0), -1):
            mac_bytes = data[offset : offset + 6]
            # A valid MAC has at least some non-zero bytes and isn't all FF
            if (
                mac_bytes != b"\x00" * 6
                and mac_bytes != b"\xff" * 6
                and any(b != 0 for b in mac_bytes[:3])
            ):
                # Check if this looks like a real MAC (OUI should be plausible)
                mac_str = ":".join(f"{b:02X}" for b in mac_bytes)
                result["mac"] = mac_str
                mac_found = True
                break

        if not mac_found:
            # Fallback: use bytes at offset 6-12
            mac_bytes = data[6:12]
            result["mac"] = ":".join(f"{b:02X}" for b in mac_bytes)

        if addr:
            result["ip"] = addr

        return result
    except Exception as e:
        _LOGGER.debug("Error parsing beacon: %s", e)
        return None


class _JuraDiscoveryProtocol(asyncio.DatagramProtocol):
    """asyncio UDP protocol that sends probes and collects responses."""

    def __init__(self, callback):
        self.callback = callback
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        _LOGGER.debug("UDP datagram from %s, length=%d", addr[0], len(data))
        # Ignore our own probe echoed back
        if data == DISCOVERY_PROBE:
            return
        if len(data) >= MIN_RESPONSE_SIZE:
            machine = parse_beacon(data, addr=addr[0])
            if machine:
                self.callback(machine)

    def error_received(self, exc):
        _LOGGER.debug("Discovery socket error: %s", exc)


async def discover_machines(
    timeout: float = 5.0, target_ip: str | None = None
) -> list[dict]:
    """Send discovery probes and return discovered Jura machines.

    Sends the J.O.E. discovery probe to the broadcast address (and optionally
    a specific IP), waits *timeout* seconds for responses, then returns results.
    """
    machines: dict[str, dict] = {}

    def on_machine(machine: dict):
        key = machine.get("mac") or machine.get("ip", "")
        machines[key] = machine

    loop = asyncio.get_event_loop()
    try:
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _JuraDiscoveryProtocol(on_machine),
            local_addr=("0.0.0.0", DISCOVERY_PORT),
            allow_broadcast=True,
        )
    except OSError as e:
        _LOGGER.warning("Cannot bind UDP port %d for discovery: %s", DISCOVERY_PORT, e)
        # Try without binding to specific port (ephemeral port)
        try:
            transport, _protocol = await loop.create_datagram_endpoint(
                lambda: _JuraDiscoveryProtocol(on_machine),
                local_addr=("0.0.0.0", 0),
                allow_broadcast=True,
            )
        except OSError as e2:
            _LOGGER.error("Cannot create discovery socket: %s", e2)
            return []

    try:
        # Send discovery probe as broadcast
        sock = transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        transport.sendto(DISCOVERY_PROBE, ("255.255.255.255", DISCOVERY_PORT))
        _LOGGER.debug("Sent discovery probe to broadcast")

        # Also send to specific IP if provided
        if target_ip:
            transport.sendto(DISCOVERY_PROBE, (target_ip, DISCOVERY_PORT))
            _LOGGER.debug("Sent discovery probe to %s", target_ip)

        # Send a second probe after 1 second in case first was lost
        await asyncio.sleep(1.0)
        transport.sendto(DISCOVERY_PROBE, ("255.255.255.255", DISCOVERY_PORT))

        await asyncio.sleep(timeout - 1.0 if timeout > 1.0 else timeout)
    finally:
        transport.close()

    return list(machines.values())
