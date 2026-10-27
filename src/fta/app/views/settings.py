"""Settings screen: theme, Material You accent colour, files and transfers, about."""

from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from fta import __version__
from fta.app.storage import PROTOCOLS
from fta.app.ui import SEED_COLORS, headline
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
        self.parallel = ft.SegmentedButton(
            segments=[ft.Segment(value=str(n), label=ft.Text(str(n))) for n in (1, 2, 3, 4)],
            selected=[str(settings.parallel_transfers)],
            on_change=self._on_parallel,
        )
        self.show_hidden = ft.Switch(
            label="Показывать скрытые файлы", value=settings.show_hidden, on_change=self._on_show_hidden
        )
        self.check_updates = ft.Switch(
            label="Проверять обновления при запуске", value=settings.check_updates, on_change=self._on_check_updates
        )
        self._render_swatches()

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
                                f"Настройки хранятся в {app.store.path}",
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
