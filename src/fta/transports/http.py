"""HTTP and HTTPS: GET to download, PUT to upload, HEAD for metadata."""

from __future__ import annotations

import base64
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from http.client import HTTPResponse
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlunsplit

from fta.core.errors import NotFound, TransferError
from fta.core.registry import register
from fta.core.transport import Progress, ProgressCallback, RemoteEntry, Transport, copy_stream


@register
class HttpTransport(Transport):
    """Uploads need a server that accepts PUT. Options: username, password, timeout."""

    schemes = ("http", "https")

    @property
    def http_scheme(self) -> str:
        return self.endpoint.scheme

    def connect(self) -> None:
        """HTTP is stateless, so there is no session to open."""

    def close(self) -> None:
        """Nothing to release."""

    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None) -> None:
        with self._send(remote) as response, local.open("wb") as out:
            copy_stream(response, out, Progress(progress, _int_header(response, "Content-Length")))

    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None) -> None:
        size = local.stat().st_size
        with local.open("rb") as fp:
            data = Progress(progress, size).wrap_reader(fp)
            headers = {"Content-Length": str(size), "Content-Type": "application/octet-stream"}
            with self._send(remote, method="PUT", data=data, headers=headers):
                pass

    def stat(self, remote: str) -> RemoteEntry:
        with self._send(remote, method="HEAD") as response:
            return RemoteEntry(
                name=PurePosixPath(remote).name,
                path=remote,
                is_dir=False,
                size=_int_header(response, "Content-Length"),
                modified=parse_http_date(response.headers.get("Last-Modified")),
            )

    def delete(self, remote: str) -> None:
        with self._send(remote, method="DELETE"):
            pass

    def _send(
        self,
        remote: str,
        *,
        method: str = "GET",
        data: object = None,
        headers: dict[str, str] | None = None,
    ) -> HTTPResponse:
        """Send one request and return the open response; HTTP errors become TransferError."""
        ep = self.endpoint
        url = urlunsplit((self.http_scheme, ep.netloc, quote(remote or "/"), ep.query, ""))
        all_headers = dict(headers or {})
        if self.username:
            credentials = f"{self.username}:{self.password or ''}".encode()
            all_headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
        request = urllib.request.Request(url, data=data, headers=all_headers, method=method)
        try:
            return urllib.request.urlopen(request, timeout=self.float_option("timeout", 30))
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404:
                raise NotFound(f"{method} {url}: 404 Not Found") from exc
            raise TransferError(f"{method} {url}: HTTP {exc.code} {exc.reason}") from exc


def parse_http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None


def _int_header(response: HTTPResponse, name: str) -> int | None:
    value = response.headers.get(name)
    return int(value) if value and value.isdigit() else None
