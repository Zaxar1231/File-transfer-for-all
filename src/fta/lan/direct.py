"""Direct device-to-device transfer over HTTPS, in the spirit of LocalSend.

The sender offers a list of files (relative paths, sizes, times, permissions).
The receiver asks its user, then accepts or declines. Accepted files are sent
one by one with ``PUT``; each lands as ``<name>.fta-part`` and is renamed only
once complete and its SHA-256 matches, so an interrupted send resumes where it
stopped and never leaves a truncated file under the real name.

Endpoints (JSON unless noted)::

    GET  /api/info                              {"id", "name", "version"}
    POST /api/offer        {"sender", "files", "pin", "resume"}  → {"session"} or 403
    GET  /api/file?session&index                {"have": bytes already received}
    PUT  /api/file?session&index&offset  (raw bytes)  → {"sha256"}
    POST /api/done?session                      {"files", "bytes"}
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import secrets
import socket
import ssl
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fta import __version__
from fta.core.errors import AccessDenied, IntegrityError, TransferError
from fta.core.manager import FileResult
from fta.core.throttle import Metered
from fta.lan.identity import Identity, certificate_fingerprint
from fta.lan.links import DEFAULT_PORT
from fta.lan.shares import Share, ShareRegistry

logger = logging.getLogger(__name__)

PART_SUFFIX = ".fta-part"
_CHUNK = 256 * 1024
MAX_FILES = 100_000


class Declined(AccessDenied):
    """The receiving device's user declined the files, or the PIN was wrong."""


class FingerprintMismatch(AccessDenied):
    """The device that answered is not the one in the link or the peer list."""


@dataclass
class FileOffer:
    path: str
    """Relative POSIX path, e.g. ``Photos/2024/cat.jpg``."""
    size: int = 0
    mtime: float | None = None
    mode: int | None = None
    """Permission bits (``0o644``); applied on systems that have them."""
    is_dir: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "mtime": self.mtime, "mode": self.mode, "dir": self.is_dir}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FileOffer:
        return cls(
            path=str(data["path"]),
            size=int(data.get("size") or 0),
            mtime=float(data["mtime"]) if data.get("mtime") is not None else None,
            mode=int(data["mode"]) & 0o777 if data.get("mode") is not None else None,
            is_dir=bool(data.get("dir")),
        )


@dataclass
class Offer:
    """Files one device wants to send to this one; the receiving UI shows it."""

    sender_name: str
    sender_id: str
    files: list[FileOffer]
    address: str = ""
    session: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    state: str = "pending"
    """pending → accepted | declined → done | cancelled."""
    received: int = 0
    """Bytes written so far, across all files."""
    destinations: dict[int, Path] = field(default_factory=dict)
    finished_at: float | None = None

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files if not f.is_dir)

    @property
    def file_count(self) -> int:
        return sum(1 for f in self.files if not f.is_dir)

    def summary(self, limit: int = 3) -> str:
        """The top-level names, e.g. ``album, notes.txt +2``."""
        top = list(dict.fromkeys(f.path.split("/", 1)[0] for f in self.files))
        return ", ".join(top[:limit]) + (f" +{len(top) - limit}" if len(top) > limit else "")

    def describe(self) -> str:
        """``3 file(s), 1234567 bytes: album, notes.txt`` for the command line."""
        return f"{self.file_count} file(s), {self.total_size} bytes: {self.summary()}"


OfferHandler = Callable[[Offer], bool]
"""Decides on an offer (may block, e.g. waiting for the user); True accepts."""


def safe_relative_path(text: str) -> PurePosixPath:
    """A relative path that cannot leave the receiving folder, or ValueError."""
    if not text or "\x00" in text or "\\" in text or len(text) > 4096:
        raise ValueError(f"bad path: {text!r}")
    parts = text.split("/")  # PurePosixPath would quietly drop "." and empty parts
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ValueError(f"bad path: {text!r}")
    return PurePosixPath(*parts)


def _unique(path: Path, taken: set[Path]) -> Path:
    """``name.ext`` → ``name (1).ext`` while the name exists or is already promised to another file."""
    candidate, number = path, 0
    while candidate in taken or candidate.exists() or candidate.with_name(candidate.name + PART_SUFFIX).exists():
        number += 1
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
    return candidate


