"""Command-line interface: ``fta cp | ls | stat | mkdir | rm | serve | send | receive | peers | history | schemes``."""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from fta.app.storage import default_data_dir
from fta.core.endpoint import Endpoint
from fta.core.errors import TransferError
from fta.core.journal import Journal, JournalEntry
from fta.core.manager import copy
from fta.core.retry import is_retryable, retry_delay
from fta.core.registry import known_schemes, open_transport
from fta.core.transport import RemoteEntry


def _key_value(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    return key, value


def _add_option_flag(parser: argparse.ArgumentParser, flag: str, help_text: str) -> None:
    parser.add_argument(flag, type=_key_value, action="append", default=[], metavar="KEY=VALUE", help=help_text)


class _ProgressLine:
    """One progress line on stderr per copied file."""

    def __init__(self) -> None:
        self.label = ""
        self._open = False
        self._shown = 0.0

    def start(self, path: str) -> None:
        self.finish()
        self.label = path

    def update(self, done: int, total: int | None) -> None:
        now = time.monotonic()
        if now - self._shown < 0.1 and done != total:
            return  # redraw ten times a second at most
        self._shown = now
        if sys.stderr.isatty():  # a hedgehog runs along the line; logs and pipes get plain numbers
            from fta.fun import hedgehog_bar

            emoji = (sys.stderr.encoding or "").lower().startswith("utf")
            text = f"{self.label[-30:]:30} {hedgehog_bar(done, total, 24, emoji)}"
        elif total:
            text = f"{self.label}: {done * 100 // total}% ({done}/{total} bytes)"
        else:
            text = f"{self.label}: {done} bytes"
        sys.stderr.write(f"\r{text}")
        sys.stderr.flush()
        self._open = True

    def finish(self) -> None:
        if self._open:
            sys.stderr.write("\n")
            self._open = False


def _format_entry(entry: RemoteEntry) -> str:
    size = "-" if entry.size is None else str(entry.size)
    modified = datetime.fromtimestamp(entry.modified).strftime("%Y-%m-%d %H:%M") if entry.modified else "-"
    return f"{'d' if entry.is_dir else '-'} {size:>12}  {modified:16}  {entry.name}"


def _journal() -> Journal:
    return Journal(default_data_dir() / "journal.jsonl")


def _cp(args: argparse.Namespace) -> int:
    line = _ProgressLine()
    attempt = 0
    while True:
        try:
            results = copy(
                args.source,
                args.destination,
                progress=line.update,
                recursive=args.recursive,
                resume=args.resume or attempt > 0,
                checksum=True,
                verify=args.verify,
                on_file=line.start,
                source_options=dict(args.src_opt),
                destination_options=dict(args.dst_opt),
            )
            break
        except (TransferError, OSError) as exc:
            line.finish()
            if attempt >= args.retries or not is_retryable(exc):
                _journal().append(JournalEntry(args.source, args.destination, 0, "failed", error=str(exc)))
                raise
            attempt += 1
            delay = retry_delay(attempt)
            print(f"fta: {exc}; retry {attempt}/{args.retries} in {delay:.0f} s", file=sys.stderr)
            time.sleep(delay)
        finally:
            line.finish()
    journal = _journal()
    for result in results:
        journal.append(
            JournalEntry(
                result.source,
                result.destination,
                result.size,
                "done",
                seconds=round(result.seconds, 3),
                sha256=result.sha256,
                verified=result.verified,
                resumed_from=result.resumed_from,
            )
        )
        if args.verify:
            print(f"verified sha256:{result.sha256}  {result.destination}")
    return 0


def _serve(args: argparse.Namespace) -> int:
    from fta.app.storage import Store
    from fta.app.transfers import TransferQueue
    from fta.server.passwords import hash_password
    from fta.server.ssh import server_from_store

    store = Store()
    settings = store.settings
    if args.root:
        settings.server_root = str(Path(args.root).expanduser().resolve())
    if args.port:
        settings.server_port = args.port
    if args.user:
        settings.server_user = args.user
    if args.set_password:
        password = getpass.getpass(f"New password for {settings.server_user}: ")
        if not password or password != getpass.getpass("Repeat: "):
            print("fta: error: passwords are empty or do not match", file=sys.stderr)
            return 1
        settings.server_password_hash = hash_password(password)
    store.save()
    keys = store.data_dir / "authorized_keys"
    if not settings.server_password_hash and not keys.exists():
        print(f"fta: error: no way to log in yet; run `fta serve --set-password` or add keys to {keys}", file=sys.stderr)
        return 1

    journal = _journal()
    server = server_from_store(store, TransferQueue(journal=journal), journal)
    server.config.host = args.host
    server.start()
    print(f"Serving {store.server_root} over SSH/SFTP on port {server.port}")
    print(f"Host key fingerprint: {server.fingerprint}")
    for address in server.addresses():
        print(f"  PuTTY:  host {address}, port {server.port}, user {settings.server_user}")
        print(f"  psftp:  psftp -P {server.port} {settings.server_user}@{address}")
        print(f"  ssh:    ssh -p {server.port} {settings.server_user}@{address}")
    print(f"Public keys are read from {keys}. Stop with Ctrl+C.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def _lan(store, on_offer=lambda offer: False, port: int = 0, pin: str = "", directory: str | None = None, on_update=None):
    from fta.lan.service import LanService

    receive_dir = Path(directory).expanduser().resolve() if directory else store.receive_dir
    return LanService(
        store.data_dir,
        receive_dir,
        on_offer,
        name=store.settings.device_name,
        port=port,
        pin=pin,
        on_update=on_update,
    )


def _wait_for_peers(service, seconds: float, wanted: str | None = None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if wanted and service.find(wanted):
            return service.find(wanted)
        time.sleep(0.2)
    return service.find(wanted) if wanted else None


def _peers(args: argparse.Namespace) -> int:
    from fta.app.storage import Store

    service = _lan(Store())
    service.start()
    try:
        _wait_for_peers(service, args.wait)
        peers = service.peers
        if not peers:
            print("no devices found on the local network")
        for peer in peers:
            ssh = f"  ssh port {peer.ssh_port}" if peer.ssh_port else ""
            print(f"{peer.name:24} {peer.address}:{peer.port}{ssh}  fingerprint {peer.fingerprint[:16]}…")
    finally:
        service.stop()
    return 0


def _receive(args: argparse.Namespace) -> int:
    from fta.app.storage import Store
    from fta.lan.identity import short_fingerprint
    from fta.lan.links import local_address, qr_text

    store = Store()

    def ask(offer) -> bool:
        print(f"\n{offer.sender_name} ({offer.address}) wants to send {offer.describe()}")
        if args.yes:
            print("accepted (--yes)")
            return True
        try:
            return input("Accept? [y/N] ").strip().lower() in ("y", "yes", "д", "да")
        except EOFError:
            return False

    def update(offer) -> None:
        if offer.state == "done":
            print(f"received {offer.file_count} file(s), {offer.received} bytes from {offer.sender_name}")

    service = _lan(store, ask, port=args.port, pin=args.pin or store.settings.lan_pin, directory=args.dir, on_update=update)
    service.start()
    address = local_address()
    link = service.link(address)
    print(f"Receiving into {service.receiver.directory} as {service.identity.name!r}")
    print(f"Address {address}:{service.receiver.port}, certificate {short_fingerprint(service.identity.fingerprint)}")
    print(f"Link: {link.url()}")
    if args.qr:
        print(qr_text(link.url()))
    print("Stop with Ctrl+C.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
    return 0


def _send(args: argparse.Namespace) -> int:
    from fta.app.storage import Store
    from fta.lan.links import looks_like_address, parse_link

    store = Store()
    service = _lan(store)
    paths = [Path(p).expanduser() for p in args.paths]
    target: object
    if looks_like_address(args.target):
        target = parse_link(args.target)
        if not target.fingerprint:
            print("note: no fingerprint in the target; the device's certificate is not checked", file=sys.stderr)
    else:
        service.start()
        try:
            target = _wait_for_peers(service, args.wait, args.target)
        finally:
            service.stop()
        if target is None:
            print(f"fta: error: no device named {args.target!r} answered; try `fta peers`", file=sys.stderr)
            return 1
    label = getattr(target, "name", "") or getattr(target, "host", "") or args.target
    line = _ProgressLine()
    print(f"Waiting for {label} to accept…", file=sys.stderr)
    results = service.sender(target, paths, pin=args.pin)(line.update, line.start, False)
    line.finish()
    journal = _journal()
    for result in results:
        journal.append(
            JournalEntry(
                result.source,
                f"fta://{label}{result.destination}",
                result.size,
                "done",
                seconds=round(result.seconds, 3),
                sha256=result.sha256,
                verified=True,
            )
        )
    print(f"sent {len(results)} file(s), checked with SHA-256 on the receiving device")
    return 0


def _cloud(args: argparse.Namespace) -> int:
    from fta.tools import rclone

    if args.action == "install":
        print(f"installed {rclone.install()}")
    elif args.action == "list":
        if rclone.find() is None:
            print("rclone is not installed; run `fta cloud install`")
            return 1
        for remote in rclone.remotes():
            print(f"{remote.name:20} {remote.label:20} rclone://{remote.name}/")
    elif args.action == "add":
        if not args.name or not args.type:
            print("usage: fta cloud add NAME TYPE [KEY=VALUE…]  (TYPE: " + ", ".join(rclone.PROVIDERS) + ")", file=sys.stderr)
            return 2
        if args.type in rclone.OAUTH:
            print("A browser window opens to sign in…", file=sys.stderr)
        rclone.create_remote(args.name, args.type, dict(args.params))
        print(f"added: rclone://{args.name}/")
    elif args.action == "remove":
        rclone.delete_remote(args.name)
    return 0


def _passphrase(confirm: bool) -> str:
    password = getpass.getpass("Passphrase: ")
    if confirm and password != getpass.getpass("Repeat: "):
        raise ValueError("passphrases do not match")
    if not password:
        raise ValueError("empty passphrase")
    return password


def _my_age_key() -> str:
    """This device's age secret key (the same one the app uses), created on first use."""
    from fta.crypto import age

    path = default_data_dir() / "age_identity.txt"
    try:
        return path.read_text().strip().splitlines()[-1]
    except (OSError, IndexError):
        identity = age.Identity.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# public key: {identity.recipient}\n{identity}\n")
        path.chmod(0o600)
        return str(identity)


def _encrypt(args: argparse.Namespace) -> int:
    from fta.crypto import age

    options = {"recipients": args.recipient} if args.recipient else {"passphrase": _passphrase(confirm=True)}
    for name in args.files:
        print(age.encrypt_file(Path(name), **options))
    return 0


def _decrypt(args: argparse.Namespace) -> int:
    from fta.crypto import age

    identities = [Path(f).read_text().strip().splitlines()[-1] for f in args.identity] + [_my_age_key()]
    passphrase = None
    for name in args.files:
        try:
            print(age.decrypt_file(Path(name), identities=identities, passphrase=passphrase))
        except age.AgeError as exc:
            if "passphrase" not in str(exc) or passphrase:
                raise
            passphrase = _passphrase(confirm=False)
            print(age.decrypt_file(Path(name), identities=identities, passphrase=passphrase))
    return 0


def _share(args: argparse.Namespace) -> int:
    from fta.app.storage import Store
    from fta.lan.links import local_address, qr_text

    seconds = {"m": 60, "h": 3600, "d": 86400}[args.ttl[-1]] * int(args.ttl[:-1])
    service = _lan(Store(), port=args.port)
    service.discover = False
    service.start()
    url, share = service.share(Path(args.path), seconds, not args.many, local_address())
    print(f"Link ({'one download' if share.one_time else 'any number of downloads'}, valid {args.ttl}):")
    print(url)
    print(qr_text(url))
    print("The browser warns about the certificate: it is this device's own. Stop with Ctrl+C.")
    try:
        while share.active:
            time.sleep(0.5)
        time.sleep(2)  # let a running download finish
        print(f"Link {share.status()}.")
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
    return 0


def _sync(args: argparse.Namespace) -> int:
    from fta.core.sync import sync

    line = _ProgressLine()
    report = sync(
        args.source,
        args.destination,
        manifest=default_data_dir() / "sync-manifest.json",
        delete=args.delete,
        progress=line.update,
        on_file=line.start,
        source_options=dict(args.src_opt),
        destination_options=dict(args.dst_opt),
    )
    line.finish()
    print(report.summary())
    return 0


def _stats(args: argparse.Namespace) -> int:
    from fta.fun import human, statistics, summary_card_svg

    since = time.time() - args.days * 86400 if args.days else None
    stats = statistics(_journal().entries(), since=since)
    period = f"за {args.days} дн." if args.days else "за всё время"
    print(f"Files {stats.files}, {human(stats.bytes)} ({period}); failed {stats.failed}, verified {stats.verified}")
    print(f"Average {human(stats.average_speed)}/s; saved by resume and delta {human(stats.saved_by_resume)}")
    if stats.biggest[1]:
        print(f"Biggest: {human(stats.biggest[0])} {stats.biggest[1]}")
    if stats.fastest[1]:
        print(f"Fastest: {human(stats.fastest[0])}/s {stats.fastest[1]}")
    print("Most used: " + (", ".join(f"{n} {c}" for n, c in stats.by_protocol.most_common(5)) or "-"))
    if args.card:
        Path(args.card).write_text(summary_card_svg(stats, "Мои передачи", period), encoding="utf-8")
        print(f"Summary card: {args.card}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fta", description="Transfer files over any supported protocol.")
    commands = parser.add_subparsers(dest="command", required=True)

    cp = commands.add_parser("cp", help="copy a file or, with -r, a directory")
    cp.add_argument("source")
    cp.add_argument("destination")
    cp.add_argument("-r", "--recursive", action="store_true", help="copy directories recursively")
    cp.add_argument("--resume", action="store_true", help="continue partial files instead of resending them")
    cp.add_argument("--verify", action="store_true", help="compare SHA-256 of both sides after each file")
    cp.add_argument("--retries", type=int, default=0, metavar="N", help="retry N times, resuming each time")
    _add_option_flag(cp, "--src-opt", "option for the source transport (repeatable)")
    _add_option_flag(cp, "--dst-opt", "option for the destination transport (repeatable)")

    sync = commands.add_parser("sync", help="mirror a folder; changed files send only their changed blocks")
    sync.add_argument("source")
    sync.add_argument("destination")
    sync.add_argument("--delete", action="store_true", help="also delete destination files missing from the source")
    _add_option_flag(sync, "--src-opt", "option for the source transport (repeatable)")
    _add_option_flag(sync, "--dst-opt", "option for the destination transport (repeatable)")

    parser.add_argument("--limit", help="speed limit for all transfers, e.g. 500K or 2M (bytes per second)")

    serve = commands.add_parser("serve", help="run the SSH/SFTP server for PuTTY, WinSCP, psftp, ssh")
    serve.add_argument("--root", help="shared folder (default: from settings, else ~/FTA Shared)")
    serve.add_argument("--port", type=int, help="port (default: from settings, 2222)")
    serve.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    serve.add_argument("--user", help="login name (default: from settings, fta)")
    serve.add_argument("--set-password", action="store_true", help="ask for a new password and save its hash")

    send = commands.add_parser("send", help="send files or folders to a nearby device")
    send.add_argument("target", help="device name (see `fta peers`), fta:// link from its QR code, or host[:port]")
    send.add_argument("paths", nargs="+", metavar="PATH")
    send.add_argument("--pin", default="", help="PIN shown on the receiving device, if it asks for one")
    send.add_argument("--wait", type=float, default=5, help="seconds to look for the device by name (default 5)")

    receive = commands.add_parser("receive", help="accept files from nearby devices")
    receive.add_argument("--dir", help="where to save (default: from settings, else Downloads/FTA)")
    receive.add_argument("--port", type=int, default=53318, help="port (default 53318; 0 picks a free one)")
    receive.add_argument("--pin", help="require this PIN from senders")
    receive.add_argument("-y", "--yes", action="store_true", help="accept every offer without asking")
    receive.add_argument("--qr", action="store_true", help="print a QR code with this device's link")

    cloud = commands.add_parser("cloud", help="cloud drives via rclone: list, add, remove, install")
    cloud.add_argument("action", choices=["list", "add", "remove", "install"])
    cloud.add_argument("name", nargs="?", help="name for the cloud, used as rclone://NAME/")
    cloud.add_argument("type", nargs="?", help="drive, dropbox, yandex, onedrive, mailru, webdav, …")
    cloud.add_argument("params", nargs="*", type=_key_value, metavar="KEY=VALUE", help="e.g. user=me pass=secret")

    encrypt = commands.add_parser("encrypt", help="encrypt files to FILE.age (age format; asks for a passphrase)")
    encrypt.add_argument("files", nargs="+", metavar="FILE")
    encrypt.add_argument("-r", "--recipient", action="append", default=[], help="age1… public key instead of a passphrase")
    decrypt = commands.add_parser("decrypt", help="decrypt FILE.age (with this device's key or a passphrase)")
    decrypt.add_argument("files", nargs="+", metavar="FILE")
    decrypt.add_argument("-i", "--identity", action="append", default=[], help="file with an AGE-SECRET-KEY-1…")
    commands.add_parser("keygen", help="show this device's age public key, for others to encrypt to")

    share = commands.add_parser("share", help="give a file or folder away by a one-time link and QR code")
    share.add_argument("path")
    share.add_argument("--ttl", default="1h", help="how long the link works: 10m, 1h, 1d (default 1h)")
    share.add_argument("--many", action="store_true", help="allow more than one download until it expires")
    share.add_argument("--port", type=int, default=0, help="port (default: any free one)")

    stats = commands.add_parser("stats", help="totals from the journal; --card writes a shareable SVG")
    stats.add_argument("--days", type=int, default=0, help="only the last N days")
    stats.add_argument("--card", metavar="FILE.svg", help="write a summary card picture")
    commands.add_parser("ёж", aliases=["hedgehog"], help="a hedgehog")

    peers = commands.add_parser("peers", help="list FTA devices on the local network")
    peers.add_argument("--wait", type=float, default=3, help="seconds to listen (default 3)")

    history = commands.add_parser("history", help="show the transfer journal")
    history.add_argument("-n", type=int, default=20, help="how many entries (default 20)")
    history.add_argument("--csv", metavar="FILE", help="export the whole journal to a CSV file")

    for name, help_text in (
        ("ls", "list a directory"),
        ("stat", "show one file or directory"),
        ("mkdir", "create a directory"),
        ("rm", "delete a file or an empty directory"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("location")
        _add_option_flag(command, "--opt", "transport option (repeatable)")
        if name == "mkdir":
            command.add_argument("-p", "--parents", action="store_true", help="no error if it exists")

    commands.add_parser("schemes", help="list supported URL schemes")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from fta.core import hostkeys

    args = build_parser().parse_args(argv)
    # `fta serve` and `fta receive` run for hours: their status lines must reach a pipe or
    # a service log at once, not when an 8 KiB buffer fills.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    hostkeys.DEFAULT_PATH = default_data_dir() / "known_hosts"
    if args.limit:
        from fta.core.throttle import LIMITER, parse_rate

        LIMITER.set_rate(parse_rate(args.limit))
    from fta.tools import rclone

    rclone.DATA_DIR = default_data_dir()
    while True:
        try:
            return _run(args)
        except hostkeys.UnknownHostKey as unknown:
            # As PuTTY and ssh do: show the fingerprint and let the user decide.
            print(f"The server {unknown.host}:{unknown.port} is not known yet.", file=sys.stderr)
            print(f"Its {unknown.key_type} key fingerprint is {unknown.fingerprint}", file=sys.stderr)
            if not sys.stdin.isatty():
                print("fta: error: unknown host key; run interactively once to trust it", file=sys.stderr)
                return 1
            if input("Trust it and continue? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
                return 1
            hostkeys.trust(unknown.host, unknown.port, unknown.key)


def _run(args: argparse.Namespace) -> int:
    from fta.core import hostkeys

    try:
        if args.command == "cp":
            return _cp(args)
        elif args.command == "serve":
            return _serve(args)
        elif args.command == "sync":
            return _sync(args)
        elif args.command == "send":
            return _send(args)
        elif args.command == "receive":
            return _receive(args)
        elif args.command == "peers":
            return _peers(args)
        elif args.command == "cloud":
            return _cloud(args)
        elif args.command == "encrypt":
            return _encrypt(args)
        elif args.command == "decrypt":
            return _decrypt(args)
        elif args.command == "keygen":
            from fta.crypto import age

            print(age.Identity.parse(_my_age_key()).recipient)
        elif args.command == "share":
            return _share(args)
        elif args.command == "stats":
            return _stats(args)
        elif args.command in ("ёж", "hedgehog"):
            from fta.fun import HEDGEHOG, random_meme

            print(HEDGEHOG.rstrip() + "\n  " + random_meme())
        elif args.command == "history":
            journal = _journal()
            if args.csv:
                print(journal.export_csv(Path(args.csv)))
            for entry in journal.entries(limit=args.n):
                when = datetime.fromtimestamp(entry.time).strftime("%Y-%m-%d %H:%M")
                digest = f"  sha256:{entry.sha256[:12]}" if entry.sha256 else ""
                print(f"{when}  {entry.status:9}  {entry.size:>12}  {entry.source} -> {entry.destination}{digest}")
        elif args.command == "schemes":
            print("\n".join(known_schemes()))
        else:
            path = Endpoint.parse(args.location).path
            with open_transport(args.location, **dict(args.opt)) as transport:
                if args.command == "ls":
                    for entry in transport.listdir(path):
                        print(_format_entry(entry))
                elif args.command == "stat":
                    print(_format_entry(transport.stat(path)))
                elif args.command == "mkdir":
                    transport.mkdir(path, exist_ok=args.parents)
                else:
                    transport.delete(path)
    except hostkeys.UnknownHostKey:
        raise
    except (TransferError, OSError, ValueError) as exc:
        print(f"fta: error: {exc}", file=sys.stderr)
        return 1
    return 0
