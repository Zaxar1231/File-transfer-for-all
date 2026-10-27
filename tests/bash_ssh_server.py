"""An SSH server that gives each login a real bash in a pseudo-terminal, like sshd does, plus SFTP over the real filesystem.

Tests use it to drive the built-in terminal against a real shell; run it directly
(``python tests/bash_ssh_server.py PORT``) for a server to try the app against.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import struct
import sys
import termios
import threading

import asyncssh

PASSWORD = "terminal-pass"


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def _session(process: asyncssh.SSHServerProcess) -> None:
    cols, rows, *_ = process.get_terminal_size()
    master, slave = pty.openpty()
    _set_size(slave, cols or 80, rows or 24)
    env = {
        "TERM": process.get_terminal_type() or "xterm",
        "PS1": r"\u@test:\w$ ",
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
    }
    child = await asyncio.create_subprocess_exec(
        "bash", "--norc", "--noprofile", "-i", stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True
    )
    os.close(slave)
    loop = asyncio.get_running_loop()

    def pump_out() -> None:
        try:
            data = os.read(master, 65536)
        except OSError:
            data = b""
        if data:
            process.stdout.write(data)
        else:
            loop.remove_reader(master)

    loop.add_reader(master, pump_out)

    async def pump_in() -> None:
        while True:
            try:
                data = await process.stdin.read(65536)
            except asyncssh.TerminalSizeChanged as change:
                _set_size(master, change.width, change.height)
                continue
            if not data:
                return
            os.write(master, data)

    pump = asyncio.ensure_future(pump_in())
    try:
        await asyncio.wait([pump, asyncio.ensure_future(child.wait())], return_when=asyncio.FIRST_COMPLETED)
    except (asyncssh.BreakReceived, asyncssh.SignalReceived, OSError):
        pass
    finally:
        pump.cancel()
        loop.remove_reader(master)
        if child.returncode is None:
            child.kill()
        await child.wait()
        os.close(master)
        process.exit(child.returncode or 0)


class BashServer:
    def __init__(self, host_key_path: str, port: int = 0) -> None:
        self.host_key_path = host_key_path
        self.port = port
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._acceptor = None

    def start(self) -> None:
        if not os.path.exists(self.host_key_path):
            asyncssh.generate_private_key("ssh-ed25519").write_private_key(self.host_key_path)
        self._thread.start()

        class Server(asyncssh.SSHServer):
            def begin_auth(self, username: str) -> bool:
                return True

            def password_auth_supported(self) -> bool:
                return True

            def validate_password(self, username: str, password: str) -> bool:
                return password == PASSWORD

        async def listen():
            return await asyncssh.create_server(
                Server, "127.0.0.1", self.port, server_host_keys=[self.host_key_path], process_factory=_session,
                encoding=None, sftp_factory=True,
            )

        self._acceptor = asyncio.run_coroutine_threadsafe(listen(), self._loop).result(10)
        self.port = self._acceptor.get_port()

    @property
    def fingerprint(self) -> str:
        return asyncssh.read_private_key(self.host_key_path).get_fingerprint("sha256")

    def stop(self) -> None:
        async def close():
            self._acceptor.close()
            await self._acceptor.wait_closed()

        asyncio.run_coroutine_threadsafe(close(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)


if __name__ == "__main__":
    server = BashServer(sys.argv[2] if len(sys.argv) > 2 else "/tmp/fta-bash-host-key", int(sys.argv[1]))
    server.start()
    print(f"bash over SSH on 127.0.0.1:{server.port}, any user, password {PASSWORD!r}, key {server.fingerprint}")
    threading.Event().wait()
