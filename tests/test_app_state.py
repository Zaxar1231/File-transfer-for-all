"""The app's UI-independent state: saved connections and the transfer queue."""

import json
import os
import sys
import time

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from fta.app.storage import Connection, Store
from fta.app.transfers import JobState, TransferJob, TransferQueue


def test_connection_url_hides_default_port_and_quotes_user():
    assert Connection("a", "sftp", "nas", port=22, username="me").url("/x") == "sftp://me@nas/x"
    assert Connection("a", "ftp", "h", port=2121, username="a@b").url("/my dir") == "ftp://a%40b@h:2121/my%20dir"
    assert Connection("a", "s3", "bucket").url() == "s3://bucket/"


def test_password_goes_to_options_not_url():
    connection = Connection("a", "sftp", "nas", username="me", password="secret")
    assert "secret" not in connection.url()
    assert connection.transport_options()["password"] == "secret"


def test_password_is_saved_only_on_request(tmp_path):
    path = tmp_path / "config.toml"
    store = Store(path, environ={})
    store.upsert(Connection("kept", "ftp", "h", username="u", password="p1", save_password=True))
    store.upsert(Connection("dropped", "ftp", "h", username="u", password="p2"))

    saved = tomllib.loads(path.read_text())
    assert [c["password"] for c in saved["connections"]] == ["p1", ""]
    if sys.platform != "win32":
        assert os.stat(path).st_mode & 0o777 == 0o600

    reloaded = Store(path, environ={})
    assert [c.name for c in reloaded.connections] == ["kept", "dropped"]
    assert reloaded.connections[0].password == "p1"
    assert reloaded.connections[0].port is None  # TOML has no null; the default comes back


