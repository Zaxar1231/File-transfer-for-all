"""The built-in SSH/SFTP server and its transfer shell, driven by paramiko and real PuTTY tools."""

from __future__ import annotations

import shutil
import subprocess
import time

import pytest

paramiko = pytest.importorskip("paramiko")
asyncssh = pytest.importorskip("asyncssh")

from fta import copy  # noqa: E402
from fta.app.transfers import TransferQueue  # noqa: E402
from fta.core.journal import Journal  # noqa: E402
from fta.server.passwords import check_password, hash_password  # noqa: E402
from fta.server.shell import Shell, ShellContext  # noqa: E402
from fta.server.ssh import SshServer, SshServerConfig  # noqa: E402
from fta.transports import memory  # noqa: E402
from test_protocols import resume_and_verify, roundtrip_tree  # noqa: E402

PASSWORD = "s3cret pass"


@pytest.fixture
def context(tmp_path):
    root = tmp_path / "share"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "report.txt").write_text("quarterly report")
    memory.reset()
    return ShellContext(
        root=root,
        queue=TransferQueue(update_interval=0),
        journal=Journal(tmp_path / "journal.jsonl"),
        resolve=lambda text: (text.replace("@box:", "mem://box"), {}),
        peers=lambda: ["laptop (192.168.1.20)"],
        retries=0,
    )


@pytest.fixture
def server(tmp_path, context):
    config = SshServerConfig(
        root=context.root,
        host_key=tmp_path / "host_key",
        port=0,
        host="127.0.0.1",
        users={"alice": hash_password(PASSWORD)},
        authorized_keys=tmp_path / "authorized_keys",
    )
    instance = SshServer(config, lambda: context)
    instance.start()
    yield instance
    instance.stop()


def test_password_hashes():
    stored = hash_password("pw")
    assert "pw" not in stored
    assert check_password("pw", stored)
    assert not check_password("PW", stored)
    assert not check_password("pw", "garbage")


def test_shell_navigation_stays_inside_the_share(context):
    shell = Shell(context)
    assert "docs" in shell.run("ls")
    assert shell.run("cd docs") == ""
    assert shell.run("pwd") == "/docs\n"
    assert "report.txt" in shell.run("ls")
    assert "outside the shared folder" in shell.run("cd ../..")
    assert "outside the shared folder" in shell.run("ls /../../etc")
    assert shell.prompt == "fta:/docs> "
    assert "unknown command" in shell.run("format c:")


def test_shell_queues_transfers_and_reports_them(context):
    shell = Shell(context)
    assert "queued #" in shell.run("send docs/report.txt @box:/inbox/report.txt")
    assert context.queue.wait_idle()
    assert memory._stores["box"]["/inbox/report.txt"] == b"quarterly report"

    assert "queued #" in shell.run("recv mem://box/inbox/report.txt docs/back")
    (context.root / "docs" / "back").mkdir(exist_ok=True)
    assert context.queue.wait_idle()
    assert "done" in shell.run("queue")
    assert "laptop" in shell.run("peers")
    assert "error: no transfer #999" in shell.run("pause 999")
    assert shell.run("history 1").count("\n") == 1


def test_sftp_client_against_our_server(server, tmp_path):
    url = f"sftp://alice@127.0.0.1:{server.port}"
    options = {"password": PASSWORD, "accept_unknown_host": "true"}
    work = tmp_path / "work"
    work.mkdir()
    roundtrip_tree(url, work, options)
    resume_and_verify(url, work, options)


def test_exec_and_interactive_shell(server):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("127.0.0.1", port=server.port, username="alice", password=PASSWORD, look_for_keys=False, allow_agent=False)
    try:
        _, stdout, _ = client.exec_command("ls docs")
        assert "report.txt" in stdout.read().decode()

        channel = client.invoke_shell()
        channel.send(b"help\n")
        output = b""
        deadline = time.monotonic() + 5
        while b"transfer shell" not in output and time.monotonic() < deadline:
            if channel.recv_ready():
                output += channel.recv(65536)
            time.sleep(0.05)
        assert b"transfer shell" in output and b"fta:/>" in output
        channel.send(b"exit\n")
    finally:
        client.close()


def test_wrong_password_is_refused(server):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    with pytest.raises(paramiko.AuthenticationException):
        client.connect("127.0.0.1", port=server.port, username="alice", password="nope", look_for_keys=False, allow_agent=False)
    client.close()


def test_public_key_login(server, tmp_path):
    key = asyncssh.generate_private_key("ssh-ed25519")
    key.write_private_key(str(tmp_path / "id_ed25519"))
    key.write_public_key(str(server.config.authorized_keys))
    server.stop()
    server.start()  # authorized_keys is read when the server starts

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        "127.0.0.1",
        port=server.port,
        username="bob",
        key_filename=str(tmp_path / "id_ed25519"),
        look_for_keys=False,
        allow_agent=False,
    )
    _, stdout, _ = client.exec_command("pwd")
    assert stdout.read().decode().strip() == "/"
    client.close()


