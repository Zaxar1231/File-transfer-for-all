import pytest

from fta import UnsupportedOperation, known_schemes, open_transport


def test_builtin_protocols_are_registered():
    assert {"http", "https", "ftp", "ftps", "sftp", "dav", "davs", "smb", "s3"} <= set(known_schemes())


def test_planned_protocol_fails_clearly_on_connect():
    with pytest.raises(UnsupportedOperation, match="not implemented"):
        with open_transport("sftp://host/file"):
            pass
