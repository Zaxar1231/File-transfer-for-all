"""Direct device-to-device transfer, device links and QR codes, mDNS discovery."""

from __future__ import annotations

import os
import threading
import time

import pytest

from fta.core.errors import IntegrityError, TransferError
from fta.lan import direct
from fta.lan.direct import Declined, FingerprintMismatch, Receiver, fetch_info, safe_relative_path, send_files
from fta.lan.identity import Identity
from fta.lan.links import PeerLink, parse_link, qr_png, qr_text


@pytest.fixture
def receiver(tmp_path):
    offers = []
    instance = Receiver(
        Identity.load(tmp_path / "rx-data", "Receiver"),
        tmp_path / "inbox",
        on_offer=lambda offer: offers.append(offer) or offer.sender_name != "Mallory",
        host="127.0.0.1",
        port=0,
    )
    instance.offers_seen = offers
    instance.start()
    yield instance
    instance.stop()


@pytest.fixture
def sender(tmp_path):
    return Identity.load(tmp_path / "tx-data", "Sender")


def make_tree(root):
    (root / "album" / "2024" / "empty").mkdir(parents=True)
    (root / "album" / "cover.jpg").write_bytes(os.urandom(300_000))
    (root / "album" / "2024" / "notes.txt").write_text("hello")
    script = root / "album" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    old = time.time() - 86400 * 30
    for path in [script, root / "album" / "2024" / "notes.txt", root / "album" / "2024"]:
        os.utime(path, (old, old))
    return root / "album"


def test_identity_is_stable(tmp_path):
    first = Identity.load(tmp_path, "Laptop")
    second = Identity.load(tmp_path)
    assert first.id == second.id and first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64
    assert (tmp_path / "lan_key.pem").stat().st_mode & 0o077 == 0


def test_send_folder_keeps_structure_times_and_permissions(tmp_path, receiver, sender):
    source = make_tree(tmp_path / "src")
    progress = []
    result = send_files(
        "127.0.0.1",
        [source],
        port=receiver.port,
        fingerprint=receiver.identity.fingerprint,
        sender=sender,
        progress=lambda done, total: progress.append((done, total)),
    )
    inbox = receiver.directory / "album"
    assert result.files == 3 and result.fingerprint == receiver.identity.fingerprint
    assert (inbox / "cover.jpg").read_bytes() == (source / "cover.jpg").read_bytes()
    assert (inbox / "2024" / "notes.txt").read_text() == "hello"
    assert (inbox / "2024" / "empty").is_dir()
    assert (inbox / "run.sh").stat().st_mode & 0o777 == 0o755
    assert abs((inbox / "run.sh").stat().st_mtime - (source / "run.sh").stat().st_mtime) < 1
    assert abs((inbox / "2024").stat().st_mtime - (source / "2024").stat().st_mtime) < 1
    assert progress[-1][0] == progress[-1][1] == result.bytes
    assert not list(receiver.directory.rglob("*.fta-part"))
    offer = receiver.offers_seen[0]
    assert offer.sender_name == "Sender" and offer.state == "done" and offer.summary() == "album"

    # The same folder again lands next to the first copy instead of overwriting it.
    send_files("127.0.0.1", [source], port=receiver.port, fingerprint=receiver.identity.fingerprint, sender=sender)
    assert (receiver.directory / "album (1)" / "cover.jpg").exists()


def test_declined_wrong_pin_and_wrong_fingerprint(tmp_path, receiver, sender):
    file = tmp_path / "a.txt"
    file.write_text("x")
    with pytest.raises(Declined):
        send_files("127.0.0.1", [file], port=receiver.port, sender_name="Mallory")
    receiver.pin = "4821"
    with pytest.raises(Declined, match="PIN"):
        send_files("127.0.0.1", [file], port=receiver.port, pin="0000")
    send_files("127.0.0.1", [file], port=receiver.port, pin="4821")
    with pytest.raises(FingerprintMismatch):
        send_files("127.0.0.1", [file], port=receiver.port, fingerprint=sender.fingerprint, pin="4821")
    assert sorted(p.name for p in receiver.directory.iterdir()) == ["a.txt"]


