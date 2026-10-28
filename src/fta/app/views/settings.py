"""Settings screen: theme, Material You accent colour, files and transfers, about."""

from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from fta import __version__
from fta.app.storage import PROTOCOLS
from fta.app.ui import SEED_COLORS, headline, safe_update
from fta.app.updates import RELEASES_PAGE

if TYPE_CHECKING:
    from fta.app.main import App

REPOSITORY_URL = "https://github.com/Zaxar1231/File-transfer-for-all"


def _section(title: str, *controls: ft.Control) -> ft.Container:
    return ft.Container(
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
        border_radius=24,
        padding=20,
        content=ft.Column(
            [ft.Text(title, theme_style=ft.TextThemeStyle.TITLE_MEDIUM, color=ft.Colors.PRIMARY), *controls],
            spacing=16,
        ),
    )


def _label_pill(text: str) -> ft.Container:
    return ft.Container(
        ft.Text(text, theme_style=ft.TextThemeStyle.LABEL_LARGE, color=ft.Colors.ON_SECONDARY_CONTAINER),
        bgcolor=ft.Colors.SECONDARY_CONTAINER,
        border_radius=8,
        padding=ft.Padding.symmetric(horizontal=12, vertical=6),
    )


class SettingsView:
    def __init__(self, app: App) -> None:
        self.app = app
        settings = app.store.settings

        self.theme_mode = ft.SegmentedButton(
            segments=[
                ft.Segment(value="system", label=ft.Text("Системная"), icon=ft.Icon(ft.Icons.BRIGHTNESS_AUTO_OUTLINED)),
                ft.Segment(value="light", label=ft.Text("Светлая"), icon=ft.Icon(ft.Icons.LIGHT_MODE_OUTLINED)),
                ft.Segment(value="dark", label=ft.Text("Тёмная"), icon=ft.Icon(ft.Icons.DARK_MODE_OUTLINED)),
            ],
            selected=[settings.theme_mode],
            on_change=self._on_theme_mode,
        )
        self.swatches = ft.Row(wrap=True, spacing=12, run_spacing=12)
        self.retries = ft.SegmentedButton(
            segments=[ft.Segment(value=str(n), label=ft.Text(str(n))) for n in (0, 1, 3, 5)],
            selected=[str(settings.retries) if settings.retries in (0, 1, 3, 5) else "3"],
            on_change=self._on_retries,
        )
        self.verify = ft.Switch(
            label="Проверять SHA-256 после передачи", value=settings.verify, on_change=self._on_verify
        )
        self.parallel = ft.SegmentedButton(
            segments=[ft.Segment(value=str(n), label=ft.Text(str(n))) for n in (1, 2, 3, 4)],
            selected=[str(settings.parallel_transfers)],
            on_change=self._on_parallel,
        )
        limits = {"0": "Нет", "512": "512 КБ/с", "1024": "1 МБ/с", "5120": "5 МБ/с", "10240": "10 МБ/с"}
        current = str(settings.speed_limit_kib)
        if current not in limits:
            limits[current] = f"{settings.speed_limit_kib} КБ/с"
        self.speed_limit = ft.SegmentedButton(
            segments=[ft.Segment(value=key, label=ft.Text(text)) for key, text in limits.items()],
            selected=[current],
            on_change=self._on_speed_limit,
        )
        self.show_hidden = ft.Switch(
            label="Показывать скрытые файлы", value=settings.show_hidden, on_change=self._on_show_hidden
        )
        self.check_updates = ft.Switch(
            label="Проверять обновления при запуске", value=settings.check_updates, on_change=self._on_check_updates
        )
        self._render_swatches()
        self._build_server_controls()
        self._build_lan_controls()
        self._build_security_controls()

        self.view = ft.Column(
            [
                headline("Настройки"),
                ft.Column(
                    [
                        _section(
                            "Оформление",
                            ft.Text("Тема"),
                            self.theme_mode,
                            ft.Text("Цвет акцента — из него Material You строит всю палитру"),
                            self.swatches,
                        ),
                        _section(
                            "Файлы и передачи",
                            self.show_hidden,
                            ft.Text("Одновременных передач"),
                            self.parallel,
                            ft.Text("Повторов при ошибке (каждый продолжает с места обрыва)"),
                            self.retries,
                            self.verify,
                            ft.Text("Ограничение скорости для всех передач вместе"),
                            self.speed_limit,
                        ),
                        _section(
                            "Устройства рядом",
                            ft.Text(
                                "Как это устройство видят другие на вкладке «Рядом», куда сохранять полученное "
                                "и нужен ли PIN. Каждую входящую передачу вы подтверждаете сами.",
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                            ft.Row([self.device_name, self.lan_pin], wrap=True, spacing=12),
                            ft.Row(
                                [self.receive_dir_text, ft.IconButton(ft.Icons.FOLDER_OPEN_OUTLINED, tooltip="Выбрать папку", on_click=self._pick_receive_dir)],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                        ),
                        _section(
                            "SSH-сервер (PuTTY, WinSCP, psftp)",
                            ft.Text(
                                "Другие устройства подключаются к этому по SSH: PuTTY открывает оболочку "
                                "передач (send, recv, queue, help), а psftp, WinSCP и FileZilla — общую папку по SFTP.",
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                            self.server_switch,
                            ft.Row([self.server_port, self.server_user], wrap=True, spacing=12),
                            ft.Row(
                                [self.server_root_text, ft.IconButton(ft.Icons.FOLDER_OPEN_OUTLINED, tooltip="Выбрать папку", on_click=self._pick_server_root)],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            ft.Row(
                                [self.server_password, ft.FilledTonalButton("Задать пароль", icon=ft.Icons.KEY_OUTLINED, on_click=self._set_server_password)],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                wrap=True,
                            ),
                            self.server_status,
                        ),
                        _section(
                            "Безопасность",
                            ft.Text("Мой ключ шифрования — дайте его тем, кто шифрует файлы для вас (age):"),
                            ft.Row(
                                [
                                    self.age_key,
                                    ft.IconButton(ft.Icons.COPY, tooltip="Скопировать", on_click=self._copy_age_key),
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            self.totp_switch,
                            self.totp_status,
                        ),
                        _section(
                            "Обновления",
                            ft.Text(f"Установлена версия {__version__}"),
                            self.check_updates,
                            ft.Row(
                                [
                                    ft.FilledTonalButton(
                                        "Проверить сейчас",
                                        icon=ft.Icons.SYSTEM_UPDATE,
                                        on_click=lambda e: app.updater.check_in_background(silent=False),
                                    ),
                                    ft.TextButton("Все версии", icon=ft.Icons.OPEN_IN_NEW, url=RELEASES_PAGE),
                                ],
                                wrap=True,
                            ),
                        ),
                        _section(
                            "О приложении",
                            ft.Text("File Transfer for All — передача файлов через любые протоколы."),
                            ft.Row(
                                [_label_pill("Локальные файлы"), *(_label_pill(name) for name in PROTOCOLS.values())],
                                wrap=True,
                                spacing=8,
                                run_spacing=8,
                            ),
                            ft.Text(
                                f"Настройки: {app.store.path}\nЖурнал: {app.journal.path}\nЛог: {app.log_path}",
                                theme_style=ft.TextThemeStyle.BODY_SMALL,
                                color=ft.Colors.ON_SURFACE_VARIANT,
                                selectable=True,
                            ),
                            ft.Row([ft.TextButton("Исходный код на GitHub", icon=ft.Icons.CODE, url=REPOSITORY_URL)]),
                        ),
                    ],
                    spacing=16,
                    scroll=ft.ScrollMode.AUTO,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                    expand=True,
                ),
            ],
            expand=True,
            spacing=16,
        )

    # --- local network -------------------------------------------------------------------

    def _build_lan_controls(self) -> None:
        settings = self.app.store.settings
        self.device_name = ft.TextField(
            label="Имя устройства",
            value=settings.device_name,
            hint_text="по умолчанию — имя компьютера",
            width=260,
            max_length=40,
            on_blur=self._on_lan_field,
            on_submit=self._on_lan_field,
        )
        self.lan_pin = ft.TextField(
            label="PIN для отправителей",
            value=settings.lan_pin,
            hint_text="пусто — без PIN",
            width=200,
            password=True,
            can_reveal_password=True,
            input_filter=ft.NumbersOnlyInputFilter(),
            max_length=8,
            on_blur=self._on_lan_field,
            on_submit=self._on_lan_field,
        )
        self.receive_dir_text = ft.Text(str(self.app.store.receive_dir), expand=True, selectable=True)

    def _on_lan_field(self, e: ft.Event[ft.TextField]) -> None:
        settings = self.app.store.settings
        name, pin = (self.device_name.value or "").strip(), (self.lan_pin.value or "").strip()
        if (name, pin) == (settings.device_name, settings.lan_pin):
            return
        settings.device_name, settings.lan_pin = name, pin
        self.app.store.save()
        self.app.apply_lan()

    async def _pick_receive_dir(self, e: ft.Event[ft.IconButton]) -> None:
        path = await self.app.picker.get_directory_path(dialog_title="Куда сохранять полученные файлы")
        if path:
            self.app.store.settings.receive_dir = path
            self.receive_dir_text.value = path
            self.app.store.save()
            self.app.apply_lan()

    # --- security ------------------------------------------------------------------------

    def _build_security_controls(self) -> None:
        from fta.app.views.security import my_identity

        settings = self.app.store.settings
        self.age_key = ft.Text(my_identity(self.app).recipient, selectable=True, expand=True, font_family="FtaMono", size=12)
        self.totp_switch = ft.Switch(
            label="Двухфакторный вход в SSH-сервер (пароль + код из приложения-аутентификатора)",
            value=bool(settings.server_totp_secret),
            on_change=self._on_totp_switch,
        )
        self.totp_status = ft.Text(theme_style=ft.TextThemeStyle.BODY_SMALL, color=ft.Colors.ON_SURFACE_VARIANT)
        self._show_totp_status()

    def _show_totp_status(self) -> None:
        on = bool(self.app.store.settings.server_totp_secret)
        self.totp_status.value = (
            "Включён: PuTTY и ssh спросят пароль и код. Вход по ключам из authorized_keys — без кода."
            if on
            else "Выключен"
        )
        safe_update(self.totp_status)

    async def _copy_age_key(self, e: ft.Event[ft.IconButton]) -> None:
        await self.app.clipboard.set(self.age_key.value or "")
        self.app.notify("Ключ скопирован")

    def _on_totp_switch(self, e: ft.Event[ft.Switch]) -> None:
        settings = self.app.store.settings
        if not e.control.value:
            settings.server_totp_secret = ""
            self.app.store.save()
            self.app.apply_server()
            self._show_totp_status()
            return
        e.control.value = False  # stays off until a code proves the app is set up
        safe_update(e.control)
        self._enroll_totp()

    def _enroll_totp(self) -> None:
        from fta.crypto import totp
        from fta.lan.links import qr_png

        secret = totp.new_secret()
        account = f"{self.app.store.settings.server_user}@{self.app.lan.identity.name if self.app.lan else 'FTA'}"
        code = ft.TextField(label="Код из приложения", width=180, keyboard_type=ft.KeyboardType.NUMBER, max_length=6, autofocus=True)
        verifier = totp.Verifier(secret)

        def confirm(_: ft.Event[ft.Control]) -> None:
            if not verifier.check(code.value or ""):
                code.error = "Неверный код — проверьте время на телефоне"
                safe_update(code)
                return
            self.app.store.settings.server_totp_secret = secret
            self.app.store.save()
            self.app.page.pop_dialog()
            self.totp_switch.value = True
            safe_update(self.totp_switch)
            self._show_totp_status()
            self.app.apply_server()
            self.app.notify("Двухфакторный вход включён")

        code.on_submit = confirm
        self.app.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                icon=ft.Icon(ft.Icons.PHONELINK_LOCK),
                title=ft.Text("Двухфакторный вход"),
                content=ft.Column(
                    [
                        ft.Text("Отсканируйте QR-код в Google Authenticator, Aegis, 1Password или другом приложении:"),
                        ft.Container(
                            ft.Image(src=qr_png(totp.provisioning_uri(secret, account)), width=200, height=200),
                            bgcolor=ft.Colors.WHITE,
                            border_radius=12,
                            padding=6,
                            alignment=ft.Alignment.CENTER,
                        ),
                        ft.Text(f"Или введите ключ вручную: {secret}", selectable=True, theme_style=ft.TextThemeStyle.BODY_SMALL),
                        code,
                    ],
                    tight=True,
                    spacing=12,
                    width=420,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                actions=[
                    ft.TextButton("Отмена", on_click=lambda e: self.app.page.pop_dialog()),
                    ft.FilledButton("Включить", on_click=confirm),
                ],
            )
        )

    # --- SSH server ----------------------------------------------------------------------

    def _build_server_controls(self) -> None:
        settings = self.app.store.settings
        self.server_switch = ft.Switch(label="Включить сервер", value=settings.server_enabled, on_change=self._on_server_switch)
        self.server_port = ft.TextField(
            label="Порт",
            value=str(settings.server_port),
            width=120,
            keyboard_type=ft.KeyboardType.NUMBER,
            input_filter=ft.NumbersOnlyInputFilter(),
            on_blur=self._on_server_field,
            on_submit=self._on_server_field,
        )
        self.server_user = ft.TextField(
            label="Логин", value=settings.server_user, width=200, on_blur=self._on_server_field, on_submit=self._on_server_field
        )
        self.server_root_text = ft.Text(str(self.app.store.server_root), expand=True, selectable=True)
        self.server_password = ft.TextField(label="Новый пароль", password=True, can_reveal_password=True, width=260)
        self.server_status = ft.Text(selectable=True, theme_style=ft.TextThemeStyle.BODY_SMALL)

    def refresh_server(self) -> None:
        app = self.app
        settings = app.store.settings
        if app.server is not None:
            hosts = ", ".join(f"{a}:{app.server.port}" for a in app.server.addresses())
            self.server_status.value = (
                f"Работает: {hosts} · логин {settings.server_user}\n"
                f"Отпечаток ключа (сверьте с PuTTY при первом входе): {app.server.fingerprint}\n"
                f"PuTTY: Host — адрес, Port — {app.server.port}, Connection type — SSH."
            )
            self.server_status.color = ft.Colors.PRIMARY
        elif app.server_error:
            self.server_status.value = f"Не удалось запустить: {app.server_error}"
            self.server_status.color = ft.Colors.ERROR
        elif settings.server_enabled and not settings.server_password_hash:
            self.server_status.value = "Задайте пароль или добавьте ключи в authorized_keys"
            self.server_status.color = ft.Colors.ERROR
        else:
            self.server_status.value = "Выключен"
            self.server_status.color = ft.Colors.ON_SURFACE_VARIANT
        if not settings.server_password_hash:
            keys = app.store.data_dir / "authorized_keys"
            self.server_status.value += f"\nВход по ключам: {keys}"
        safe_update(self.server_status)

    def _on_server_switch(self, e: ft.Event[ft.Switch]) -> None:
        self.app.store.settings.server_enabled = bool(e.control.value)
        self.app.store.save()
        self.app.apply_server()

    def _on_server_field(self, e: ft.Event[ft.TextField]) -> None:
        settings = self.app.store.settings
        port = int(self.server_port.value or 0)
        user = (self.server_user.value or "").strip() or "fta"
        if (port, user) == (settings.server_port, settings.server_user) or not 0 < port < 65536:
            return
        settings.server_port, settings.server_user = port, user
        self.app.store.save()
        self.app.apply_server()

    def _set_server_password(self, e: ft.Event[ft.Control]) -> None:
        from fta.server.passwords import hash_password

        password = self.server_password.value or ""
        if len(password) < 6:
            self.server_password.error = "Не короче 6 символов"
            safe_update(self.server_password)
            return
        self.server_password.error = None
        self.server_password.value = ""
        self.app.store.settings.server_password_hash = hash_password(password)
        self.app.store.save()
        self.app.apply_server()
        self.app.notify("Пароль сервера сохранён (хранится только его хеш)")

    async def _pick_server_root(self, e: ft.Event[ft.IconButton]) -> None:
        path = await self.app.picker.get_directory_path(dialog_title="Общая папка сервера")
        if path:
            self.app.store.settings.server_root = path
            self.server_root_text.value = path
            self.app.store.save()
            self.app.apply_server()

    def _render_swatches(self) -> None:
        current = self.app.store.settings.seed_color
        self.swatches.controls = [
            ft.Container(
                width=48,
                height=48,
                border_radius=24,
                bgcolor=color,
                tooltip=name,
                border=ft.Border.all(3, ft.Colors.ON_SURFACE) if color == current else None,
                content=ft.Icon(ft.Icons.CHECK, color=ft.Colors.WHITE) if color == current else None,
                alignment=ft.Alignment.CENTER,
                on_click=lambda e, c=color: self._on_seed(c),
            )
            for color, name in SEED_COLORS.items()
        ]

    def _on_theme_mode(self, e: ft.Event[ft.SegmentedButton]) -> None:
        self.app.store.settings.theme_mode = e.control.selected[0]
        self._apply()

    def _on_seed(self, color: str) -> None:
        self.app.store.settings.seed_color = color
        self._render_swatches()
        self._apply()

    def _on_retries(self, e: ft.Event[ft.SegmentedButton]) -> None:
        self.app.store.settings.retries = int(e.control.selected[0])
        self.app.store.save()

    def _on_speed_limit(self, e: ft.Event[ft.SegmentedButton]) -> None:
        self.app.store.settings.speed_limit_kib = int(e.control.selected[0])
        self.app.store.save()
        self.app.apply_speed_limit()

    def _on_verify(self, e: ft.Event[ft.Switch]) -> None:
        self.app.store.settings.verify = bool(e.control.value)
        self.app.store.save()

    def _on_parallel(self, e: ft.Event[ft.SegmentedButton]) -> None:
        self.app.store.settings.parallel_transfers = int(e.control.selected[0])
        self.app.queue.set_parallel(self.app.store.settings.parallel_transfers)
        self.app.store.save()

    def _on_check_updates(self, e: ft.Event[ft.Switch]) -> None:
        self.app.store.settings.check_updates = bool(e.control.value)
        self.app.store.save()

    def _on_show_hidden(self, e: ft.Event[ft.Switch]) -> None:
        self.app.store.settings.show_hidden = bool(e.control.value)
        self.app.store.save()
        self.app.browser.left.rerender()
        self.app.browser.right.rerender()

    def _apply(self) -> None:
        self.app.store.save()
        self.app.apply_theme()
        self.app.page.update()
