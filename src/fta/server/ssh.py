"""SSH server with an SFTP subsystem and the transfer shell, so PuTTY, psftp,
WinSCP, FileZilla and OpenSSH can connect to the app.

The server runs its own asyncio loop on a background thread; ``start()`` returns
once it listens and ``stop()`` shuts it down from that loop's thread.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncssh

from fta.server.passwords import check_password
from fta.server.shell import Shell, ShellContext

logger = logging.getLogger(__name__)


@dataclass
class SshServerConfig:
    root: Path
    """The shared folder: SFTP is chrooted to it and the shell cannot leave it."""
    host_key: Path
    """Created (ed25519) on first start and reused, so clients see a stable fingerprint."""
    port: int = 2222
    host: str = "0.0.0.0"
    users: dict[str, str] = field(default_factory=dict)
    """User name → password hash from ``hash_password``. Empty: password login is off."""
    authorized_keys: Path | None = None
    """OpenSSH authorized_keys file; PuTTYgen shows the line to paste into it."""


class SshServer:
    def __init__(self, config: SshServerConfig, shell_context: Callable[[], ShellContext]) -> None:
        self.config = config
        self.shell_context = shell_context
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._acceptor: Any = None
        self.port = config.port

    @property
    def running(self) -> bool:
        return self._acceptor is not None

    @property
    def fingerprint(self) -> str:
        """SHA256:... as PuTTY and OpenSSH print it when they first connect."""
        return self._host_key().get_fingerprint("sha256")

    def addresses(self) -> list[str]:
        """Addresses other devices can use; the loopback one only when nothing else exists."""
        found: list[str] = []
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 9))  # no packet is sent; picks the outgoing interface
                found.append(probe.getsockname()[0])
        except OSError:
            pass
        return found or ["127.0.0.1"]

    def start(self) -> None:
        if self.running:
            return
        self.config.root.mkdir(parents=True, exist_ok=True)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="fta-ssh", daemon=True)
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._listen(), self._loop)
        try:
            self._acceptor = future.result(15)
        except Exception:
            self._shutdown_loop()
            raise
        self.port = self._acceptor.get_port()
        logger.info("SSH server on port %s, root %s, %s", self.port, self.config.root, self.fingerprint)

    def stop(self) -> None:
        if self._loop is None:
            return

        async def close() -> None:
            if self._acceptor is not None:
                self._acceptor.close()
                await self._acceptor.wait_closed()

        try:
            asyncio.run_coroutine_threadsafe(close(), self._loop).result(10)
        finally:
            self._acceptor = None
            self._shutdown_loop()

    def _shutdown_loop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(5)
        self._loop = self._thread = None

    def _host_key(self) -> asyncssh.SSHKey:
        path = self.config.host_key
        if path.exists():
            return asyncssh.read_private_key(str(path))
        key = asyncssh.generate_private_key("ssh-ed25519")
        path.parent.mkdir(parents=True, exist_ok=True)
        key.write_private_key(str(path))
        path.chmod(0o600)
        return key

    async def _listen(self) -> Any:
        users = self.config.users
        keys = self.config.authorized_keys
        options: dict[str, Any] = {}
        if keys is not None and keys.exists():
            options["authorized_client_keys"] = str(keys)

        class Server(asyncssh.SSHServer):
            def begin_auth(self, username: str) -> bool:
                return True  # always authenticate

            def password_auth_supported(self) -> bool:
                return bool(users)

            def validate_password(self, username: str, password: str) -> bool:
                stored = users.get(username)
                ok = stored is not None and check_password(password, stored)
                logger.info("SSH password login for %r: %s", username, "ok" if ok else "refused")
                return ok

        root = str(self.config.root.resolve())
        return await asyncssh.create_server(
            Server,
            self.config.host,
            self.config.port,
            server_host_keys=[self._host_key()],
            sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=root),
            process_factory=self._session,
            allow_scp=True,
            **options,
        )

    async def _session(self, process: asyncssh.SSHServerProcess) -> None:
        shell = Shell(self.shell_context())
        loop = asyncio.get_running_loop()
        if process.command:  # `plink host ls` or `ssh host queue`: one command, then exit
            process.stdout.write(await loop.run_in_executor(None, shell.run, process.command))
            process.exit(0)
            return
        process.stdout.write("Type help for commands.\n" + shell.prompt)
        while True:
            try:
                line = await process.stdin.readline()
            except asyncssh.TerminalSizeChanged:
                continue
            except (asyncssh.BreakReceived, asyncssh.SignalReceived, asyncssh.DisconnectError):
                break
            if not line:  # end of input
                break
            if line.strip().lower() in ("exit", "quit", "logout"):
                break
            process.stdout.write(await loop.run_in_executor(None, shell.run, line))
            process.stdout.write(shell.prompt)
        process.exit(0)


def server_from_store(store: Any, queue: Any, journal: Any, peers: Callable[[], list[str]] = list) -> SshServer:
    """The server as configured in the app's settings (``fta.app.storage.Store``)."""
    from fta.server.shell import ShellContext as _Context

    settings = store.settings
    users = {settings.server_user: settings.server_password_hash} if settings.server_password_hash else {}
    config = SshServerConfig(
        root=store.server_root,
        host_key=store.data_dir / "ssh_host_ed25519_key",
        port=settings.server_port,
        users=users,
        authorized_keys=store.data_dir / "authorized_keys",
    )
    return SshServer(
        config,
        lambda: _Context(
            root=store.server_root,
            queue=queue,
            journal=journal,
            resolve=store.resolve,
            peers=peers,
            retries=store.settings.retries,
        ),
    )
