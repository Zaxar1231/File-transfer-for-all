"""Transfers screen: one card per job with live progress, pause, priority, retry; the journal."""

from __future__ import annotations

import math
import threading
import time
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import flet as ft

from fta.app.transfers import JobState, TransferJob
from fta.app.ui import empty_state, format_time, headline, human_size, safe_update
from fta.app.updates import downloads_folder

if TYPE_CHECKING:
    from fta.app.main import App

_STATE_LOOK = {
    JobState.QUEUED: (ft.Icons.SCHEDULE, ft.Colors.ON_SURFACE_VARIANT),
    JobState.RUNNING: (ft.Icons.SYNC_ALT, ft.Colors.PRIMARY),
    JobState.PAUSED: (ft.Icons.PAUSE_CIRCLE_OUTLINE, ft.Colors.ON_SURFACE_VARIANT),
    JobState.DONE: (ft.Icons.CHECK_CIRCLE, ft.Colors.PRIMARY),
    JobState.FAILED: (ft.Icons.ERROR_OUTLINE, ft.Colors.ERROR),
    JobState.CANCELLED: (ft.Icons.CANCEL_OUTLINED, ft.Colors.ON_SURFACE_VARIANT),
}


def describe(job: TransferJob, sysadmin: bool = False) -> str:
    """Status line under the progress bar; the sysadmin mode adds the raw numbers."""
    text = _describe(job)
    if not sysadmin:
        return text
    details = [f"#{job.id}", f"попытка {job.attempts}/{job.max_retries + 1}", f"{job.bytes_done:,} Б".replace(",", " ")]
    if job.speed:
        details.append(f"{job.speed * 8 / 1_000_000:.1f} Мбит/с")
    if job.state is JobState.DONE and job.results:
        details.append("sha256 " + job.results[-1].sha256 if job.results[-1].sha256 else "без хеша")
    return text + "\n" + " · ".join(details) + f"\n{job.source_url or job.source} → {job.destination_url}"


def _describe(job: TransferJob) -> str:
    if job.state is JobState.QUEUED:
        if job.waiting_for_retry:
            return (
                f"Ошибка: {job.error} · повтор через {math.ceil(job.waiting_for_retry)} с "
                f"(попытка {job.attempts + 1} из {job.max_retries + 1})"
            )
        return "В очереди" + (f" · приоритет {job.priority}" if job.priority else "")
    if job.state is JobState.PAUSED:
        return f"На паузе · {human_size(job.bytes_done)}" if job.bytes_done else "На паузе"
    if job.state is JobState.RUNNING:
        if job.bytes_total and job.bytes_done >= job.bytes_total:
            # every byte is handed over; the server has not confirmed yet
            return f"{human_size(job.bytes_total)} · завершение…"
        parts = []
        if job.fraction is not None:
            parts.append(f"{job.fraction:.0%}")
        if job.bytes_total:
            parts.append(f"{human_size(job.bytes_done)} из {human_size(job.bytes_total)}")
        elif job.bytes_done:
            parts.append(human_size(job.bytes_done))
        if job.speed:
            parts.append(f"{human_size(job.speed)}/с")
        if job.recursive and job.current_file:
            parts.append(f"файл {job.files_done + 1}: {PurePosixPath(job.current_file).name}")
        return " · ".join(parts) or "Подключение…"
    if job.state is JobState.DONE:
        parts = [f"Готово · файлов: {job.files_done}" if job.recursive else "Готово"]
        if len(job.results) == 1 and job.results[0].sha256:
            result = job.results[0]
            parts.append(f"SHA-256 {result.sha256[:16]}…")
            if result.verified:
                parts.append("проверено")
            if result.resumed_from:
                parts.append(f"продолжено с {human_size(result.resumed_from)}")
        elif job.verify and job.results:
            parts.append("все файлы проверены по SHA-256")
        return " · ".join(parts)
    if job.state is JobState.FAILED:
        return f"Ошибка: {job.error}"
    return "Отменено"


