"""HTTP and HTTPS: GET to download, PUT to upload."""

from __future__ import annotations

import base64
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlunsplit

from fta.core.registry import register
from fta.core.transport import CHUNK_SIZE, Progress, ProgressCallback, Transport


@register
class HttpTransport(Transport):
    """Uploads need a server that accepts PUT."""

    schemes = ("http", "https")

    def connect(self) -> None:
        """HTTP is stateless, so there is no session to open."""

    def close(self) -> None:
        """Nothing to release."""

    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None) -> None:
        with urllib.request.urlopen(self._request(remote)) as response, local.open("wb") as out:
            tracker = Progress(progress, _content_length(response))
            for chunk in iter(lambda: response.read(CHUNK_SIZE), b""):
                out.write(chunk)
                tracker.advance(len(chunk))

    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None) -> None:
        size = local.stat().st_size
        tracker = Progress(progress, size)
        with local.open("rb") as fp:
            request = self._request(
                remote,
                method="PUT",
                data=tracker.wrap_reader(fp),
                extra_headers={"Content-Length": str(size)},
            )
            with urllib.request.urlopen(request):
                pass

    def _request(
        self,
        remote: str,
        *,
        method: str = "GET",
        data: object = None,
        extra_headers: dict[str, str] | None = None,
    ) -> urllib.request.Request:
        ep = self.endpoint
        url = urlunsplit((ep.scheme, ep.netloc, quote(remote), ep.query, ""))
        headers = dict(extra_headers or {})
        if ep.username:
            credentials = f"{ep.username}:{ep.password or ''}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
        return urllib.request.Request(url, data=data, headers=headers, method=method)


def _content_length(response: object) -> int | None:
    value = response.headers.get("Content-Length")  # type: ignore[attr-defined]
    return int(value) if value and value.isdigit() else None
