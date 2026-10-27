"""Nearby screen: devices on the local network, sending to them, incoming files, this device's QR code."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from fta.app.transfers import TransferJob
from fta.app.ui import empty_state, headline, human_size, safe_update
from fta.lan.direct import Offer
from fta.lan.discovery import Peer
from fta.lan.identity import short_fingerprint
from fta.lan.links import PeerLink, local_address, looks_like_address, parse_link, qr_png

if TYPE_CHECKING:
    from fta.app.main import App

DECISION_SECONDS = 120
"""An offer nobody answers is declined after this long."""

_STATES = {
    "pending": "ждёт ответа",
    "accepted": "принимается",
    "declined": "отклонено",
    "done": "получено",
    "cancelled": "отменено",
}


def plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def offer_text(offer: Offer) -> str:
    count = offer.file_count
    return f"{count} {plural(count, 'файл', 'файла', 'файлов')} ({human_size(offer.total_size)}): {offer.summary()}"


class NearbyView:
    def __init__(self, app: App) -> None:
        self.app = app
        self.status = ft.Text(theme_style=ft.TextThemeStyle.BODY_MEDIUM, selectable=True)
        self.receive_switch = ft.Switch(
            label="Принимать файлы и быть видимым", value=app.store.settings.lan_enabled, on_change=self._on_switch
        )
        self.peer_list = ft.Column(spacing=8)
        self.peers_empty = empty_state(
            ft.Icons.DEVICES_OTHER_OUTLINED,
            "Рядом никого не видно",
            "Откройте File Transfer for All на другом устройстве в той же сети "
            "или подключитесь по QR-коду и адресу.",
        )
        self.incoming = ft.Column(spacing=8)
        self.incoming_title = ft.Text("Входящие", theme_style=ft.TextThemeStyle.TITLE_MEDIUM, visible=False)
        self._offer_rows: dict[str, ft.ListTile] = {}
        self.tailnet_title = ft.Row(
            [
                ft.Text("Tailscale", theme_style=ft.TextThemeStyle.TITLE_MEDIUM, expand=True),
                ft.IconButton(ft.Icons.REFRESH, tooltip="Обновить", on_click=lambda e: self.refresh_tailnet()),
            ],
            visible=False,
        )
        self.tailnet = ft.Column(spacing=8)
        self.shares_title = ft.Text("Ссылки на файлы", theme_style=ft.TextThemeStyle.TITLE_MEDIUM, visible=False)
        self.shares = ft.Column(spacing=4)
        self._last_key: tuple[str, str] = ("", "")
        self._last_shown = 0.0

        self.view = ft.Column(
            [
                headline(
                    "Рядом",
                    ft.IconButton(ft.Icons.QR_CODE_2, tooltip="QR-код этого устройства", on_click=lambda e: self.show_qr()),
                    ft.IconButton(ft.Icons.CASINO_OUTLINED, tooltip="Рулетка: файлы случайному устройству рядом", on_click=self._roulette),
                    ft.FilledTonalButton("По адресу", icon=ft.Icons.LINK, on_click=lambda e: self.ask_address()),
                ),
                ft.Column(
                    [
                        ft.Container(
                            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                            border_radius=24,
                            padding=20,
                            content=ft.Column([self.receive_switch, self.status], spacing=8),
                        ),
                        ft.Text("Устройства", theme_style=ft.TextThemeStyle.TITLE_MEDIUM),
                        self.peer_list,
                        self.peers_empty,
                        self.tailnet_title,
                        self.tailnet,
                        self.incoming_title,
                        self.incoming,
                        self.shares_title,
                        self.shares,
                    ],
                    spacing=16,
                    scroll=ft.ScrollMode.AUTO,
                    expand=True,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
            ],
            expand=True,
            spacing=16,
        )
        self.refresh()
        self.refresh_tailnet()

    # --- state -----------------------------------------------------------------------

    def refresh(self) -> None:
        app = self.app
        lan = app.lan
        if lan is not None and lan.running:
            identity = lan.identity
            self.status.value = (
                f"Это устройство: {identity.name} · {local_address()}:{lan.receiver.port}\n"
                f"Отпечаток сертификата: {short_fingerprint(identity.fingerprint)} · "
                f"файлы сохраняются в {lan.receiver.directory}"
                + (f"\n{lan.error}" if lan.error else "")
            )
            self.status.color = ft.Colors.ON_SURFACE_VARIANT
        elif app.lan_error:
            self.status.value = f"Не удалось включить: {app.lan_error}"
            self.status.color = ft.Colors.ERROR
        else:
            self.status.value = "Выключено: устройство не видно другим, отправлять можно по адресу"
            self.status.color = ft.Colors.ON_SURFACE_VARIANT
        self._refresh_shares()
        peers = lan.peers if lan is not None else []
        self.peer_list.controls = [self._peer_card(peer) for peer in peers]
        self.peer_list.visible = bool(peers)
        self.peers_empty.visible = not peers
        safe_update(self.status, self.peer_list, self.peers_empty)

    # --- Tailscale ---------------------------------------------------------------------

    def refresh_tailnet(self) -> None:
        """Devices from Tailscale; each online one is asked in the background whether it runs FTA."""
        from fta.tools import tailscale

        if tailscale.find() is None:
            return

        def worker() -> None:
            from fta.lan.direct import fetch_info
            from fta.lan.links import DEFAULT_PORT

            try:
                peers = [p for p in tailscale.peers() if p.online]
            except Exception as exc:  # logged out, daemon stopped
                self.tailnet.controls = [ft.Text(f"Tailscale: {exc}", color=ft.Colors.ON_SURFACE_VARIANT)]
                self.tailnet_title.visible = True
                safe_update(self.tailnet_title, self.tailnet)
                return
            cards = []
            for peer in peers:
                try:
                    info, fingerprint = fetch_info(peer.address, DEFAULT_PORT, timeout=2)
                except Exception:  # noqa: BLE001 - no FTA there: Taildrop it is
                    cards.append(self._tailnet_card(peer, None))
                else:
                    cards.append(self._tailnet_card(peer, PeerLink(peer.address, DEFAULT_PORT, fingerprint, info.get("name") or peer.name)))
            self.tailnet.controls = cards or [ft.Text("В сети Tailscale никого нет онлайн", color=ft.Colors.ON_SURFACE_VARIANT)]
            self.tailnet_title.visible = True
            safe_update(self.tailnet_title, self.tailnet)

        self.app.page.run_thread(worker)

    def _tailnet_card(self, peer, link: PeerLink | None) -> ft.Card:
        via = "File Transfer for All" if link else "Taildrop"
        return ft.Card(
            variant=ft.CardVariant.OUTLINED,
            content=ft.ListTile(
                leading=ft.CircleAvatar(content=ft.Icon(ft.Icons.VPN_LOCK), bgcolor=ft.Colors.SECONDARY_CONTAINER),
                title=ft.Text(peer.name, theme_style=ft.TextThemeStyle.TITLE_MEDIUM),
                subtitle=ft.Text(f"{peer.address} · {peer.os} · через {via}", color=ft.Colors.ON_SURFACE_VARIANT),
                trailing=ft.IconButton(ft.Icons.SEND_OUTLINED, tooltip=f"Отправить через {via}", on_click=lambda e: self._tailnet_send(peer, link)),
                on_click=lambda e: self._tailnet_send(peer, link),
            ),
        )

    async def _tailnet_send(self, peer, link: PeerLink | None) -> None:
        if link is not None:
            await self._pick_files(link)
            return
        from fta.core.manager import FileResult
        from fta.tools import tailscale

        files = await self.app.picker.pick_files(dialog_title=f"Taildrop: {peer.name}", allow_multiple=True)
        paths = [Path(f.path) for f in files or [] if f.path]
        if not paths:
            return

        def action(progress, on_file, resume):
            total = sum(p.stat().st_size for p in paths)
            started = time.monotonic()
            tailscale.taildrop(paths, peer)
            progress(total, total)
            return [FileResult(str(p), f"/{p.name}", p.stat().st_size, time.monotonic() - started) for p in paths]

        self.app.queue.add(
            TransferJob(
                name=paths[0].name if len(paths) == 1 else f"{paths[0].name} и ещё {len(paths) - 1}",
                source=str(paths[0].parent),
                destination=f"🔒 {peer.name} (Taildrop)",
                source_url="",
                destination_url=f"taildrop://{peer.address}",
                action=action,
            )
        )
        self.app.notify(f"Taildrop: отправляем на {peer.name}", action="Передачи", on_action=lambda e: self.app.go(1))

    def _refresh_shares(self) -> None:
        shares = self.app.lan.receiver.shares.listing() if self.app.lan else []
        self.shares.controls = [
            ft.ListTile(
                leading=ft.Icon(ft.Icons.LINK if share.active else ft.Icons.LINK_OFF),
                title=ft.Text(share.name),
                subtitle=ft.Text(share.status(), color=ft.Colors.ON_SURFACE_VARIANT),
                trailing=ft.IconButton(
                    ft.Icons.BLOCK,
                    tooltip="Отозвать ссылку",
                    visible=share.active,
                    on_click=lambda e, t=share.token: self._revoke(t),
                ),
            )
            for share in shares
        ]
        self.shares_title.visible = bool(shares)
        safe_update(self.shares_title, self.shares)

    def _revoke(self, token: str) -> None:
        if self.app.lan:
            self.app.lan.receiver.shares.revoke(token)
        self._refresh_shares()

    def _peer_card(self, peer: Peer) -> ft.Card:
        details = f"{peer.address}" + (f" · SSH {peer.ssh_port}" if peer.ssh_port else "") + (
            f" · {short_fingerprint(peer.fingerprint)}" if peer.fingerprint else ""
        )
        return ft.Card(
            variant=ft.CardVariant.OUTLINED,
            content=ft.ListTile(
                leading=ft.CircleAvatar(
                    content=ft.Icon(ft.Icons.DEVICES),
                    bgcolor=ft.Colors.TERTIARY_CONTAINER,
                    color=ft.Colors.ON_TERTIARY_CONTAINER,
                ),
                title=ft.Text(peer.name, theme_style=ft.TextThemeStyle.TITLE_MEDIUM),
                subtitle=ft.Text(details, color=ft.Colors.ON_SURFACE_VARIANT),
                trailing=ft.PopupMenuButton(
                    icon=ft.Icons.SEND_OUTLINED,
                    tooltip="Отправить",
                    items=[
                        ft.PopupMenuItem(content="Файлы…", icon=ft.Icons.DESCRIPTION_OUTLINED, on_click=lambda e, p=peer: self._pick_files(p)),
                        ft.PopupMenuItem(content="Папку…", icon=ft.Icons.FOLDER_OUTLINED, on_click=lambda e, p=peer: self._pick_folder(p)),
                        ft.PopupMenuItem(content="Записка или мем…", icon=ft.Icons.MAIL_OUTLINE, on_click=lambda e, p=peer: self._note(p)),
                        ft.PopupMenuItem(
                            content="С самоуничтожением…", icon=ft.Icons.TIMER_OUTLINED, on_click=lambda e, p=peer: self._bomb(p)
                        ),
                        *(
                            [
                                ft.PopupMenuItem(
                                    content="Файлы и терминал по SSH…",
                                    icon=ft.Icons.TERMINAL,
                                    on_click=lambda e, p=peer: self._quick(p),
                                ),
                            ]
                            if peer.ssh_port
                            else []
                        ),
                    ],
                ),
                on_click=lambda e, p=peer: self._pick_files(p),
                content_padding=ft.Padding.symmetric(horizontal=16, vertical=4),
            ),
        )

    def _quick(self, peer: Peer) -> None:
        """A device whose SSH server is on: its shared folder or its transfer shell, by address."""
        self.app.quick_connect(host=peer.address, port=peer.ssh_port, username="fta", name=peer.name)

    def _on_switch(self, e: ft.Event[ft.Switch]) -> None:
        self.app.store.settings.lan_enabled = bool(e.control.value)
        self.app.store.save()
        self.app.apply_lan()

    # --- sending ---------------------------------------------------------------------

    async def _pick_files(self, target: Peer | PeerLink, pin: str = "") -> None:
        files = await self.app.picker.pick_files(dialog_title=f"Отправить: {_name(target)}", allow_multiple=True)
        paths = [Path(f.path) for f in files or [] if f.path]
        if files and not paths:
            self.app.notify("Система не дала путь к файлам; выберите их через вкладку «Файлы»", error=True)
        if paths:
            self.send(target, paths, pin)

    async def _pick_folder(self, target: Peer | PeerLink) -> None:
        path = await self.app.picker.get_directory_path(dialog_title=f"Отправить папку: {_name(target)}")
        if path:
            self.send(target, [Path(path)])

    def send(
        self, target: Peer | PeerLink, paths: list[Path], pin: str = "", message: str = "", burn_after: float | None = None
    ) -> None:
        if self.app.lan is None:
            self.app.notify("Включите «Принимать файлы и быть видимым», чтобы отправлять", error=True)
            return
        if not paths:
            name = "✉ Записка"
        else:
            name = paths[0].name if len(paths) == 1 else f"{paths[0].name} и ещё {len(paths) - 1}"
            name = ("💣 " if burn_after else "") + name
        host = target.address if isinstance(target, Peer) else target.host
        self.app.queue.add(
            TransferJob(
                name=name,
                source=str(paths[0].parent) if paths else "записка",
                destination=f"📱 {_name(target)}",
                source_url="",
                destination_url=f"fta://{host}:{target.port}",
                action=self.app.lan.sender(target, paths, pin=pin, message=message, burn_after=burn_after),
                max_retries=self.app.store.settings.retries,
            )
        )
        self.app.notify(f"Ждём, пока {_name(target)} примет файлы", action="Передачи", on_action=lambda e: self.app.go(1))

    # --- fun: notes, the timer bomb, the roulette ----------------------------------------

    def _note(self, target: Peer | PeerLink) -> None:
        from fta.fun import random_meme

        text = ft.TextField(label="Записка", multiline=True, min_lines=2, max_lines=6, max_length=2000, autofocus=True)

        def meme(_: ft.Event[ft.Control]) -> None:
            text.value = random_meme()
            safe_update(text)

        def go(_: ft.Event[ft.Control]) -> None:
            if not (text.value or "").strip():
                return
            self.app.page.pop_dialog()
            self.send(target, [], message=text.value.strip())

        self.app.page.show_dialog(
            ft.AlertDialog(
                icon=ft.Icon(ft.Icons.MAIL_OUTLINE),
                title=ft.Text(f"Мем-почта: {_name(target)}"),
                content=ft.Column([text], tight=True, width=420),
                actions=[
                    ft.TextButton("Случайный мем", icon=ft.Icons.SHUFFLE, on_click=meme),
                    ft.TextButton("Отмена", on_click=lambda e: self.app.page.pop_dialog()),
                    ft.FilledButton("Отправить", icon=ft.Icons.SEND, on_click=go),
                ],
            )
        )

    def _bomb(self, target: Peer | PeerLink) -> None:
        minutes = ft.SegmentedButton(
            segments=[ft.Segment(value=str(m), label=ft.Text(label)) for m, label in ((5, "5 мин"), (30, "30 мин"), (60, "1 час"), (1440, "1 день"))],
            selected=["30"],
        )

        async def go(_: ft.Event[ft.Control]) -> None:
            seconds = int(minutes.selected[0]) * 60
            self.app.page.pop_dialog()
            files = await self.app.picker.pick_files(dialog_title="Файлы с самоуничтожением", allow_multiple=True)
            paths = [Path(f.path) for f in files or [] if f.path]
            if paths:
                self.send(target, paths, message=f"💣 Исчезнет через {minutes.selected[0]} мин после получения", burn_after=seconds)

        self.app.page.show_dialog(
            ft.AlertDialog(
                icon=ft.Icon(ft.Icons.TIMER_OUTLINED),
                title=ft.Text("Таймер-бомба"),
                content=ft.Column(
                    [
                        ft.Text(f"Файлы удалятся у {_name(target)} через выбранное время после получения. Получатель увидит это до того, как примет."),
                        minutes,
                    ],
                    tight=True,
                    spacing=12,
                    width=440,
                ),
                actions=[
                    ft.TextButton("Отмена", on_click=lambda e: self.app.page.pop_dialog()),
                    ft.FilledButton("Выбрать файлы", icon=ft.Icons.ATTACH_FILE, on_click=go),
                ],
            )
        )

    async def _roulette(self, e: ft.Event[ft.Control]) -> None:
        import random

        peers = self.app.lan.peers if self.app.lan else []
        if not peers:
            self.app.notify("Рядом никого — рулетке некого выбрать", error=True)
            return
        files = await self.app.picker.pick_files(dialog_title="Рулетка: что отправить", allow_multiple=True)
        paths = [Path(f.path) for f in files or [] if f.path]
        if not paths:
            return
        winner = random.choice(peers)

        def go(_: ft.Event[ft.Control]) -> None:
            self.app.page.pop_dialog()
            self.send(winner, paths, message="🎲 Тебе выпало в рулетку!")

        self.app.page.show_dialog(
            ft.AlertDialog(
                icon=ft.Icon(ft.Icons.CASINO),
                title=ft.Text(f"Выпало: {winner.name}"),
                content=ft.Text(f"Отправить {len(paths)} файл(ов) на {winner.describe()}? Получатель всё равно подтвердит приём."),
                actions=[
                    ft.TextButton("Не надо", on_click=lambda e: self.app.page.pop_dialog()),
                    ft.FilledButton("Крутим!", icon=ft.Icons.CASINO, on_click=go),
                ],
            )
        )

    def ask_address(self) -> None:
        field = ft.TextField(
            label="Адрес или ссылка",
            hint_text="192.168.1.20 или fta://… из QR-кода",
            autofocus=True,
        )
        pin = ft.TextField(label="PIN (если устройство его просит)", width=220, keyboard_type=ft.KeyboardType.NUMBER)

        async def go(_: ft.Event[ft.Control]) -> None:
            text = (field.value or "").strip()
            try:
                if not looks_like_address(text):
                    raise ValueError("нужен IP-адрес, имя.local или ссылка fta://")
                link = parse_link(text)
            except ValueError as exc:
                field.error = str(exc)
                safe_update(field)
                return
            self.app.page.pop_dialog()
            await self._pick_files(link, (pin.value or "").strip())

        field.on_submit = go
        self.app.page.show_dialog(
            ft.AlertDialog(
                icon=ft.Icon(ft.Icons.LINK),
                title=ft.Text("Отправить по адресу"),
                content=ft.Column(
                    [
                        field,
                        pin,
                        ft.Text(
                            "Ссылка из QR-кода содержит отпечаток сертификата: приложение проверит, что отвечает "
                            "именно то устройство. Голый адрес такой проверки не даёт.",
                            theme_style=ft.TextThemeStyle.BODY_SMALL,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                    tight=True,
                    spacing=12,
                    width=420,
                ),
                actions=[
                    ft.TextButton("Отмена", on_click=lambda e: self.app.page.pop_dialog()),
                    ft.FilledButton("Выбрать файлы", on_click=go),
                ],
            )
        )

    def show_qr(self) -> None:
        lan = self.app.lan
        if lan is None:
            self.app.notify("Сначала включите «Принимать файлы и быть видимым»", error=True)
            return
        link = lan.link(local_address())
        self.app.page.show_dialog(
            ft.AlertDialog(
                icon=ft.Icon(ft.Icons.QR_CODE_2),
                title=ft.Text(lan.identity.name),
                content=ft.Column(
                    [
                        ft.Container(
                            ft.Image(src=qr_png(link.url()), width=240, height=240),
                            bgcolor=ft.Colors.WHITE,
                            border_radius=16,
                            padding=8,
                        ),
                        ft.Text(
                            "Отсканируйте камерой другого устройства или введите ссылку в «По адресу»",
                            text_align=ft.TextAlign.CENTER,
                        ),
                        ft.Text(link.url(), selectable=True, theme_style=ft.TextThemeStyle.BODY_SMALL),
                    ],
                    tight=True,
                    spacing=12,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    width=320,
                ),
                actions=[ft.TextButton("Готово", on_click=lambda e: self.app.page.pop_dialog())],
            )
        )

    # --- receiving (called from receiver threads) ----------------------------------------

    def decide(self, offer: Offer) -> bool:
        """Ask the user about an offer and wait for the answer; silence declines it."""
        answer = {"accepted": False}
        decided = threading.Event()

        def choose(accepted: bool) -> None:
            answer["accepted"] = accepted
            decided.set()
            try:
                self.app.page.pop_dialog()
            except (RuntimeError, AssertionError):
                pass

        self.app.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                icon=ft.Icon(ft.Icons.DOWNLOAD_FOR_OFFLINE_OUTLINED),
                title=ft.Text(f"{offer.sender_name} отправляет файлы"),
                content=ft.Column(
                    [
                        ft.Text(offer_text(offer)),
                        *([ft.Text(f"«{offer.message}»", italic=True)] if offer.message else []),
                        *(
                            [
                                ft.Text(
                                    f"💣 Файлы удалятся сами через {int(offer.burn_after // 60)} мин после получения",
                                    color=ft.Colors.ERROR,
                                )
                            ]
                            if offer.burn_after
                            else []
                        ),
                        ft.Text(
                            f"С адреса {offer.address}. Сохранятся в {self.app.lan.receiver.directory if self.app.lan else ''}",
                            theme_style=ft.TextThemeStyle.BODY_SMALL,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                    tight=True,
                    spacing=8,
                    width=420,
                ),
                actions=[
                    ft.TextButton("Отклонить", on_click=lambda e: choose(False)),
                    ft.FilledButton("Принять", icon=ft.Icons.CHECK, on_click=lambda e: choose(True)),
                ],
            )
        )
        if not decided.wait(DECISION_SECONDS):
            choose(False)
        return answer["accepted"]

    def offer_changed(self, offer: Offer) -> None:
        now = time.monotonic()
        key = (offer.session, offer.state)
        if key == self._last_key and now - self._last_shown < 0.2:
            return  # bytes arrive in 256 KiB chunks; five redraws a second are enough
        self._last_key, self._last_shown = key, now
        row = self._offer_rows.get(offer.session)
        if row is None:
            if offer.state == "pending":
                return
            row = ft.ListTile(
                leading=ft.Icon(ft.Icons.DOWNLOAD),
                title=ft.Text(),
                subtitle=ft.Text(color=ft.Colors.ON_SURFACE_VARIANT),
                trailing=ft.IconButton(
                    ft.Icons.CLOSE, tooltip="Остановить", on_click=lambda e, o=offer: self.app.lan and self.app.lan.receiver.cancel(o)
                ),
            )
            self._offer_rows[offer.session] = row
            self.incoming.controls.insert(0, row)
            self.incoming_title.visible = True
        total = offer.total_size
        progress = f" · {offer.received * 100 // total}%" if total and offer.state == "accepted" else ""
        row.title.value = f"{offer.sender_name}: {offer.summary()}" if offer.files else f"✉ {offer.sender_name}: {offer.message}"
        row.subtitle.value = f"{_STATES.get(offer.state, offer.state)}{progress} · {human_size(offer.received)} из {human_size(total)}"
        row.trailing.visible = offer.state == "accepted"
        safe_update(self.incoming_title, self.incoming)
        if offer.state == "done":
            self.app.received(offer)


def _name(target: Peer | PeerLink) -> str:
    return target.name or getattr(target, "host", "") or getattr(target, "address", "")
