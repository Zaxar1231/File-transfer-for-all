"""HTTP and HTTPS: GET to download, PUT to upload, HEAD for metadata."""

from __future__ import annotations

import base64
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from http.client import HTTPResponse
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit, urlunsplit

from fta.core.errors import NotFound, TransferError
from fta.core.registry import register
from fta.core.transport import Progress, ProgressCallback, RemoteEntry, Transport, copy_stream, open_for_resume


@register
class HttpTransport(Transport):
    """Uploads need a server that accepts PUT. Options: username, password, timeout."""

    schemes = ("http", "https")
    can_resume_download = True  # Range requests; servers that ignore Range get a full restart

    @property
    def http_scheme(self) -> str:
        return self.endpoint.scheme

    def connect(self) -> None:
        """HTTP is stateless, so there is no session to open."""

    def close(self) -> None:
        """Nothing to release."""

    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None, offset: int = 0) -> None:
        headers = {"Range": f"bytes={offset}-"} if offset else None
        with self._send(remote, headers=headers) as response:
            if offset and response.status != 206:
                offset = 0  # the server ignored Range and sends the whole file
            length = _int_header(response, "Content-Length")
            with open_for_resume(local, offset) as out:
                copy_stream(response, out, Progress(progress, offset + length if length is not None else None, offset))

    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None, offset: int = 0) -> None:
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
        open_url = urllib.request.urlopen
        if proxy_url := self.options.get("proxy"):
            from fta.core.proxy import opener

            open_url = opener(proxy_url).open
        try:
            return open_url(request, timeout=self.float_option("timeout", 30))
        except urllib.error.HTTPError as exc:
            exc.close()
            location = urlsplit(urllib.parse.urljoin(url, exc.headers.get("Location", ""))).path
            if exc.code in (301, 308) and not (remote or "/").endswith("/") and location == quote(remote) + "/":
                # Apache and nginx answer a folder path without "/" with a redirect, which urllib
                # follows only for GET; PROPFIND and friends are simply sent again to the folder.
                return self._send(remote + "/", method=method, data=data, headers=headers)
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