def test_interrupted_send_resumes_without_asking_again(tmp_path, receiver, sender):
    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(2_000_000))
    sessions = []

    def unplug_midway(done, total):
        if done > 1_000_000:
            raise ConnectionResetError("cable pulled")

    with pytest.raises(TransferError, match="cable pulled"):
        send_files("127.0.0.1", [big], port=receiver.port, sender=sender, progress=unplug_midway, on_session=sessions.append)
    part = receiver.directory / "big.bin.fta-part"
    deadline = time.monotonic() + 5
    while not (part.exists() and part.stat().st_size) and time.monotonic() < deadline:
        time.sleep(0.05)

    progress = []
    result = send_files(
        "127.0.0.1",
        [big],
        port=receiver.port,
        sender=sender,
        resume_session=sessions[0],
        progress=lambda done, total: progress.append(done),
    )
    assert len(receiver.offers_seen) == 1  # the user was asked once
    assert 0 < result.results[0].resumed_from < 2_000_000
    assert progress[0] >= result.results[0].resumed_from
    assert result.results[0].verified
    assert (receiver.directory / "big.bin").read_bytes() == big.read_bytes()
    assert not part.exists()

    # A forged or stale session id gets an ordinary offer, which the user sees.
    send_files("127.0.0.1", [big], port=receiver.port, sender=sender, resume_session="forged")
    assert len(receiver.offers_seen) == 2
    assert (receiver.directory / "big (1).bin").exists()


def test_receiver_rejects_paths_that_escape(receiver):
    conn = direct._PinnedConnection("127.0.0.1", receiver.port, "", 10)
    for bad in ["../evil", "/etc/passwd", "a/../../b", "C:/x", "a\\b", ""]:
        body = direct.json.dumps({"sender": {"name": "x"}, "files": [{"path": bad, "size": 1}]}).encode()
        status, _ = direct._call(conn, "POST", "/api/offer", body, {"Content-Type": "application/json"})
        assert status == 400, bad
    status, _ = direct._call(conn, "PUT", "/api/file?session=nope&index=0&offset=0", b"x", {"Content-Length": "1"})
    assert status == 403
    conn.close()
    assert receiver.offers_seen == []
    with pytest.raises(ValueError):
        safe_relative_path("a/./b")


def test_sender_detects_corruption(tmp_path, receiver, monkeypatch):
    file = tmp_path / "data.bin"
    file.write_bytes(b"0123456789" * 1000)
    original = receiver._receive

    def corrupt(query, length, stream):
        original(query, length, stream)  # receive for real, then lie about the hash
        return {"sha256": "0" * 64}

    monkeypatch.setattr(receiver, "_receive", corrupt)
    with pytest.raises(IntegrityError):
        send_files("127.0.0.1", [file], port=receiver.port)


def test_info_and_cancel(tmp_path, receiver):
    info, seen = fetch_info("127.0.0.1", receiver.port)
    assert info["name"] == "Receiver" and seen == receiver.identity.fingerprint

    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(3_000_000))
    started = threading.Event()

    def on_progress(done, total):
        started.set()
        time.sleep(0.01)

    errors = []
    thread = threading.Thread(
        target=lambda: _capture(errors, lambda: send_files("127.0.0.1", [big], port=receiver.port, progress=on_progress))
    )
    thread.start()
    assert started.wait(5)
    receiver.cancel(receiver.offers_seen[-1])
    thread.join(15)
    assert errors and not (receiver.directory / "big.bin").exists()


def _capture(errors, action):
    try:
        action()
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


