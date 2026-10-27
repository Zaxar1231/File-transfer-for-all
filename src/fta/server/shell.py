"""The interactive transfer shell behind the SSH server.

It is plain synchronous code with no SSH in it: ``Shell.run(line)`` returns the
text to print, which keeps every command testable directly.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from fta import __version__
from fta.app.transfers import JobState, TransferJob, TransferQueue
from fta.core.endpoint import Endpoint
from fta.core.journal import Journal

Resolver = Callable[[str], tuple[str, dict[str, Any]]]
"""Turns what the user typed (a URL or ``@connection:/path``) into a URL and transport options."""

HELP = f"""File Transfer for All {__version__}: transfer shell

  ls [path]                  list files in the shared folder
  cd <dir>, pwd              move around the shared folder
  send <file> <url>          copy a shared file to a URL or @connection:/path
  recv <url> [dir]           download a URL into the shared folder
  queue                      transfers with their ids and progress
  pause <id> / resume <id>   pause or continue a transfer
  cancel <id>                cancel a transfer
  peers                      devices found on the local network
  history [n]                last transfers from the journal (default 10)
  help, exit

  Files can also be copied with SFTP: psftp, WinSCP, FileZilla, sftp.
"""


@dataclass
class ShellContext:
    root: Path
    queue: TransferQueue
    journal: Journal | None = None
    resolve: Resolver = lambda text: (text, {})
    peers: Callable[[], list[str]] = list
    retries: int = 3


class Shell:
    def __init__(self, context: ShellContext) -> None:
        self.context = context
        self.root = context.root.resolve()
        self.cwd = self.root

    @property
    def prompt(self) -> str:
        return f"fta:{self.display(self.cwd)}> "

    def display(self, path: Path) -> str:
        relative = path.relative_to(self.root).as_posix()
        return "/" if relative == "." else f"/{relative}"

    def run(self, line: str) -> str:
        try:
            words = shlex.split(line)
        except ValueError as exc:
            return f"error: {exc}\n"
        if not words:
            return ""
        command, args = words[0].lower(), words[1:]
        handler = getattr(self, f"cmd_{command}", None)
        if handler is None:
            return f"unknown command: {command} (type help)\n"
        try:
            return handler(args)
        except (OSError, ValueError, LookupError) as exc:
            return f"error: {exc}\n"

    # --- shared folder ----------------------------------------------------------------

    def _path(self, text: str) -> Path:
        """A path inside the shared folder; anything that escapes it is refused."""
        base = self.root if text.startswith("/") else self.cwd
        path = (base / text.lstrip("/")).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"{text}: outside the shared folder")
        return path

    def cmd_help(self, args: list[str]) -> str:
        return HELP

    def cmd_pwd(self, args: list[str]) -> str:
        return self.display(self.cwd) + "\n"

    def cmd_cd(self, args: list[str]) -> str:
        target = self._path(args[0] if args else "/")
        if not target.is_dir():
            raise ValueError(f"{args[0]}: not a directory")
        self.cwd = target
        return ""

    def cmd_ls(self, args: list[str]) -> str:
        target = self._path(args[0] if args else ".")
        if target.is_file():
            return _ls_line(target) + "\n"
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        return "".join(_ls_line(child) + "\n" for child in children)

    # --- transfers -------------------------------------------------------------------

    def cmd_send(self, args: list[str]) -> str:
        if len(args) != 2:
            raise ValueError("usage: send <file> <url>")
        local = self._path(args[0])
        if not local.exists():
            raise ValueError(f"{args[0]}: no such file")
        url, options = self._remote(args[1])
        job = self._queue(local.name, str(local), url, {}, options, local.is_dir(), f"SSH · {self.display(local)}", args[1])
        return f"queued #{job.id}: {self.display(local)} -> {args[1]}\n"

    def cmd_recv(self, args: list[str]) -> str:
        if len(args) not in (1, 2):
            raise ValueError("usage: recv <url> [dir]")
        url, options = self._remote(args[0])
        folder = self._path(args[1] if len(args) == 2 else ".")
        name = PurePosixPath(url.split("?", 1)[0].rstrip("/")).name or "download"
        job = self._queue(name, url, str(folder), options, {}, True, args[0], f"SSH · {self.display(folder)}")
        return f"queued #{job.id}: {args[0]} -> {self.display(folder)}\n"

    def _remote(self, text: str) -> tuple[str, dict[str, Any]]:
        """A URL or @connection. Local paths are refused: over SSH, the shared folder is
        the only part of this machine's disk a client may read or write."""
        url, options = self.context.resolve(text)
        if Endpoint.parse(url).is_local:
            raise ValueError(f"{text}: use a URL or @connection; local paths outside the shared folder are not allowed")
        return url, options

    def _queue(
        self, name, source, destination, source_options, destination_options, recursive, source_label, destination_label
    ) -> TransferJob:
        return self.context.queue.add(
            TransferJob(
                name=name,
                source=source_label,
                destination=destination_label,
                source_url=source,
                destination_url=destination,
                recursive=recursive,
                source_options=source_options,
                destination_options=destination_options,
                max_retries=self.context.retries,
            )
        )

    def cmd_queue(self, args: list[str]) -> str:
        jobs = self.context.queue.jobs
        if not jobs:
            return "no transfers\n"
        lines = []
        for job in jobs:
            progress = f"{job.fraction:.0%}" if job.fraction is not None and job.state is JobState.RUNNING else ""
            detail = job.error if job.state is JobState.FAILED else progress
            lines.append(f"#{job.id:<4} {job.state.value:<9} {job.name}  {detail}".rstrip())
        return "\n".join(lines) + "\n"

    def _job(self, args: list[str]) -> TransferJob:
        if len(args) != 1:
            raise ValueError("usage: <command> <id>")
        wanted = int(args[0].lstrip("#"))
        for job in self.context.queue.jobs:
            if job.id == wanted:
                return job
        raise LookupError(f"no transfer #{wanted}")

    def cmd_pause(self, args: list[str]) -> str:
        job = self._job(args)
        self.context.queue.pause(job)
        return f"#{job.id} pausing\n"

    def cmd_resume(self, args: list[str]) -> str:
        job = self._job(args)
        self.context.queue.resume(job)
        return f"#{job.id} resumed\n"

    def cmd_cancel(self, args: list[str]) -> str:
        job = self._job(args)
        self.context.queue.cancel(job)
        return f"#{job.id} cancelled\n"

    def cmd_peers(self, args: list[str]) -> str:
        peers = self.context.peers()
        return "".join(f"{peer}\n" for peer in peers) if peers else "no devices found on the local network\n"

    def cmd_history(self, args: list[str]) -> str:
        if self.context.journal is None:
            return "the journal is off\n"
        limit = int(args[0]) if args else 10
        entries = self.context.journal.entries(limit=limit)
        if not entries:
            return "no transfers yet\n"
        lines = [
            f"{datetime.fromtimestamp(e.time):%Y-%m-%d %H:%M}  {e.status:<9} {e.size:>12}  {e.source} -> {e.destination}"
            for e in entries
        ]
        return "\n".join(lines) + "\n"


def _ls_line(path: Path) -> str:
    info = path.stat()
    kind = "d" if path.is_dir() else "-"
    size = "" if path.is_dir() else str(info.st_size)
    return f"{kind} {size:>12}  {datetime.fromtimestamp(info.st_mtime):%Y-%m-%d %H:%M}  {path.name}"
