"""This device on the local network: a name, a stable id and a self-signed TLS certificate."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import socket
import ssl
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Identity:
    id: str
    name: str
    cert_file: Path
    key_file: Path

    @classmethod
    def load(cls, directory: Path, name: str = "") -> Identity:
        """Read the identity kept in ``directory``, creating it on first use."""
        directory.mkdir(parents=True, exist_ok=True)
        meta_file = directory / "lan_identity.json"
        cert_file, key_file = directory / "lan_cert.pem", directory / "lan_key.pem"
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {"id": uuid.uuid4().hex}
            meta_file.write_text(json.dumps(meta), encoding="utf-8")
        if not cert_file.exists() or not key_file.exists():
            _write_certificate(cert_file, key_file, meta["id"])
        return cls(meta["id"], name or default_device_name(), cert_file, key_file)

    @property
    def fingerprint(self) -> str:
        return certificate_fingerprint(ssl.PEM_cert_to_DER_cert(self.cert_file.read_text(encoding="ascii")))

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.cert_file, self.key_file)
        return context


def default_device_name() -> str:
    name = socket.gethostname().split(".")[0]
    return name or "FTA device"


def certificate_fingerprint(der: bytes) -> str:
    """SHA-256 of the certificate, as lowercase hex: what a QR code or mDNS record carries."""
    return hashlib.sha256(der).hexdigest()


def short_fingerprint(fingerprint: str) -> str:
    """The first 8 hex digits in two groups, for people to compare by eye."""
    return f"{fingerprint[:4]} {fingerprint[4:8]}".upper()


def _write_certificate(cert_file: Path, key_file: Path, device_id: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"fta-{device_id}")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fp:
        fp.write(
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        )
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
