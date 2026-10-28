"""Statistics dialog: totals from the journal, the last two weeks, and a summary card to share."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from fta.app.ui import safe_update
from fta.fun import Stats, human, last_days, statistics, summary_card_svg

if TYPE_CHECKING:
    from fta.app.main import App

PERIODS = {"7": "7 дней", "30": "30 дней", "365": "Год", "0": "Всё время"}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _fact(label: str, value: str) -> ft.Container:
    return ft.Container(
        ft.Column(
            [
                ft.Text(label, theme_style=ft.TextThemeStyle.LABEL_MEDIUM, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text(value, theme_style=ft.TextThemeStyle.HEADLINE_SMALL, weight=ft.FontWeight.BOLD),
            ],
            spacing=2,
            tight=True,
        ),
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
        border_radius=16,
        padding=14,
        width=200,
    )


class StatsDialog:
    def __init__(self, app: App) -> None:
        self.app = app
        self.period = ft.SegmentedButton(
            segments=[ft.Segment(value=key, label=ft.Text(text)) for key, text in PERIODS.items()],
            selected=["30"],
            on_change=lambda e: self._render(),
        )
        self.body = ft.Column(spacing=14)
        self.stats = Stats()
        self.dialog = ft.AlertDialog(
            icon=ft.Icon(ft.Icons.INSIGHTS),
            title=ft.Text("Статистика"),
            content=ft.Column([self.period, self.body], tight=True, spacing=16, width=440),
            actions=[
                ft.TextButton("Закрыть", on_click=lambda e: app.page.pop_dialog()),
                ft.FilledTonalButton("Карточка итогов", icon=ft.Icons.IMAGE_OUTLINED, on_click=self._card),
            ],
            scrollable=True,
        )
        self._render()

    def show(self) -> None:
        self.app.page.show_dialog(self.dialog)

    def _render(self) -> None:
        days = int(self.period.selected[0])
        self.stats = stats = statistics(self.app.journal.entries(), since=time.time() - days * 86400 if days else None)
        bars = last_days(stats, 14)
        peak = max((value for _, value in bars), default=0) or 1
        chart = ft.Row(
            [
                ft.Column(
                    [
                        ft.Container(
                            width=20,
                            height=max(3, 90 * value / peak),
                            bgcolor=ft.Colors.PRIMARY if value else ft.Colors.OUTLINE_VARIANT,
                            border_radius=6,
                            tooltip=f"{day:%d.%m}: {human(value)}",
                        ),
                        ft.Text(WEEKDAYS[day.weekday()], size=10, color=ft.Colors.ON_SURFACE_VARIANT),
                    ],
                    spacing=4,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    alignment=ft.MainAxisAlignment.END,
                )
                for day, value in bars
            ],
            spacing=8,
            height=120,
            vertical_alignment=ft.CrossAxisAlignment.END,
        )
        hour = f"{stats.busiest_hour}:00–{stats.busiest_hour + 1}:00" if stats.busiest_hour is not None else "—"
        lines = []
        if stats.biggest[1]:
            lines.append(f"Самый большой файл: {human(stats.biggest[0])} — {Path(stats.biggest[1]).name}")
        if stats.fastest[1]:
            lines.append(f"Рекорд скорости: {human(stats.fastest[0])}/с — {Path(stats.fastest[1]).name}")
        lines.append(f"Чаще всего передаёте в {hour}")
        lines.append("Протоколы: " + (", ".join(f"{n} — {c}" for n, c in stats.by_protocol.most_common(4)) or "пока нет"))
        self.body.controls = [
            ft.Row(
                [
                    _fact("Файлов", str(stats.files)),
                    _fact("Передано", human(stats.bytes)),
                    _fact("Средняя скорость", f"{human(stats.average_speed)}/с"),
                    _fact("Сэкономлено докачкой и дельтой", human(stats.saved_by_resume)),
                ],
                wrap=True,
                spacing=10,
                run_spacing=10,
            ),
            ft.Text("Последние 14 дней", theme_style=ft.TextThemeStyle.TITLE_SMALL),
            chart,
            *[ft.Text(line) for line in lines],
            *([ft.Text(f"Ошибок: {stats.failed}", color=ft.Colors.ERROR)] if stats.failed else []),
        ]
        safe_update(self.body)

    def _card(self, e: ft.Event[ft.Control]) -> None:
        period = PERIODS[self.period.selected[0]].lower()
        svg = summary_card_svg(
            self.stats,
            f"Мои передачи: {period}",
            f"на {date.today():%d.%m.%Y} · {self.app.store.settings.device_name or 'File Transfer for All'}",
            accent=self.app.store.settings.seed_color,
        )
        downloads = Path.home() / "Downloads"
        folder = downloads if downloads.is_dir() else Path.home()
        path = folder / f"fta-итоги-{date.today():%Y-%m-%d}.svg"
        path.write_text(svg, encoding="utf-8")
        from fta.app.main import _open_folder

        self.app.notify(f"Карточка сохранена: {path}", action="Открыть папку", on_action=lambda e: _open_folder(folder))
