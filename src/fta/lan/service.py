"""Everything the app needs from the local network, started and stopped as one piece:
this device's identity, the receiver for incoming files and mDNS discovery."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fta.lan.direct import Offer, OfferHandler, Receiver, send_files
from fta.lan.discovery import Discovery, Peer
from fta.lan.identity import Identity
from fta.lan.links import PeerLink

logger = logging.getLogger(__name__)


class LanService:
    def __init__(
        self,
        data_dir: Path,
        receive_dir: Path,
        on_offer: OfferHandler,
        *,
        name: str = "",
        port: int = 0,
        pin: str = "",
        on_update: Callable[[Offer], None] | None = None,
        on_peers: Callable[[], None] | None = None,
        ssh_port: Callable[[], int | None] = lambda: None,
        discover: bool = True,
        host: str = "0.0.0.0",
        interfaces: list[str] | None = None,
    ) -> None:
        self.identity = Identity.load(data_dir, name)
        self.receiver = Receiver(self.identity, receive_dir, on_offer, host=host, port=port, pin=pin, on_update=on_update)
        self.on_peers = on_peers or (lambda: None)
        self.ssh_port = ssh_port
        self.discover = discover
        self.interfaces = interfaces
        self.discovery: Discovery | None = None
        self.error = ""

    @property
    def running(self) -> bool:
        return self.receiver.running

    def start(self) -> None:
        """Start receiving now; announce and browse in the background, since mDNS start-up
        waits on the network and must not run on the UI's event loop."""
        self.error = ""
        self.receiver.start()
        if not self.discover:
            return
        discovery = Discovery(
            self.identity.id,
            self.identity.name,
            self.receiver.port,
            self.identity.fingerprint,
            ssh_port=self.ssh_port(),
            on_change=self.on_peers,
            interfaces=self.interfaces,
        )
        self.discovery = discovery
        threading.Thread(target=self._start_discovery, args=(discovery,), name="fta-mdns", daemon=True).start()

    def _start_discovery(self, discovery: Discovery) -> None:
        try:
            discovery.start()
        except Exception as exc:  # no multicast (some VPNs, phones on mobile data)
            logger.warning("mDNS discovery is unavailable: %s", exc)
            self.error = f"поиск устройств недоступен: {exc}"
            if self.discovery is discovery:
                self.discovery = None
            self.on_peers()
            return
        if self.discovery is not discovery:  # stopped while starting
            discovery.stop()

    def stop(self) -> None:
        discovery, self.discovery = self.discovery, None
        if discovery is not None:  # the goodbye packets block too; send them off the UI thread
            threading.Thread(target=discovery.stop, name="fta-mdns-stop", daemon=True).start()
        self.receiver.stop()

    @property
    def peers(self) -> list[Peer]:
        return self.discovery.peers if self.discovery else []

    def peer_names(self) -> list[str]:
        return [
            f"{peer.describe()}" + (f" · SSH {peer.address}:{peer.ssh_port}" if peer.ssh_port else "")
            for peer in self.peers
        ]

    def find(self, text: str) -> Peer | None:
        return self.discovery.find(text) if self.discovery else None

    def link(self, address: str) -> PeerLink:
        """What a QR code for this device carries."""
        return PeerLink(address, self.receiver.port, self.identity.fingerprint, self.identity.name)

    def sender(self, target: Peer | PeerLink, paths: list[Path], pin: str = "") -> Any:
        """An ``Action`` for the transfer queue that sends ``paths`` to ``target``.

        Retries and resumes after a pause continue the same session, so the
        receiving user is asked once and partial files are continued.
        """
        host = target.address if isinstance(target, Peer) else target.host
        port, fingerprint = target.port, target.fingerprint
        session = {"id": ""}

        def action(progress, on_file, resume):
            result = send_files(
                host,
                paths,
                port=port,
                fingerprint=fingerprint,
                sender=self.identity,
                pin=pin,
                progress=progress,
                on_file=on_file,
                resume_session=session["id"] if resume else "",
                on_session=lambda value: session.update(id=value),
            )
            return result.results

        return action
