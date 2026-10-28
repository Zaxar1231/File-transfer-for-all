"""Security dialogs: encrypt and decrypt files (age), and share a file by a one-time link."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from fta.app.ui import safe_update
from fta.crypto import age
from fta.lan.links import local_address, qr_png

if TYPE_CHECKING:
    from fta.app.main import App

TTL_CHOICES = {"600": "10 минут", "3600": "1 час", "86400": "1 день", "604800": "7 дней"}


def my_identity(app: App) -> age.Identity:
    """This device's own age key, created on first use; others encrypt to its age1… recipient."""
    path = app.store.data_dir / "age_identity.txt"
    try:
        return age.Identity.parse(path.read_text().strip().splitlines()[-1])
    except (OSError, IndexError, age.AgeError):
        identity = age.Identity.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# public key: {identity.recipient}\n{identity}\n")
        path.chmod(0o600)
        return identity


def _busy_dialog(app: App, title: str) -> tuple[ft.AlertDialog, ft.Text, ft.ProgressBar]:
    text = ft.Text()
    bar = ft.ProgressBar(value=0)
    dialog = ft.AlertDialog(modal=True, title=ft.Text(title), content=ft.Column([text, bar], tight=True, width=400))
    return dialog, text, bar


class EncryptDialog:
    def __init__(self, app: App, paths: list[Path], on_done) -> None:
        self.app, self.paths, self.on_done = app, paths, on_done
        self.mode = ft.SegmentedButton(
            segments=[
                ft.Segment(value="pass", label=ft.Text("Паролем"), icon=ft.Icon(ft.Icons.PASSWORD)),
                ft.Segment(value="keys", label=ft.Text("Ключом получателя"), icon=ft.Icon(ft.Icons.KEY_OUTLINED)),
            ],
            selected=["pass"],
            on_change=lambda e: self._adapt(),
        )
        self.password = ft.TextField(label="Пароль", password=True, can_reveal_password=True, autofocus=True)
        self.repeat = ft.TextField(label="Повторите пароль", password=True, can_reveal_password=True)
        self.recipients = ft.TextField(
            label="Ключи получателей (age1…)",
            hint_text="по одному в строке; ключ получателя есть у него в Настройках → Шифрование",
            multiline=True,
            min_lines=2,
            max_lines=5,
        )
        self.include_me = ft.Checkbox(label="Также моим ключом (чтобы я сам мог открыть)", value=True)
        self.error = ft.Text(color=ft.Colors.ERROR, theme_style=ft.TextThemeStyle.BODY_SMALL)
        names = ", ".join(p.name for p in paths[:3]) + (f" и ещё {len(paths) - 3}" if len(paths) > 3 else "")
        self.dialog = ft.AlertDialog(
            icon=ft.Icon(ft.Icons.LOCK_OUTLINE),
            title=ft.Text("Зашифровать"),
            content=ft.Column(
                [
                    ft.Text(f"{names} → *.age. Формат age: файлы открываются и этим приложением, и утилитой age."),
                    self.mode,
                    self.password,
                    self.repeat,
                    self.recipients,
                    self.include_me,
                    self.error,
                ],
                tight=True,
                spacing=12,
                width=460,
            ),
            actions=[
                ft.TextButton("Отмена", on_click=lambda e: app.page.pop_dialog()),
                ft.FilledButton("Зашифровать", icon=ft.Icons.LOCK, on_click=self._go),
            ],
        )
        self._adapt()

    def show(self) -> None:
        self.app.page.show_dialog(self.dialog)

    def _adapt(self) -> None:
        by_password = self.mode.selected[0] == "pass"
        self.password.visible = self.repeat.visible = by_password
        self.recipients.visible = self.include_me.visible = not by_password
        safe_update(self.dialog)

    def _options(self) -> dict:
        """Encryption options from the form; a ValueError carries what to fix."""
        if self.mode.selected[0] == "pass":
            password = self.password.value or ""
            if len(password) < 8:
                raise ValueError("Пароль не короче 8 символов")
            if password != self.repeat.value:
                raise ValueError("Пароли не совпадают")
            return {"passphrase": password}
        keys = [line.strip() for line in (self.recipients.value or "").splitlines() if line.strip()]
        if self.include_me.value:
            keys.append(my_identity(self.app).recipient)
        if not keys:
            raise ValueError("Добавьте хотя бы один ключ")
        for key in keys:
            try:
                age.parse_recipient(key)
            except age.AgeError as exc:
                raise ValueError(str(exc)) from None
        return {"recipients": keys}

    def _go(self, e: ft.Event[ft.Control]) -> None:
        try:
            options = self._options()
        except ValueError as exc:
            self.error.value = str(exc)
            safe_update(self.error)
            return
        self.app.page.pop_dialog()
        _run_batch(
            self.app, "Шифрование", self.paths, lambda p, progress: age.encrypt_file(p, progress=progress, **options), self.on_done
        )


