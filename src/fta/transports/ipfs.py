"""IPFS: ``ipfs://<CID>/path`` — content-addressed files, downloaded from any gateway.

Reading uses the local IPFS node (Kubo) when one runs, which also verifies every
block against its hash; otherwise an HTTP gateway (``gateway`` option, default
ipfs.io). Downloads resume with Range requests. Publishing a file needs a local
node: ``add()`` returns its CID, ``fta ipfs add`` and the Files menu use it.
"""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode

from fta.core.errors import NotFound, TransferError, UnsupportedOperation
from fta.core.registry import register
from fta.core.transport import Progress, ProgressCallback, RemoteEntry, Transport, copy_stream, open_for_resume

DEFAULT_GATEWAY = "https://ipfs.io"
DEFAULT_API = "http://127.0.0.1:5001"


def api_url(options: dict[str, Any] | None = None) -> str:
    return ((options or {}).get("api") or os.environ.get("FTA_IPFS_API") or DEFAULT_API).rstrip("/")


def _api(base: str, command: str, timeout: float = 30, **params: str) -> Any:
    request = urllib.request.Request(f"{base}/api/v0/{command}?{urlencode(params)}", method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the user's own node
        return json.loads(response.read() or b"{}")


def node_available(base: str) -> bool:
    try:
        _api(base, "version", timeout=2)
        return True
    except (OSError, ValueError):
        return False


def add(path: Path, options: dict[str, Any] | None = None, progress: ProgressCallback | None = None) -> str:
    """Add a file to the local node, pinned; returns its CID."""
    base = api_url(options)
    boundary = uuid.uuid4().hex
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{quote(path.name)}\"\r\n"
        f"Content-Type: {mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    size = path.stat().st_size

    class Body:
        def __init__(self) -> None:
            self.parts = [head, None, tail]
            self.fp = path.open("rb")
            self.tracker = Progress(progress, size)

        def read(self, amount: int = -1) -> bytes:
            while self.parts:
                part = self.parts[0]
                if part is None:
                    chunk = self.fp.read(amount if amount > 0 else 1 << 20)
                    if chunk:
                        self.tracker.advance(len(chunk))
                        return chunk
                    self.fp.close()
                    self.parts.pop(0)
                    continue
                self.parts.pop(0)
                return part
            return b""

    request = urllib.request.Request(
        f"{base}/api/v0/add?pin=true&cid-version=1",
        data=Body(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(head) + size + len(tail))},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:  # noqa: S310
            lines = [json.loads(line) for line in response.read().splitlines() if line.strip()]
    except OSError as exc:
        raise TransferError(f"no IPFS node at {base}: start IPFS Desktop or `ipfs daemon` ({exc})") from exc
    return lines[-1]["Hash"]


@register
class IpfsTransport(Transport):
    """Options: gateway (an HTTP gateway URL), api (a local Kubo node), timeout."""

    schemes = ("ipfs",)
    can_resume_download = True

    def connect(self) -> None:
        self.cid = self.endpoint.host_as_written or self.endpoint.host or ""
        if not self.cid:
            raise TransferError("ipfs:// needs a CID, e.g. ipfs://bafy…/file.txt")
        self.api = api_url(self.options)
        self.local = node_available(self.api)
        self.gateway = (self.options.get("gateway") or os.environ.get("FTA_IPFS_GATEWAY") or DEFAULT_GATEWAY).rstrip("/")

    def close(self) -> None:
        pass

    def _ipfs_path(self, remote: str) -> str:
        return f"/ipfs/{self.cid}/{remote.strip('/')}".rstrip("/")

    def listdir(self, remote: str) -> list[RemoteEntry]:
        if not self.local:
            raise UnsupportedOperation("listing IPFS folders needs a local IPFS node (IPFS Desktop or `ipfs daemon`)")
        try:
            reply = _api(self.api, "ls", arg=self._ipfs_path(remote))
        except urllib.error.HTTPError as exc:
            raise NotFound(f"{self._ipfs_path(remote)}: {exc.reason}") from exc
        entries = []
        for item in reply.get("Objects", [{}])[0].get("Links", []):
            is_dir = item.get("Type") == 1
            entries.append(
                RemoteEntry(item["Name"], f"{remote.rstrip('/')}/{item['Name']}", is_dir, None if is_dir else item.get("Size"))
            )
        return entries

    def stat(self, remote: str) -> RemoteEntry:
        name = PurePosixPath(remote).name
        if self.local:
            try:
                info = _api(self.api, "files/stat", arg=self._ipfs_path(remote))
            except urllib.error.HTTPError as exc:
                raise NotFound(f"{self._ipfs_path(remote)}: {exc.reason}") from exc
            is_dir = info.get("Type") == "directory"
            return RemoteEntry(name, remote or "/", is_dir, None if is_dir else info.get("Size"))
        with self._get(remote, method="HEAD") as response:
            length = response.headers.get("Content-Length")
            is_dir = (response.headers.get("Content-Type") or "").startswith("text/html") and not name
            return RemoteEntry(name, remote or "/", is_dir, int(length) if length and not is_dir else None)

    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None, offset: int = 0) -> None:
        if self.local:
            params = {"arg": self._ipfs_path(remote)}
            if offset:
                params["offset"] = str(offset)
            size = self.stat(remote).size
            request = urllib.request.Request(f"{self.api}/api/v0/cat?{urlencode(params)}", method="POST")
            with urllib.request.urlopen(request, timeout=self.float_option("timeout", 60)) as response, open_for_resume(local, offset) as out:  # noqa: S310
                copy_stream(response, out, Progress(progress, size, offset))
            return
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with self._get(remote, headers=headers) as response:
            if offset and response.status != 206:
                offset = 0
            length = response.headers.get("Content-Length")
            total = offset + int(length) if length else None
            with open_for_resume(local, offset) as out:
                copy_stream(response, out, Progress(progress, total, offset))

    def _get(self, remote: str, method: str = "GET", headers: dict[str, str] | None = None) -> Any:
        url = self.gateway + quote(self._ipfs_path(remote))
        request = urllib.request.Request(url, headers=headers or {}, method=method)
        try:
            return urllib.request.urlopen(request, timeout=self.float_option("timeout", 60))  # noqa: S310
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404:
                raise NotFound(f"{url}: not found") from exc
            raise TransferError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
