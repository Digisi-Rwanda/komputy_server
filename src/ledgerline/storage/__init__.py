"""Async S3/MinIO object storage client."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, BinaryIO

from aiobotocore.session import AioSession, get_session
from fastapi import status

from ledgerline.config import Settings, get_settings

if TYPE_CHECKING:
    from ledgerline.kernel.errors import ECLException

CHUNK_SIZE = 65536  # 64 KB

_session: AioSession | None = None
_client: Any = None
_client_cm: Any = None


class _HashingReader:
    """Sync file wrapper that computes SHA-256 while streaming."""

    def __init__(self, source: BinaryIO) -> None:
        self._source = source
        self._hasher = hashlib.sha256()
        self.bytes_written = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = CHUNK_SIZE
        chunk = self._source.read(size)
        if chunk:
            self._hasher.update(chunk)
            self.bytes_written += len(chunk)
        return chunk

    @property
    def hex_digest(self) -> str:
        return self._hasher.hexdigest()


def _client_kwargs(settings: Settings) -> dict[str, str | None]:
    kwargs: dict[str, str | None] = {
        "service_name": "s3",
        "aws_access_key_id": settings.storage_access_key,
        "aws_secret_access_key": settings.storage_secret_key,
        "region_name": settings.storage_region,
    }
    if settings.storage_endpoint_url:
        kwargs["endpoint_url"] = settings.storage_endpoint_url
    return kwargs


def _aio_config() -> Any:
    from aiobotocore.config import AioConfig

    # Path-style + SigV4 works for both MinIO (dev) and Cloudflare R2 (prod).
    return AioConfig(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 5, "mode": "adaptive"})


async def init_storage() -> None:
    """Initialize the shared aiobotocore S3 client."""
    global _session, _client, _client_cm
    if _client is not None:
        return

    settings = get_settings()
    _session = get_session()
    _client_cm = _session.create_client(**_client_kwargs(settings), config=_aio_config())
    _client = await _client_cm.__aenter__()


async def close_storage() -> None:
    """Close the shared S3 client."""
    global _session, _client, _client_cm
    if _client_cm is not None:
        await _client_cm.__aexit__(None, None, None)
    _session = None
    _client = None
    _client_cm = None


async def get_storage_client() -> Any:
    """Return the singleton S3 client, initializing on first use."""
    if _client is None:
        await init_storage()
    return _client


def build_storage_path(
    tenant_id: str,
    run_id: str,
    subfolder: str,
    filename: str,
) -> str:
    """Build a tenant-scoped object key."""
    return f"/{tenant_id}/runs/{run_id}/{subfolder}/{filename}"


def _normalize_key(key: str) -> str:
    return key.lstrip("/")


def _storage_error(exc: Exception) -> ECLException:
    from ledgerline.kernel.errors import ECLException
    return ECLException(
        "STORAGE_UNAVAILABLE",
        "File storage is unavailable. Please try again or contact support.",
        status.HTTP_503_SERVICE_UNAVAILABLE,
    )


async def upload_stream(
    key: str,
    file_obj: BinaryIO | Any,
    content_type: str,
) -> tuple[str, int]:
    """
    Read *file_obj* into memory, compute SHA-256, upload to object storage.

    Returns ``(hex_digest, bytes_written)``.
    """
    settings = get_settings()
    client = await get_storage_client()

    # Read fully into memory — botocore checksum code requires seekable body;
    # reading upfront avoids that constraint and lets us hash in one pass.
    if hasattr(file_obj, "read"):
        raw: bytes = file_obj.read()
    else:
        raw = bytes(file_obj)

    sha256 = hashlib.sha256(raw).hexdigest()
    size = len(raw)

    try:
        await client.put_object(
            Bucket=settings.storage_bucket_name,
            Key=_normalize_key(key),
            Body=raw,
            ContentType=content_type,
        )
    except Exception as exc:
        raise _storage_error(exc) from exc
    return sha256, size


async def download_bytes(key: str) -> bytes:
    """Download an object and return its full contents."""
    settings = get_settings()
    client = await get_storage_client()
    try:
        response = await client.get_object(
            Bucket=settings.storage_bucket_name,
            Key=_normalize_key(key),
        )
        async with response["Body"] as body:
            return await body.read()
    except Exception as exc:
        raise _storage_error(exc) from exc


async def presign_download(key: str, expires_seconds: int = 900) -> str:
    """Return a time-limited presigned GET URL for *key*."""
    settings = get_settings()
    client = await get_storage_client()
    presign: Callable[..., Any] = client.generate_presigned_url
    try:
        url = presign(
            "get_object",
            Params={
                "Bucket": settings.storage_bucket_name,
                "Key": _normalize_key(key),
            },
            ExpiresIn=expires_seconds,
        )
        if inspect.isawaitable(url):
            return await url
        return url
    except Exception as exc:
        raise _storage_error(exc) from exc


async def delete_object(key: str) -> None:
    """Delete an object from storage."""
    settings = get_settings()
    client = await get_storage_client()
    try:
        await client.delete_object(
            Bucket=settings.storage_bucket_name,
            Key=_normalize_key(key),
        )
    except Exception as exc:
        raise _storage_error(exc) from exc


# ─── Direct-to-storage uploads (browser → R2, never through our processes) ─────────────

_presign_cm: Any = None
_presign_client: Any = None


async def _get_presign_client() -> Any:
    """Presigned URLs must be signed for the host the *browser* uses (public endpoint), not the internal one."""
    global _presign_cm, _presign_client
    s = get_settings()
    if not s.storage_public_endpoint_url or s.storage_public_endpoint_url == s.storage_endpoint_url:
        return await get_storage_client()
    if _presign_client is None:
        kwargs = {**_client_kwargs(s), "endpoint_url": s.storage_public_endpoint_url}
        _presign_cm = get_session().create_client(**kwargs, config=_aio_config())
        _presign_client = await _presign_cm.__aenter__()
    return _presign_client


async def _maybe_await(v: Any) -> Any:
    return await v if inspect.isawaitable(v) else v


async def create_multipart(key: str, content_type: str) -> str:
    s, client = get_settings(), await get_storage_client()
    try:
        r = await client.create_multipart_upload(Bucket=s.storage_bucket_name, Key=_normalize_key(key), ContentType=content_type)
    except Exception as exc:
        raise _storage_error(exc) from exc
    return str(r["UploadId"])


async def presign_parts(key: str, upload_id: str, parts: int) -> list[str]:
    s, client = get_settings(), await _get_presign_client()
    return [
        await _maybe_await(client.generate_presigned_url(
            "upload_part",
            Params={"Bucket": s.storage_bucket_name, "Key": _normalize_key(key), "UploadId": upload_id, "PartNumber": n},
            ExpiresIn=s.presign_ttl_seconds,
        ))
        for n in range(1, parts + 1)
    ]


async def complete_multipart(key: str, upload_id: str, parts: list[dict[str, Any]]) -> None:
    s, client = get_settings(), await get_storage_client()
    try:
        await client.complete_multipart_upload(
            Bucket=s.storage_bucket_name, Key=_normalize_key(key), UploadId=upload_id,
            MultipartUpload={"Parts": [{"ETag": p["etag"], "PartNumber": int(p["partNumber"])} for p in sorted(parts, key=lambda p: int(p["partNumber"]))]},
        )
    except Exception as exc:
        from ledgerline.kernel.errors import ECLException

        raise ECLException("UPLOAD_INCOMPLETE", "The upload didn't finish. Upload the file again.", 400) from exc


async def abort_multipart(key: str, upload_id: str) -> None:
    s, client = get_settings(), await get_storage_client()
    try:
        await client.abort_multipart_upload(Bucket=s.storage_bucket_name, Key=_normalize_key(key), UploadId=upload_id)
    except Exception:  # noqa: S110  (already gone: nothing to clean)
        pass


async def presign_put(key: str, content_type: str, expires_seconds: int | None = None) -> str:
    s, client = get_settings(), await _get_presign_client()
    return await _maybe_await(client.generate_presigned_url(
        "put_object", Params={"Bucket": s.storage_bucket_name, "Key": _normalize_key(key), "ContentType": content_type},
        ExpiresIn=expires_seconds or s.presign_ttl_seconds,
    ))


async def presign_get(key: str, filename: str | None = None, expires_seconds: int | None = None) -> str:
    s, client = get_settings(), await _get_presign_client()
    params: dict[str, Any] = {"Bucket": s.storage_bucket_name, "Key": _normalize_key(key)}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    return await _maybe_await(client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires_seconds or s.presign_ttl_seconds))


async def head(key: str) -> dict[str, Any] | None:
    s, client = get_settings(), await get_storage_client()
    try:
        r = await client.head_object(Bucket=s.storage_bucket_name, Key=_normalize_key(key))
    except Exception:
        return None
    return {"size": int(r["ContentLength"]), "content_type": r.get("ContentType"), "etag": r.get("ETag")}


async def sha256_of(key: str) -> tuple[str, int]:
    """Stream an object and hash it (no full copy in memory)."""
    s, client = get_settings(), await get_storage_client()
    h, n = hashlib.sha256(), 0
    try:
        r = await client.get_object(Bucket=s.storage_bucket_name, Key=_normalize_key(key))
        async with r["Body"] as body:
            while chunk := await body.read(1024 * 1024):
                h.update(chunk)
                n += len(chunk)
    except Exception as exc:
        raise _storage_error(exc) from exc
    return h.hexdigest(), n


async def upload_bytes(key: str, data: bytes, content_type: str) -> tuple[str, int]:
    return await upload_stream(key, data, content_type)


async def delete_prefix(prefix: str) -> int:
    """Delete every object under a prefix (tenant purge). Returns the count."""
    s, client = get_settings(), await get_storage_client()
    deleted, token = 0, None
    while True:
        kw: dict[str, Any] = {"Bucket": s.storage_bucket_name, "Prefix": _normalize_key(prefix)}
        if token:
            kw["ContinuationToken"] = token
        page = await client.list_objects_v2(**kw)
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            await client.delete_objects(Bucket=s.storage_bucket_name, Delete={"Objects": keys, "Quiet": True})
            deleted += len(keys)
        if not page.get("IsTruncated"):
            return deleted
        token = page.get("NextContinuationToken")


async def ensure_bucket() -> None:
    """Dev/test convenience: create the bucket if missing (R2 buckets are created by infra)."""
    s, client = get_settings(), await get_storage_client()
    try:
        await client.head_bucket(Bucket=s.storage_bucket_name)
    except Exception:
        await client.create_bucket(Bucket=s.storage_bucket_name)