class JobCard:
    def __init__(self, app: App, job: TransferJob) -> None:
        self.app = app
        self.job = job
        self.icon = ft.Icon(ft.Icons.SCHEDULE)
        self.status = ft.Text(theme_style=ft.TextThemeStyle.BODY_SMALL, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
        self.progress = ft.ProgressBar(value=0, bar_height=6, border_radius=3)
        self.action = ft.IconButton(on_click=self._on_action)
        self.menu = ft.PopupMenuButton(
            icon=ft.Icons.MORE_VERT,
            items=[
                ft.PopupMenuItem(content="Наверх очереди", icon=ft.Icons.VERTICAL_ALIGN_TOP, on_click=self._to_top),
                ft.PopupMenuItem(content="Отменить", icon=ft.Icons.CLOSE, on_click=lambda e: app.queue.cancel(job)),
            ],
        )
        self.view = ft.Card(
            variant=ft.CardVariant.FILLED,
            content=ft.Container(
                padding=16,
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                self.icon,
                                ft.Column(
                                    [
                                        ft.Text(
                                            job.name,
                                            theme_style=ft.TextThemeStyle.TITLE_SMALL,
                                            max_lines=1,
                                            overflow=ft.TextOverflow.ELLIPSIS,
                                        ),
                                        ft.Text(
                                            f"{job.source}  →  {job.destination}",
                                            theme_style=ft.TextThemeStyle.BODY_SMALL,
                                            color=ft.Colors.ON_SURFACE_VARIANT,
                                            max_lines=1,
                                            overflow=ft.TextOverflow.ELLIPSIS,
                                        ),
                                    ],
                                    spacing=2,
                                    expand=True,
                                ),
                                self.action,
                                self.menu,
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=0,
                        ),
                        self.progress,
                        self.status,
                    ],
                    spacing=10,
                ),
            ),
        )
        self.refresh()

    def refresh(self) -> None:
        job = self.job
        self.icon.icon, self.icon.color = _STATE_LOOK[job.state]
        sysadmin = self.app.store.settings.sysadmin
        self.status.value = describe(job, sysadmin)
        self.status.max_lines = 4 if sysadmin else 2
        self.status.selectable = sysadmin
        failed = job.state is JobState.FAILED or job.waiting_for_retry
        self.status.color = ft.Colors.ERROR if failed else ft.Colors.ON_SURFACE_VARIANT
        self.progress.value = 0 if job.state is JobState.QUEUED else job.fraction
        self.progress.color = ft.Colors.ERROR if job.state is JobState.FAILED else None
        self.progress.visible = job.state in (JobState.QUEUED, JobState.RUNNING, JobState.PAUSED)
        self.menu.visible = not job.state.finished
        if job.state is JobState.DONE:
            self.action.icon, self.action.tooltip, self.action.disabled = ft.Icons.CHECK, None, True
        elif job.state.finished:
            self.action.icon, self.action.tooltip, self.action.disabled = ft.Icons.REPLAY, "Повторить", False
        elif job.state is JobState.PAUSED:
            self.action.icon, self.action.tooltip, self.action.disabled = ft.Icons.PLAY_ARROW, "Продолжить", False
        else:
            self.action.icon, self.action.tooltip, self.action.disabled = ft.Icons.PAUSE, "Пауза", False

    def _on_action(self, e: ft.Event[ft.IconButton]) -> None:
        queue, job = self.app.queue, self.job
        if job.state.finished:
            queue.retry(job)
        elif job.state is JobState.PAUSED:
            queue.resume(job)
        else:
            queue.pause(job)

    def _to_top(self, e: ft.Event[ft.PopupMenuItem]) -> None:
        top = max((j.priority for j in self.app.queue.jobs), default=0)
        self.app.queue.set_priority(self.job, top + 1)


