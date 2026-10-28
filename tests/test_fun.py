"""The playful features: notes, the timer bomb, statistics, the summary card, Konami, the hedgehog."""

from __future__ import annotations

import random
import time
import xml.dom.minidom
from datetime import datetime

import pytest

from fta import fun
from fta.core.journal import JournalEntry
from fta.lan.burn import Burner
from fta.lan.direct import send_files
from test_lan import receiver, sender  # noqa: F401 - fixtures


def test_a_note_alone_is_delivered_without_files(receiver, sender):  # noqa: F811
    result = send_files("127.0.0.1", [], port=receiver.port, sender=sender, message="Вжух! Держи мем")
    assert result.files == 0
    offer = list(receiver.offers.values())[-1]
    assert offer.message == "Вжух! Держи мем" and offer.state == "done" and receiver.offers_seen == []
    assert not any(receiver.directory.iterdir())


def test_timer_bomb_deletes_received_files(tmp_path, receiver, sender):  # noqa: F811
    folder = tmp_path / "secret"
    folder.mkdir()
    (folder / "plan.txt").write_text("burn after reading")
    send_files("127.0.0.1", [folder], port=receiver.port, sender=sender, burn_after=60, message="💣")
    offer = receiver.offers_seen[-1]
    assert offer.burn_after == 60 and offer.message == "💣"  # the receiving user is told before accepting
    received = receiver.directory / "secret"
    assert received.exists()
    assert receiver.burner.sweep(time.time() + 30) == []
    assert receiver.burner.sweep(time.time() + 61) == [str(received)]
    assert not received.exists()


def test_burn_list_survives_a_restart(tmp_path):
    victim = tmp_path / "file.txt"
    victim.write_text("x")
    first = Burner(tmp_path / "burn.json")
    first.schedule([victim], 3600)
    first.stop()
    later = Burner(tmp_path / "burn.json")
    assert later.pending and victim.exists()
    assert later.sweep(time.time() + 3601) == [str(victim)] and not victim.exists()


def test_bad_self_destruct_times_are_refused(tmp_path, receiver):  # noqa: F811
    from fta.core.errors import TransferError

    (tmp_path / "f").write_text("x")
    for bad in (1, 10**9):
        with pytest.raises(TransferError, match="self-destruct"):
            send_files("127.0.0.1", [tmp_path / "f"], port=receiver.port, burn_after=bad)


def entries():
    day = datetime(2026, 10, 20, 14, 0).timestamp()
    return [
        JournalEntry("/a/movie.mkv", "sftp://nas/movie.mkv", 3_000_000_000, "done", seconds=30, verified=True, time=day),
        JournalEntry("/a/doc.pdf", "s3://bucket/doc.pdf", 1_000_000, "done", seconds=0.2, resumed_from=500_000, time=day + 86400),
        JournalEntry("/a/x", "ftp://h/x", 10, "failed", time=day),
        JournalEntry("/a/y", "/b/y", 5, "done", seconds=2, time=day + 3600),
    ]


def test_statistics():
    stats = fun.statistics(entries())
    assert stats.files == 3 and stats.failed == 1 and stats.verified == 1
    assert stats.bytes == 3_001_000_005 and stats.saved_by_resume == 500_000
    assert stats.biggest == (3_000_000_000, "/a/movie.mkv")
    assert stats.fastest[1] == "/a/movie.mkv"
    assert stats.by_protocol["SFTP"] == 1 and stats.by_protocol["Локально"] == 1
    assert stats.busiest_hour in (14, 15)
    assert fun.statistics(entries(), since=datetime(2026, 10, 21).timestamp()).files == 1


def test_summary_card_is_valid_svg():
    svg = fun.summary_card_svg(fun.statistics(entries()), "Итоги октября", "File Transfer for All")
    document = xml.dom.minidom.parseString(svg.encode())
    assert document.documentElement.tagName == "svg"
    assert "Итоги октября" in svg and "2.8 ГБ" in svg
    assert "<script" not in svg  # safe to open anywhere


def test_konami_code():
    detector = fun.KonamiDetector()
    assert not any(detector.press(key) for key in fun.KONAMI[:-1])
    assert detector.press("a")
    # A wrong key starts over; a repeated first key keeps the first step.
    assert not detector.press("Arrow Up") and not detector.press("Arrow Up") and not detector.press("Arrow Up")
    for key in fun.KONAMI[2:-1]:
        assert not detector.press(key)
    assert detector.press("A")


def test_hedgehog_and_vzhuh():
    bar = fun.hedgehog_bar(50, 100, width=20)
    assert "🦔" in bar and bar.endswith(" 50%") and bar.startswith("▬" * 10)
    assert "@" in fun.hedgehog_bar(10, None, emoji=False)
    assert "отчёт.pdf" in fun.vzhuh("отчёт.pdf", random.Random(1))
    assert fun.random_meme(random.Random(2)) in fun.MEMES
    assert "|||" in fun.HEDGEHOG


def test_shell_fun_commands(tmp_path):
    from fta.app.transfers import TransferQueue
    from fta.core.journal import Journal
    from fta.server.shell import Shell, ShellContext

    journal = Journal(tmp_path / "j.jsonl")
    for entry in entries():
        journal.append(entry)
    shell = Shell(ShellContext(root=tmp_path, queue=TransferQueue(update_interval=0), journal=journal))
    assert "|||" in shell.run("ёж") and "Вжух" in shell.run("вжух")
    assert "files 3" in shell.run("stats") and "SFTP" in shell.run("статистика")


def test_sysadmin_status_line():
    pytest.importorskip("flet")
    from fta.app.transfers import JobState, TransferJob
    from fta.app.views.transfers import describe
    from fta.core.manager import FileResult

    job = TransferJob("a.bin", "/a", "sftp://nas", "/a/a.bin", "sftp://nas/in/", max_retries=3)
    job.state, job.attempts, job.bytes_done = JobState.DONE, 2, 1234567
    job.results = [FileResult("/a/a.bin", "/in/a.bin", 1234567, 1.0, "ab" * 32, True)]
    plain, raw = describe(job), describe(job, sysadmin=True)
    assert "\n" not in plain
    assert "попытка 2/4" in raw and "1 234 567 Б" in raw and "sha256 " + "ab" * 32 in raw and "sftp://nas/in/" in raw
