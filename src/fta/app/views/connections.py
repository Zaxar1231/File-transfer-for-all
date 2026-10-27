"""Connections screen: saved servers and the dialog to add or edit one."""

from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from fta.app.storage import DEFAULT_PORTS, PROTOCOLS, Connection
from fta.app.ui import empty_state, headline, protocol_icon
from fta.core.registry import open_transport

if TYPE_CHECKING:
    from fta.app.main import App


class ConnectionsView:
    def __init__(self, app: App) -> None:
        self.app = app
        self.list = ft.ListView(expand=True, spacing=8)
        self.empty = empty_state(
            ft.Icons.DNS_OUTLINED,
            "Нет сохранённых подключений",
            "Добавьте сервер SFTP, FTP, WebDAV или бакет S3, чтобы работать с ним на вкладке «Файлы»",
            ft.FilledButton("Добавить подключение", icon=ft.Icons.ADD, on_click=lambda e: self.edit()),
        )
        self.view = ft.Column(
            [
                headline("Подключения", ft.FilledButton("Добавить", icon=ft.Icons.ADD, on_click=lambda e: self.edit())),
                self.list,
                self.empty,
            ],
            expand=True,
            spacing=16,
        )
        self.refresh()

    def refresh(self) -> None:
        connections = self.app.store.connections
        self.list.controls = [self._card(c) for c in connections]
        self.list.visible = bool(connections)
        self.empty.visible = not connections

    def _card(self, connection: Connection) -> ft.Card:
        return ft.Card(
            variant=ft.CardVariant.OUTLINED,
            content=ft.ListTile(
                leading=ft.CircleAvatar(
                    content=ft.Icon(protocol_icon(connection.scheme)),
                    bgcolor=ft.Colors.PRIMARY_CONTAINER,
                    color=ft.Colors.ON_PRIMARY_CONTAINER,
                ),
                title=ft.Text(connection.name, theme_style=ft.TextThemeStyle.TITLE_MEDIUM),
                subtitle=ft.Text(
                    f"{connection.label} · {connection.url()}",
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                trailing=ft.PopupMenuButton(
                    icon=ft.Icons.MORE_VERT,
                    items=[
                        ft.PopupMenuItem(
                            content="Открыть в файлах",
                            icon=ft.Icons.FOLDER_OPEN_OUTLINED,
                            on_click=lambda e, c=connection: self.app.open_in_browser(c),
                        ),
                        ft.PopupMenuItem(
                            content="Изменить", icon=ft.Icons.EDIT_OUTLINED, on_click=lambda e, c=connection: self.edit(c)
                        ),
                        ft.PopupMenuItem(
                            content="Удалить", icon=ft.Icons.DELETE_OUTLINE, on_click=lambda e, c=connection: self._remove(c)
                        ),
                    ],
                ),
                on_click=lambda e, c=connection: self.app.open_in_browser(c),
                content_padding=ft.Padding.symmetric(horizontal=16, vertical=4),
            ),
        )

    def edit(self, connection: Connection | None = None) -> None:
        ConnectionDialog(self.app, connection, on_saved=self._saved).show()

    def _saved(self, connection: Connection) -> None:
        self.app.store.upsert(connection)
        self.app.connections_changed()

    def _remove(self, connection: Connection) -> None:
        def remove(_: ft.Event[ft.Control]) -> None:
            self.app.page.pop_dialog()
            self.app.store.remove(connection.id)
            self.app.connections_changed()

        self.app.page.show_dialog(
            ft.AlertDialog(
                title=ft.Text(f"Удалить «{connection.name}»?"),
                content=ft.Text("Файлы на сервере не затрагиваются, удаляется только запись о подключении."),
                actions=[
                    ft.TextButton("Отмена", on_click=lambda _: self.app.page.pop_dialog()),
                    ft.FilledButton("Удалить", on_click=remove),
                ],
            )
        )


class ConnectionDialog:
    def __init__(self, app: App, connection: Connection | None, on_saved) -> None:
        self.app = app
        self.original = connection
        self.on_saved = on_saved
        c = connection or Connection(name="", scheme="sftp", host="")

        self.name = ft.TextField(label="Название", value=c.name, autofocus=True)
        self.scheme = ft.Dropdown(
            label="Протокол",
            value=c.scheme,
            options=[ft.DropdownOption(key=key, text=text, leading_icon=protocol_icon(key)) for key, text in PROTOCOLS.items()],
            on_select=lambda e: self._adapt(),
            expand=True,
        )
        self.host = ft.TextField(label="Сервер", value=c.host, expand=True)
        self.port = ft.TextField(
            label="Порт",
            value=str(c.port) if c.port else "",
            width=110,
            keyboard_type=ft.KeyboardType.NUMBER,
            input_filter=ft.NumbersOnlyInputFilter(),
        )
        self.username = ft.TextField(label="Пользователь", value=c.username)
        self.password = ft.TextField(label="Пароль", value=c.password, password=True, can_reveal_password=True)
        self.save_password = ft.Checkbox(label="Запомнить пароль на этом устройстве", value=c.save_password)
        self.path = ft.TextField(label="Начальная папка", value=c.path or "/")
        self.key_file = ft.TextField(
            label="Файл ключа SSH", helper="Необязательно", value=c.options.get("key_file", "")
        )
        self.accept_host = ft.Switch(
            label="Доверять новому ключу сервера", value=c.options.get("accept_unknown_host") == "true"
        )
        self.endpoint_url = ft.TextField(
            label="Endpoint URL (для MinIO, R2 и т. п.)", value=c.options.get("endpoint_url", "")
        )
        self.region = ft.TextField(label="Регион", value=c.options.get("region", ""))
        self.test_result = ft.Text("", theme_style=ft.TextThemeStyle.BODY_SMALL)
        self.testing = ft.ProgressRing(width=16, height=16, stroke_width=2, visible=False)

        self.dialog = ft.AlertDialog(
            title=ft.Text("Новое подключение" if connection is None else "Подключение"),
            content=ft.Column(
                [
                    self.name,
                    ft.Row([self.scheme]),
                    ft.Row([self.host, self.port]),
                    self.username,
                    self.password,
                    self.save_password,
                    self.path,
                    self.key_file,
                    self.accept_host,
                    self.endpoint_url,
                    self.region,
                    ft.Row([self.testing, self.test_result], spacing=8),
                ],
                tight=True,
                spacing=12,
                width=440,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                scroll=ft.ScrollMode.AUTO,
            ),
            actions=[
                ft.TextButton("Проверить", icon=ft.Icons.CABLE, on_click=self._test),
                ft.TextButton("Отмена", on_click=lambda e: self.app.page.pop_dialog()),
                ft.FilledButton("Сохранить", on_click=self._save),
            ],
            scrollable=True,
        )
        self._adapt()

    def show(self) -> None:
        self.app.page.show_dialog(self.dialog)

    def _adapt(self) -> None:
        """Show the fields that make sense for the chosen protocol."""
        scheme = self.scheme.value or "sftp"
        s3 = scheme == "s3"
        self.host.label = "Бакет" if s3 else "Сервер"
        self.port.visible = not s3
        self.port.hint_text = str(DEFAULT_PORTS.get(scheme, ""))
        self.username.label = "Access key ID (необязательно)" if s3 else "Пользователь"
        self.password.label = "Secret access key" if s3 else "Пароль"
        self.key_file.visible = self.accept_host.visible = scheme == "sftp"
        self.endpoint_url.visible = self.region.visible = s3
        try:
            self.dialog.update()
        except (RuntimeError, AssertionError):
            pass

    def _build(self) -> Connection | None:
        errors = False
        for field in (self.name, self.host):
            field.error = None if (field.value or "").strip() else "Обязательное поле"
            errors |= field.error is not None
        if errors:
            self.dialog.update()
            return None
        scheme = self.scheme.value or "sftp"
        options = {}
        if scheme == "sftp":
            if self.key_file.value:
                options["key_file"] = self.key_file.value.strip()
            if self.accept_host.value:
                options["accept_unknown_host"] = "true"
        if scheme == "s3":
            for key, field in (("endpoint_url", self.endpoint_url), ("region", self.region)):
                if field.value:
                    options[key] = field.value.strip()
        path = (self.path.value or "/").strip()
        connection = Connection(
            name=(self.name.value or "").strip(),
            scheme=scheme,
            host=(self.host.value or "").strip(),
            port=int(self.port.value) if self.port.value and not scheme == "s3" else None,
            username=(self.username.value or "").strip(),
            password=self.password.value or "",
            save_password=bool(self.save_password.value),
            path=path if path.startswith("/") else f"/{path}",
            options=options,
        )
        if self.original:
            connection.id = self.original.id
        return connection

    def _save(self, e: ft.Event[ft.FilledButton]) -> None:
        connection = self._build()
        if connection:
            self.app.page.pop_dialog()
            self.on_saved(connection)

    def _test(self, e: ft.Event[ft.TextButton]) -> None:
        connection = self._build()
        if not connection:
            return
        self.testing.visible = True
        self.test_result.value = "Подключаюсь…"
        self.test_result.color = ft.Colors.ON_SURFACE_VARIANT
        self.dialog.update()

        def worker() -> None:
            try:
                with open_transport(connection.url(), **connection.transport_options()) as transport:
                    transport.stat(connection.path)
            except Exception as exc:  # shown in the dialog
                self.test_result.value = f"Не удалось: {exc}"
                self.test_result.color = ft.Colors.ERROR
            else:
                self.test_result.value = "Подключение работает"
                self.test_result.color = ft.Colors.PRIMARY
            self.testing.visible = False
            try:
                self.dialog.update()
            except (RuntimeError, AssertionError):
                pass  # the dialog was closed meanwhile

        self.app.page.run_thread(worker)
