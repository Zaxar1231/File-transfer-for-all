import pytest

from fta import UnsupportedOperation, known_schemes, open_transport


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
