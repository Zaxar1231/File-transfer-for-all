"""Small Material 3 building blocks and formatting helpers shared by the views."""

from __future__ import annotations

from datetime import datetime

import flet as ft

PROTOCOL_ICONS = {
    "sftp": ft.Icons.TERMINAL,
    "smb": ft.Icons.LAN_OUTLINED,
    "ftp": ft.Icons.FOLDER_COPY_OUTLINED,
    "ftps": ft.Icons.FOLDER_COPY_OUTLINED,
    "dav": ft.Icons.CLOUD_OUTLINED,
    "davs": ft.Icons.CLOUD_OUTLINED,
    "s3": ft.Icons.CLOUD_QUEUE,
    "http": ft.Icons.PUBLIC,
    "https": ft.Icons.PUBLIC,
    "rclone": ft.Icons.CLOUD_SYNC_OUTLINED,
}

SEED_COLORS = {
    "#6750A4": "Фиолетовый",
    "#0B57D0": "Синий",
    "#006A6A": "Бирюзовый",
    "#386A20": "Зелёный",
    "#8B5000": "Янтарный",
    "#B3261E": "Красный",
    "#984061": "Розовый",
}


def human_size(size: float | None) -> str:
    if size is None:
        return ""
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def format_time(timestamp: float | None) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M") if timestamp else ""


def short_path(path: str, home: str | None = None, keep: int = 2) -> str:
    """``/home/me/a/b/c`` → ``~/…/b/c``: the end of a path is the part people look for."""
    if home:
        home = home.rstrip("/\\")
        if path == home or path.startswith((home + "/", home + "\\")):
            path = "~" + path[len(home) :]
    sep = "\\" if "\\" in path and "/" not in path else "/"
    parts = path.split(sep)
    if len(parts) <= keep + 2:
        return path
    return sep.join([parts[0], "…", *parts[-keep:]])


def protocol_icon(scheme: str | None) -> ft.IconData:
    return PROTOCOL_ICONS.get(scheme or "", ft.Icons.COMPUTER)


def headline(text: str, *actions: ft.Control) -> ft.Row:
    return ft.Row(
        [ft.Text(text, theme_style=ft.TextThemeStyle.HEADLINE_SMALL, expand=True), *actions],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )


def empty_state(icon: ft.IconData, title: str, hint: str, action: ft.Control | None = None) -> ft.Container:
    return ft.Container(
        alignment=ft.Alignment.CENTER,
        expand=True,
        padding=24,
        content=ft.Column(
            [
                ft.Icon(icon, size=56, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text(title, theme_style=ft.TextThemeStyle.TITLE_MEDIUM, text_align=ft.TextAlign.CENTER),
                ft.Text(hint, color=ft.Colors.ON_SURFACE_VARIANT, text_align=ft.TextAlign.CENTER),
                *([action] if action else []),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=12,
            tight=True,
        ),
    )


def safe_update(*controls: ft.Control) -> None:
    """Update controls that may belong to a tab that is not on screen right now.

    Hidden controls keep their new state and are sent when their tab is shown.
    """
    for control in controls:
        try:
            control.update()
        except (RuntimeError, AssertionError):
            pass
