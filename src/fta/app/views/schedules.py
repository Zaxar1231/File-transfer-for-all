"""Schedules dialog: folders synced or copied on their own at a time of day."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from fta.app.scheduler import describe
from fta.app.storage import Schedule
from fta.app.ui import safe_update, short_path

if TYPE_CHECKING:
    from fta.app.main import App

DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class SchedulesDialog:
    def __init__(self, app: App) -> None:
        self.app = app
        self.home = str(Path.home())
        self.list = ft.Column(spacing=4)
        self.name = ft.TextField(label="Название", hint_text="Резервная копия фото")
        self.source = ft.TextField(label="Откуда", hint_text="папка, URL или @подключение:/путь", expand=True)
        self.destination = ft.TextField(label="Куда", hint_text="например @NAS:/backup или sftp://…", expand=True)
        self.mode = ft.SegmentedButton(
            segments=[
                ft.Segment(value="sync", label=ft.Text("Синхронизация"), icon=ft.Icon(ft.Icons.SYNC)),
                ft.Segment(value="copy", label=ft.Text("Копирование"), icon=ft.Icon(ft.Icons.COPY_ALL)),
            ],
            selected=["sync"],
            on_change=lambda e: self._adapt(),
        )
        self.delete = ft.Checkbox(label="Удалять в копии то, что удалено в источнике")
        self.time = ft.TextField(label="Время", value="03:00", width=110, hint_text="ЧЧ:ММ")
        self.days = ft.SegmentedButton(
            segments=[ft.Segment(value=str(i), label=ft.Text(d)) for i, d in enumerate(DAYS)],
            selected=[str(i) for i in range(7)],
            allow_multiple_selection=True,
            show_selected_icon=False,
        )
        self.error = ft.Text(color=ft.Colors.ERROR, theme_style=ft.TextThemeStyle.BODY_SMALL)
        self.form = ft.Column(
            [
                ft.Text("Новое расписание", theme_style=ft.TextThemeStyle.TITLE_SMALL),
                self.name,
                ft.Row([self.source, ft.IconButton(ft.Icons.FOLDER_OPEN_OUTLINED, tooltip="Выбрать папку", on_click=self._pick)]),
                self.destination,
                self.mode,
                self.delete,
                self.time,
                self.days,  # not inside a wrapping Row: there a SegmentedButton gets no width and nothing renders
                self.error,
                ft.Row([ft.FilledButton("Добавить", icon=ft.Icons.ADD, on_click=self._add)], alignment=ft.MainAxisAlignment.END),
            ],
            spacing=10,
        )
        self.dialog = ft.AlertDialog(
            icon=ft.Icon(ft.Icons.SCHEDULE),
            title=ft.Text("Расписание"),
            content=ft.Column(
                [
                    ft.Text(
                        "Синхронизация отправляет только изменившиеся блоки файлов (по 4 МиБ); "
                        "пропущенный запуск выполнится при следующем открытии приложения в тот же день.",
                        theme_style=ft.TextThemeStyle.BODY_SMALL,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    self.list,
                    ft.Divider(),
                    self.form,
                ],
                tight=True,
                spacing=12,
                width=560,
            ),
            actions=[ft.TextButton("Закрыть", on_click=lambda e: app.page.pop_dialog())],
            scrollable=True,
        )
        self._render()
        self._adapt()

    def show(self) -> None:
        self.app.page.show_dialog(self.dialog)

    def _adapt(self) -> None:
        self.delete.visible = self.mode.selected[0] == "sync"
        safe_update(self.dialog)

    def _render(self) -> None:
        schedules = self.app.store.schedules
        self.list.controls = [self._row(s) for s in schedules] or [
            ft.Text("Пока нет расписаний", color=ft.Colors.ON_SURFACE_VARIANT)
        ]
        safe_update(self.list)

    def _row(self, schedule: Schedule) -> ft.ListTile:
        return ft.ListTile(
            leading=ft.Switch(value=schedule.enabled, on_change=lambda e, s=schedule: self._toggle(s, e.control.value)),
            title=ft.Text(schedule.name),
            subtitle=ft.Text(
                f"{short_path(schedule.source, self.home)} → {short_path(schedule.destination, self.home)}\n{describe(schedule)}",
                color=ft.Colors.ON_SURFACE_VARIANT,
            ),
            trailing=ft.Row(
                [
                    ft.IconButton(ft.Icons.PLAY_ARROW, tooltip="Запустить сейчас", on_click=lambda e, s=schedule: self._run(s)),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Удалить", on_click=lambda e, s=schedule: self._remove(s)),
                ],
                tight=True,
            ),
            is_three_line=True,
        )

    def _toggle(self, schedule: Schedule, enabled: bool) -> None:
        schedule.enabled = bool(enabled)
        self.app.store.save()

    def _run(self, schedule: Schedule) -> None:
        try:
            self.app.scheduler.run_now(schedule)
        except LookupError as exc:  # its connection was deleted
            self.app.notify(str(exc), error=True)
            return
        self.app.page.pop_dialog()
        self.app.go(1)

    def _remove(self, schedule: Schedule) -> None:
        self.app.store.schedules = [s for s in self.app.store.schedules if s.id != schedule.id]
        self.app.store.save()
        self._render()

    async def _pick(self, e: ft.Event[ft.IconButton]) -> None:
        path = await self.app.picker.get_directory_path(dialog_title="Папка, которую копировать")
        if path:
            self.source.value = path
            safe_update(self.source)

    def _add(self, e: ft.Event[ft.Control]) -> None:
        source, destination = (self.source.value or "").strip(), (self.destination.value or "").strip()
        time_text = (self.time.value or "").strip()
        problem = ""
        try:
            hour, minute = (int(part) for part in time_text.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
        except ValueError:
            problem = "Время в виде ЧЧ:ММ, например 03:30"
        if not source or not destination:
            problem = "Укажите, откуда и куда"
        elif not self.days.selected:
            problem = "Выберите хотя бы один день"
        else:
            try:
                self.app.store.resolve(source)
                self.app.store.resolve(destination)
            except LookupError as exc:
                problem = f"Нет такого подключения: {exc}"
        if problem:
            self.error.value = problem
            safe_update(self.error)
            return
        self.app.store.schedules.append(
            Schedule(
                name=(self.name.value or "").strip() or source.rstrip("/").rsplit("/", 1)[-1] or "Расписание",
                source=source,
                destination=destination,
                time=f"{hour:02d}:{minute:02d}",
                days=sorted(int(d) for d in self.days.selected),
                mode=self.mode.selected[0],
                delete=bool(self.delete.value) and self.mode.selected[0] == "sync",
                last_run=time.time(),  # a schedule made at 20:00 for 03:00 has not "missed" today's run
            )
        )
        self.app.store.save()
        self.error.value = ""
        self.name.value = self.source.value = self.destination.value = ""
        self._render()
        safe_update(self.form)