class DecryptDialog:
    def __init__(self, app: App, paths: list[Path], on_done) -> None:
        self.app, self.paths, self.on_done = app, paths, on_done
        self.password = ft.TextField(
            label="Пароль (если файл зашифрован паролем)",
            password=True,
            can_reveal_password=True,
            autofocus=True,
            on_submit=self._go,
        )
        self.dialog = ft.AlertDialog(
            icon=ft.Icon(ft.Icons.LOCK_OPEN),
            title=ft.Text("Расшифровать"),
            content=ft.Column(
                [
                    ft.Text("Файлы, зашифрованные вашим ключом, откроются без пароля."),
                    self.password,
                ],
                tight=True,
                spacing=12,
                width=420,
            ),
            actions=[
                ft.TextButton("Отмена", on_click=lambda e: app.page.pop_dialog()),
                ft.FilledButton("Расшифровать", icon=ft.Icons.LOCK_OPEN, on_click=self._go),
            ],
        )

    def show(self) -> None:
        self.app.page.show_dialog(self.dialog)

    def _go(self, e: ft.Event[ft.Control]) -> None:
        password = self.password.value or None
        identity = str(my_identity(self.app))
        self.app.page.pop_dialog()
        _run_batch(
            self.app,
            "Расшифровка",
            self.paths,
            lambda p, progress: age.decrypt_file(p, passphrase=password, identities=[identity], progress=progress),
            self.on_done,
        )


def _run_batch(app: App, title: str, paths: list[Path], action, on_done) -> None:
    dialog, text, bar = _busy_dialog(app, title)
    app.page.show_dialog(dialog)

    def worker() -> None:
        errors, done = [], []
        for path in paths:
            text.value = path.name
            bar.value = 0
            safe_update(text, bar)
            try:
                done.append(action(path, lambda d, t: (setattr(bar, "value", d / t if t else None), safe_update(bar))))
            except Exception as exc:  # shown below; the other files go on
                errors.append(f"{path.name}: {exc}")
        try:
            app.page.pop_dialog()
        except (RuntimeError, AssertionError):
            pass
        on_done()
        if errors:
            app.notify("; ".join(errors), error=True)
        else:
            app.notify(f"{title}: готово ({', '.join(p.name for p in done[:3])}{'…' if len(done) > 3 else ''})")

    app.page.run_thread(worker)


class ShareDialog:
    """A link and a QR code that let anyone nearby download the file in a browser."""

    def __init__(self, app: App, path: Path) -> None:
        self.app, self.path = app, path
        self.ttl = ft.SegmentedButton(
            segments=[ft.Segment(value=key, label=ft.Text(text)) for key, text in TTL_CHOICES.items()],
            selected=["3600"],
        )
        self.one_time = ft.Switch(label="Одноразовая — перестаёт работать после первого скачивания", value=True)
        self.result = ft.Column(spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER, visible=False)
        self.create_button = ft.FilledButton("Создать ссылку", icon=ft.Icons.LINK, on_click=self._create)
        self.dialog = ft.AlertDialog(
            icon=ft.Icon(ft.Icons.SHARE_OUTLINED),
            title=ft.Text(f"Поделиться: {path.name}"),
            content=ft.Column(
                [ft.Text("Срок действия"), self.ttl, self.one_time, self.result],
                tight=True,
                spacing=12,
                width=460,
                scroll=ft.ScrollMode.AUTO,
            ),
            actions=[ft.TextButton("Закрыть", on_click=lambda e: app.page.pop_dialog()), self.create_button],
        )

    def show(self) -> None:
        self.app.page.show_dialog(self.dialog)

    def _create(self, e: ft.Event[ft.Control]) -> None:
        lan = self.app.lan
        if lan is None:
            self.app.notify("Включите «Принимать файлы и быть видимым» на вкладке «Рядом»", error=True)
            return
        url, share = lan.share(self.path, float(self.ttl.selected[0]), bool(self.one_time.value), local_address())
        self.result.controls = [
            ft.Container(ft.Image(src=qr_png(url), width=200, height=200), bgcolor=ft.Colors.WHITE, border_radius=12, padding=6),
            ft.Text(url, selectable=True, theme_style=ft.TextThemeStyle.BODY_SMALL),
            ft.FilledTonalButton("Скопировать ссылку", icon=ft.Icons.COPY, on_click=lambda e: self._copy(url)),
            ft.Text(
                "Откройте ссылку на телефоне или компьютере в той же сети. Браузер предупредит о "
                "сертификате — это сертификат этого устройства, отпечаток виден на вкладке «Рядом». "
                f"Ссылка: {share.status()}.",
                theme_style=ft.TextThemeStyle.BODY_SMALL,
                color=ft.Colors.ON_SURFACE_VARIANT,
                text_align=ft.TextAlign.CENTER,
            ),
        ]
        self.result.visible = True
        self.create_button.visible = False
        self.ttl.disabled = self.one_time.disabled = True
        safe_update(self.dialog)
        self.app.nearby.refresh()

    async def _copy(self, url: str) -> None:
        await self.app.clipboard.set(url)
        self.app.notify("Ссылка скопирована")