class Receiver:
    """HTTPS endpoint that accepts files into ``directory`` after ``on_offer`` agrees."""

    def __init__(
        self,
        identity: Identity,
        directory: Path,
        on_offer: OfferHandler,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        pin: str = "",
        on_update: Callable[[Offer], None] | None = None,
    ) -> None:
        self.identity = identity
        self.directory = directory
        self.on_offer = on_offer
        self.on_update = on_update or (lambda offer: None)
        self.pin = pin
        self.host = host
        self.port = port
        self.offers: dict[str, Offer] = {}
        self.shares = ShareRegistry()
        """Links that give a file away (``/s/<token>``), served by this same HTTPS server."""
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> None:
        if self._httpd is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            httpd = _Server((self.host, self.port), _make_handler(self))
        except OSError as exc:
            if not self.port:
                raise
            # Another copy of the app (or LocalSend-like tool) has the port: take any free one;
            # mDNS and the QR code carry the real port, so nearby devices still find us.
            logger.warning("port %s is taken (%s); using a free one", self.port, exc)
            httpd = _Server((self.host, 0), _make_handler(self))
        httpd.daemon_threads = True
        # The TLS handshake happens on the handler's first read, in its own thread,
        # so a client that stalls mid-handshake cannot block other connections.
        httpd.socket = self.identity.server_context().wrap_socket(
            httpd.socket, server_side=True, do_handshake_on_connect=False
        )
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever, name="fta-receiver", daemon=True)
        self._thread.start()
        logger.info("receiving into %s on port %s", self.directory, self.port)

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(5)
        self._httpd = self._thread = None

    def cancel(self, offer: Offer) -> None:
        """Stop an accepted transfer; the sender's next write is refused."""
        if offer.state in ("pending", "accepted"):
            offer.state = "cancelled"
            self.on_update(offer)

    # --- request handling (called from handler threads) --------------------------------

    def _offer(self, body: Any, address: str) -> tuple[int, dict[str, Any]]:
        if not isinstance(body, dict):
            return 400, {"error": "an offer is a JSON object"}
        sender = body.get("sender") if isinstance(body.get("sender"), dict) else {}
        raw_files = body.get("files")
        if not isinstance(raw_files, list) or not raw_files or len(raw_files) > MAX_FILES:
            return 400, {"error": "no files"}
        try:
            files = [FileOffer.from_json(item) for item in raw_files]
            for item in files:
                safe_relative_path(item.path)
                if item.size < 0:
                    raise ValueError("negative size")
        except (KeyError, TypeError, ValueError) as exc:
            return 400, {"error": str(exc)}
        if self.pin and not secrets.compare_digest(str(body.get("pin") or ""), self.pin):
            logger.info("offer from %s refused: wrong PIN", address)
            return 403, {"error": "pin"}
        offer = Offer(str(sender.get("name") or "?")[:80], str(sender.get("id") or "")[:64], files, address)
        earlier = self.offers.get(str(body.get("resume") or ""))
        if earlier is not None and earlier.state == "accepted" and _same_files(earlier.files, files):
            # The sender holds the secret session id of an offer the user already accepted:
            # continue it into the same files, without asking again.
            self.on_update(earlier)
            return 200, {"session": earlier.session}
        with self._lock:
            self.offers[offer.session] = offer
        try:
            accepted = bool(self.on_offer(offer))
        except Exception:
            logger.exception("offer handler failed")
            accepted = False
        if not accepted:
            offer.state = "declined"
            self.on_update(offer)
            return 403, {"error": "declined"}
        self._plan(offer)
        offer.state = "accepted"
        self.on_update(offer)
        return 200, {"session": offer.session}

    def _plan(self, offer: Offer) -> None:
        """Pick where each file goes. A top-level name that already exists here is
        received as ``name (1)``, so nothing on this device is ever overwritten."""
        tops: dict[str, str] = {}
        taken: set[Path] = set()
        for index, item in enumerate(offer.files):
            parts = safe_relative_path(item.path).parts
            if parts[0] not in tops:
                fresh = _unique(self.directory / parts[0], taken)
                taken.add(fresh)
                tops[parts[0]] = fresh.name
            target = self.directory.joinpath(tops[parts[0]], *parts[1:])
            offer.destinations[index] = target
            if item.is_dir:
                target.mkdir(parents=True, exist_ok=True)

    def _session(self, query: dict[str, list[str]]) -> tuple[Offer, int, FileOffer]:
        offer = self.offers.get(query.get("session", [""])[0])
        if offer is None or offer.state != "accepted":
            raise PermissionError("unknown or closed session")
        index = int(query.get("index", ["-1"])[0])
        if index not in offer.destinations or offer.files[index].is_dir:
            raise LookupError("no such file in the offer")
        return offer, index, offer.files[index]

    def _have(self, query: dict[str, list[str]]) -> dict[str, Any]:
        offer, index, _ = self._session(query)
        part = _part(offer.destinations[index])
        return {"have": part.stat().st_size if part.exists() else 0}

    def _receive(self, query: dict[str, list[str]], length: int, stream: Any) -> dict[str, Any]:
        offer, index, item = self._session(query)
        offset = int(query.get("offset", ["0"])[0])
        target = offer.destinations[index]
        part = _part(target)
        have = part.stat().st_size if part.exists() else 0
        if offset != have and offset != 0:
            raise ValueError(f"offset {offset} does not match the {have} bytes received")
        if offset + length != item.size:
            raise ValueError("size does not match the offer")
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with open(part, "r+b" if offset else "wb") as fp:
            if offset:
                fp.truncate(offset)
                while chunk := fp.read(_CHUNK):  # hash what is already there
                    digest.update(chunk)
                fp.seek(offset)
            remaining = length
            while remaining:
                chunk = stream.read(min(_CHUNK, remaining))
                if not chunk:
                    raise ConnectionError("the sender disconnected")
                fp.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
                offer.received += len(chunk)
                self.on_update(offer)
                if offer.state != "accepted":
                    raise ConnectionError("cancelled on this device")
        os.replace(part, target)
        _apply_metadata(target, item)
        return {"sha256": digest.hexdigest()}

    def _done(self, query: dict[str, list[str]]) -> dict[str, Any]:
        offer = self.offers.get(query.get("session", [""])[0])
        if offer is None or offer.state != "accepted":
            raise PermissionError("unknown or closed session")
        # Directory times last: writing files into a folder changes its mtime.
        for index, item in sorted(enumerate(offer.files), key=lambda pair: -len(pair[1].path)):
            if item.is_dir:
                _apply_metadata(offer.destinations[index], item)
        offer.state = "done"
        offer.finished_at = time.time()
        self.on_update(offer)
        return {"files": offer.file_count, "bytes": offer.received}


