"""An SSH terminal session: paramiko carries the bytes, pyte turns them into a screen.

pyte is a VT100/xterm emulator, so full-screen programs (top, nano, mc, vim)
draw correctly, with colours. ``rows()`` returns the screen as runs of
same-styled text that any UI can paint; nothing here depends on the UI.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fta.core import hostkeys
from fta.core.errors import TransferError

logger = logging.getLogger(__name__)

TERM = "xterm-256color"


@dataclass(frozen=True)
class Run:
    """A stretch of characters on one line sharing one style."""

    text: str
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    underline: bool = False
    cursor: bool = False


# Keys a UI cannot type as text, as the bytes an xterm sends for them.
KEYS: dict[str, str] = {
    "Enter": "\r",
    "Backspace": "\x7f",
    "Tab": "\t",
    "Escape": "\x1b",
    "Arrow Up": "\x1b[A",
    "Arrow Down": "\x1b[B",
    "Arrow Right": "\x1b[C",
    "Arrow Left": "\x1b[D",
    "Home": "\x1b[H",
    "End": "\x1b[F",
    "Page Up": "\x1b[5~",
    "Page Down": "\x1b[6~",
    "Insert": "\x1b[2~",
    "Delete": "\x1b[3~",
    **{f"F{n}": code for n, code in zip(range(1, 13), ["\x1bOP", "\x1bOQ", "\x1bOR", "\x1bOS", "\x1b[15~", "\x1b[17~", "\x1b[18~", "\x1b[19~", "\x1b[20~", "\x1b[21~", "\x1b[23~", "\x1b[24~"])},
}


def control(letter: str) -> str:
    """Ctrl+letter: Ctrl+C is ``\\x03``, Ctrl+D ``\\x04``."""
    return chr(ord(letter.upper()) - 64) if letter.isalpha() and len(letter) == 1 else ""


class SshTerminal:
    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str = "",
        password: str = "",
        key_file: str = "",
        *,
        cols: int = 100,
        rows: int = 30,
        known_hosts: Path | None = None,
        proxy: str = "",
        on_change: Callable[[], None] | None = None,
        on_close: Callable[[str], None] | None = None,
    ) -> None:
        import pyte

        self.host, self.port, self.username = host, port, username
        self._password, self._key_file = password, key_file
        self.known_hosts = known_hosts
        self.proxy = proxy
        self.on_change = on_change or (lambda: None)
        self.on_close = on_close or (lambda reason: None)
        self._replies: list[bytes] = []
        replies = self._replies

        class Screen(pyte.Screen):
            def write_process_input(self, data: str) -> None:
                replies.append(data.encode())  # answers to "where is the cursor?" and the like

        self.screen = Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.title = f"{username}@{host}" if username else host
        self._lock = threading.Lock()
        self._client: Any = None
        self._channel: Any = None
        self._reader: threading.Thread | None = None
        self.closed = False

    @property
    def connected(self) -> bool:
        return self._channel is not None and not self.closed

    def connect(self, timeout: float = 20) -> None:
        """Log in and open a shell; raises UnknownHostKey for a server seen the first time."""
        import paramiko

        client = paramiko.SSHClient()
        extra = {}
        if self.proxy:  # Tor or another SOCKS proxy
            from fta.core.proxy import Proxy

            extra["sock"] = Proxy.parse(self.proxy).connect(self.host, self.port, timeout)
        try:
            hostkeys.connect(
                client,
                self.host,
                self.port,
                self.known_hosts,
                username=self.username or None,
                password=self._password or None,
                key_filename=self._key_file or None,
                timeout=timeout,
                look_for_keys=not self._password,
                allow_agent=not self._password,
                **extra,
            )
            channel = client.invoke_shell(term=TERM, width=self.screen.columns, height=self.screen.lines)
        except (hostkeys.UnknownHostKey, hostkeys.HostKeyChanged):
            client.close()
            raise
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            raise TransferError(f"ssh {self.host}:{self.port}: {exc}") from exc
        self._client, self._channel = client, channel
        self._reader = threading.Thread(target=self._read, name="fta-terminal", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        reason = "сеанс завершён"
        try:
            while True:
                data = self._channel.recv(65536)
                if not data:
                    break
                with self._lock:
                    self.stream.feed(data)
                    if self.screen.title:
                        self.title = self.screen.title
                    replies = self._pending_replies()
                for reply in replies:  # e.g. the cursor position some programs ask for
                    self.send(reply)
                self.on_change()
        except (OSError, EOFError) as exc:
            reason = f"соединение потеряно: {exc or type(exc).__name__}"
        finally:
            self.closed = True
            if self._client is not None:
                self._client.close()
            self.on_close(reason)

    def _pending_replies(self) -> list[bytes]:
        replies = list(self._replies)
        self._replies.clear()
        return replies

    def send(self, data: str | bytes) -> None:
        if not self.connected or not data:
            return
        try:
            self._channel.sendall(data.encode() if isinstance(data, str) else data)
        except (OSError, EOFError):
            logger.debug("terminal write after disconnect", exc_info=True)

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(cols, 20), max(rows, 5)
        if (cols, rows) == (self.screen.columns, self.screen.lines):
            return
        with self._lock:
            self.screen.resize(rows, cols)
        if self.connected:
            try:
                self._channel.resize_pty(width=cols, height=rows)
            except (OSError, EOFError):
                pass
        self.on_change()

    def close(self) -> None:
        """Safe at any time, also after the server or the reader thread already hung up."""
        self.closed = True
        for resource in (self._channel, self._client):
            if resource is not None:
                try:
                    resource.close()
                except Exception:  # noqa: BLE001 - EOFError, socket errors: it is closed either way
                    logger.debug("closing a terminal that is already gone", exc_info=True)

    # --- screen ------------------------------------------------------------------------

    def text(self) -> str:
        """The screen as plain text, trailing spaces trimmed (for tests and copying)."""
        with self._lock:
            return "\n".join(line.rstrip() for line in self.screen.display)

    def rows(self) -> list[list[Run]]:
        with self._lock:
            screen = self.screen
            cursor = (screen.cursor.x, screen.cursor.y) if not screen.cursor.hidden else (-1, -1)
            result: list[list[Run]] = []
            for y in range(screen.lines):
                line = screen.buffer[y]
                runs: list[Run] = []
                current: Run | None = None
                chars: list[str] = []
                for x in range(screen.columns):
                    char = line[x]
                    fg, bg = (char.bg, char.fg) if char.reverse else (char.fg, char.bg)
                    style = Run("", fg, bg, char.bold, char.underscore, (x, y) == cursor)
                    if current is None or style != current:
                        if current is not None:
                            runs.append(_with_text(current, chars))
                        current, chars = style, []
                    chars.append(char.data or " ")
                if current is not None:
                    runs.append(_with_text(current, chars))
                result.append(_trim(runs))
            return result


def _with_text(style: Run, chars: list[str]) -> Run:
    return Run("".join(chars), style.fg, style.bg, style.bold, style.underline, style.cursor)


def _trim(runs: list[Run]) -> list[Run]:
    """Drop trailing unstyled blanks so lines stay short."""
    while runs and runs[-1].bg == "default" and not runs[-1].cursor and not runs[-1].text.strip():
        runs.pop()
    if runs and runs[-1].bg == "default" and not runs[-1].cursor:
        last = runs[-1]
        runs[-1] = Run(last.text.rstrip(), last.fg, last.bg, last.bold, last.underline)
    return runs
