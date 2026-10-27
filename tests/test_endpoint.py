from fta import Endpoint


def test_remote_url_parts():
    ep = Endpoint.parse("ftp://alice:p%40ss@example.com:2121/pub/file.bin")

    assert ep.scheme == "ftp"
    assert ep.host == "example.com"
    assert ep.port == 2121
    assert ep.username == "alice"
    assert ep.password == "p@ss"
    assert ep.path == "/pub/file.bin"
    assert not ep.is_local


def test_query_and_netloc():
    ep = Endpoint.parse("https://example.com:8443/a.txt?token=abc")

    assert ep.query == "token=abc"
    assert ep.netloc == "example.com:8443"


def test_plain_and_file_paths_are_local():
    assert Endpoint.parse("relative/a.txt").is_local
    assert Endpoint.parse("/tmp/a.txt").is_local

    file_url = Endpoint.parse("file:///tmp/a.txt")
    assert file_url.is_local
    assert file_url.path == "/tmp/a.txt"