def test_links_and_qr():
    link = PeerLink("192.168.1.20", 53318, "ab" * 32, "Laptop Олег")
    parsed = parse_link(link.url())
    assert parsed == link
    assert parse_link("10.0.0.5") == PeerLink("10.0.0.5")
    assert parse_link("10.0.0.5:4000").port == 4000
    with pytest.raises(ValueError):
        parse_link("fta://host/?fp=zz")
    assert qr_png(link.url()).startswith(b"\x89PNG")
    assert "█" in qr_text(link.url()) or "▀" in qr_text(link.url())


def test_mdns_discovery_between_two_devices():
    zeroconf = pytest.importorskip("zeroconf")
    from fta.lan.discovery import Discovery

    a = Discovery("a" * 32, "Alpha", 1111, "f" * 64, ssh_port=2222, interfaces=["127.0.0.1"])
    b = Discovery("b" * 32, "Beta", 2222, "e" * 64, interfaces=["127.0.0.1"])
    try:
        a.start()
        b.start()
    except OSError as exc:
        pytest.skip(f"no multicast here: {exc}")
    try:
        deadline = time.monotonic() + 10
        while (not a.find("beta") or not b.find("alpha")) and time.monotonic() < deadline:
            time.sleep(0.1)
        if not a.peers and not b.peers:
            pytest.skip("multicast is not delivered on this machine")
        names = [p.name for p in a.peers]  # other FTA devices on the network may show up too
        assert "Beta" in names and "Alpha" not in names
        assert b.find("alpha").ssh_port == 2222 and b.find("alpha").fingerprint == "f" * 64
        assert b.find("alpha").port == 1111
    finally:
        a.stop()
        b.stop()
    assert zeroconf


def test_queue_sends_to_a_device_and_resumes_after_pause(tmp_path, receiver, sender):
    from fta.app.transfers import JobState, TransferJob, TransferQueue
    from fta.core.journal import Journal
    from fta.lan.links import PeerLink
    from fta.lan.service import LanService

    big = tmp_path / "movie.bin"
    big.write_bytes(os.urandom(3_000_000))
    service = LanService(tmp_path / "tx-data", tmp_path / "unused", lambda offer: False, discover=False)
    target = PeerLink("127.0.0.1", receiver.port, receiver.identity.fingerprint, "Receiver")
    queue = TransferQueue(update_interval=0, journal=Journal(tmp_path / "journal.jsonl"))
    paused = threading.Event()

    def pause_once(job):
        if job.state is JobState.RUNNING and job.bytes_done > 1_000_000 and not paused.is_set():
            paused.set()
            queue.pause(job)

    queue.subscribe(pause_once)
    job = queue.add(
        TransferJob(
            name="movie.bin",
            source=str(big),
            destination="Receiver",
            source_url="",
            destination_url=f"fta://127.0.0.1:{receiver.port}",
            action=service.sender(target, [big]),
        )
    )
    deadline = time.monotonic() + 10
    while job.state is not JobState.PAUSED and time.monotonic() < deadline:
        time.sleep(0.02)
    assert job.state is JobState.PAUSED
    queue.resume(job)
    assert queue.wait_idle(20)
    assert job.state is JobState.DONE, job.error
    assert len(receiver.offers_seen) == 1
    assert job.results[0].resumed_from > 0
    assert (receiver.directory / "movie.bin").read_bytes() == big.read_bytes()
    entry = queue.journal.entries(limit=1)[0]
    assert entry.destination == f"fta://127.0.0.1:{receiver.port}/movie.bin"
    assert entry.verified


