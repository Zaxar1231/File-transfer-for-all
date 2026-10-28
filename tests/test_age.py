"""age encryption: round trips, tampering, and compatibility with the official age tool."""

from __future__ import annotations

import io
import os
import shutil
import subprocess

import pytest

from fta.crypto import age

AGE = os.environ.get("FTA_AGE_BIN") or shutil.which("age")
AGE_KEYGEN = (os.path.join(os.path.dirname(AGE), "age-keygen") if AGE else None) or shutil.which("age-keygen")
needs_age = pytest.mark.skipif(AGE is None, reason="the age tool is not installed")

FAST = {"scrypt_log_n": 10}  # tests need no brute-force resistance


def roundtrip(data: bytes, passphrase: str) -> bytes:
    sealed, opened = io.BytesIO(), io.BytesIO()
    age.encrypt(io.BytesIO(data), sealed, passphrase=passphrase, **FAST)
    age.decrypt(io.BytesIO(sealed.getvalue()), opened, passphrase=passphrase)
    return opened.getvalue()


@pytest.mark.parametrize("size", [0, 1, age.CHUNK - 1, age.CHUNK, age.CHUNK + 1, 3 * age.CHUNK, 200_000])
def test_passphrase_roundtrip_at_chunk_edges(size):
    data = os.urandom(size)
    assert roundtrip(data, passphrase="correct horse") == data


def test_keys_roundtrip_and_format():
    me = age.Identity.generate()
    assert me.recipient.startswith("age1") and str(me).startswith("AGE-SECRET-KEY-1")
    assert age.Identity.parse(str(me)).recipient == me.recipient
    data = os.urandom(100_000)
    sealed = io.BytesIO()
    age.encrypt(io.BytesIO(data), sealed, recipients=[me.recipient, age.Identity.generate().recipient])
    out = io.BytesIO()
    age.decrypt(io.BytesIO(sealed.getvalue()), out, identities=[str(me)])
    assert out.getvalue() == data
    with pytest.raises(age.AgeError, match="checksum"):
        age.parse_recipient(me.recipient[:-1] + ("q" if me.recipient[-1] != "q" else "p"))


def test_wrong_passphrase_wrong_key_and_tampering():
    sealed = io.BytesIO()
    age.encrypt(io.BytesIO(b"secret plans" * 10000), sealed, passphrase="right", **FAST)
    blob = sealed.getvalue()
    with pytest.raises(age.AgeError, match="wrong passphrase"):
        age.decrypt(io.BytesIO(blob), io.BytesIO(), passphrase="wrong")
    with pytest.raises(age.AgeError, match="needs a passphrase"):
        age.decrypt(io.BytesIO(blob), io.BytesIO(), identities=[str(age.Identity.generate())])
    flipped = bytearray(blob)
    flipped[-100] ^= 1
    with pytest.raises(age.AgeError, match="damaged or was tampered"):
        age.decrypt(io.BytesIO(bytes(flipped)), io.BytesIO(), passphrase="right")
    with pytest.raises(age.AgeError):
        age.decrypt(io.BytesIO(blob[:-17]), io.BytesIO(), passphrase="right")  # last chunk cut off
    with pytest.raises(age.AgeError, match="not an age file"):
        age.decrypt(io.BytesIO(b"hello"), io.BytesIO(), passphrase="right")


def test_files_are_replaced_only_when_complete(tmp_path):
    plain = tmp_path / "report.pdf"
    plain.write_bytes(os.urandom(150_000))
    sealed = age.encrypt_file(plain, passphrase="pw", **FAST)
    assert sealed.name == "report.pdf.age" and age.is_encrypted(sealed) and not age.is_encrypted(plain)
    plain.rename(tmp_path / "original.pdf")
    opened = age.decrypt_file(sealed, passphrase="pw")
    assert opened == plain and opened.read_bytes() == (tmp_path / "original.pdf").read_bytes()
    opened.unlink()
    with pytest.raises(age.AgeError):
        age.decrypt_file(sealed, passphrase="nope")
    assert not opened.exists() and not list(tmp_path.glob("*.part"))


@needs_age
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")  # the child execs at once
def test_official_age_opens_our_files_and_we_open_theirs(tmp_path):
    data = os.urandom(150_000)
    (tmp_path / "plain").write_bytes(data)

    # Keys: ours → age, age-keygen → ours.
    me = age.Identity.generate()
    age.encrypt_file(tmp_path / "plain", tmp_path / "ours.age", recipients=[me.recipient])
    (tmp_path / "key.txt").write_text(str(me) + "\n")
    out = subprocess.run([AGE, "-d", "-i", str(tmp_path / "key.txt"), str(tmp_path / "ours.age")], capture_output=True, check=True)
    assert out.stdout == data

    subprocess.run([AGE_KEYGEN, "-o", str(tmp_path / "theirs.txt")], check=True, capture_output=True)
    their_secret = next(line for line in (tmp_path / "theirs.txt").read_text().splitlines() if line.startswith("AGE-SECRET"))
    their_recipient = age.Identity.parse(their_secret).recipient
    subprocess.run([AGE, "-r", their_recipient, "-o", str(tmp_path / "theirs.age"), str(tmp_path / "plain")], check=True)
    assert age.decrypt_file(tmp_path / "theirs.age", tmp_path / "back", identities=[their_secret]).read_bytes() == data

    # Passphrases need a terminal for age; drive it through a pty.
    import pty

    age.encrypt_file(tmp_path / "plain", tmp_path / "pw.age", passphrase="hunter2 hunter2", **FAST)
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child
        os.execv(AGE, [AGE, "-d", "-o", str(tmp_path / "pw.out"), str(tmp_path / "pw.age")])
    output = b""
    while b"passphrase" not in output.lower():
        output += os.read(fd, 1024)
    os.write(fd, b"hunter2 hunter2\n")
    while True:
        try:
            if not os.read(fd, 1024):
                break
        except OSError:
            break
    os.waitpid(pid, 0)
    assert (tmp_path / "pw.out").read_bytes() == data
