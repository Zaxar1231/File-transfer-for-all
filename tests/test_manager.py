import pytest

from fta import Transport, TransferError, UnsupportedScheme, copy, register


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

    def mkdir(self, remote, exist_ok=False):
        pass  # keys are flat, like S3


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


def test_trailing_slash_copies_into_directory(tmp_path):
    src = tmp_path / "c.txt"
    src.write_bytes(b"c")

    copy(str(src), "mem://h/inbox/")

    assert MemoryTransport.store[("h", "/inbox/c.txt")] == b"c"


def test_progress_reports_totals(tmp_path):
    src = tmp_path / "c.bin"
    src.write_bytes(b"1234")
    events = []

    copy(str(src), "mem://h/c.bin", progress=lambda done, total: events.append((done, total)))

    assert events == [(4, 4)]


def test_recursive_local_tree_to_remote(tmp_path):
    src = tmp_path / "tree"
    (src / "sub").mkdir(parents=True)
    (src / "top.txt").write_bytes(b"top")
    (src / "sub" / "deep.txt").write_bytes(b"deep")
    seen = []

    copy(str(src), "mem://h/backup/", recursive=True, on_file=seen.append)

    assert MemoryTransport.store == {
        ("h", "/backup/tree/top.txt"): b"top",
        ("h", "/backup/tree/sub/deep.txt"): b"deep",
    }
    assert len(seen) == 2


def test_recursive_local_to_local(tmp_path):
    src = tmp_path / "tree"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f.txt").write_bytes(b"f")
    dst = tmp_path / "dst"
    dst.mkdir()

    copy(str(src), str(dst), recursive=True)

    assert (dst / "tree" / "sub" / "f.txt").read_bytes() == b"f"


def test_copy_onto_itself_is_refused(tmp_path):
    src = tmp_path / "same.txt"
    src.write_bytes(b"keep me")

    with pytest.raises(TransferError):
        copy(str(src), str(src))
    assert src.read_bytes() == b"keep me"


def test_unknown_scheme_is_rejected(tmp_path):
    src = tmp_path / "d.bin"
    src.write_bytes(b"x")

    with pytest.raises(UnsupportedScheme):
        copy(str(src), "nope://host/d.bin")