class _Server(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        logger.debug("connection from %s failed", client_address[0], exc_info=True)  # TLS probes, scanners


def _same_files(a: list[FileOffer], b: list[FileOffer]) -> bool:
    return [(f.path, f.size, f.is_dir) for f in a] == [(f.path, f.size, f.is_dir) for f in b]


def _part(target: Path) -> Path:
    return target.with_name(target.name + PART_SUFFIX)


def _apply_metadata(path: Path, item: FileOffer) -> None:
    try:
        if item.mode is not None and os.name == "posix":
            os.chmod(path, item.mode | (stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if item.is_dir else 0)))
        if item.mtime is not None:
            os.utime(path, (item.mtime, item.mtime))
    except OSError as exc:  # FAT, network folders: the data matters more than the times
        logger.warning("cannot keep times/permissions of %s: %s", path, exc)


def _make_handler(receiver: Receiver) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"fta/{__version__}"
        protocol_version = "HTTP/1.1"
        timeout = 120

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("%s %s", self.address_string(), format % args)

        def _reply(self, status: int, data: dict[str, Any]) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _page(self, status: int, title: str, text: str) -> None:
            body = (
                f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                f"<title>{title}</title><body style='font-family:sans-serif;margin:3em'>"
                f"<h2>{title}</h2><p>{text}</p></body>"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_share(self, share: Share) -> None:
            from urllib.parse import quote

            name = share.name
            disposition = f"attachment; filename*=UTF-8''{quote(name)}"
            self.send_response(200)
            self.send_header("Content-Disposition", disposition)
            self.send_header("Cache-Control", "no-store")
            if share.path.is_dir():
                import zipfile

                self.send_header("Content-Type", "application/zip")
                self.send_header("Connection", "close")  # size unknown until the zip is built
                self.end_headers()
                self.close_connection = True
                with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted(share.path.rglob("*")):
                        if path.is_file():
                            archive.write(path, path.relative_to(share.path.parent).as_posix())
            else:
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(share.path.stat().st_size))
                self.end_headers()
                with share.path.open("rb") as fp:
                    while chunk := fp.read(_CHUNK):
                        self.wfile.write(chunk)
            logger.info("shared %s with %s", share.path, self.client_address[0])

        def _route(self, method: str) -> None:
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            try:
                if method == "GET" and url.path == "/api/info":
                    self._reply(200, {"id": receiver.identity.id, "name": receiver.identity.name, "version": __version__})
                elif method == "POST" and url.path == "/api/offer":
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > 64 * 1024 * 1024:
                        self._reply(413, {"error": "offer too large"})
                        return
                    body = json.loads(self.rfile.read(length) or b"{}")
                    self._reply(*receiver._offer(body, self.client_address[0]))
                elif method == "GET" and url.path == "/api/file":
                    self._reply(200, receiver._have(query))
                elif method == "PUT" and url.path == "/api/file":
                    length = int(self.headers.get("Content-Length") or -1)
                    if length < 0:
                        self._reply(411, {"error": "Content-Length required"})
                        return
                    self._reply(200, receiver._receive(query, length, self.rfile))
                elif method == "GET" and url.path.startswith("/s/"):
                    share = receiver.shares.claim(url.path[3:])
                    if share is None:
                        self._page(410, "Ссылка больше не действует", "Она была одноразовой, истекла или её отозвали.")
                    else:
                        self._send_share(share)
                elif method == "POST" and url.path == "/api/done":
                    self._reply(200, receiver._done(query))
                else:
                    self._reply(404, {"error": "not found"})
            except PermissionError as exc:
                self._reply(403, {"error": str(exc)})
            except LookupError as exc:
                self._reply(404, {"error": str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                self._reply(400, {"error": str(exc)})
                self.close_connection = True  # the body may not have been read
            except (ConnectionError, ssl.SSLError, TimeoutError, OSError) as exc:
                logger.info("transfer from %s interrupted: %s", self.client_address[0], exc)
                self.close_connection = True
            except Exception:  # a bug must answer 500, not drop the connection silently
                logger.exception("request %s %s from %s failed", method, url.path, self.client_address[0])
                self._reply(500, {"error": "internal error"})
                self.close_connection = True

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def do_PUT(self) -> None:
            self._route("PUT")

    return Handler


# --- sending ---------------------------------------------------------------------------


@dataclass
class SendResult:
    files: int
    bytes: int
    fingerprint: str
    """The receiver's certificate fingerprint, to pin next time."""
    results: list[FileResult] = field(default_factory=list)


def collect(paths: list[Path]) -> list[tuple[FileOffer, Path | None]]:
    """Files and folders to offer, with paths relative to each item's parent folder."""
    items: list[tuple[FileOffer, Path | None]] = []
    for root in paths:
        root = root.resolve()
        if root.is_dir():
            for folder, dirs, files in os.walk(root):
                dirs.sort()
                base = Path(folder)
                relative = base.relative_to(root.parent).as_posix()
                items.append((_offer_for(base, relative, True), None))
                for name in sorted(files):
                    path = base / name
                    if path.is_file():
                        items.append((_offer_for(path, f"{relative}/{name}", False), path))
        elif root.is_file():
            items.append((_offer_for(root, root.name, False), root))
        else:
            raise FileNotFoundError(str(root))
    return items


def _offer_for(path: Path, relative: str, is_dir: bool) -> FileOffer:
    info = path.stat()
    return FileOffer(relative, 0 if is_dir else info.st_size, info.st_mtime, stat.S_IMODE(info.st_mode), is_dir)


class _PinnedConnection(http.client.HTTPSConnection):
    """HTTPS that trusts exactly one certificate, identified by its SHA-256."""

    def __init__(self, host: str, port: int, fingerprint: str, timeout: float) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # replaced by the fingerprint check below
        super().__init__(host, port, timeout=timeout, context=context)
        self.expected = fingerprint.lower()
        self.seen = ""

    def connect(self) -> None:
        super().connect()
        der = self.sock.getpeercert(binary_form=True) or b""
        self.seen = certificate_fingerprint(der)
        if self.expected and not secrets.compare_digest(self.seen, self.expected):
            self.close()
            raise FingerprintMismatch(
                f"{self.host} presented certificate {self.seen[:16]}…, expected {self.expected[:16]}…"
            )


def _call(conn: _PinnedConnection, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    raw = response.read()
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        data = {"error": raw[:200].decode(errors="replace")}
    return response.status, data


class _CountingReader:
    def __init__(self, fp: Any, remaining: int, digest: Any, progress: Callable[[int], None]) -> None:
        self.fp, self.remaining, self.digest, self.progress = fp, remaining, digest, progress

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        size = self.remaining if size < 0 else min(size, self.remaining)
        data = self.fp.read(size)
        self.remaining -= len(data)
        self.digest.update(data)
        self.progress(len(data))
        return data


def send_files(
    host: str,
    paths: list[Path],
    *,
    port: int = DEFAULT_PORT,
    fingerprint: str = "",
    sender: Identity | None = None,
    sender_name: str = "",
    pin: str = "",
    progress: Callable[[int, int | None], None] | None = None,
    on_file: Callable[[str], None] | None = None,
    resume_session: str = "",
    on_session: Callable[[str], None] | None = None,
    decision_timeout: float = 300.0,
    timeout: float = 30.0,
) -> SendResult:
    """Offer ``paths`` to the device at ``host:port`` and send them once it accepts.

    With ``fingerprint`` the receiver must present that certificate; without one
    the result reports the fingerprint seen, for the caller to keep. ``on_session``
    receives the session id; passing it back as ``resume_session`` after an
    interruption continues the same transfer without asking the receiver again.
    """
    items = collect(paths)
    total = sum(item.size for item, _ in items if not item.is_dir)
    progress = Metered(progress)  # the app-wide speed limit applies to nearby devices too
    me = {"id": sender.id if sender else "", "name": sender_name or (sender.name if sender else socket.gethostname())}
    conn = _PinnedConnection(host, port, fingerprint, timeout)
    try:
        conn.connect()
        conn.sock.settimeout(decision_timeout)  # the receiver's user may take a while to answer
        status, reply = _call(
            conn,
            "POST",
            "/api/offer",
            json.dumps(
                {"sender": me, "files": [item.to_json() for item, _ in items], "pin": pin, "resume": resume_session}
            ).encode(),
            {"Content-Type": "application/json"},
        )
        conn.sock.settimeout(timeout)
        if status == 403:
            raise Declined("wrong PIN" if reply.get("error") == "pin" else "the receiver declined the files")
        if status != 200:
            raise TransferError(f"offer refused: {reply.get('error', status)}")
        session = reply["session"]
        if on_session:
            on_session(session)
        done = 0
        results: list[FileResult] = []

        def advance(count: int) -> None:
            nonlocal done
            done += count
            if progress:
                progress(done, total)

        for index, (item, source) in enumerate(items):
            if item.is_dir or source is None:
                continue
            if on_file:
                on_file(item.path)
            status, reply = _call(conn, "GET", f"/api/file?session={session}&index={index}")
            have = int(reply.get("have", 0)) if status == 200 else 0
            have = have if 0 < have <= item.size else 0
            digest = hashlib.sha256()
            started = time.monotonic()
            with open(source, "rb") as fp:
                if have:
                    remaining = have
                    while remaining:  # the receiver keeps the prefix; hash ours to compare at the end
                        chunk = fp.read(min(_CHUNK, remaining))
                        if not chunk:
                            raise TransferError(f"{source} shrank while sending")
                        digest.update(chunk)
                        remaining -= len(chunk)
                    advance(have)
                body = _CountingReader(fp, item.size - have, digest, advance)
                status, reply = _call(
                    conn,
                    "PUT",
                    f"/api/file?session={session}&index={index}&offset={have}",
                    body,
                    {"Content-Length": str(item.size - have), "Content-Type": "application/octet-stream"},
                )
            if status != 200:
                raise TransferError(f"{item.path}: {reply.get('error', status)}")
            if reply.get("sha256") != digest.hexdigest():
                raise IntegrityError(f"{item.path}: SHA-256 differs on the receiving device")
            results.append(
                FileResult(
                    str(source), "/" + item.path, item.size, time.monotonic() - started, digest.hexdigest(), True, have
                )
            )
        status, reply = _call(conn, "POST", f"/api/done?session={session}")
        if status != 200:
            raise TransferError(f"finishing failed: {reply.get('error', status)}")
        return SendResult(len(results), total, conn.seen, results)
    except (OSError, http.client.HTTPException) as exc:
        raise TransferError(f"{host}:{port}: {exc}") from exc
    finally:
        conn.close()


def fetch_info(host: str, port: int = DEFAULT_PORT, fingerprint: str = "", timeout: float = 5.0) -> tuple[dict[str, Any], str]:
    """The device's name and id, plus the certificate fingerprint it presented."""
    conn = _PinnedConnection(host, port, fingerprint, timeout)
    try:
        status, info = _call(conn, "GET", "/api/info")
        if status != 200:
            raise TransferError(f"{host}:{port} is not an FTA device")
        return info, conn.seen
    except (OSError, http.client.HTTPException) as exc:
        raise TransferError(f"{host}:{port}: {exc}") from exc
    finally:
        conn.close()
