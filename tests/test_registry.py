import threading
import time

import pytest

from fta import Transport, UnsupportedOperation, known_schemes, open_transport
from fta.core import registry


def test_concurrent_first_use_sees_every_transport(monkeypatch):
    """Two file panes list folders at start-up on separate threads; neither may see an empty registry."""

    class Slow(Transport):
        schemes = ("slow",)

        def connect(self):
            pass

        def close(self):
            pass

    def slow_import(name):
        time.sleep(0.05)
        registry.register(Slow)

    monkeypatch.setattr(registry, "_registry", {})
    monkeypatch.setattr(registry, "_loaded", False)
    monkeypatch.setattr(registry, "BUILTIN_MODULES", ("slow",))
    monkeypatch.setattr(registry, "import_module", slow_import)

    start = threading.Barrier(8)
    results = []

    def worker():
        start.wait()
        try:
            results.append(registry.transport_class("slow"))
        except Exception as exc:
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [Slow] * 8


def test_builtin_protocols_are_registered():
    expected = {"file", "http", "https", "dav", "davs", "ftp", "ftps", "sftp", "s3", "smb"}
    assert expected <= set(known_schemes())


def test_plain_paths_open_the_local_transport(tmp_path):
    with open_transport(str(tmp_path)) as transport:
        assert transport.stat(str(tmp_path)).is_dir


def test_planned_protocol_fails_clearly_on_connect():
    with pytest.raises(UnsupportedOperation, match="not implemented"):
        with open_transport("smb://host/share/file"):
            pass
