"""Files screen: two panes, each browsing this device or a saved connection.

Controls here are never removed and re-added: Flet 1.0 renders a re-inserted
control empty, so states (list, loading, message) and layouts are switched with
``visible`` instead.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import flet as ft

from fta.app.storage import LOCAL_ID, Connection
from fta.app.transfers import JobState, TransferJob
from fta.app.ui import empty_state, format_time, headline, human_size, protocol_icon, safe_update, short_path
from fta.core.registry import open_transport
from fta.core.transport import RemoteEntry, Transport, join_path

if TYPE_CHECKING:
    from fta.app.main import App

LOCAL_NAME = "Это устройство"


class Pane:
    """One side of the file browser."""

    def __init__(self, app: App, on_selection_change: Callable[[], None]) -> None:
        self.app = app
        self.on_selection_change = on_selection_change
        self.connection: Connection | None = None  # None means this device
        self.home = str(Path.home())
        self.path = self.home
        self.entries: list[RemoteEntry] = []
        self.selected: dict[str, RemoteEntry] = {}
        self._rows: dict[str, tuple[ft.ListTile, ft.Checkbox]] = {}
        self._generation = 0

        self.source = ft.Dropdown(
            label="Источник",
            value=LOCAL_ID,
            leading_icon=ft.Icons.COMPUTER,
            on_select=self._on_source_select,
            expand=True,
            dense=True,
        )
        self.path_text = ft.Text(
            self.path, color=ft.Colors.ON_SURFACE_VARIANT, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True
        )
        self.pick_folder_button = ft.IconButton(
            ft.Icons.FOLDER_OPEN_OUTLINED, tooltip="Выбрать папку", on_click=self._pick_folder
        )
        self.delete_button = ft.IconButton(
            ft.Icons.DELETE_OUTLINE, tooltip="Удалить выбранное", on_click=self._confirm_delete, disabled=True
        )
        self.selection_text = ft.Text("", theme_style=ft.TextThemeStyle.LABEL_LARGE, color=ft.Colors.PRIMARY)
        self.list = ft.ListView(expand=True, spacing=2)
        self.spinner = ft.Container(
            ft.ProgressRing(width=36, height=36, stroke_width=3), alignment=ft.Alignment.CENTER, expand=True
        )
        self.message = ft.Container(expand=True)
        self.body = ft.Column([self.list, self.spinner, self.message], expand=True, spacing=0)
        self._show(self.spinner)

        self.view = ft.Container(
            expand=True,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border_radius=24,
            padding=ft.Padding.only(left=12, top=16, right=12, bottom=8),
            content=ft.Column(
                [
                    ft.Row([self.source]),
                    ft.Row(
                        [
                            ft.IconButton(ft.Icons.ARROW_UPWARD, tooltip="На уровень вверх", on_click=self.go_up),
                            self.path_text,
                            self.pick_folder_button,
                            ft.IconButton(ft.Icons.REFRESH, tooltip="Обновить", on_click=lambda e: self.load()),
                            ft.IconButton(
                                ft.Icons.CREATE_NEW_FOLDER_OUTLINED, tooltip="Новая папка", on_click=self._ask_new_folder
                            ),
                            self.delete_button,
                        ],
                        spacing=0,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Divider(height=1),
                    self.body,
                    self.selection_text,
                ],
                spacing=8,
                expand=True,
            ),
        )
        self.refresh_sources()

    # --- where this pane points ------------------------------------------------

    @property
    def name(self) -> str:
        return self.connection.name if self.connection else LOCAL_NAME

    def location(self, path: str) -> str:
        return self.connection.url(path) if self.connection else path

    def options(self) -> dict[str, str]:
        return self.connection.transport_options() if self.connection else {}

    def display_path(self, path: str) -> str:
        return short_path(path, None if self.connection else self.home)

    def label(self, path: str) -> str:
        return f"{self.name} · {self.display_path(path)}"

    def destination_dir(self) -> str:
        """Location of the current folder, written so copies land inside it."""
        if self.connection:
            return self.connection.url(self.path.rstrip("/") + "/")
        return self.path

    def open_transport(self) -> Transport:
        return open_transport(self.location(self.path), **self.options())

    def refresh_sources(self) -> None:
        options = [ft.DropdownOption(key=LOCAL_ID, text=LOCAL_NAME, leading_icon=ft.Icons.COMPUTER)]
        options += [
            ft.DropdownOption(key=c.id, text=c.name, leading_icon=protocol_icon(c.scheme))
            for c in self.app.store.connections
        ]
        self.source.options = options
        if self.connection and not self.app.store.get(self.connection.id):
            self.set_source(None)
        safe_update(self.source)

    def set_source(self, connection: Connection | None) -> None:
        self.connection = connection
        self.source.value = connection.id if connection else LOCAL_ID
        self.source.leading_icon = protocol_icon(connection.scheme if connection else None)
        self.pick_folder_button.visible = connection is None
        self.path = (connection.path or "/") if connection else self.home
        if connection:
            self.app.ensure_password(connection, self.load)
        else:
            self.load()

    def _on_source_select(self, e: ft.Event[ft.Dropdown]) -> None:
        value = e.control.value
        self.set_source(None if value == LOCAL_ID else self.app.store.get(value or ""))

    # --- listing -----------------------------------------------------------------

    def _show(self, part: ft.Control) -> None:
        for candidate in (self.list, self.spinner, self.message):
            candidate.visible = candidate is part

    def load(self) -> None:
        self._generation += 1
        generation, path = self._generation, self.path
        self.path_text.value = self.display_path(path)
        self.path_text.tooltip = path
        self._clear_selection()
        self._show(self.spinner)
        safe_update(self.view)
        self.app.page.run_thread(self._load_worker, generation, path)

    def _load_worker(self, generation: int, path: str) -> None:
        try:
            with self.open_transport() as transport:
                entries = transport.listdir(path)
        except Exception as exc:  # shown in the pane; the user can retry or switch source
            if generation == self._generation:
                self._show_error(str(exc) or type(exc).__name__)
            return
        if generation != self._generation:
            return  # the user navigated elsewhere meanwhile
        self.entries = sorted(entries, key=lambda entry: (not entry.is_dir, entry.name.casefold()))
        self._render()

    def _render(self) -> None:
        self._rows.clear()
        show_hidden = self.app.store.settings.show_hidden
        entries = [e for e in self.entries if show_hidden or not e.name.startswith(".")]
        self.list.controls = [self._row(entry) for entry in entries]
        if entries:
            self._show(self.list)
        else:
            self.message.content = empty_state(ft.Icons.FOLDER_OPEN_OUTLINED, "Папка пуста", "Здесь пока нет файлов")
            self._show(self.message)
        safe_update(self.view)

    def rerender(self) -> None:
        """Redraw the current listing, e.g. after the hidden-files setting changed."""
        self._clear_selection()
        self._render()

    def _show_error(self, message: str) -> None:
        self.message.content = empty_state(
            ft.Icons.ERROR_OUTLINE,
            "Не удалось открыть папку",
            message,
            ft.FilledTonalButton("Повторить", icon=ft.Icons.REFRESH, on_click=lambda e: self.load()),
        )
        self._show(self.message)
        safe_update(self.view)

    def _row(self, entry: RemoteEntry) -> ft.ListTile:
        details = [human_size(entry.size) if not entry.is_dir else "Папка", format_time(entry.modified)]
        checkbox = ft.Checkbox(value=False, on_change=lambda e, en=entry: self.toggle(en, bool(e.control.value)))
        tile = ft.ListTile(
            leading=ft.Icon(
                ft.Icons.FOLDER if entry.is_dir else ft.Icons.INSERT_DRIVE_FILE_OUTLINED,
                color=ft.Colors.PRIMARY if entry.is_dir else ft.Colors.ON_SURFACE_VARIANT,
            ),
            title=ft.Text(entry.name, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Text(" · ".join(d for d in details if d), color=ft.Colors.ON_SURFACE_VARIANT),
            trailing=checkbox,
            dense=True,
            shape=ft.RoundedRectangleBorder(radius=16),
            selected_tile_color=ft.Colors.SECONDARY_CONTAINER,
            selected_color=ft.Colors.ON_SECONDARY_CONTAINER,
            on_click=lambda e, en=entry: self.open(en),
        )
        self._rows[entry.path] = (tile, checkbox)
        return tile

    # --- navigation and selection ------------------------------------------------

    def open(self, entry: RemoteEntry) -> None:
        if entry.is_dir:
            self.path = entry.path
            self.load()
        else:
            self.toggle(entry, entry.path not in self.selected)

    def go_up(self, e: ft.Event[ft.IconButton] | None = None) -> None:
        parent = str(Path(self.path).parent) if self.connection is None else str(PurePosixPath(self.path).parent)
        if parent != self.path:
            self.path = parent
            self.load()

    def toggle(self, entry: RemoteEntry, selected: bool) -> None:
        if selected:
            self.selected[entry.path] = entry
        else:
            self.selected.pop(entry.path, None)
        tile, checkbox = self._rows[entry.path]
        tile.selected = checkbox.value = selected
        self._selection_changed()
        safe_update(tile)

    def _clear_selection(self) -> None:
        self.selected.clear()
        for tile, checkbox in self._rows.values():
            tile.selected = checkbox.value = False
        self._selection_changed()

    def clear_selection(self) -> None:
        self._clear_selection()
        safe_update(self.view)

    def _selection_changed(self) -> None:
        count = len(self.selected)
        self.selection_text.value = f"Выбрано: {count}" if count else ""
        self.delete_button.disabled = not count
        safe_update(self.selection_text, self.delete_button)
        self.on_selection_change()

    # --- actions -------------------------------------------------------------------

    async def _pick_folder(self, e: ft.Event[ft.IconButton]) -> None:
        path = await self.app.picker.get_directory_path(dialog_title="Выберите папку", initial_directory=self.path)
        if path:
            self.path = path
            self.load()

    def _ask_new_folder(self, e: ft.Event[ft.IconButton]) -> None:
        field = ft.TextField(label="Имя папки", autofocus=True)

        def create(_: ft.Event[ft.Control]) -> None:
            name = (field.value or "").strip()
            if not name or "/" in name or "\\" in name:
                field.error = "Введите имя без «/» и «\\»"
                field.update()
                return
            self.app.page.pop_dialog()
            target = str(Path(self.path) / name) if self.connection is None else join_path(self.path, name)
            self._run_action(lambda t: t.mkdir(target), f"Папка «{name}» создана")

        field.on_submit = create
        self.app.page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("Новая папка"),
                content=field,
                actions=[
                    ft.TextButton("Отмена", on_click=lambda _: self.app.page.pop_dialog()),
                    ft.FilledButton("Создать", on_click=create),
                ],
            )
        )

    def _confirm_delete(self, e: ft.Event[ft.IconButton]) -> None:
        entries = list(self.selected.values())

        def delete(_: ft.Event[ft.Control]) -> None:
            self.app.page.pop_dialog()
            self._run_action(lambda t: [_delete_tree(t, entry) for entry in entries], f"Удалено: {len(entries)}")

        self.app.page.show_dialog(
            ft.AlertDialog(
                icon=ft.Icon(ft.Icons.DELETE_OUTLINE),
                title=ft.Text(f"Удалить {len(entries)} объект(ов)?"),
                content=ft.Text("Папки удаляются вместе с содержимым. Это действие нельзя отменить."),
                actions=[
                    ft.TextButton("Отмена", on_click=lambda _: self.app.page.pop_dialog()),
                    ft.FilledButton("Удалить", on_click=delete),
                ],
            )
        )

    def _run_action(self, action: Callable[[Transport], object], success: str) -> None:
        def worker() -> None:
            try:
                with self.open_transport() as transport:
                    action(transport)
            except Exception as exc:  # reported to the user
                self.app.notify(f"Ошибка: {exc}", error=True)
            else:
                self.app.notify(success)
            self.load()

        self.app.page.run_thread(worker)


def _delete_tree(transport: Transport, entry: RemoteEntry) -> None:
    if entry.is_dir:
        for child in transport.listdir(entry.path):
            _delete_tree(transport, child)
    transport.delete(entry.path)


class BrowserView:
    """Two panes side by side on wide screens, one at a time on phones."""

    def __init__(self, app: App) -> None:
        self.app = app
        self.left = Pane(app, self._update_copy_buttons)
        self.right = Pane(app, self._update_copy_buttons)
        self._refresh_after: dict[int, Pane] = {}
        self.compact = False
        self.active = self.left

        self.copy_right = ft.FilledTonalButton(
            "Копировать", icon=ft.Icons.ARROW_FORWARD, on_click=lambda e: self.copy(self.left, self.right)
        )
        self.copy_left = ft.FilledTonalButton(
            "Копировать", icon=ft.Icons.ARROW_BACK, on_click=lambda e: self.copy(self.right, self.left)
        )
        self.middle = ft.Column(
            [self.copy_right, self.copy_left],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=12,
        )
        self.pane_switch = ft.SegmentedButton(
            segments=[
                ft.Segment(value="left", label=ft.Text("Панель 1")),
                ft.Segment(value="right", label=ft.Text("Панель 2")),
            ],
            selected=["left"],
            on_change=self._on_pane_switch,
        )
        self.switch_row = ft.Row([self.pane_switch])
        self.copy_other = ft.FilledButton(
            "Копировать в панель 2", icon=ft.Icons.SWAP_HORIZ, on_click=lambda e: self.copy_from_active()
        )
        self.bottom_row = ft.Row([self.copy_other], alignment=ft.MainAxisAlignment.END)
        self.view = ft.Column(
            [
                headline("Файлы"),
                self.switch_row,
                ft.Row(
                    [self.left.view, self.middle, self.right.view],
                    expand=True,
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                self.bottom_row,
            ],
            expand=True,
            spacing=16,
        )
        self.set_compact(False)
        self._update_copy_buttons()
        app.queue.subscribe(self._on_job)
        self.left.load()
        self.right.load()

    def set_compact(self, compact: bool) -> None:
        self.compact = compact
        self.switch_row.visible = self.bottom_row.visible = compact
        self.middle.visible = not compact
        self.left.view.visible = not compact or self.active is self.left
        self.right.view.visible = not compact or self.active is self.right

    def open_connection(self, connection: Connection) -> None:
        """Show a saved connection in the second pane."""
        self._activate(self.right)
        self.right.set_source(connection)

    def _activate(self, pane: Pane) -> None:
        self.active = pane
        self.pane_switch.selected = ["left" if pane is self.left else "right"]
        self.set_compact(self.compact)
        self._update_copy_buttons()
        safe_update(self.view)

    def _on_pane_switch(self, e: ft.Event[ft.SegmentedButton]) -> None:
        self._activate(self.left if e.control.selected == ["left"] else self.right)

    def copy_from_active(self) -> None:
        other = self.right if self.active is self.left else self.left
        self.copy(self.active, other)

    def copy(self, source: Pane, target: Pane) -> None:
        entries = list(source.selected.values())
        if not entries:
            return
        for entry in entries:
            job = TransferJob(
                name=entry.name,
                source=source.label(entry.path),
                destination=target.label(target.path),
                source_url=source.location(entry.path),
                destination_url=target.destination_dir(),
                recursive=entry.is_dir,
                source_options=source.options(),
                destination_options=target.options(),
            )
            self._refresh_after[job.id] = target
            self.app.queue.add(job)
        source.clear_selection()
        self.app.notify(f"Добавлено в очередь: {len(entries)}", action="Передачи", on_action=lambda e: self.app.go(1))

    def _update_copy_buttons(self) -> None:
        self.copy_right.disabled = not self.left.selected
        self.copy_left.disabled = not self.right.selected
        other = "2" if self.active is self.left else "1"
        self.copy_other.content = f"Копировать в панель {other}"
        self.copy_other.disabled = not self.active.selected
        safe_update(self.copy_right, self.copy_left, self.copy_other)

    def _on_job(self, job: TransferJob) -> None:
        if job.state.finished and job.id in self._refresh_after:
            pane = self._refresh_after.pop(job.id)
            if job.state is JobState.DONE:
                pane.load()
