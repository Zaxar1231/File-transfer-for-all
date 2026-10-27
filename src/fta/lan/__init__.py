"""Local network: find nearby devices (mDNS) and send files straight to them over HTTPS.

The receiving side shows each offer and accepts or declines it; certificates are
self-signed and pinned by fingerprint (from mDNS, a QR code or the peer list),
so nothing travels in the clear and no certificate authority is involved.
"""

from fta.lan.direct import FileOffer, Offer, Receiver, SendResult, send_files
from fta.lan.identity import Identity
from fta.lan.links import PeerLink, parse_link

__all__ = [
    "FileOffer",
    "Identity",
    "Offer",
    "PeerLink",
    "Receiver",
    "SendResult",
    "parse_link",
    "send_files",
]
