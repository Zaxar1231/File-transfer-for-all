"""Write docs/fta.1, the manual page, from the command line's own argparse description.

    python scripts/make_man.py          # regenerate
    man ./docs/fta.1                    # read it

A test fails when the page is out of date, so it always matches the program.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fta import __version__  # noqa: E402
from fta.cli import build_parser  # noqa: E402

PAGE = ROOT / "docs" / "fta.1"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("-", "\\-").replace("'", "\\(aq")


def _usage(parser: argparse.ArgumentParser) -> str:
    return " ".join(parser.format_usage().split()[1:])


def render() -> str:
    parser = build_parser()
    lines = [
        f'.TH FTA 1 "" "fta {__version__}" "File Transfer for All"',
        ".SH NAME",
        "fta \\- transfer files over SFTP, FTP, SMB, WebDAV, S3, HTTP, clouds and to nearby devices",
        ".SH SYNOPSIS",
        f".B fta\n{_escape(_usage(parser).removeprefix('fta '))}",
        ".SH DESCRIPTION",
        _escape(
            "fta is the command line of File Transfer for All. Locations are local paths, URLs such as "
            "sftp://user@host/dir, smb://server/share, ftp://, dav://, s3://bucket, rclone://cloud/ and "
            "@connection:/path for connections saved in the app."
        ),
        ".SH GLOBAL OPTIONS",
    ]
    for action in parser._actions:
        if action.option_strings and action.help != argparse.SUPPRESS and not isinstance(action, argparse._SubParsersAction):
            lines += [".TP", f".B {_escape(', '.join(action.option_strings))}", _escape(action.help or "")]
    lines.append(".SH COMMANDS")
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {choice.dest: choice.help for choice in commands._choices_actions}
    seen = set()
    for name, sub in commands.choices.items():
        if id(sub) in seen:
            continue  # aliases share one parser
        seen.add(id(sub))
        lines += [".SS " + _escape(name), _escape(helps.get(name) or ""), ".PP", f".B fta {_escape(_usage(sub).removeprefix('fta '))}"]
        for action in sub._actions:
            if isinstance(action, argparse._HelpAction) or action.help == argparse.SUPPRESS:
                continue
            label = ", ".join(action.option_strings) or (action.metavar or action.dest)
            lines += [".TP", f".B {_escape(str(label))}", _escape(action.help or "")]
    lines += [
        ".SH FILES",
        ".TP\n.I ~/.config/fta/config.toml\nsettings and saved connections (FTA_CONFIG, FTA_DATA_DIR change it; "
        "an fta\\-data folder next to the program makes it portable)",
        ".TP\n.I ~/.config/fta/journal.jsonl\nthe transfer journal (fta history, fta stats)",
        ".SH ENVIRONMENT",
        ".TP\n.B FTA_<SETTING>\nany setting, e.g. FTA_RETRIES=5 or FTA_SPEED_LIMIT_KIB=2048",
        ".TP\n.B FTA_RCLONE\nthe rclone program to use for clouds",
        ".SH EXAMPLES",
        ".nf",
        _escape("fta cp --resume --verify https://example.com/big.iso ./"),
        _escape("fta sync ./photos sftp://me@nas.local/backup/photos --delete"),
        _escape("fta send \"Laptop\" ./report.pdf"),
        _escape("fta share ./photos --ttl 1d"),
        _escape("fta encrypt -r age1... secret.pdf"),
        ".fi",
        ".SH SEE ALSO",
        "Project page: https://github.com/Zaxar1231/File\\-transfer\\-for\\-all",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    PAGE.write_text(render(), encoding="utf-8")
    print(f"wrote {PAGE}")
