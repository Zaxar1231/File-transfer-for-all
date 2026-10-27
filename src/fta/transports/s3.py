"""Amazon S3 and S3-compatible storage (MinIO, Ceph, Cloudflare R2, ...) via boto3.

URLs look like ``s3://bucket/key``. Credentials come from the usual AWS chain
(environment, ~/.aws, instance role) unless given as options.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from fta.core.errors import NotFound, TransferError
from fta.core.registry import register
from fta.core.transport import Progress, ProgressCallback, RemoteEntry, Transport, translate_errors

_MISSING_CODES = ("404", "NoSuchKey", "NotFound")


@register
class S3Transport(Transport):
    """Options: endpoint_url, region, profile, access_key, secret_key.

    S3 has no real directories: a "directory" is a key prefix, ``mkdir`` does
    nothing, and empty directories are not copied.
    """

    schemes = ("s3",)

    _client: Any = None
    _client_error: Any = None

    def connect(self) -> None:
        try:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError:
            raise TransferError('s3:// needs boto3: pip install "fta[s3]"') from None
        if not self.endpoint.host:
            raise TransferError("s3 URLs need a bucket: s3://bucket/key")
        self.library_errors = (BotoCoreError, ClientError)
        self._client_error = ClientError
        session = boto3.session.Session(
            profile_name=self.options.get("profile"),
            region_name=self.options.get("region"),
        )
        self._client = session.client(
            "s3",
            endpoint_url=self.options.get("endpoint_url"),
            aws_access_key_id=self.options.get("access_key") or self.username,
            aws_secret_access_key=self.options.get("secret_key") or self.password,
        )

    def close(self) -> None:
        self._client = None

    @property
    def bucket(self) -> str:
        return self.endpoint.host or ""

    @translate_errors
    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None) -> None:
        tracker = Progress(progress, local.stat().st_size)
        self._session.upload_file(str(local), self.bucket, _key(remote), Callback=tracker.advance)

    @translate_errors
    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None) -> None:
        tracker = Progress(progress, self.stat(remote).size)
        self._session.download_file(self.bucket, _key(remote), str(local), Callback=tracker.advance)

    @translate_errors
    def listdir(self, remote: str) -> list[RemoteEntry]:
        prefix = _prefix(remote)
        paginator = self._session.get_paginator("list_objects_v2")
        entries = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
            for common in page.get("CommonPrefixes", []):
                key = common["Prefix"].rstrip("/")
                entries.append(RemoteEntry(name=PurePosixPath(key).name, path=f"/{key}", is_dir=True))
            for obj in page.get("Contents", []):
                if obj["Key"] == prefix:  # placeholder object some tools create for folders
                    continue
                entries.append(
                    RemoteEntry(
                        name=PurePosixPath(obj["Key"]).name,
                        path=f"/{obj['Key']}",
                        is_dir=False,
                        size=obj["Size"],
                        modified=obj["LastModified"].timestamp(),
                    )
                )
        return entries

    @translate_errors
    def stat(self, remote: str) -> RemoteEntry:
        key = _key(remote)
        name = PurePosixPath(key).name
        if key:
            try:
                head = self._session.head_object(Bucket=self.bucket, Key=key)
                return RemoteEntry(
                    name=name,
                    path=remote,
                    is_dir=False,
                    size=head["ContentLength"],
                    modified=head["LastModified"].timestamp(),
                )
            except self._client_error as exc:
                if exc.response.get("Error", {}).get("Code") not in _MISSING_CODES:
                    raise
        listing = self._session.list_objects_v2(Bucket=self.bucket, Prefix=_prefix(remote), MaxKeys=1)
        if not key or listing.get("KeyCount", 0):
            return RemoteEntry(name=name, path=remote, is_dir=True)
        raise NotFound(f"s3://{self.bucket}/{key}: no such key")

    def mkdir(self, remote: str, exist_ok: bool = False) -> None:
        """Prefixes appear on their own once objects are stored under them."""

    @translate_errors
    def delete(self, remote: str) -> None:
        self._session.delete_object(Bucket=self.bucket, Key=_key(remote))

    @property
    def _session(self) -> Any:
        if self._client is None:
            raise RuntimeError("S3 client is not open; use 'with' or call connect()")
        return self._client


def _key(remote: str) -> str:
    return remote.lstrip("/")


def _prefix(remote: str) -> str:
    key = _key(remote).rstrip("/")
    return f"{key}/" if key else ""
