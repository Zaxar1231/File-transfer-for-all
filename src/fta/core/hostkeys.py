"""SSH host keys, trusted on first use like PuTTY and OpenSSH do.

An unknown server raises ``UnknownHostKey`` carrying its fingerprint, so the
caller can show it and, once the user agrees, ``trust()`` it. A key that differs
from the remembered one raises ``HostKeyChanged``: that is either a reinstalled
server or someone in the middle, and only the user can tell which.
"""

from __future__ import annotations

import base64
import hashlib
import threading
from pathlib import Path
from typing import Any

from fta.core.errors import AccessDenied

DEFAULT_PATH: Path | None = None
"""The app's own known_hosts file; set by the app and the CLI at start-up."""

_lock = threading.Lock()


class UnknownHostKey(AccessDenied):
    def __init__(self, host: str, port: int, key: Any) -> None:
        self.host, self.port, self.key = host, port, key
        self.key_type = key.get_name()
        self.fingerprint = fingerprint(key)
        super().__init__(f"{host}:{port}: unknown host key {self.key_type} {self.fingerprint}")


class HostKeyChanged(AccessDenied):
    def __init__(self, host: str, port: int, key: Any) -> None:
        self.host, self.port, self.key = host, port, key
        self.key_type = key.get_name()
        self.fingerprint = fingerprint(key)
        super().__init__(
            f"{host}:{port}: the host key CHANGED, now {self.key_type} {self.fingerprint}. "
            "If the server was not reinstalled, someone may be intercepting the connection."
        )


def fingerprint(key: Any) -> str:
    """``SHA256:…`` exactly as PuTTY, OpenSSH and our own server print it."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def entry_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def prepare(client: Any, known_hosts: Path | None = None) -> None:
    """Load known keys into a paramiko SSHClient and make unknown ones raise UnknownHostKey."""
    import paramiko

    client.load_system_host_keys()
    path = known_hosts or DEFAULT_PATH
    if path is not None and path.exists():
        client.load_host_keys(str(path))

    class AskUser(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
            host, port = _split(hostname)
            raise UnknownHostKey(host, port, key)

    client.set_missing_host_key_policy(AskUser())


def connect(client: Any, host: str, port: int, known_hosts: Path | None = None, **kwargs: Any) -> None:
    """``client.connect`` with host keys checked; a changed key raises HostKeyChanged."""
    import paramiko

    prepare(client, known_hosts)
    try:
        client.connect(host, port=port, **kwargs)
    except paramiko.BadHostKeyException as exc:
        raise HostKeyChanged(host, port, exc.key) from exc


def trust(host: str, port: int, key: Any, known_hosts: Path | None = None) -> None:
    """Remember ``key`` for ``host:port``, replacing a previous key."""
    import paramiko

    path = known_hosts or DEFAULT_PATH
    if path is None:
        raise ValueError("no known_hosts file configured")
    with _lock:
        keys = paramiko.HostKeys()
        if path.exists():
            keys.load(str(path))
        name = entry_name(host, port)
        if name in keys:
            del keys[name]
        keys.add(name, key.get_name(), key)
        path.parent.mkdir(parents=True, exist_ok=True)
        keys.save(str(path))


def _split(hostname: str) -> tuple[str, int]:
    if hostname.startswith("[") and "]:" in hostname:
        host, port = hostname[1:].split("]:", 1)
        return host, int(port)
    return hostname, 22
