"""Terminal screen: an SSH session drawn from fta.terminal, like a small PuTTY inside the app.

Text is typed into an input line (that also brings up the phone keyboard and
takes pastes); keys that are not text (arrows, Tab, Esc, Ctrl+…) come from the
hardware keyboard or from the key bar under the screen.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import flet as ft

from fta.app.storage import Connection
from fta.app.ui import safe_update
from fta.core.hostkeys import HostKeyChanged, UnknownHostKey
from fta.terminal import KEYS, Run, SshTerminal, control

if TYPE_CHECKING:
    from fta.app.main import App

FONT = "FtaMono"
"""Bundled DejaVu Sans Mono (main.py registers it); system monospace fonts by name otherwise."""
FONT_FALLBACK = ["DejaVu Sans Mono", "Menlo", "Consolas", "Cascadia Mono", "Courier New", "monospace"]
FONT_SIZE = 14
CHAR_WIDTH = FONT_SIZE * 0.6
LINE_HEIGHT = FONT_SIZE * 1.25
PADDING = 12
SENTINEL = " "
"""The input line always holds one space, so Backspace on it is still a change we can see."""

BACKGROUND = "#1E1F22"
FOREGROUND = "#D8DAE0"
PALETTE = {
    "black": "#2E3036", "red": "#E06C75", "green": "#98C379", "brown": "#E5C07B", "yellow": "#E5C07B",
    "blue": "#61AFEF", "magenta": "#C678DD", "cyan": "#56B6C2", "white": "#D8DAE0",
    "brightblack": "#6B7080", "brightred": "#FF7A85", "brightgreen": "#B5E890", "brightbrown": "#FFD68A",
    "brightyellow": "#FFD68A", "brightblue": "#82C4FF", "brightmagenta": "#DDA0F0", "brightcyan": "#7FD8E3",
    "brightwhite": "#FFFFFF",
}  # fmt: skip

KEY_BAR = (
    ("Esc", KEYS["Escape"]),
    ("Tab", KEYS["Tab"]),
    ("Ctrl+C", control("c")),
    ("Ctrl+D", control("d")),
    ("Ctrl+Z", control("z")),
    ("↑", KEYS["Arrow Up"]),
    ("↓", KEYS["Arrow Down"]),
    ("←", KEYS["Arrow Left"]),
    ("→", KEYS["Arrow Right"]),
)


def _color(name: str, default: str) -> str:
    if name == "default":
        return default
    if len(name) == 6 and all(c in "0123456789abcdefABCDEF" for c in name):
        return f"#{name}"
    return PALETTE.get(name, default)


def _span(run: Run) -> ft.TextSpan:
    fg, bg = _color(run.fg, FOREGROUND), _color(run.bg, BACKGROUND)
    if run.cursor:
        fg, bg = BACKGROUND, FOREGROUND
    return ft.TextSpan(
        run.text or " ",
        style=ft.TextStyle(
            color=fg,
            bgcolor=None if bg == BACKGROUND else bg,
            weight=ft.FontWeight.BOLD if run.bold else None,
            decoration=ft.TextDecoration.UNDERLINE if run.underline else None,
        ),
    )


class TerminalView:
    def __init__(self, app: App) -> None:
        self.app = app
        self.session: SshTerminal | None = None
        self.connection: Connection | None = None
        self.return_to = 0
        self._dirty = threading.Event()
        self._size = (100, 30)
        self._typed = ""
        """What the input field holds after the sentinel, as already sent to the server."""

        self.title = ft.Text("Терминал", theme_style=ft.TextThemeStyle.TITLE_LARGE, expand=True, max_lines=1)
        self.status = ft.Text(theme_style=ft.TextThemeStyle.BODY_SMALL, color=ft.Colors.ON_SURFACE_VARIANT)
        self.screen = ft.Text(
            spans=[],
            selectable=True,
            no_wrap=True,
            style=ft.TextStyle(
                font_family=FONT, font_family_fallback=FONT_FALLBACK, size=FONT_SIZE, height=1.25, color=FOREGROUND
            ),
        )
        self.input = ft.TextField(
            value=SENTINEL,
            hint_text="Ввод — текст уходит в терминал по мере набора",
            prefix_icon=ft.Icons.KEYBOARD_OUTLINED,
            dense=True,
            border_radius=12,
            text_style=ft.TextStyle(font_family=FONT, font_family_fallback=FONT_FALLBACK),
            on_change=self._on_input,
            on_submit=self._on_submit,
            expand=True,
            autocorrect=False,
            enable_suggestions=False,
        )
        self.reconnect_button = ft.FilledTonalButton(
            "Переподключить", icon=ft.Icons.REFRESH, on_click=lambda e: self.reconnect(), visible=False
        )
        self.view = ft.Column(
            [
                ft.Row(
                    [
                        ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Назад", on_click=lambda e: self.app.go(self.return_to)),
                        ft.Column([self.title, self.status], spacing=0, expand=True, tight=True),
                        self.reconnect_button,
                        ft.IconButton(ft.Icons.FOLDER_OPEN_OUTLINED, tooltip="Файлы этого сервера (SFTP)", on_click=lambda e: self.open_files()),
                        ft.IconButton(ft.Icons.LINK_OFF, tooltip="Отключиться", on_click=lambda e: self.disconnect()),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Container(
                    content=ft.Column([self.screen], scroll=ft.ScrollMode.AUTO, horizontal_alignment=ft.CrossAxisAlignment.START),
                    bgcolor=BACKGROUND,
                    border_radius=16,
                    padding=PADDING,
                    expand=True,
                    on_click=lambda e: self._focus(),
                    on_size_change=self._on_size,
                    size_change_interval=150,
                ),
                ft.Row(
                    [
                        ft.OutlinedButton(label, on_click=lambda e, code=code: self._press(code))
                        for label, code in KEY_BAR
                    ],
                    scroll=ft.ScrollMode.AUTO,
                    spacing=6,
                ),
                ft.Row([self.input]),
            ],
            expand=True,
            spacing=12,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        threading.Thread(target=self._painter, name="fta-terminal-paint", daemon=True).start()

    # --- sessions ------------------------------------------------------------------------

    def open(self, connection: Connection) -> None:
        """Connect to an SSH server and show its shell."""
        self.disconnect()
        self.connection = connection
        self.return_to = self.app.index if self.app.index != self.app.views.index(self) else self.return_to
        self.app.go(self.app.views.index(self))
        self.reconnect()

    def reconnect(self) -> None:
        connection = self.connection
        if connection is None:
            return
        cols, rows = self._size
        self.session = SshTerminal(
            connection.host,
            connection.port or 22,
            connection.username,
            connection.password,
            connection.options.get("key_file", ""),
            proxy=connection.options.get("proxy", ""),
            cols=cols,
            rows=rows,
            on_change=self._dirty.set,
            on_close=self._on_close,
        )
        self.title.value = f"{connection.username}@{connection.host}" if connection.username else connection.host
        self._set_status(f"Подключение к {connection.host}:{connection.port or 22}…")
        self.reconnect_button.visible = False
        safe_update(self.view)
        threading.Thread(target=self._connect, args=(self.session,), daemon=True).start()

    def _connect(self, session: SshTerminal) -> None:
        try:
            session.connect()
        except (UnknownHostKey, HostKeyChanged) as exc:
            self._set_status("Ключ сервера не подтверждён", error=True)
            self.app.confirm_host_key(exc, then=self.reconnect)
            return
        except Exception as exc:  # wrong password, no route: shown in the header
            self._set_status(str(exc) or type(exc).__name__, error=True)
            self.reconnect_button.visible = True
            safe_update(self.reconnect_button)
            return
        if session is not self.session:  # replaced while connecting
            session.close()
            return
        self._set_status(f"{session.host}:{session.port} · {session.screen.columns}×{session.screen.lines}")
        self._dirty.set()
        self._focus()

    def _on_close(self, reason: str) -> None:
        self._set_status(f"Отключено: {reason}")
        self.reconnect_button.visible = True
        safe_update(self.reconnect_button)
        self._dirty.set()

    def disconnect(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    def open_files(self) -> None:
        """The same server in the file browser, over SFTP with the same login."""
        if self.connection is None:
            return
        self.app.open_in_browser(self.app.store.add_temporary(self.connection), refresh=True)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status.value = text
        self.status.color = ft.Colors.ERROR if error else ft.Colors.ON_SURFACE_VARIANT
        safe_update(self.status)

    # --- drawing -----------------------------------------------------------------------

    def _painter(self) -> None:
        """Redraw at most 25 times a second however fast output arrives."""
        while True:
            self._dirty.wait()
            self._dirty.clear()
            session = self.session
            if session is not None:
                spans: list[ft.TextSpan] = []
                for index, row in enumerate(session.rows()):
                    spans.extend(_span(run) for run in row)
                    if index < session.screen.lines - 1:
                        spans.append(ft.TextSpan("\n"))
                self.screen.spans = spans
                if session.title and session.title != self.title.value:
                    self.title.value = session.title
                safe_update(self.screen, self.title)
            time.sleep(0.04)

    def _on_size(self, e: ft.LayoutSizeChangeEvent) -> None:
        cols = int((e.width - 2 * PADDING) // CHAR_WIDTH)
        rows = int((e.height - 2 * PADDING) // LINE_HEIGHT)
        self._size = (max(cols, 20), max(rows, 5))
        if self.session is not None:
            self.session.resize(*self._size)

    # --- input -------------------------------------------------------------------------

    def _send(self, data: str) -> None:
        if self.session is not None:
            self.session.send(data)

    def _press(self, code: str) -> None:
        self._send(code)
        self._reset_input()
        self._focus()

    def _on_input(self, e: ft.Event[ft.TextField]) -> None:
        """Send what changed since the last value seen. The field is only cleared on Enter
        and after cursor keys: clearing it on every keystroke races with fast typing."""
        value = e.control.value or ""
        if not value.startswith(SENTINEL):
            if not value:  # Backspace ate the sentinel: the line was empty
                self._send(KEYS["Backspace"])
                self._reset_input()
                return
            # Focusing selects the field's text, so the first key replaces everything in it.
            self._send(KEYS["Backspace"] * len(self._typed) + value.replace("\n", "\r"))
            self._typed = value
            self.input.value = SENTINEL + value
            safe_update(self.input)
            return
        before, after = self._typed, value[len(SENTINEL):]
        common = 0
        while common < min(len(before), len(after)) and before[common] == after[common]:
            common += 1
        self._send(KEYS["Backspace"] * (len(before) - common) + after[common:].replace("\n", "\r"))
        self._typed = after

    def _reset_input(self) -> None:
        self._typed = ""
        self.input.value = SENTINEL
        safe_update(self.input)

    def _on_submit(self, e: ft.Event[ft.TextField]) -> None:
        self._send(KEYS["Enter"])
        self._reset_input()
        self._focus()

    def on_keyboard(self, e: ft.KeyboardEvent) -> None:
        """Keys the input line cannot carry; text, Enter and Backspace come through it."""
        if self.session is None:
            return
        key = e.key
        if e.ctrl and not e.alt and len(key) == 1 and key.isalpha():
            if key.upper() != "V":  # Ctrl+V pastes into the input line
                self._send(control(key))
                self._reset_input()
            return
        if key in ("Enter", "Backspace") or len(key) == 1 or key == " ":
            return
        if key in KEYS:
            self._send(("\x1b" if e.alt else "") + KEYS[key])
            self._reset_input()  # the remote line changed in ways the input field cannot mirror
            if key == "Tab":
                self._focus()  # Tab would otherwise move focus off the input line

    def _focus(self) -> None:
        async def focus() -> None:
            await self.input.focus()

        try:
            self.app.page.run_task(focus)
        except (RuntimeError, AssertionError):
            pass
