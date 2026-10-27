"""Find other FTA devices on the local network with mDNS/DNS-SD (zeroconf, Bonjour).

Each device announces ``_fta._tcp.local.`` with its id, name, certificate
fingerprint and, when its SSH server runs, the SSH port. Browsing collects the
other devices into ``peers``.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from fta import __version__

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_fta._tcp.local."


@dataclass(frozen=True)
class Peer:
    id: str
    name: str
    addresses: tuple[str, ...]
    port: int
    fingerprint: str = ""
    ssh_port: int | None = None
    version: str = ""
    seen: float = field(default_factory=time.time, compare=False)

    @property
    def address(self) -> str:
        """The address to try first: IPv4 before IPv6."""
        return sorted(self.addresses, key=lambda a: ":" in a)[0]

    def describe(self) -> str:
        return f"{self.name} ({self.address})"


class Discovery:
    """Announces this device and keeps ``peers`` up to date until ``stop()``."""

    def __init__(
        self,
        device_id: str,
        name: str,
        port: int,
        fingerprint: str,
        *,
        ssh_port: int | None = None,
        on_change: Callable[[], None] | None = None,
        interfaces: list[str] | None = None,
    ) -> None:
        self.device_id = device_id
        self.name = name
        self.port = port
        self.fingerprint = fingerprint
        self.ssh_port = ssh_port
        self.on_change = on_change or (lambda: None)
        self.interfaces = interfaces
        self._peers: dict[str, Peer] = {}
        self._names: dict[str, str] = {}
        """mDNS service name → device id, to drop a device when its record goes away."""
        self._lock = threading.Lock()
        self._zeroconf = None
        self._browser = None
        self._info = None

    @property
    def peers(self) -> list[Peer]:
        with self._lock:
            return sorted(self._peers.values(), key=lambda p: p.name.casefold())

    def find(self, text: str) -> Peer | None:
        """A peer by name (case-insensitive), id or address."""
        wanted = text.casefold()
        for peer in self.peers:
            if wanted in (peer.name.casefold(), peer.id) or text in peer.addresses:
                return peer
        return None

    def start(self) -> None:
        """Blocks for a few seconds while the announcement goes out; call it off the UI thread."""
        from zeroconf import InterfaceChoice, IPVersion, ServiceBrowser, ServiceInfo, Zeroconf

        with self._lock:
            if self._zeroconf is not None:
                return
            self._zeroconf = Zeroconf(interfaces=self.interfaces or InterfaceChoice.All, ip_version=IPVersion.V4Only)
        properties = {"id": self.device_id, "name": self.name, "fp": self.fingerprint, "v": __version__}
        if self.ssh_port:
            properties["ssh"] = str(self.ssh_port)
        label = _service_label(self.name, self.device_id)
        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{label}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(a) for a in _local_addresses(self.interfaces)],
            port=self.port,
            properties=properties,
            server=f"fta-{self.device_id[:12]}.local.",
        )
        try:
            self._zeroconf.register_service(self._info, allow_name_change=True)
        except Exception:
            logger.exception("cannot announce this device on the local network")
        self._browser = ServiceBrowser(self._zeroconf, SERVICE_TYPE, handlers=[self._on_service_state_change])

    def stop(self) -> None:
        if self._zeroconf is None:
            return
        try:
            if self._browser is not None:
                self._browser.cancel()
            if self._info is not None:
                self._zeroconf.unregister_service(self._info)
        except Exception:
            logger.debug("mDNS shutdown", exc_info=True)
        finally:
            self._zeroconf.close()
            self._zeroconf = self._browser = self._info = None
            with self._lock:
                self._peers.clear()
                self._names.clear()

    def _on_service_state_change(self, zeroconf, service_type: str, name: str, state_change) -> None:
        from zeroconf import ServiceStateChange

        if state_change is ServiceStateChange.Removed:
            with self._lock:
                device_id = self._names.pop(name, None)
                removed = self._peers.pop(device_id, None) if device_id else None
            if removed:
                self.on_change()
            return
        # Resolving waits for network answers; never do that on zeroconf's own thread.
        threading.Thread(target=self._resolve, args=(zeroconf, service_type, name), daemon=True).start()

    def _resolve(self, zeroconf, service_type: str, name: str) -> None:
        info = zeroconf.get_service_info(service_type, name, timeout=3000)
        if info is None:
            return
        props = {k.decode(): (v or b"").decode(errors="replace") for k, v in (info.properties or {}).items()}
        device_id = props.get("id", "")
        if not device_id or device_id == self.device_id:
            return
        peer = Peer(
            id=device_id,
            name=props.get("name") or name.split(".")[0],
            addresses=tuple(info.parsed_scoped_addresses()) or ("",),
            port=info.port or 0,
            fingerprint=props.get("fp", ""),
            ssh_port=int(props["ssh"]) if props.get("ssh", "").isdigit() else None,
            version=props.get("v", ""),
        )
        with self._lock:
            self._names[name] = device_id
            changed = self._peers.get(device_id) != peer
            self._peers[device_id] = peer
        if changed:
            self.on_change()


def _service_label(name: str, device_id: str) -> str:
    clean = "".join(c for c in name if c.isalnum() or c in " -_")[:40].strip() or "FTA"
    return f"{clean} [{device_id[:6]}]"


def _local_addresses(interfaces: list[str] | None) -> list[str]:
    if interfaces:
        return [a for a in interfaces if "." in a]
    try:
        import ifaddr

        found = [
            ip.ip
            for adapter in ifaddr.get_adapters()
            for ip in adapter.ips
            if isinstance(ip.ip, str) and not ip.ip.startswith(("127.", "169.254."))
        ]
    except Exception:
        found = []
    return found or ["127.0.0.1"]
