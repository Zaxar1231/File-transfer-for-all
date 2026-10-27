"""Command-line interface: ``fta cp | ls | stat | mkdir | rm | schemes``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime

from fta.core.endpoint import Endpoint
from fta.core.errors import TransferError
from fta.core.manager import copy
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

    def start(self, path: str) -> None:
        self.finish()
        self.label = path

    def update(self, done: int, total: int | None) -> None:
        text = f"{self.label}: {done * 100 // total}% ({done}/{total} bytes)" if total else f"{self.label}: {done} bytes"
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fta", description="Transfer files over any supported protocol.")
    commands = parser.add_subparsers(dest="command", required=True)

    cp = commands.add_parser("cp", help="copy a file or, with -r, a directory")
    cp.add_argument("source")
    cp.add_argument("destination")
    cp.add_argument("-r", "--recursive", action="store_true", help="copy directories recursively")
    _add_option_flag(cp, "--src-opt", "option for the source transport (repeatable)")
    _add_option_flag(cp, "--dst-opt", "option for the destination transport (repeatable)")

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
    args = build_parser().parse_args(argv)
    try:
        if args.command == "cp":
            line = _ProgressLine()
            try:
                copy(
                    args.source,
                    args.destination,
                    progress=line.update,
                    recursive=args.recursive,
                    on_file=line.start,
                    source_options=dict(args.src_opt),
                    destination_options=dict(args.dst_opt),
                )
            finally:
                line.finish()
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
    except (TransferError, OSError) as exc:
        print(f"fta: error: {exc}", file=sys.stderr)
        return 1
    return 0
