"""App shell: theme, adaptive navigation (rail on wide screens, bar on phones), dialogs."""

from __future__ import annotations

from collections.abc import Callable

import flet as ft

from fta.app.storage import Connection, Store, setup_logging
from fta.app.transfers import TransferQueue
from fta.core.journal import Journal
from fta.app.ui import safe_update
from fta.app.views.browser import BrowserView
from fta.app.views.connections import ConnectionsView
from fta.app.views.nearby import NearbyView, offer_text
from fta.app.views.settings import SettingsView
from fta.app.views.transfers import TransfersView
from fta.app.views.update import Updater

APP_TITLE = "File Transfer for All"
COMPACT_WIDTH = 720
"""Below this width the app uses the phone layout (Material 3 "compact" + "medium" classes)."""

NEEDS_PASSWORD = {"sftp", "smb", "ftp", "ftps", "dav", "davs", "http", "https"}

_THEME_MODES = {"system": ft.ThemeMode.SYSTEM, "light": ft.ThemeMode.LIGHT, "dark": ft.ThemeMode.DARK}

_SECTIONS = (
    ("Файлы", ft.Icons.FOLDER_OUTLINED, ft.Icons.FOLDER),
    ("Передачи", ft.Icons.SWAP_VERT, ft.Icons.SWAP_VERT),
    ("Рядом", ft.Icons.WIFI_TETHERING, ft.Icons.WIFI_TETHERING),
    ("Подключения", ft.Icons.DNS_OUTLINED, ft.Icons.DNS),
    ("Настройки", ft.Icons.SETTINGS_OUTLINED, ft.Icons.SETTINGS),
)