def test_store_survives_unknown_and_broken_files(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[settings\nbroken")
    assert Store(path, environ={}).connections == []

    path.write_text('[settings]\ntheme_mode = "dark"\nfrom_the_future = 1\n')
    assert Store(path, environ={}).settings.theme_mode == "dark"


def test_environment_overrides_are_not_written_back(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[settings]\nparallel_transfers = 2\nverify = false\n")
    store = Store(path, environ={"FTA_PARALLEL_TRANSFERS": "6", "FTA_VERIFY": "yes", "FTA_RETRIES": "oops"})

    assert store.settings.parallel_transfers == 6
    assert store.settings.verify is True
    assert store.settings.retries == 3  # an unparsable value is ignored

    store.settings.theme_mode = "dark"
    store.save()
    saved = tomllib.loads(path.read_text())["settings"]
    assert saved["parallel_transfers"] == 2 and saved["verify"] is False
    assert saved["theme_mode"] == "dark"


def test_config_path_and_data_dir_from_environment(tmp_path):
    store = Store(environ={"FTA_CONFIG": str(tmp_path / "custom.toml")})
    assert store.path == tmp_path / "custom.toml"
    assert store.data_dir == tmp_path


def test_json_config_from_older_versions_is_migrated(tmp_path):
    legacy = tmp_path / "config.json"
    legacy.write_text(json.dumps({"settings": {"seed_color": "#006A6A"}, "connections": [{"name": "nas", "scheme": "sftp", "host": "nas.local", "id": "x"}]}))

    store = Store(tmp_path / "config.toml", environ={})

    assert store.settings.seed_color == "#006A6A"
    assert [c.name for c in store.connections] == ["nas"]
    assert (tmp_path / "config.toml").exists()
    assert not legacy.exists() and (tmp_path / "config.json.migrated").exists()


def test_upsert_replaces_by_id_and_remove(tmp_path):
    store = Store(tmp_path / "config.toml", environ={})
    connection = Connection("a", "ftp", "h")
    store.upsert(connection)
    store.upsert(Connection("renamed", "ftp", "h", id=connection.id))
    assert [c.name for c in store.connections] == ["renamed"]
    store.remove(connection.id)
    assert store.connections == []


@pytest.fixture
def queue():
    return TransferQueue(parallel=2, update_interval=0)


def make_job(source, destination, **kwargs):
    return TransferJob(
        name=os.path.basename(str(source)),
        source=str(source),
        destination=str(destination),
        source_url=str(source),
        destination_url=str(destination),
        **kwargs,
    )


def test_job_copies_and_reports_states(queue, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 200_000)
    out = tmp_path / "out"
    out.mkdir()
    states = []
    queue.subscribe(lambda job: states.append(job.state))

    job = queue.add(make_job(src, out))
    assert queue.wait_idle()

    assert job.state is JobState.DONE
    assert job.fraction == 1.0
    assert (out / "a.bin").read_bytes() == src.read_bytes()
    assert states[0] is JobState.QUEUED and JobState.RUNNING in states and states[-1] is JobState.DONE


def test_recursive_job_counts_files(queue, tmp_path):
    src = tmp_path / "tree"
    (src / "sub").mkdir(parents=True)
    for name in ("a", "b", "sub/c"):
        (src / name).write_bytes(b"1")
    out = tmp_path / "out"
    out.mkdir()

    job = queue.add(make_job(src, out, recursive=True))
    assert queue.wait_idle()

    assert job.state is JobState.DONE
    assert job.files_done == 3


def test_failure_is_kept_on_the_job(queue, tmp_path):
    job = queue.add(make_job(tmp_path / "missing.bin", tmp_path))
    assert queue.wait_idle()

    assert job.state is JobState.FAILED
    assert job.error


def test_cancel_stops_a_running_transfer(queue, tmp_path):
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 4_000_000)
    out = tmp_path / "out.bin"

    def cancel_on_first_bytes(job):
        if job.state is JobState.RUNNING and job.bytes_done:
            queue.cancel(job)

    queue.subscribe(cancel_on_first_bytes)
    job = queue.add(make_job(src, out))
    assert queue.wait_idle()

    assert job.state is JobState.CANCELLED
    assert out.stat().st_size < src.stat().st_size


def test_progress_burst_then_stall_still_reaches_listeners(tmp_path):
    """An upload can push every byte into socket buffers at once and then wait for the
    server; throttled updates must still deliver the final byte count before DONE."""
    from fta import Transport, register

    class BurstTransport(Transport):
        schemes = ("burst",)

        def connect(self):
            pass

        def close(self):
            pass

        def upload(self, local, remote, progress=None):
            for done in range(1, 11):
                progress(done * 100, 1000)
            time.sleep(0.6)  # the server is still busy

    register(BurstTransport)
    src = tmp_path / "a.bin"
    src.write_bytes(b"x")
    queue = TransferQueue(update_interval=0.15)
    seen_while_running = []
    queue.subscribe(lambda job: job.state is JobState.RUNNING and seen_while_running.append(job.bytes_done))

    job = queue.add(make_job(src, "burst://host/a.bin"))
    assert queue.wait_idle()

    assert job.state is JobState.DONE
    assert seen_while_running[-1] == 1000


def test_retry_queues_a_fresh_job(queue, tmp_path):
    failed = queue.add(make_job(tmp_path / "missing.bin", tmp_path))
    assert queue.wait_idle()
    (tmp_path / "missing.bin").write_bytes(b"now here")
    out = tmp_path / "out"
    out.mkdir()
    failed.destination_url = str(out)

    retried = queue.retry(failed)
    assert queue.wait_idle()

    assert retried is not failed and retried.state is JobState.DONE
    assert (out / "missing.bin").read_bytes() == b"now here"


def test_clear_finished_keeps_nothing_done(queue, tmp_path):
    src = tmp_path / "a"
    src.write_bytes(b"1")
    queue.add(make_job(src, tmp_path / "b"))
    assert queue.wait_idle()
    queue.clear_finished()
    assert queue.jobs == []


def test_app_modules_import():
    """The UI modules at least import and wire up against the installed Flet."""
    pytest.importorskip("flet")
    import fta.app.main  # noqa: F401
