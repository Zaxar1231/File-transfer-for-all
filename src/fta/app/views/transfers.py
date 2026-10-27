"""Transfers screen: one card per job with live progress, cancel and retry."""

from __future__ import annotations

import threading
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import flet as ft

from fta.app.transfers import JobState, TransferJob
from fta.app.ui import empty_state, headline, human_size, safe_update

if TYPE_CHECKING:
    from fta.app.main import App

_STATE_LOOK = {
    JobState.QUEUED: (ft.Icons.SCHEDULE, ft.Colors.ON_SURFACE_VARIANT),
    JobState.RUNNING: (ft.Icons.SYNC_ALT, ft.Colors.PRIMARY),
    JobState.DONE: (ft.Icons.CHECK_CIRCLE, ft.Colors.PRIMARY),
    JobState.FAILED: (ft.Icons.ERROR_OUTLINE, ft.Colors.ERROR),
    JobState.CANCELLED: (ft.Icons.CANCEL_OUTLINED, ft.Colors.ON_SURFACE_VARIANT),
}


def describe(job: TransferJob) -> str:
    """Status line under the progress bar."""
    if job.state is JobState.QUEUED:
        return "В очереди"
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
        return f"Готово · файлов: {job.files_done}" if job.recursive else "Готово"
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
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
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
        self.status.value = describe(job)
        self.status.color = ft.Colors.ERROR if job.state is JobState.FAILED else ft.Colors.ON_SURFACE_VARIANT
        self.progress.value = 0 if job.state is JobState.QUEUED else job.fraction
        self.progress.color = ft.Colors.ERROR if job.state is JobState.FAILED else None
        self.progress.visible = job.state in (JobState.QUEUED, JobState.RUNNING)
        if job.state.finished:
            done = job.state is JobState.DONE
            self.action.icon = ft.Icons.CHECK if done else ft.Icons.REPLAY
            self.action.tooltip = None if done else "Повторить"
            self.action.disabled = done
        else:
            self.action.icon, self.action.tooltip, self.action.disabled = ft.Icons.CLOSE, "Отменить", False

    def _on_action(self, e: ft.Event[ft.IconButton]) -> None:
        if self.job.state.finished:
            self.app.queue.retry(self.job)
        else:
            self.app.queue.cancel(self.job)


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
        self.view = ft.Column(
            [headline("Передачи", self.clear_button), self.list, self.empty], expand=True, spacing=16
        )
        self._render_body()
        app.queue.subscribe(self._on_job)

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