class App:
    def __init__(self, page: ft.Page, store: Store | None = None) -> None:
        self.page = page
        self.store = store or Store()
        self.log_path = setup_logging(self.store.data_dir, self.store.settings.log_level)
        self.journal = Journal(self.store.data_dir / "journal.jsonl")
        self.queue = TransferQueue(parallel=self.store.settings.parallel_transfers, journal=self.journal)
        self._asked_password: set[str] = set()
        self.index = 0
        self.server = None
        self.server_error = ""
        self.lan = None
        self.lan_error = ""

        page.title = APP_TITLE
        page.padding = 0
        page.on_resize = lambda e: self.layout()
        page.on_close = lambda e: self.shutdown()
        self.apply_theme()

        self.picker = ft.FilePicker()  # registers itself as a page service
        self.updater = Updater(self)
        self.browser = BrowserView(self)
        self.transfers = TransfersView(self)
        self.nearby = NearbyView(self)
        self.connections = ConnectionsView(self)
        self.settings = SettingsView(self)
        self.views = [self.browser, self.transfers, self.nearby, self.connections, self.settings]

        # The active-transfers badge only renders on an Icon (not on the destination itself),
        # and the selected tab shows selected_icon, so the rail's and the bar's icons all carry it.
        self._badge_icons = [ft.Icon(ft.Icons.SWAP_VERT) for _ in range(4)]

        def icons(index: int, offset: int) -> dict[str, ft.IconData | ft.Icon]:
            _, icon, selected = _SECTIONS[index]
            if index != 1:
                return {"icon": icon, "selected_icon": selected}
            return {"icon": self._badge_icons[offset], "selected_icon": self._badge_icons[offset + 1]}

        self.rail = ft.NavigationRail(
            selected_index=0,
            label_type=ft.NavigationRailLabelType.ALL,
            group_alignment=-0.9,
            min_width=96,
            leading=ft.Container(
                ft.FloatingActionButton(
                    icon=ft.Icons.ADD, tooltip="Новое подключение", on_click=lambda e: self.connections.edit()
                ),
                padding=ft.Padding.only(top=8, bottom=16),
            ),
            destinations=[
                ft.NavigationRailDestination(label=label, **icons(index, 0))
                for index, (label, _, _) in enumerate(_SECTIONS)
            ],
            on_change=lambda e: self.go(e.control.selected_index),
        )
        self.bar = ft.NavigationBar(
            selected_index=0,
            destinations=[
                ft.NavigationBarDestination(label=label, **icons(index, 2))
                for index, (label, _, _) in enumerate(_SECTIONS)
            ],
            on_change=lambda e: self.go(e.control.selected_index),
        )
        # Everything stays mounted; layout() only flips visibility (see browser.py for why).
        self.content = ft.Container(ft.Column([v.view for v in self.views], expand=True), expand=True)
        page.navigation_bar = self.bar
        page.add(
            ft.SafeArea(
                ft.Row([self.rail, self.content], expand=True, spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH),
                expand=True,
            )
        )
        self.layout()
        if self.store.settings.check_updates:
            self.updater.check_in_background(silent=True)
        self.apply_server()
        self.apply_lan()

    # --- SSH/SFTP server --------------------------------------------------------------

    def apply_server(self) -> None:
        """Start or stop the SSH/SFTP server to match the settings; errors land in server_error."""
        from fta.server.ssh import server_from_store

        if self.server is not None:
            self.server.stop()
            self.server = None
        self.server_error = ""
        self.lan = None
        self.lan_error = ""
        if self.store.settings.server_enabled:
            server = server_from_store(self.store, self.queue, self.journal, peers=self.peer_names)
            try:
                server.start()
            except Exception as exc:  # port taken, no permission: shown in Settings
                self.server_error = str(exc) or type(exc).__name__
            else:
                self.server = server
        self.settings.refresh_server()
        if self.lan is not None:
            self.apply_lan()  # announce the new SSH port to nearby devices

    def shutdown(self) -> None:
        """Release what outlives the window: the server's and the receiver's ports."""
        if self.server is not None:
            self.server.stop()
            self.server = None
        if self.lan is not None:
            self.lan.stop()
            self.lan = None

    # --- local network ---------------------------------------------------------------

    def apply_lan(self) -> None:
        """Start or stop receiving and discovery to match the settings."""
        from fta.lan.service import LanService

        if self.lan is not None:
            self.lan.stop()
            self.lan = None
        self.lan_error = ""
        settings = self.store.settings
        if settings.lan_enabled:
            lan = LanService(
                self.store.data_dir,
                self.store.receive_dir,
                self.nearby.decide,
                name=settings.device_name,
                port=settings.lan_port,
                pin=settings.lan_pin,
                on_update=self.nearby.offer_changed,
                on_peers=self.nearby.refresh,
                ssh_port=lambda: self.server.port if self.server else None,
            )
            try:
                lan.start()
            except Exception as exc:  # port taken, no network: shown on the Nearby screen
                self.lan_error = str(exc) or type(exc).__name__
            else:
                self.lan = lan
        self.nearby.refresh()

    def received(self, offer) -> None:
        """Files from a nearby device have arrived: journal them and tell the user."""
        from fta.core.journal import JournalEntry

        for index, item in enumerate(offer.files):
            if not item.is_dir:
                self.journal.append(
                    JournalEntry(f"fta://{offer.sender_name}/{item.path}", str(offer.destinations[index]), item.size, "done")
                )
        folder = self.lan.receiver.directory if self.lan else None
        self.notify(
            f"Получено от {offer.sender_name}: {offer_text(offer)}",
            action="Открыть папку" if folder else None,
            on_action=(lambda e: _open_folder(folder)) if folder else None,
        )

    def peer_names(self) -> list[str]:
        """Devices on the local network, for the server shell's `peers`."""
        return self.lan.peer_names() if self.lan else []

    # --- layout and navigation ------------------------------------------------------

    @property
    def compact(self) -> bool:
        return (self.page.width or 1024) < COMPACT_WIDTH

    def layout(self) -> None:
        compact = self.compact
        self.browser.set_compact(compact)
        self.rail.visible = not compact
        self.bar.visible = compact
        self.content.padding = ft.Padding.symmetric(horizontal=16, vertical=12) if compact else ft.Padding.all(24)
        for index, view in enumerate(self.views):
            view.view.visible = index == self.index
        self.page.update()

    def go(self, index: int) -> None:
        self.index = self.rail.selected_index = self.bar.selected_index = index
        self.layout()

    def update_badge(self) -> None:
        active = self.queue.active_count
        badge = str(active) if active else None
        for icon in self._badge_icons:
            icon.badge = badge
        safe_update(*self._badge_icons)

    # --- theme --------------------------------------------------------------------------

    def apply_theme(self) -> None:
        settings = self.store.settings
        self.page.theme = ft.Theme(color_scheme_seed=settings.seed_color)
        self.page.dark_theme = ft.Theme(color_scheme_seed=settings.seed_color)
        self.page.theme_mode = _THEME_MODES.get(settings.theme_mode, ft.ThemeMode.SYSTEM)

    # --- cross-screen actions -----------------------------------------------------------

    def connections_changed(self) -> None:
        self.connections.refresh()
        self.browser.left.refresh_sources()
        self.browser.right.refresh_sources()
        self.page.update()

    def open_in_browser(self, connection: Connection) -> None:
        self.go(0)
        self.browser.open_connection(connection)

    def ensure_password(self, connection: Connection, then: Callable[[], None]) -> None:
        """Ask once per session for a password that was not saved, then continue."""
        needs = (
            connection.scheme in NEEDS_PASSWORD
            and connection.username
            and not connection.password
            and not connection.options.get("key_file")
            and connection.id not in self._asked_password
        )
        if not needs:
            then()
            return
        field = ft.TextField(label="Пароль", password=True, can_reveal_password=True, autofocus=True)

        def submit(_: ft.Event[ft.Control]) -> None:
            self._asked_password.add(connection.id)
            connection.password = field.value or ""
            self.page.pop_dialog()
            then()

        field.on_submit = submit
        hint = "Оставьте пустым для входа по SSH-ключу или агенту" if connection.scheme == "sftp" else ""
        self.page.show_dialog(
            ft.AlertDialog(
                icon=ft.Icon(ft.Icons.KEY_OUTLINED),
                title=ft.Text(connection.name),
                content=ft.Column(
                    [ft.Text(f"Пароль для {connection.username}@{connection.host}"), field]
                    + ([ft.Text(hint, theme_style=ft.TextThemeStyle.BODY_SMALL)] if hint else []),
                    tight=True,
                    spacing=12,
                ),
                actions=[ft.FilledButton("Подключиться", on_click=submit)],
            )
        )

    def notify(self, message: str, error: bool = False, action: str | None = None, on_action=None) -> None:
        snack = ft.SnackBar(
            ft.Text(message),
            action=action,
            on_action=on_action,
            show_close_icon=error,
            behavior=ft.SnackBarBehavior.FLOATING,
        )
        try:
            self.page.show_dialog(snack)
        except (RuntimeError, AssertionError):
            pass


def _open_folder(folder) -> None:
    import subprocess
    import sys

    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        subprocess.Popen(["explorer", str(folder)])
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])


def main(page: ft.Page) -> None:
    App(page)


def run() -> None:
    ft.run(main)
