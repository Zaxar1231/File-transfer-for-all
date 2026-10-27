"""Connections screen: saved servers and the dialog to add or edit one."""

from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from fta.app.storage import DEFAULT_PORTS, PROTOCOLS, Connection
from fta.app.ui import empty_state, headline, protocol_icon, safe_update
from fta.core.hostkeys import HostKeyChanged, UnknownHostKey
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
            "Добавьте сервер SFTP, FTP, WebDAV или бакет S3, чтобы работать с ним на вкладке «Файлы», "
            "или подключитесь сразу по IP-адресу",
            ft.Row(
                [
                    ft.FilledButton("Добавить подключение", icon=ft.Icons.ADD, on_click=lambda e: self.edit()),
                    ft.FilledTonalButton("По IP", icon=ft.Icons.LAN_OUTLINED, on_click=lambda e: self.app.quick_connect()),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                wrap=True,
            ),
        )
        self.view = ft.Column(
            [
                headline(
                    "Подключения",
                    ft.FilledTonalButton("По IP", icon=ft.Icons.LAN_OUTLINED, on_click=lambda e: self.app.quick_connect()),
                    ft.FilledButton("Добавить", icon=ft.Icons.ADD, on_click=lambda e: self.edit()),
                ),
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
                        *(
                            [
                                ft.PopupMenuItem(
                                    content="Терминал",
                                    icon=ft.Icons.TERMINAL,
                                    on_click=lambda e, c=connection: self.app.open_terminal(c),
                                )
                            ]
                            if connection.scheme == "sftp"
                            else []
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
            label="Доверять новому ключу сервера без вопроса", value=c.options.get("accept_unknown_host") == "true"
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
        smb = scheme == "smb"
        self.path.label = "Общий ресурс и папка" if smb else "Начальная папка"
        self.path.hint_text = "/share/папка" if smb else None
        self.username.hint_text = "ДОМЕН\\пользователь" if smb else None
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
        if self.scheme.value == "smb" and not (self.path.value or "").strip("/ "):
            self.path.error = "Укажите общий ресурс, например /public"
            errors = True
        else:
            self.path.error = None
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
            except (UnknownHostKey, HostKeyChanged) as exc:
                self.test_result.value = "Подтвердите ключ сервера и проверьте ещё раз"
                self.test_result.color = ft.Colors.ON_SURFACE_VARIANT
                self.app.confirm_host_key(exc, then=lambda: self._test(e))
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


QUICK_PROTOCOLS = {
    "sftp": "SSH / SFTP",
    "smb": "SMB",
    "ftp": "FTP",
    "dav": "WebDAV",
}


def split_address(text: str, default_port: int | None) -> tuple[str, int | None]:
    """``192.168.1.5``, ``nas.local:2222`` or ``[fe80::1]:22`` → host and port."""
    text = text.strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
    if text.count(":") == 1:
        host, port = text.split(":")
        if port.isdigit():
            return host, int(port)
    return text, default_port


class QuickConnectDialog:
    """Open a server by its address right away: files, or a terminal for SSH. Saving is optional."""

    def __init__(
        self,
        app: App,
        host: str = "",
        port: int | None = None,
        scheme: str = "sftp",
        username: str = "",
        name: str = "",
    ) -> None:
        self.app = app
        self.name = name
        self.scheme = ft.SegmentedButton(
            segments=[ft.Segment(value=key, label=ft.Text(label)) for key, label in QUICK_PROTOCOLS.items()],
            selected=[scheme],
            on_change=lambda e: self._adapt(),
        )
        address = f"{host}:{port}" if host and port and port != DEFAULT_PORTS.get(scheme) else host
        self.address = ft.TextField(
            label="IP-адрес или имя",
            value=address,
            hint_text="192.168.1.20 или nas.local:2222",
            autofocus=not host,
            keyboard_type=ft.KeyboardType.URL,
            on_submit=lambda e: self._files(e),
        )
        self.username = ft.TextField(label="Пользователь", value=username, autofocus=bool(host) and not username)
        self.password = ft.TextField(
            label="Пароль",
            password=True,
            can_reveal_password=True,
            autofocus=bool(host and username),
            on_submit=lambda e: self._primary(e),
        )
        self.path = ft.TextField(label="Общий ресурс", hint_text="/public")
        self.save = ft.Checkbox(label="Сохранить в подключениях")
        self.terminal_button = ft.FilledButton("Терминал", icon=ft.Icons.TERMINAL, on_click=self._terminal)
        self.dialog = ft.AlertDialog(
            icon=ft.Icon(ft.Icons.LAN_OUTLINED),
            title=ft.Text("Подключиться по адресу"),
            content=ft.Column(
                [self.scheme, self.address, self.username, self.password, self.path, self.save],
                tight=True,
                spacing=12,
                width=440,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            actions=[
                ft.TextButton("Отмена", on_click=lambda e: self.app.page.pop_dialog()),
                ft.FilledTonalButton("Файлы", icon=ft.Icons.FOLDER_OPEN_OUTLINED, on_click=self._files),
                self.terminal_button,
            ],
        )
        self._adapt()

    def show(self) -> None:
        self.app.page.show_dialog(self.dialog)

    def _adapt(self) -> None:
        scheme = self.scheme.selected[0]
        self.terminal_button.visible = scheme == "sftp"
        self.path.visible = scheme == "smb"
        self.username.hint_text = "ДОМЕН\\пользователь" if scheme == "smb" else None
        try:
            self.dialog.update()
        except (RuntimeError, AssertionError):
            pass

    def _build(self) -> Connection | None:
        scheme = self.scheme.selected[0]
        host, port = split_address(self.address.value or "", None)
        self.address.error = None if host else "Укажите адрес"
        share = (self.path.value or "").strip().strip("/")
        self.path.error = "Укажите общий ресурс, например /public" if scheme == "smb" and not share else None
        if self.address.error or self.path.error:
            safe_update(self.dialog)
            return None
        connection = Connection(
            name=self.name or (f"{self.username.value}@{host}" if self.username.value else host),
            scheme=scheme,
            host=host,
            port=port,
            username=(self.username.value or "").strip(),
            password=self.password.value or "",
            path=f"/{share}" if scheme == "smb" else "/",
        )
        if self.save.value:
            self.app.store.upsert(connection)
            self.app.connections_changed()
        else:
            connection = self.app.store.add_temporary(connection)
        return connection

    def _files(self, e: ft.Event[ft.Control]) -> None:
        connection = self._build()
        if connection:
            self.app.page.pop_dialog()
            self.app.open_in_browser(connection, refresh=True)

    def _terminal(self, e: ft.Event[ft.Control]) -> None:
        connection = self._build()
        if connection:
            self.app.page.pop_dialog()
            self.app.terminal.open(connection)

    def _primary(self, e: ft.Event[ft.Control]) -> None:
        (self._terminal if self.scheme.selected[0] == "sftp" else self._files)(e)