@pytest.mark.skipif(not shutil.which("psftp") or not shutil.which("plink"), reason="PuTTY tools are not installed")
def test_real_putty_tools(server, tmp_path):
    """psftp and plink from PuTTY, with the host key pinned by its fingerprint as a user would check it."""
    upload = tmp_path / "from-putty.bin"
    upload.write_bytes(b"sent by psftp" * 1000)
    script = tmp_path / "psftp.txt"
    script.write_text(f"cd docs\nput {upload} from-putty.bin\nls\nget report.txt {tmp_path / 'got.txt'}\nquit\n")
    common = ["-P", str(server.port), "-pw", PASSWORD, "-batch", "-hostkey", server.fingerprint]

    psftp = subprocess.run(["psftp", *common, "-b", str(script), "alice@127.0.0.1"], capture_output=True, text=True, timeout=60)
    assert psftp.returncode == 0, psftp.stderr
    assert "from-putty.bin" in psftp.stdout
    assert (server.config.root / "docs" / "from-putty.bin").read_bytes() == upload.read_bytes()
    assert (tmp_path / "got.txt").read_text() == "quarterly report"

    plink = subprocess.run(["plink", *common, "alice@127.0.0.1", "ls docs"], capture_output=True, text=True, timeout=60)
    assert plink.returncode == 0, plink.stderr
    assert "report.txt" in plink.stdout and "from-putty.bin" in plink.stdout

    wrong = subprocess.run(
        ["plink", "-P", str(server.port), "-pw", PASSWORD, "-batch", "-hostkey", "SHA256:" + "A" * 43, "alice@127.0.0.1", "ls"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert wrong.returncode != 0  # a pinned fingerprint that does not match is rejected


def test_host_key_survives_restarts(server):
    first = server.fingerprint
    server.stop()
    server.start()
    assert server.fingerprint == first
    assert copy  # keep the import used by the helpers


@pytest.mark.skipif(not shutil.which("plink"), reason="PuTTY tools are not installed")
def test_fta_serve_command(tmp_path):
    """`fta serve` as a user runs it: settings from config.toml, then PuTTY connects."""
    import os
    import signal
    import sys

    from fta.app.storage import Store

    data = tmp_path / "data"
    share = tmp_path / "public"
    share.mkdir()
    (share / "hello.txt").write_text("hi")
    store = Store(data / "config.toml", environ={})
    store.settings.server_password_hash = hash_password(PASSWORD)
    store.save()

    env = {**os.environ, "FTA_DATA_DIR": str(data)}
    process = subprocess.Popen(
        [sys.executable, "-m", "fta", "serve", "--port", "0", "--host", "127.0.0.1", "--root", str(share)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        lines = [process.stdout.readline() for _ in range(2)]
        port = int(lines[0].rsplit(" ", 1)[-1])
        fingerprint = lines[1].split(": ", 1)[1].strip()
        result = subprocess.run(
            ["plink", "-P", str(port), "-pw", PASSWORD, "-batch", "-hostkey", fingerprint, "fta@127.0.0.1", "ls"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "hello.txt" in result.stdout
    finally:
        process.send_signal(signal.SIGINT)
        process.wait(15)
    assert process.returncode == 0


def test_shell_refuses_local_paths_outside_the_share(context, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("not for ssh users")
    shell = Shell(context)
    assert "not allowed" in shell.run(f"recv {secret}")
    assert "not allowed" in shell.run(f"recv file://{secret}")
    assert "not allowed" in shell.run(f"send docs/report.txt {tmp_path / 'dropped.txt'}")
    assert context.queue.jobs == []


def test_two_factor_login(tmp_path, context):
    from fta.crypto import totp

    secret = totp.new_secret()
    config = SshServerConfig(
        root=context.root,
        host_key=tmp_path / "host_key_2fa",
        port=0,
        host="127.0.0.1",
        users={"alice": hash_password(PASSWORD)},
        totp_secret=secret,
    )
    server = SshServer(config, lambda: context)
    server.start()
    try:
        def login(password, code):
            transport = paramiko.Transport(("127.0.0.1", server.port))
            transport.start_client()
            try:
                answers = iter([password, code])
                transport.auth_interactive("alice", lambda title, instructions, prompts: [next(answers) for _ in prompts])
                return transport.is_authenticated()
            except paramiko.AuthenticationException:
                return False
            finally:
                transport.close()

        # A plain password is no longer enough.
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        with pytest.raises(paramiko.SSHException):
            client.connect("127.0.0.1", port=server.port, username="alice", password=PASSWORD, look_for_keys=False, allow_agent=False)
        client.close()

        assert not login(PASSWORD, "000000" if totp.code(secret) != "000000" else "111111")
        assert not login("wrong", totp.code(secret))
        good = totp.code(secret)
        assert login(PASSWORD, good)
        assert not login(PASSWORD, good)  # the same code cannot be used twice
    finally:
        server.stop()


def test_totp_matches_rfc_6238_vectors():
    import base64

    from fta.crypto import totp

    secret = base64.b32encode(b"12345678901234567890").decode()
    # RFC 6238 appendix B (SHA-1), last six digits
    for at, expected in [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")]:
        assert totp.code(secret, at) == expected
    verifier = totp.Verifier(secret)
    assert verifier.check("287 082", at=59 + 30)  # one step of clock drift is fine
    assert not verifier.check("287082", at=59 + 30)  # but only once
    assert not verifier.check("12345", at=59)
    uri = totp.provisioning_uri(secret, "fta@laptop")
    assert uri.startswith("otpauth://totp/File%20Transfer%20for%20All%3Afta%40laptop?secret=")