class TransfersView:
    def __init__(self, app: App) -> None:
        self.app = app
        self.cards: dict[int, JobCard] = {}
        self._lock = threading.Lock()
        self.list = ft.ListView(expand=True, spacing=8)
        self.empty = empty_state(
            ft.Icons.SWAP_VERT,
            "Передач пока нет",
            "Выберите файлы на вкладке «Файлы» и нажмите «Копировать»",
        )
        self.clear_button = ft.TextButton("Очистить завершённые", icon=ft.Icons.CLEAR_ALL, on_click=self._clear)
        journal_button = ft.TextButton("Журнал", icon=ft.Icons.HISTORY, on_click=lambda e: self.show_journal())
        self.view = ft.Column(
            [
                headline(
                    "Передачи",
                    ft.TextButton("Статистика", icon=ft.Icons.INSIGHTS, on_click=lambda e: self._stats()),
                    ft.TextButton("Расписание", icon=ft.Icons.SCHEDULE, on_click=lambda e: self._schedules()),
                    journal_button,
                    self.clear_button,
                ),
                self.list,
                self.empty,
            ],
            expand=True,
            spacing=16
        )
        self._render_body()
        self._celebrated: set[int] = set()
        app.queue.subscribe(self._on_job)

    def _stats(self) -> None:
        from fta.app.views.stats import StatsDialog

        StatsDialog(self.app).show()

    def _schedules(self) -> None:
        from fta.app.views.schedules import SchedulesDialog

        SchedulesDialog(self.app).show()

    def _render_body(self) -> None:
        self.list.visible = bool(self.cards)
        self.empty.visible = not self.cards
        self.clear_button.disabled = not any(c.job.state.finished for c in self.cards.values())

    def _on_job(self, job: TransferJob) -> None:
        with self._lock:
            card = self.cards.get(job.id)
            if card is None:
                card = self.cards[job.id] = JobCard(self.app, job)
                self.list.controls.insert(0, card.view)  # a new control, never one shown before
                self._render_body()
                safe_update(self.view)
            else:
                card.refresh()
                if job.state.finished:
                    self._render_body()
                    safe_update(card.view, self.clear_button)
                    if job.state is JobState.DONE and job.id not in self._celebrated:
                        self._celebrated.add(job.id)
                        if self.app.store.settings.fun_messages:
                            from fta.fun import vzhuh

                            self.app.notify(vzhuh(job.name))
                else:
                    safe_update(card.view)
        self.app.update_badge()

    def _clear(self, e: ft.Event[ft.TextButton]) -> None:
        self.app.queue.clear_finished()
        with self._lock:
            for job_id in [i for i, c in self.cards.items() if c.job.state.finished]:
                self.list.controls.remove(self.cards.pop(job_id).view)
            self._render_body()
        safe_update(self.view)

    def show_journal(self) -> None:
        entries = self.app.journal.entries(limit=100)
        icons = {"done": ft.Icons.CHECK_CIRCLE, "failed": ft.Icons.ERROR_OUTLINE}
        rows: list[ft.Control] = [
            ft.ListTile(
                leading=ft.Icon(icons.get(entry.status, ft.Icons.CANCEL_OUTLINED)),
                title=ft.Text(
                    f"{PurePosixPath(entry.source).name or entry.source} → {entry.destination}",
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                subtitle=ft.Text(
                    " · ".join(
                        part
                        for part in (
                            format_time(entry.time),
                            human_size(entry.size),
                            f"SHA-256 {entry.sha256[:12]}…" if entry.sha256 else "",
                            entry.error,
                        )
                        if part
                    ),
                    max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                dense=True,
            )
            for entry in entries
        ]

        def export(_: ft.Event[ft.Control]) -> None:
            target = downloads_folder() / f"fta-journal-{time.strftime('%Y%m%d-%H%M%S')}.csv"
            self.app.journal.export_csv(target)
            self.app.page.pop_dialog()
            self.app.notify(f"Журнал сохранён: {target}")

        self.app.page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("Журнал передач"),
                content=ft.Container(
                    ft.ListView(rows, spacing=0) if rows else ft.Text("Пока пусто."),
                    width=560,
                    height=420 if rows else None,
                ),
                actions=[
                    ft.TextButton("Закрыть", on_click=lambda e: self.app.page.pop_dialog()),
                    ft.FilledButton("Экспорт в CSV", icon=ft.Icons.DOWNLOAD, on_click=export, disabled=not rows),
                ],
            )
        )
