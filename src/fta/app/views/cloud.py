"""Cloud drives dialog: sign in to Google Drive, Dropbox, Яндекс.Диск… through rclone."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import flet as ft

from fta.app.storage import Connection
from fta.app.ui import safe_update
from fta.tools import rclone

if TYPE_CHECKING:
    from fta.app.main import App


class CloudDialog:
    def __init__(self, app: App) -> None:
        self.app = app
        self.provider = ft.Dropdown(
            label="Облако",
            value="drive",
            options=[ft.DropdownOption(key=key, text=text) for key, text in rclone.PROVIDERS.items()],
            on_select=lambda e: self._adapt(),
            expand=True,
        )
        self.name = ft.TextField(label="Название", hint_text="например, Рабочий диск")
        self.url = ft.TextField(label="Адрес WebDAV", hint_text="https://cloud.example.com/remote.php/dav/files/me")
        self.user = ft.TextField(label="Логин")
        self.password = ft.TextField(label="Пароль (для Mail.ru — пароль приложения)", password=True, can_reveal_password=True)
        self.hint = ft.Text(theme_style=ft.TextThemeStyle.BODY_SMALL, color=ft.Colors.ON_SURFACE_VARIANT)
        self.status = ft.Text(theme_style=ft.TextThemeStyle.BODY_SMALL)
        self.busy = ft.ProgressRing(width=16, height=16, stroke_width=2, visible=False)
        self.install_button = ft.FilledTonalButton("Установить rclone", icon=ft.Icons.DOWNLOAD, on_click=self._install)
        self.connect_button = ft.FilledButton("Войти", icon=ft.Icons.LOGIN, on_click=self._connect)
        self.existing = ft.Column(spacing=4)
        self.dialog = ft.AlertDialog(
            icon=ft.Icon(ft.Icons.CLOUD_OUTLINED),
            title=ft.Text("Подключить облако"),
            content=ft.Column(
                [
                    ft.Row([self.provider]),
                    self.name,
                    self.url,
                    self.user,
                    self.password,
                    self.hint,
                    ft.Row([self.busy, self.status], spacing=8),
                    self.existing,
                ],
                tight=True,
                spacing=12,
                width=460,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                scroll=ft.ScrollMode.AUTO,
            ),
            actions=[
                ft.TextButton("Закрыть", on_click=lambda e: self.app.page.pop_dialog()),
                self.install_button,
                self.connect_button,
            ],
            scrollable=True,
        )
        self._adapt()

    def show(self) -> None:
        self.app.page.show_dialog(self.dialog)
        self.app.page.run_thread(self._load_existing)

    def _adapt(self) -> None:
        kind = self.provider.value or "drive"
        installed = rclone.find() is not None
        oauth = kind in rclone.OAUTH
        self.url.visible = kind == "webdav"
        self.user.visible = self.password.visible = not oauth
        self.install_button.visible = not installed
        self.connect_button.disabled = not installed
        self.connect_button.content = "Войти через браузер" if oauth else "Подключить"
        if not installed:
            self.hint.value = (
                "Облака работают через rclone — свободную программу для 70+ хранилищ. "
                "Приложение скачает официальную сборку и проверит её SHA-256."
            )
        elif oauth:
            self.hint.value = (
                f"Откроется браузер со входом в {rclone.PROVIDERS[kind]}. Разрешите доступ — "
                "приложение получит ключ, пароль вводится только на сайте сервиса."
            )
        else:
            self.hint.value = "Пароль хранится в настройках rclone в зашифрованном (obscured) виде."
        safe_update(self.dialog)

    def _set_status(self, text: str, error: bool = False, busy: bool = False) -> None:
        self.status.value = text
        self.status.color = ft.Colors.ERROR if error else ft.Colors.ON_SURFACE_VARIANT
        self.busy.visible = busy
        safe_update(self.status, self.busy)

    # --- installing rclone ------------------------------------------------------------

    def _install(self, e: ft.Event[ft.Control]) -> None:
        self.install_button.disabled = True
        self._set_status("Скачиваю rclone…", busy=True)

        def progress(done: int, total: int | None) -> None:
            if total:
                self._set_status(f"Скачиваю rclone… {done * 100 // total}%", busy=True)

        def worker() -> None:
            try:
                path = rclone.install(progress)
            except Exception as exc:  # shown in the dialog
                self._set_status(f"Не удалось установить: {exc}", error=True)
                self.install_button.disabled = False
            else:
                self._set_status(f"rclone {rclone.VERSION} установлен: {path}")
            self._adapt()
            self._load_existing()

        self.app.page.run_thread(worker)

    # --- adding a cloud ---------------------------------------------------------------

    def _connect(self, e: ft.Event[ft.Control]) -> None:
        kind = self.provider.value or "drive"
        label = (self.name.value or "").strip() or rclone.PROVIDERS[kind]
        remote = _remote_name(label, {r.name for r in rclone.remotes()})
        params: dict[str, str] = {}
        if kind not in rclone.OAUTH:
            if not self.user.value or not self.password.value:
                self._set_status("Укажите логин и пароль", error=True)
                return
            params = {"user": self.user.value.strip(), "pass": self.password.value}
            if kind == "webdav":
                params.update(url=(self.url.value or "").strip(), vendor="other")
        self.connect_button.disabled = True
        self._set_status(
            "Ждём входа в браузере…" if kind in rclone.OAUTH else "Подключаюсь…", busy=True
        )

        def worker() -> None:
            try:
                rclone.create_remote(remote, kind, params)
            except Exception as exc:  # cancelled sign-in, wrong password
                self._set_status(f"Не получилось: {exc}", error=True)
                self.connect_button.disabled = False
                safe_update(self.connect_button)
                return
            self._add_connection(label, remote, kind)

        self.app.page.run_thread(worker)

    def _add_connection(self, label: str, remote: str, kind: str) -> None:
        connection = Connection(name=label, scheme="rclone", host=remote, path="/", options={"provider": kind})
        self.app.store.upsert(connection)
        self.app.connections_changed()
        try:
            self.app.page.pop_dialog()
        except (RuntimeError, AssertionError):
            pass
        self.app.notify(f"{label} подключено", action="Открыть", on_action=lambda e: self.app.open_in_browser(connection))

    def _load_existing(self) -> None:
        """Clouds already set up in rclone (the user's own config) that are not connections yet."""
        known = {c.host for c in self.app.store.connections if c.scheme == "rclone"}
        remotes = [r for r in rclone.remotes() if r.name not in known]
        self.existing.controls = (
            [ft.Text("Уже настроены в rclone:", theme_style=ft.TextThemeStyle.LABEL_LARGE)]
            + [
                ft.ListTile(
                    leading=ft.Icon(ft.Icons.CLOUD_DONE_OUTLINED),
                    title=ft.Text(r.name),
                    subtitle=ft.Text(r.label),
                    trailing=ft.TextButton("Добавить", on_click=lambda e, r=r: self._add_connection(r.name, r.name, r.type)),
                    dense=True,
                )
                for r in remotes
            ]
            if remotes
            else []
        )
        safe_update(self.existing)


def _remote_name(label: str, taken: set[str]) -> str:
    """A config section name from what the user typed: letters, digits, dashes."""
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-").lower() or "cloud"
    name, number = base, 1
    while name in taken:
        number += 1
        name = f"{base}-{number}"
    return name
