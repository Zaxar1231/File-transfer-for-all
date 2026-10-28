"""Fuzzing: everything that reads input from other people or devices, fed with random data.

Each property says what must hold for *any* input: parsers fail only with their
own clear error type, paths from other devices never leave the receiving folder,
damaged ciphertext is rejected, the network endpoint never crashes.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from fta.core.endpoint import Endpoint  # noqa: E402
from fta.crypto import age  # noqa: E402
from fta.lan.direct import safe_relative_path  # noqa: E402
from fta.lan.links import looks_like_address, parse_link  # noqa: E402

FUZZ = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])

path_parts = st.sampled_from(["a", "..", ".", "", "b c", "C:", "\\", "/", "\x00", "ё", "con", "%2e%2e"])


@FUZZ
@given(st.text())
def test_endpoint_parse_fails_only_with_value_error(text):
    try:
        endpoint = Endpoint.parse(text)
    except ValueError:
        return  # e.g. a port that is not a number
    assert isinstance(endpoint.path, str)


@FUZZ
@given(st.text())
def test_device_links_fail_only_with_value_error(text):
    try:
        link = parse_link(text)
    except ValueError:
        return
    assert 0 < link.port < 65536 and (not link.fingerprint or len(link.fingerprint) == 64)
    looks_like_address(text)  # must not raise


@FUZZ
@given(st.one_of(st.text(), st.lists(path_parts, max_size=8).map("/".join)))
def test_paths_from_other_devices_stay_inside_the_folder(text):
    try:
        relative = safe_relative_path(text)
    except ValueError:
        return
    assert not relative.is_absolute()
    assert all(part not in ("", ".", "..") and ":" not in part and "\\" not in part for part in relative.parts)
    root = Path("/srv/inbox")
    assert root in (root / relative).parents


@FUZZ
@given(st.binary(max_size=3000))
def test_random_bytes_are_never_decrypted(blob):
    with pytest.raises(age.AgeError):
        age.decrypt(io.BytesIO(blob), io.BytesIO(), passphrase="pw")


_SEALED = io.BytesIO()
age.encrypt(io.BytesIO(b"x" * 70_000), _SEALED, passphrase="pw", scrypt_log_n=4)
SEALED = _SEALED.getvalue()


@FUZZ
@given(st.integers(0, len(SEALED) - 1), st.integers(1, 255))
def test_any_flipped_byte_is_detected(position, mask):
    damaged = bytearray(SEALED)
    damaged[position] ^= mask
    out = io.BytesIO()
    try:
        age.decrypt(io.BytesIO(bytes(damaged)), out, passphrase="pw")
    except age.AgeError:
        return
    # The only harmless flips are in base64 padding bits of the header, which must still give the same plaintext.
    assert out.getvalue() == b"x" * 70_000


@FUZZ
@given(st.text(max_size=200))
def test_ssh_shell_never_crashes_or_escapes(tmp_path_factory, line):
    from fta.server.shell import Shell, ShellContext

    root = tmp_path_factory.mktemp("share").resolve()
    queue = _NoRun()
    shell = Shell(ShellContext(root=root, queue=queue, resolve=lambda text: (text, {})))
    assert isinstance(shell.run(line), str)
    assert shell.cwd == root or root in shell.cwd.parents
    for job in queue.jobs:  # anything queued touches this machine only inside the share
        for url in (job.source_url, job.destination_url):
            if Endpoint.parse(url).is_local:
                local = Path(url).resolve()
                assert local == root or root in local.parents


class _NoRun:
    """A queue stand-in that records jobs without running them."""

    def __init__(self) -> None:
        self.jobs = []

    def add(self, job):
        self.jobs.append(job)
        return job

    def pause(self, job):
        pass

    def resume(self, job):
        pass

    def cancel(self, job):
        pass

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=20),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=12,
)
offers = st.fixed_dictionaries(
    {"sender": json_values, "files": st.lists(st.dictionaries(st.sampled_from(["path", "size", "mtime", "mode", "dir"]), json_values), max_size=4)}
) | json_values


@pytest.fixture(scope="module")
def receiver(tmp_path_factory):
    from fta.lan.direct import Receiver
    from fta.lan.identity import Identity

    base = tmp_path_factory.mktemp("fuzz-rx")
    instance = Receiver(Identity.load(base / "id"), base / "inbox", lambda offer: True, host="127.0.0.1", port=0)
    instance.start()
    yield instance
    instance.stop()


@FUZZ
@given(offers)
def test_receiver_answers_any_offer_without_crashing(receiver, body):
    from fta.lan import direct

    conn = direct._PinnedConnection("127.0.0.1", receiver.port, "", 10)
    try:
        status, _ = direct._call(conn, "POST", "/api/offer", json.dumps(body).encode(), {"Content-Type": "application/json"})
    finally:
        conn.close()
    assert status in (200, 400, 403)
    inbox = receiver.directory.resolve()
    for path in inbox.rglob("*"):
        assert inbox in path.resolve().parents  # nothing was created outside


@FUZZ
@given(st.binary(max_size=500))
def test_receiver_survives_garbage_bodies(receiver, blob):
    from fta.lan import direct

    conn = direct._PinnedConnection("127.0.0.1", receiver.port, "", 10)
    try:
        status, _ = direct._call(conn, "POST", "/api/offer", blob, {"Content-Type": "application/json"})
    finally:
        conn.close()
    assert status in (200, 400, 403)
