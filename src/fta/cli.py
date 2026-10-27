"""Command-line interface: ``fta cp``, ``fta ls``, ``fta schemes``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from fta.core.endpoint import Endpoint
from fta.core.errors import TransferError
from fta.core.manager import copy
from fta.core.registry import known_schemes, open_transport


def _key_value(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    return key, value


def _add_option_flag(parser: argparse.ArgumentParser, flag: str, help_text: str) -> None:
    parser.add_argument(flag, type=_key_value, action="append", default=[], metavar="KEY=VALUE", help=help_text)


def _progress_printer(label: str) -> Callable[[int, int | None], None]:
    def report(done: int, total: int | None) -> None:
        text = f"{label}: {done * 100 // total}% ({done}/{total} bytes)" if total else f"{label}: {done} bytes"
        sys.stderr.write(f"\r{text}")
        sys.stderr.flush()

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fta", description="Transfer files over any supported protocol.")
    commands = parser.add_subparsers(dest="command", required=True)

    cp = commands.add_parser("cp", help="copy one file between local paths and URLs")
    cp.add_argument("source")
    cp.add_argument("destination")
    _add_option_flag(cp, "--src-opt", "option for the source transport (repeatable)")
    _add_option_flag(cp, "--dst-opt", "option for the destination transport (repeatable)")

    ls = commands.add_parser("ls", help="list a remote directory")
    ls.add_argument("location")
    _add_option_flag(ls, "--opt", "transport option (repeatable)")

    commands.add_parser("schemes", help="list supported URL schemes")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "cp":
            label = Path(Endpoint.parse(args.source).path).name
            copy(
                args.source,
                args.destination,
                progress=_progress_printer(label),
                source_options=dict(args.src_opt),
                destination_options=dict(args.dst_opt),
            )
            sys.stderr.write("\n")
        elif args.command == "ls":
            endpoint = Endpoint.parse(args.location)
            with open_transport(args.location, **dict(args.opt)) as transport:
                for entry in transport.listdir(endpoint.path or "/"):
                    size = "-" if entry.size is None else str(entry.size)
                    print(f"{'d' if entry.is_dir else '-'} {size:>12}  {entry.name}")
        else:
            print("\n".join(known_schemes()))
    except (TransferError, OSError) as exc:
        print(f"fta: error: {exc}", file=sys.stderr)
        return 1
    return 0
