"""MinIO object storage helpers."""

from __future__ import annotations

import io
import os
from datetime import timedelta
from collections.abc import Iterator
from typing import Any

from server.app.common_utils import env_bool

MAX_PRESIGN_EXPIRES_SEC = 7 * 24 * 60 * 60


def _client(endpoint: str | None = None, secure: bool | None = None) -> Any:
    try:
        from minio import Minio
    except ImportError as exc:
        raise RuntimeError("missing minio dependency; install project dependencies first") from exc

    endpoint, inferred_secure = _normalize_endpoint(
        endpoint or os.getenv("MINIO_ENDPOINT", "minio:9000")
    )
    return Minio(
        endpoint=endpoint,
        access_key=os.getenv("MINIO_ACCESS_KEY", ""),
        secret_key=os.getenv("MINIO_SECRET_KEY", ""),
        secure=inferred_secure if secure is None else secure,
        region=os.getenv("MINIO_REGION", "us-east-1"),
    )


def _presign_client() -> Any:
    public_endpoint = os.getenv("MINIO_PUBLIC_ENDPOINT", "").strip()
    if not public_endpoint:
        return _client()

    endpoint, inferred_secure = _normalize_endpoint(public_endpoint)
    secure = env_bool("MINIO_PUBLIC_SECURE", inferred_secure)
    return _client(endpoint=endpoint, secure=secure)


def _normalize_endpoint(endpoint: str) -> tuple[str, bool]:
    endpoint = endpoint.strip()
    if endpoint.startswith("https://"):
        return endpoint.removeprefix("https://"), True
    if endpoint.startswith("http://"):
        return endpoint.removeprefix("http://"), False
    return endpoint, False


def ensure_bucket(bucket: str) -> None:
    if not bucket:
        raise ValueError("bucket must not be empty")
    client = _client()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def upload_file(
    local_path: str,
    bucket: str,
    object_key: str,
    content_type: str = "application/octet-stream",
) -> int:
    if not bucket:
        raise ValueError("bucket must not be empty")
    if not object_key:
        raise ValueError("object_key must not be empty")
    client = _client()
    size = os.path.getsize(local_path)
    client.fput_object(
        bucket_name=bucket,
        object_name=object_key,
        file_path=local_path,
        content_type=content_type,
    )
    return size


def upload_bytes(
    payload: bytes,
    bucket: str,
    object_key: str,
    content_type: str = "application/octet-stream",
) -> int:
    """Upload an in-memory evidence payload and return its byte size."""
    if not bucket:
        raise ValueError("bucket must not be empty")
    if not object_key:
        raise ValueError("object_key must not be empty")
    client = _client()
    client.put_object(
        bucket_name=bucket,
        object_name=object_key,
        data=io.BytesIO(payload),
        length=len(payload),
        content_type=content_type,
    )
    return len(payload)


def read_object_bytes(bucket: str, object_key: str) -> bytes:
    if not bucket:
        raise ValueError("bucket must not be empty")
    if not object_key:
        raise ValueError("object_key must not be empty")
    response = _client().get_object(bucket, object_key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def stream_object(bucket: str, object_key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """流式读取对象，并在客户端中断或读取完成后释放 MinIO 连接。"""
    if not bucket:
        raise ValueError("bucket must not be empty")
    if not object_key:
        raise ValueError("object_key must not be empty")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    response = _client().get_object(bucket, object_key)
    try:
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        response.close()
        response.release_conn()


def presigned_get_url(bucket: str, object_key: str, expires: int = 3600) -> str:
    if not bucket:
        raise ValueError("bucket must not be empty")
    if not object_key:
        raise ValueError("object_key must not be empty")
    if expires <= 0 or expires > MAX_PRESIGN_EXPIRES_SEC:
        raise ValueError("expires must be between 1 second and 7 days")
    client = _presign_client()
    return client.presigned_get_object(
        bucket_name=bucket,
        object_name=object_key,
        expires=timedelta(seconds=expires),
    )
