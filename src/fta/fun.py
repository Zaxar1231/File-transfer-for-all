"""The playful bits: statistics and a summary card, the hedgehog, «Вжух», memes, the Konami code."""

from __future__ import annotations

import random
import time
from collections import Counter, defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape

from fta.core.journal import JournalEntry

KONAMI = ["Arrow Up", "Arrow Up", "Arrow Down", "Arrow Down", "Arrow Left", "Arrow Right", "Arrow Left", "Arrow Right", "B", "A"]

HEDGEHOG = r"""
        .|||||||||.
       |||||||||||||
      /. `||||||||||||
     o__,_|||||||||||'
"""

VZHUH = [
    "Вжух! — и {name} уже там",
    "Вжух! {name} долетел, ни один бит не пострадал",
    "Вжух — и готово: {name}",
    "Ёжик донёс {name}. Пф-ф!",
    "{name}: доставлено. Можно пить чай",
]

MEMES = [
    "Вжух — и ты получил файл. А мог бы получить зарплату",
    "Файлы передаются, ёжики бегают, всё под контролем",
    "Это не баг, это фича передачи",
    "Работает? Не трогай. Передаётся? Не трогай тем более",
    "Пятница, вечер, деплой… передай мне лучше этот мем",
    "Когда скачал файл с первого раза без докачки — день удался",
    "Сисадмин: «Я же говорил — сначала проверь контрольную сумму»",
]


def vzhuh(name: str, rng: random.Random | None = None) -> str:
    return (rng or random).choice(VZHUH).format(name=name)


def random_meme(rng: random.Random | None = None) -> str:
    return (rng or random).choice(MEMES)


class KonamiDetector:
    """Feed key names; ``press`` returns True the moment the whole code has been typed."""

    def __init__(self, code: list[str] = KONAMI) -> None:
        self.code = code
        self.recent: deque[str] = deque(maxlen=len(code))

    def press(self, key: str) -> bool:
        self.recent.append(key.upper() if len(key) == 1 else key)
        if list(self.recent) == self.code:
            self.recent.clear()
            return True
        return False


def hedgehog_bar(done: int, total: int | None, width: int = 30, emoji: bool = True) -> str:
    """A progress bar with a hedgehog running along it."""
    if not total:
        position = (int(time.monotonic() * 8)) % width
        track = "·" * width
        return track[:position] + ("🦔" if emoji else "@") + track[position + 1 :]
    filled = min(width, int(width * done / total))
    runner = "🦔" if emoji else "@"
    return "▬" * filled + runner + "·" * (width - filled) + f" {done * 100 // total:3d}%"


# --- statistics ----------------------------------------------------------------------


@dataclass
class Stats:
    files: int = 0
    bytes: int = 0
    failed: int = 0
    verified: int = 0
    saved_by_resume: int = 0
    seconds: float = 0.0
    fastest: tuple[float, str] = (0.0, "")
    """(bytes per second, file) of the fastest transfer longer than a second."""
    biggest: tuple[int, str] = (0, "")
    by_day: dict[date, int] = field(default_factory=dict)
    by_protocol: Counter = field(default_factory=Counter)
    busiest_hour: int | None = None

    @property
    def average_speed(self) -> float:
        return self.bytes / self.seconds if self.seconds else 0.0


def statistics(entries: Iterable[JournalEntry], since: float | None = None) -> Stats:
    stats = Stats()
    by_day: dict[date, int] = defaultdict(int)
    hours: Counter = Counter()
    for entry in entries:
        if since is not None and entry.time < since:
            continue
        if entry.status != "done":
            stats.failed += entry.status == "failed"
            continue
        stats.files += 1
        stats.bytes += entry.size
        stats.seconds += entry.seconds or 0.0
        stats.verified += bool(entry.verified)
        stats.saved_by_resume += entry.resumed_from or 0
        when = datetime.fromtimestamp(entry.time)
        by_day[when.date()] += entry.size
        hours[when.hour] += 1
        stats.by_protocol[_protocol(entry)] += 1
        if entry.size > stats.biggest[0]:
            stats.biggest = (entry.size, entry.source)
        if (entry.seconds or 0) >= 1:
            speed = entry.size / entry.seconds
            if speed > stats.fastest[0]:
                stats.fastest = (speed, entry.source)
    stats.by_day = dict(by_day)
    stats.busiest_hour = hours.most_common(1)[0][0] if hours else None
    return stats


def _protocol(entry: JournalEntry) -> str:
    for side in (entry.destination, entry.source):
        if "://" in side:
            return side.split("://", 1)[0].upper()
    return "Локально"


def human(size: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def last_days(stats: Stats, days: int = 14, today: date | None = None) -> list[tuple[date, int]]:
    today = today or date.today()
    return [(today - timedelta(days=offset), stats.by_day.get(today - timedelta(days=offset), 0)) for offset in range(days - 1, -1, -1)]


def summary_card_svg(stats: Stats, title: str, subtitle: str, accent: str = "#6750A4") -> str:
    """A shareable picture of the totals: plain SVG, opens in any browser or messenger preview."""
    bars = last_days(stats, 14)
    peak = max((value for _, value in bars), default=0) or 1
    bar_svg = "".join(
        f'<rect x="{40 + i * 36}" y="{420 - int(140 * value / peak)}" width="24" height="{max(2, int(140 * value / peak))}" '
        f'rx="6" fill="{accent}" opacity="{0.35 + 0.65 * value / peak:.2f}"/>'
        for i, (_, value) in enumerate(bars)
    )
    top = ", ".join(f"{name} {count}" for name, count in stats.by_protocol.most_common(3)) or "—"
    facts = [
        ("Файлов", f"{stats.files}"),
        ("Передано", human(stats.bytes)),
        ("Средняя скорость", f"{human(stats.average_speed)}/с"),
        ("Сэкономлено докачкой", human(stats.saved_by_resume)),
    ]
    fact_svg = "".join(
        f'<text x="{40 + (i % 2) * 280}" y="{150 + (i // 2) * 80}" font-size="16" fill="#49454F">{escape(label)}</text>'
        f'<text x="{40 + (i % 2) * 280}" y="{182 + (i // 2) * 80}" font-size="30" font-weight="700" fill="#1D1B20">{escape(value)}</text>'
        for i, (label, value) in enumerate(facts)
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="600" height="500" viewBox="0 0 600 500" font-family="Roboto, Segoe UI, Helvetica, Arial, sans-serif">
<rect width="600" height="500" rx="32" fill="#FEF7FF"/>
<rect x="0" y="0" width="600" height="96" rx="32" fill="{accent}"/>
<rect x="0" y="64" width="600" height="32" fill="{accent}"/>
<text x="40" y="52" font-size="28" font-weight="700" fill="#FFFFFF">{escape(title)}</text>
<text x="40" y="80" font-size="16" fill="#FFFFFF" opacity="0.85">{escape(subtitle)}</text>
{fact_svg}
<text x="40" y="270" font-size="16" fill="#49454F">Чаще всего: {escape(top)}</text>
{bar_svg}
<text x="40" y="460" font-size="14" fill="#79747E">последние 14 дней · File Transfer for All 🦔</text>
</svg>
"""
