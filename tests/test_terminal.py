"""The built-in SSH terminal against a real bash, and host keys trusted on first use."""

from __future__ import annotations

import sys
import time

import pytest

pytest.importorskip("pyte")
pytest.importorskip("asyncssh")
if sys.platform == "win32":  # pragma: no cover
    pytest.skip("the test server needs a POSIX pty", allow_module_level=True)

from bash_ssh_server import PASSWORD, BashServer  # noqa: E402

from fta.core import hostkeys  # noqa: E402
from fta.terminal import KEYS, SshTerminal, control  # noqa: E402


@pytest.fixture
def server(tmp_path):
    instance = BashServer(str(tmp_path / "host_key"))
    instance.start()
    yield instance
    instance.stop()


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def open_terminal(server, tmp_path, **kwargs):
    terminal = SshTerminal(
        "127.0.0.1", server.port, "alice", PASSWORD, cols=80, rows=24, known_hosts=tmp_path / "known_hosts", **kwargs
    )
    try:
        terminal.connect()
    except hostkeys.UnknownHostKey as unknown:
        assert unknown.fingerprint == server.fingerprint  # what the user compares with the server
        hostkeys.trust(unknown.host, unknown.port, unknown.key, tmp_path / "known_hosts")
        terminal.connect()
    return terminal


def test_unknown_then_trusted_then_changed_host_key(server, tmp_path):
    known = tmp_path / "known_hosts"
    first = SshTerminal("127.0.0.1", server.port, "alice", PASSWORD, known_hosts=known)
    with pytest.raises(hostkeys.UnknownHostKey) as unknown:
        first.connect()
    hostkeys.trust("127.0.0.1", server.port, unknown.value.key, known)
    first.connect()  # remembered now: no question the second time
    first.close()

    # Another key for the same address is refused, loudly.
    import asyncssh

    asyncssh.generate_private_key("ssh-ed25519").write_private_key(str(tmp_path / "other"))
    impostor = BashServer(str(tmp_path / "other"))
    impostor.start()
    try:
        hostkeys.trust("127.0.0.1", impostor.port, unknown.value.key, known)  # pretend we knew the real key there
        with pytest.raises(hostkeys.HostKeyChanged):
            SshTerminal("127.0.0.1", impostor.port, "alice", PASSWORD, known_hosts=known).connect()
    finally:
        impostor.stop()


def test_shell_session_renders_like_a_terminal(server, tmp_path):
    changes = []
    closed = []
    terminal = open_terminal(server, tmp_path, on_change=lambda: changes.append(1), on_close=closed.append)
    assert wait_for(lambda: "@test:" in terminal.text())

    terminal.send("echo $((6*7)) $TERM\r")
    assert wait_for(lambda: "42 xterm-256color" in terminal.text())

    # Colours arrive as styled runs.
    terminal.send("printf '\\033[1;31mRED\\033[0m plain\\n'\r")
    assert wait_for(lambda: any(run.text == "RED" for row in terminal.rows() for run in row))
    red = next(run for row in terminal.rows() for run in row if run.text == "RED")
    assert red.fg == "red" and red.bold

    # The cursor is part of the picture.
    assert sum(run.cursor for row in terminal.rows() for run in row) == 1

    # Resizing reaches the remote side.
    terminal.resize(100, 30)
    terminal.send("stty size\r")
    assert wait_for(lambda: "30 100" in terminal.text())

    # Keys that are not text: Ctrl+C interrupts, arrows recall history.
    terminal.send("sleep 30\r")
    time.sleep(0.3)
    terminal.send(control("c"))
    terminal.send("echo after-interrupt\r")
    assert wait_for(lambda: "after-interrupt" in terminal.text().split("sleep 30")[-1])
    terminal.send(KEYS["Arrow Up"] + "\r")
    assert wait_for(lambda: terminal.text().count("after-interrupt") >= 3)

    terminal.send("exit\r")
    assert wait_for(lambda: bool(closed))
    assert not terminal.connected and changes


def test_full_screen_program_draws_in_place(server, tmp_path):
    terminal = open_terminal(server, tmp_path)
    assert wait_for(lambda: "@test:" in terminal.text())
    # Clear the screen and draw at a fixed position, as top or mc do.
    terminal.send("clear; printf '\\033[5;10HMIDDLE'; sleep 2\r")
    assert wait_for(lambda: len(terminal.text().splitlines()) > 4 and terminal.text().splitlines()[4].startswith(" " * 9 + "MIDDLE"))
    terminal.close()


def test_wrong_password_is_a_clear_error(server, tmp_path):
    from fta.core.errors import TransferError

    known = tmp_path / "known_hosts"
    terminal = SshTerminal("127.0.0.1", server.port, "alice", "nope", known_hosts=known)
    try:
        terminal.connect()
    except hostkeys.UnknownHostKey as unknown:
        hostkeys.trust(unknown.host, unknown.port, unknown.key, known)
    with pytest.raises(TransferError, match="ssh 127.0.0.1"):
        SshTerminal("127.0.0.1", server.port, "alice", "nope", known_hosts=known).connect()


def test_sftp_asks_about_unknown_hosts_too(server, tmp_path):
    """The file browser gets the same question instead of a bare "rejected" error."""
    from fta.core.registry import open_transport

    known = tmp_path / "known_hosts"
    url = f"sftp://alice@127.0.0.1:{server.port}/"
    with pytest.raises(hostkeys.UnknownHostKey):
        with open_transport(url, password=PASSWORD, known_hosts=str(known)):
            pass
