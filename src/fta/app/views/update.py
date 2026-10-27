"""Update flow in the UI: check on start or on demand, offer the new version, download and open it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from fta import __version__
from fta.app import updates
from fta.app.ui import human_size

if TYPE_CHECKING:
    from fta.app.main import App


class Updater:
    def __init__(self, app: App) -> None:
        self.app = app
        self.launcher = ft.UrlLauncher()  # registers itself as a page service
        self.checking = False

    @property
    def platform_key(self) -> str | None:
        platform = self.app.page.platform
        return updates.platform_key(platform.value if platform else None)

    def check_in_background(self, silent: bool) -> None:
        if self.app.page.web or self.checking:
            return
        self.checking = True
        self.app.page.run_thread(self._check, silent)

    def _check(self, silent: bool) -> None:
        try:
            release = updates.fetch_latest()
        except Exception as exc:  # offline, rate limited, GitHub down: never bother the user unless asked
            if not silent:
                self.app.notify(f"Не удалось проверить обновления: {exc}", error=True)
            return
        finally:
            self.checking = False
        if release and updates.is_newer(release.version):
            self._offer(release)
        elif not silent:
            self.app.notify(f"Установлена последняя версия ({__version__})")

    def _offer(self, release: updates.Release) -> None:
        url = release.download_url(self.platform_key)
        notes = ft.Markdown(release.notes or "Описание выпуска на странице релиза.", selectable=True)
        actions: list[ft.Control] = [ft.TextButton("Позже", on_click=lambda e: self.app.page.pop_dialog())]
        if url:
            actions.append(ft.FilledButton("Обновить", icon=ft.Icons.DOWNLOAD, on_click=lambda e: self._install(url)))
        else:
            actions.append(
                ft.FilledButton(
                    "Страница релиза",
                    icon=ft.Icons.OPEN_IN_NEW,
                    on_click=lambda e: (self.app.page.pop_dialog(), self.open_url(release.page_url)),
                )
            )
        self.app.page.show_dialog(
            ft.AlertDialog(
                icon=ft.Icon(ft.Icons.SYSTEM_UPDATE),
                title=ft.Text(f"Доступна версия {release.version}"),
                content=ft.Column(
                    [ft.Text(f"Сейчас установлена {__version__}."), notes],
                    tight=True,
                    spacing=12,
                    width=440,
                ),
                actions=actions,
                scrollable=True,  # long release notes scroll inside the dialog
            )
        )

    def _install(self, url: str) -> None:
        self.app.page.pop_dialog()
        if self.platform_key == "android":
            # The system downloads the APK and hands it to the package installer.
            self.open_url(url)
            return
        bar = ft.ProgressBar(value=None)
        status = ft.Text("Подключение…", theme_style=ft.TextThemeStyle.BODY_SMALL)
        dialog = ft.AlertDialog(
            modal=True,
            icon=ft.Icon(ft.Icons.DOWNLOAD),
            title=ft.Text("Загрузка обновления"),
            content=ft.Column([bar, status], tight=True, spacing=12, width=400),
        )
        self.app.page.show_dialog(dialog)

        def progress(done: int, total: int | None) -> None:
            bar.value = done / total if total else None
            status.value = f"{human_size(done)} из {human_size(total)}" if total else human_size(done)
            bar.update()
            status.update()

        def worker() -> None:
            try:
                path = updates.download(url, updates.downloads_folder(), progress)
            except Exception as exc:  # shown to the user, who can retry from Settings
                self.app.page.pop_dialog()
                self.app.notify(f"Не удалось скачать обновление: {exc}", error=True)
                return
            self.app.page.pop_dialog()
            self._installed(path)

        self.app.page.run_thread(worker)

    def _installed(self, path) -> None:
        how = {
            "windows-x64": "Распакуйте архив и замените папку со старой версией.",
            "macos-arm64": "Распакуйте архив и перетащите приложение в «Программы» с заменой.",
        }.get(self.platform_key or "", "Распакуйте архив на место старой версии.")
        if (self.platform_key or "").startswith("macos"):
            how = "Распакуйте архив и перетащите приложение в «Программы» с заменой."
        self.app.page.show_dialog(
            ft.AlertDialog(
                icon=ft.Icon(ft.Icons.CHECK_CIRCLE),
                title=ft.Text("Обновление скачано"),
                content=ft.Text(f"{path}\n\n{how} Затем перезапустите приложение."),
                actions=[
                    ft.TextButton("Закрыть", on_click=lambda e: self.app.page.pop_dialog()),
                    ft.FilledButton(
                        "Показать файл", icon=ft.Icons.FOLDER_OPEN_OUTLINED, on_click=lambda e: updates.reveal(path)
                    ),
                ],
            )
        )

    def open_url(self, url: str) -> None:
        self.app.page.run_task(self.launcher.launch_url, url)
