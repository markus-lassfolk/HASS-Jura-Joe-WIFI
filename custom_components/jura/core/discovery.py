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

        # Find MAC address at known offset 103 in the 142-byte beacon.
        # The ESP32 MAC is a 6-byte block at a fixed position in the response.
        # We validate by checking for a unicast MAC (LSB of first byte = 0).
        mac_found = False
        # Primary: fixed offset 103 (confirmed via Jura E8 captures)
        if len(data) >= 109:
            mac_bytes = data[103:109]
            if (
                mac_bytes != b"\x00" * 6
                and mac_bytes != b"\xff" * 6
                and (mac_bytes[0] & 0x01) == 0  # unicast MAC
            ):
                result["mac"] = ":".join(f"{b:02X}" for b in mac_bytes)
                mac_found = True

        if not mac_found:
            # Fallback: scan the second half for any plausible unicast MAC
            for offset in range(len(data) // 2, len(data) - 5):
                mac_bytes = data[offset : offset + 6]
                if (
                    mac_bytes != b"\x00" * 6
                    and mac_bytes != b"\xff" * 6
                    and (mac_bytes[0] & 0x01) == 0
                    and any(b != 0 for b in mac_bytes[:3])
                ):
                    result["mac"] = ":".join(f"{b:02X}" for b in mac_bytes)
                    mac_found = True
                    break

        if not mac_found and len(data) >= 12:
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


def _get_local_subnets() -> list[tuple[str, int]]:
    """Return list of (network_address, prefix_len) for local interfaces."""
    import ipaddress

    subnets: list[tuple[str, int]] = []
    try:
        import subprocess

        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show"],
            timeout=3,
            text=True,
        )
        for line in out.splitlines():
            parts = line.split()
            for i, part in enumerate(parts):
                if part == "inet" and i + 1 < len(parts):
                    try:
                        net = ipaddress.IPv4Interface(parts[i + 1])
                        if not net.ip.is_loopback:
                            subnets.append(
                                (str(net.network.network_address), net.network.prefixlen)
                            )
                    except ValueError:
                        pass
    except Exception:
        pass
    return subnets


async def _probe_single_host(host: str) -> dict | None:
    """Send a unicast UDP probe to one host and return parsed beacon or None."""
    loop = asyncio.get_event_loop()
    result: dict | None = None
    event = asyncio.Event()

    class _Protocol(asyncio.DatagramProtocol):
        def __init__(self):
            self.transport = None

        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            nonlocal result
            if data == DISCOVERY_PROBE or len(data) < MIN_RESPONSE_SIZE:
                return
            parsed = parse_beacon(data, addr=addr[0])
            if parsed:
                result = parsed
                event.set()

    try:
        transport, _ = await loop.create_datagram_endpoint(
            _Protocol, local_addr=("0.0.0.0", 0)
        )
    except OSError:
        return None

    try:
        transport.sendto(DISCOVERY_PROBE, (host, DISCOVERY_PORT))
        async with asyncio.timeout(2.0):
            await event.wait()
    except (TimeoutError, OSError):
        pass
    finally:
        transport.close()

    return result


async def discover_machines(
    timeout: float = 5.0, target_ip: str | None = None
) -> list[dict]:
    """Send discovery probes and return discovered Jura machines.

    Uses a multi-strategy approach:
    1. UDP broadcast to 255.255.255.255
    2. Subnet-directed broadcast to each local /24
    3. ARP neighbour unicast probes (parallel, like Yarbo discovery)
    4. Specific target_ip if provided

    The Jura machine only responds to unicast probes (not passive broadcast),
    so strategies 3-4 are the most reliable.
    """
    machines: dict[str, dict] = {}

    def on_machine(machine: dict):
        key = machine.get("mac") or machine.get("ip", "")
        machines[key] = machine

    loop = asyncio.get_event_loop()

    # --- Strategy 1+2: Broadcast probes (may not work in all network setups) ---
    try:
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _JuraDiscoveryProtocol(on_machine),
            local_addr=("0.0.0.0", 0),
            allow_broadcast=True,
        )
    except OSError as e:
        _LOGGER.warning("Cannot create broadcast socket: %s", e)
        transport = None

    if transport:
        try:
            sock = transport.get_extra_info("socket")
            if sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

            transport.sendto(DISCOVERY_PROBE, ("255.255.255.255", DISCOVERY_PORT))
            _LOGGER.debug("Sent discovery probe to broadcast")

            # Subnet-directed broadcasts
            for net_addr, prefix in _get_local_subnets():
                import ipaddress

                net = ipaddress.IPv4Network(f"{net_addr}/{prefix}", strict=False)
                bcast = str(net.broadcast_address)
                transport.sendto(DISCOVERY_PROBE, (bcast, DISCOVERY_PORT))
                _LOGGER.debug("Sent discovery probe to subnet broadcast %s", bcast)

            if target_ip:
                transport.sendto(DISCOVERY_PROBE, (target_ip, DISCOVERY_PORT))

            # Wait a bit for broadcast responses
            await asyncio.sleep(min(2.0, timeout))
        finally:
            transport.close()

    if machines:
        return list(machines.values())

    # --- Strategy 3: ARP neighbour unicast probes (most reliable) ---
    _LOGGER.debug("Broadcast discovery found nothing, trying ARP neighbour probes")
    neighbours: list[str] = []
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip", "neigh",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        for line in stdout.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 1 and ("REACHABLE" in line or "STALE" in line):
                neighbours.append(parts[0])
    except (FileNotFoundError, TimeoutError, OSError):
        pass

    if target_ip and target_ip not in neighbours:
        neighbours.append(target_ip)

    if neighbours:
        _LOGGER.debug("Probing %d ARP neighbours for Jura machines", len(neighbours))
        # Probe in parallel (max 30 concurrent)
        sem = asyncio.Semaphore(30)

        async def _limited_probe(ip: str) -> dict | None:
            async with sem:
                return await _probe_single_host(ip)

        results = await asyncio.gather(
            *[_limited_probe(ip) for ip in neighbours]
        )
        for r in results:
            if r:
                on_machine(r)

    return list(machines.values())
