"""WebDAV (RFC 4918) on top of the HTTP transport: dav:// uses http, davs:// uses https."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from fta.core.errors import NotFound, TransferError
from fta.core.registry import register
from fta.core.transport import RemoteEntry
from fta.transports.http import HttpTransport, parse_http_date

DAV = "{DAV:}"
PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<propfind xmlns="DAV:"><prop>'
    b"<resourcetype/><getcontentlength/><getlastmodified/>"
    b"</prop></propfind>"
)


@register
class WebDavTransport(HttpTransport):
    schemes = ("dav", "davs")

    @property
    def http_scheme(self) -> str:
        return "https" if self.endpoint.scheme == "davs" else "http"

    def listdir(self, remote: str) -> list[RemoteEntry]:
        own_path = _normalize(remote)
        return [entry for entry in self._propfind(remote, depth="1") if _normalize(entry.path) != own_path]

    def stat(self, remote: str) -> RemoteEntry:
        entries = self._propfind(remote, depth="0")
        if not entries:
            raise NotFound(f"{remote}: no such file or directory")
        return entries[0]

    def mkdir(self, remote: str, exist_ok: bool = False) -> None:
        try:
            with self._send(remote, method="MKCOL"):
                pass
        except TransferError:
            if exist_ok and self._is_dir(remote):
                return
            raise

    def _is_dir(self, remote: str) -> bool:
        try:
            return self.stat(remote).is_dir
        except TransferError:
            return False

    def _propfind(self, remote: str, depth: str) -> list[RemoteEntry]:
        headers = {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}
        with self._send(remote, method="PROPFIND", data=PROPFIND_BODY, headers=headers) as response:
            root = ET.parse(response).getroot()
        return [_parse_response(node) for node in root.iter(f"{DAV}response")]


def _parse_response(node: ET.Element) -> RemoteEntry:
    path = _normalize(urlsplit(node.findtext(f"{DAV}href", "")).path)
    is_dir = node.find(f".//{DAV}resourcetype/{DAV}collection") is not None
    size = node.findtext(f".//{DAV}getcontentlength")
    return RemoteEntry(
        name=PurePosixPath(path).name,
        path=path,
        is_dir=is_dir,
        size=int(size) if size and size.isdigit() and not is_dir else None,
        modified=parse_http_date(node.findtext(f".//{DAV}getlastmodified")),
    )


def _normalize(path: str) -> str:
    return unquote(path).rstrip("/") or "/"
