"""End-to-end file encryption in the age format (https://age-encryption.org/v1).

Files encrypted here open with the official ``age`` tool and the other way
round. Two kinds of recipients:

* a passphrase (scrypt), for sending a file to someone you can tell a password;
* an X25519 key, ``age1…``, for people who gave you their public key; they open
  the file with their ``AGE-SECRET-KEY-1…``.

The payload is ChaCha20-Poly1305 in 64 KiB chunks (age's STREAM), so files of
any size are encrypted with constant memory and any tampering is detected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from fta.core.errors import TransferError

INTRO = b"age-encryption.org/v1\n"
CHUNK = 64 * 1024
TAG = 16
SCRYPT_LOG_N = 18
"""Work factor the age tool uses by default (about a second, 256 MiB)."""
MAX_SCRYPT_LOG_N = 22
SUFFIX = ".age"

Progress = Callable[[int, int | None], None]


class AgeError(TransferError):
    """Wrong passphrase or key, or a damaged or tampered file."""


# --- keys -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """An X25519 key pair: the secret opens files, the recipient string encrypts to it."""

    private: X25519PrivateKey

    @classmethod
    def generate(cls) -> Identity:
        return cls(X25519PrivateKey.generate())

    @classmethod
    def parse(cls, text: str) -> Identity:
        hrp, data = bech32_decode(text.strip())
        if hrp.lower() != "age-secret-key-" or len(data) != 32:
            raise AgeError("not an age secret key (AGE-SECRET-KEY-1…)")
        return cls(X25519PrivateKey.from_private_bytes(data))

    def __str__(self) -> str:
        raw = self.private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        return bech32_encode("age-secret-key-", raw).upper()

    @property
    def recipient(self) -> str:
        raw = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return bech32_encode("age", raw)


def parse_recipient(text: str) -> X25519PublicKey:
    hrp, data = bech32_decode(text.strip())
    if hrp != "age" or len(data) != 32:
        raise AgeError(f"not an age recipient (age1…): {text[:20]}")
    return X25519PublicKey.from_public_bytes(data)


# --- encrypting ------------------------------------------------------------------------


def encrypt(
    source: BinaryIO,
    target: BinaryIO,
    *,
    passphrase: str | None = None,
    recipients: Iterable[str] = (),
    progress: Progress | None = None,
    size: int | None = None,
    scrypt_log_n: int = SCRYPT_LOG_N,
) -> None:
    recipients = list(recipients)
    if bool(passphrase) == bool(recipients):
        raise ValueError("encrypt to a passphrase or to recipients, not both or neither")
    file_key = os.urandom(16)
    stanzas = [_scrypt_stanza(file_key, passphrase, scrypt_log_n)] if passphrase else [
        _x25519_stanza(file_key, parse_recipient(r)) for r in recipients
    ]
    header = INTRO + b"".join(stanzas) + b"---"
    mac = hmac.new(_hkdf(file_key, b"", b"header"), header, hashlib.sha256).digest()
    target.write(header + b" " + _b64(mac) + b"\n")
    nonce = os.urandom(16)
    target.write(nonce)
    aead = ChaCha20Poly1305(_hkdf(file_key, nonce, b"payload"))
    done = 0
    counter = 0
    chunk = _read_full(source, CHUNK)
    while True:
        following = _read_full(source, CHUNK) if len(chunk) == CHUNK else b""
        last = not following
        target.write(aead.encrypt(_stream_nonce(counter, last), chunk, None))
        done += len(chunk)
        if progress:
            progress(done, size)
        if last:
            break
        counter += 1
        chunk = following


def decrypt(
    source: BinaryIO,
    target: BinaryIO,
    *,
    passphrase: str | None = None,
    identities: Iterable[str] = (),
    progress: Progress | None = None,
    size: int | None = None,
) -> None:
    header_lines, mac = _read_header(source)
    file_key = _unwrap(header_lines, passphrase, [Identity.parse(i) for i in identities])
    header = b"".join(header_lines) + b"---"
    expected = hmac.new(_hkdf(file_key, b"", b"header"), header, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise AgeError("the file header was changed; refusing to decrypt")
    nonce = source.read(16)
    if len(nonce) != 16:
        raise AgeError("the file is cut short")
    aead = ChaCha20Poly1305(_hkdf(file_key, nonce, b"payload"))
    counter, done = 0, 0
    block = _read_full(source, CHUNK + TAG)
    while True:
        following = _read_full(source, CHUNK + TAG) if len(block) == CHUNK + TAG else b""
        last = not following
        try:
            plain = aead.decrypt(_stream_nonce(counter, last), block, None)
        except InvalidTag:
            raise AgeError("the file is damaged or was tampered with") from None
        if not last and len(plain) != CHUNK or (counter > 0 and last and not plain):
            raise AgeError("the file is damaged")
        target.write(plain)
        done += len(block)
        if progress:
            progress(done, size)
        if last:
            break
        counter += 1
        block = following


def encrypt_file(path: Path, target: Path | None = None, **kwargs) -> Path:
    """``name`` → ``name.age`` next to it (or ``target``); the original is kept."""
    target = target or path.with_name(path.name + SUFFIX)
    tmp = target.with_name(target.name + ".part")
    try:
        with path.open("rb") as source, tmp.open("wb") as out:
            encrypt(source, out, size=path.stat().st_size, **kwargs)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def decrypt_file(path: Path, target: Path | None = None, **kwargs) -> Path:
    """``name.age`` → ``name``; nothing is written unless the whole file checks out."""
    if target is None:
        target = path.with_name(path.name[: -len(SUFFIX)] if path.name.endswith(SUFFIX) else path.name + ".decrypted")
    tmp = target.with_name(target.name + ".part")
    try:
        with path.open("rb") as source, tmp.open("wb") as out:
            decrypt(source, out, size=path.stat().st_size, **kwargs)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def is_encrypted(path: Path) -> bool:
    try:
        with path.open("rb") as fp:
            return fp.read(len(INTRO)) == INTRO
    except OSError:
        return False


# --- format details --------------------------------------------------------------------


def _scrypt_stanza(file_key: bytes, passphrase: str, log_n: int) -> bytes:
    salt = os.urandom(16)
    key = _scrypt(passphrase, salt, log_n)
    body = ChaCha20Poly1305(key).encrypt(b"\x00" * 12, file_key, None)
    return b"-> scrypt " + _b64(salt) + b" " + str(log_n).encode() + b"\n" + _wrap(_b64(body))


def _x25519_stanza(file_key: bytes, recipient: X25519PublicKey) -> bytes:
    ephemeral = X25519PrivateKey.generate()
    share = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = ephemeral.exchange(recipient)
    theirs = recipient.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = _hkdf(shared, share + theirs, b"age-encryption.org/v1/X25519")
    body = ChaCha20Poly1305(key).encrypt(b"\x00" * 12, file_key, None)
    return b"-> X25519 " + _b64(share) + b"\n" + _wrap(_b64(body))


def _unwrap(lines: list[bytes], passphrase: str | None, identities: list[Identity]) -> bytes:
    stanzas = _stanzas(lines)
    scrypt = [s for s in stanzas if s[0][0] == b"scrypt"]
    if scrypt and len(stanzas) != 1:
        raise AgeError("a passphrase-encrypted file must have exactly one recipient")
    for args, body in stanzas:
        try:
            if args[0] == b"scrypt" and passphrase:
                salt, log_n = _unb64(args[1]), int(args[2])
                if not 1 <= log_n <= MAX_SCRYPT_LOG_N or len(salt) != 16:
                    raise AgeError("unsupported scrypt parameters")
                return ChaCha20Poly1305(_scrypt(passphrase, salt, log_n)).decrypt(b"\x00" * 12, body, None)
            if args[0] == b"X25519":
                share = _unb64(args[1])
                for identity in identities:
                    shared = identity.private.exchange(X25519PublicKey.from_public_bytes(share))
                    mine = identity.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
                    key = _hkdf(shared, share + mine, b"age-encryption.org/v1/X25519")
                    try:
                        return ChaCha20Poly1305(key).decrypt(b"\x00" * 12, body, None)
                    except InvalidTag:
                        continue
        except (InvalidTag, ValueError, IndexError):
            continue
    if scrypt:
        raise AgeError("wrong passphrase" if passphrase else "this file needs a passphrase")
    raise AgeError("none of your keys opens this file" if identities else "this file needs an age secret key")


def _read_header(source: BinaryIO) -> tuple[list[bytes], bytes]:
    first = source.readline(256)
    if first != INTRO:
        raise AgeError("not an age file")
    lines = [first]
    for _ in range(1000):
        line = source.readline(4096)
        if not line.endswith(b"\n"):
            raise AgeError("the file header is damaged")
        if line.startswith(b"--- "):
            return lines, _unb64(line[4:-1])
        lines.append(line)
    raise AgeError("the file header is too long")


def _stanzas(lines: list[bytes]) -> list[tuple[list[bytes], bytes]]:
    stanzas: list[tuple[list[bytes], bytes]] = []
    index = 1
    while index < len(lines):
        line = lines[index].rstrip(b"\n")
        if not line.startswith(b"-> "):
            raise AgeError("the file header is damaged")
        args = line[3:].split(b" ")
        body = b""
        index += 1
        while index < len(lines):
            part = lines[index].rstrip(b"\n")
            index += 1
            body += part
            if len(part) < 64:
                break
        stanzas.append((args, _unb64(body)))
    return stanzas


def _stream_nonce(counter: int, last: bool) -> bytes:
    return counter.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


def _hkdf(key: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(hashes.SHA256(), 32, salt or None, info).derive(key)


def _scrypt(passphrase: str, salt: bytes, log_n: int) -> bytes:
    return Scrypt(b"age-encryption.org/v1/scrypt" + salt, 32, 2**log_n, 8, 1).derive(passphrase.encode())


def _read_full(source: BinaryIO, size: int) -> bytes:
    data = b""
    while len(data) < size:
        part = source.read(size - len(data))
        if not part:
            break
        data += part
    return data


def _b64(data: bytes) -> bytes:
    return base64.b64encode(data).rstrip(b"=")


def _unb64(data: bytes) -> bytes:
    if b"=" in data or b"\n" in data:
        raise AgeError("bad base64 in the header")
    try:
        return base64.b64decode(data + b"=" * (-len(data) % 4), validate=True)
    except ValueError:  # binascii.Error: the header was damaged
        raise AgeError("bad base64 in the header") from None


def _wrap(data: bytes) -> bytes:
    lines = [data[i : i + 64] for i in range(0, len(data), 64)] or [b""]
    if len(lines[-1]) == 64:
        lines.append(b"")
    return b"".join(line + b"\n" for line in lines)


# --- bech32 (BIP 173), as age uses for keys ----------------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    check = 1
    for value in values:
        top = check >> 25
        check = (check & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            check ^= generator[i] if (top >> i) & 1 else 0
    return check


def _expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert(data: bytes | list[int], source: int, target: int, pad: bool) -> list[int]:
    accumulator, bits, out = 0, 0, []
    for value in data:
        accumulator = (accumulator << source) | value
        bits += source
        while bits >= target:
            bits -= target
            out.append((accumulator >> bits) & ((1 << target) - 1))
    if pad and bits:
        out.append((accumulator << (target - bits)) & ((1 << target) - 1))
    elif not pad and (bits >= source or (accumulator << (target - bits)) & ((1 << target) - 1)):
        raise AgeError("bad key encoding")
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    values = _convert(data, 8, 5, True)
    checksum = _polymod(_expand(hrp) + values + [0] * 6) ^ 1
    values += [(checksum >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[v] for v in values)


def bech32_decode(text: str) -> tuple[str, bytes]:
    if text.lower() != text and text.upper() != text:
        raise AgeError("mixed-case key")
    text = text.lower()
    hrp, sep, rest = text.rpartition("1")
    if not sep or not hrp or len(rest) < 6 or any(c not in _CHARSET for c in rest):
        raise AgeError("bad key encoding")
    values = [_CHARSET.index(c) for c in rest]
    if _polymod(_expand(hrp) + values) != 1:
        raise AgeError("the key's checksum does not match (typo?)")
    return hrp, bytes(_convert(values[:-6], 5, 8, False))