def test_cli_receive_and_send(tmp_path):
    import os as _os
    import signal
    import subprocess
    import sys

    inbox = tmp_path / "inbox"
    env_rx = {**_os.environ, "FTA_DATA_DIR": str(tmp_path / "rx")}
    receiver = subprocess.Popen(
        [sys.executable, "-m", "fta", "receive", "--yes", "--port", "0", "--dir", str(inbox)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env_rx,
    )
    try:
        link = ""
        for _ in range(6):
            line = receiver.stdout.readline()
            if line.startswith("Link: "):
                link = line.split(": ", 1)[1].strip()
                break
        assert link.startswith("fta://"), link
        port = link.split(":")[2].split("/")[0]
        link = link.replace(link.split("//")[1].split(":")[0], "127.0.0.1", 1)

        source = make_tree(tmp_path / "src")
        env_tx = {**_os.environ, "FTA_DATA_DIR": str(tmp_path / "tx")}
        sent = subprocess.run(
            [sys.executable, "-m", "fta", "send", link, str(source)], capture_output=True, text=True, env=env_tx, timeout=60
        )
        assert sent.returncode == 0, sent.stderr
        assert "sent 3 file(s)" in sent.stdout
        assert (inbox / "album" / "2024" / "notes.txt").read_text() == "hello"

        # The wrong fingerprint in a link is refused before any file is offered.
        bad = link.split("fp=")[0] + "fp=" + "0" * 64
        refused = subprocess.run(
            [sys.executable, "-m", "fta", "send", bad, str(source)], capture_output=True, text=True, env=env_tx, timeout=60
        )
        assert refused.returncode == 1 and "certificate" in refused.stderr
        assert port.isdigit()
        history = subprocess.run([sys.executable, "-m", "fta", "history", "-n", "3"], capture_output=True, text=True, env=env_tx)
        assert "fta://" in history.stdout
    finally:
        receiver.send_signal(signal.SIGINT)
        receiver.wait(15)


def test_address_or_name():
    from fta.lan.links import looks_like_address

    assert looks_like_address("192.168.1.5")
    assert looks_like_address("192.168.1.5:4000")
    assert looks_like_address("laptop.local")
    assert looks_like_address("fta://x:1/?fp=")
    assert looks_like_address("nas:53318")
    assert not looks_like_address("Laptop")
    assert not looks_like_address("Olga's phone")


def test_receiver_falls_back_to_a_free_port(tmp_path, receiver):
    second = Receiver(Identity.load(tmp_path / "second"), tmp_path / "in2", lambda o: True, host="127.0.0.1", port=receiver.port)
    second.start()
    try:
        assert second.port not in (0, receiver.port)
    finally:
        second.stop()


def _get(url):
    import ssl
    import urllib.error
    import urllib.request

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # a browser shows a warning here; the test just reads
    try:
        with urllib.request.urlopen(url, context=context, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def test_one_time_and_expiring_share_links(tmp_path, receiver):
    import io
    import zipfile

    from fta.lan.service import LanService

    service = LanService(tmp_path / "rx-data", receiver.directory, lambda o: False, discover=False)
    service.receiver = receiver  # share through the running test receiver
    report = tmp_path / "отчёт.pdf"
    report.write_bytes(os.urandom(70_000))

    url, share = service.share(report, ttl_seconds=600, one_time=True, address="127.0.0.1")
    status, headers, body = _get(url)
    assert status == 200 and body == report.read_bytes()
    assert "UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82.pdf" in headers["Content-Disposition"]
    status, _, body = _get(url)
    assert status == 410 and "больше не действует".encode() in body
    assert share.status() == "скачана"

    url, share = service.share(report, ttl_seconds=600, one_time=False, address="127.0.0.1")
    assert _get(url)[0] == 200 and _get(url)[0] == 200 and share.downloads == 2
    receiver.shares.revoke(share.token)
    assert _get(url)[0] == 410

    url, share = service.share(report, ttl_seconds=600, one_time=False, address="127.0.0.1")
    share.expires_at = time.time() - 1
    assert _get(url)[0] == 410 and share.status() == "истекла"
    assert _get(url.rsplit("/", 1)[0] + "/guessed-token")[0] == 410

    album = make_tree(tmp_path / "src")
    url, _ = service.share(album, ttl_seconds=60, one_time=True, address="127.0.0.1")
    status, _, body = _get(url)
    names = zipfile.ZipFile(io.BytesIO(body)).namelist()
    assert status == 200 and "album/2024/notes.txt" in names and "album/cover.jpg" in names
