import pytest

from fta import Transport, copy, register


class MemoryTransport(Transport):
    """In-memory protocol that exercises the core without a network."""

    schemes = ("mem",)
    store: dict[tuple[str, str], bytes] = {}

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def upload(self, local, remote, progress=None):
        data = local.read_bytes()
        self.store[(self.endpoint.host or "", remote)] = data
        if progress:
            progress(len(data), len(data))

    def download(self, remote, local, progress=None):
        data = self.store[(self.endpoint.host or "", remote)]
        local.write_bytes(data)
        if progress:
            progress(len(data), len(data))


@pytest.fixture(autouse=True)
def memory_protocol():
    register(MemoryTransport)
    MemoryTransport.store.clear()
    yield


def test_local_to_remote_and_back(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"hello")
    out = tmp_path / "out"
    out.mkdir()

    copy(str(src), "mem://host/docs/a.txt")
    copy("mem://host/docs/a.txt", str(out))

    assert (out / "a.txt").read_bytes() == b"hello"


def test_remote_to_remote_is_staged(tmp_path):
    src = tmp_path / "b.bin"
    src.write_bytes(b"data")

    copy(str(src), "mem://one/x/b.bin")
    copy("mem://one/x/b.bin", "mem://two/y/b.bin")

    assert MemoryTransport.store[("two", "/y/b.bin")] == b"data"


def test_progress_reports_totals(tmp_path):
    src = tmp_path / "c.bin"
    src.write_bytes(b"1234")
    events = []

    copy(str(src), "mem://h/c.bin", progress=lambda done, total: events.append((done, total)))

    assert events == [(4, 4)]


def test_unknown_scheme_is_rejected(tmp_path):
    from fta import UnsupportedScheme

    src = tmp_path / "d.bin"
    src.write_bytes(b"x")

    with pytest.raises(UnsupportedScheme):
        copy(str(src), "nope://host/d.bin")
