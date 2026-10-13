"""S3-compatible Object Storage abstraction for Tool Factory source handoff.

PostgreSQL stores only safe object metadata. Raw OpenAPI bytes live here.
Broker delivery is never used as business state.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import Settings

logger = logging.getLogger(__name__)


class ObjectStorageError(Exception):
    """Bounded Object Storage failure (never embed credentials or full bodies)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class StoredObjectRef:
    bucket: str
    object_key: str
    content_type: str
    size_bytes: int
    sha256: str


class ObjectStorageClient(Protocol):
    async def put_bytes(
        self,
        *,
        bucket: str,
        object_key: str,
        data: bytes,
        content_type: str,
    ) -> None: ...

    async def get_bytes(self, *, bucket: str, object_key: str) -> bytes: ...

    async def delete_object(self, *, bucket: str, object_key: str) -> None: ...

    async def ensure_bucket(self, bucket: str) -> None: ...


class InMemoryObjectStorage:
    """Process-local store for unit/API tests (not durable across processes)."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self._lock = threading.Lock()

    async def put_bytes(
        self,
        *,
        bucket: str,
        object_key: str,
        data: bytes,
        content_type: str,
    ) -> None:
        with self._lock:
            self._objects[(bucket, object_key)] = (bytes(data), content_type)

    async def get_bytes(self, *, bucket: str, object_key: str) -> bytes:
        with self._lock:
            row = self._objects.get((bucket, object_key))
        if row is None:
            raise ObjectStorageError(
                "FACTORY_SOURCE_OBJECT_MISSING",
                "Factory source object was not found in Object Storage.",
            )
        return row[0]

    async def delete_object(self, *, bucket: str, object_key: str) -> None:
        with self._lock:
            self._objects.pop((bucket, object_key), None)

    async def ensure_bucket(self, bucket: str) -> None:
        return None

    def object_count(self) -> int:
        with self._lock:
            return len(self._objects)

    def has_object(self, *, bucket: str, object_key: str) -> bool:
        with self._lock:
            return (bucket, object_key) in self._objects


def _read_secret_file(path: str | None) -> str | None:
    if not path:
        return None
    raw = Path(path).read_text(encoding="utf-8")
    return raw.strip().replace("\r", "").replace("\n", "")


class S3CompatibleObjectStorage:
    """MinIO / S3-compatible client via boto3 (threaded for async callers)."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        import boto3
        from botocore.config import Config

        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )

    async def put_bytes(
        self,
        *,
        bucket: str,
        object_key: str,
        data: bytes,
        content_type: str,
    ) -> None:
        def _put() -> None:
            self._client.put_object(
                Bucket=bucket,
                Key=object_key,
                Body=data,
                ContentType=content_type,
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:  # noqa: BLE001 — map to bounded code
            logger.warning("object storage put failed code=%s", type(exc).__name__)
            raise ObjectStorageError(
                "FACTORY_SOURCE_OBJECT_STORE_FAILED",
                "Failed to store Factory source object.",
            ) from exc

    async def get_bytes(self, *, bucket: str, object_key: str) -> bytes:
        def _get() -> bytes:
            from botocore.exceptions import ClientError

            try:
                response = self._client.get_object(Bucket=bucket, Key=object_key)
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code in {"404", "NoSuchKey", "NotFound"}:
                    raise ObjectStorageError(
                        "FACTORY_SOURCE_OBJECT_MISSING",
                        "Factory source object was not found in Object Storage.",
                    ) from exc
                raise
            body = response["Body"].read()
            return bytes(body)

        try:
            return await asyncio.to_thread(_get)
        except ObjectStorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("object storage get failed code=%s", type(exc).__name__)
            raise ObjectStorageError(
                "FACTORY_SOURCE_OBJECT_FETCH_FAILED",
                "Failed to fetch Factory source object.",
            ) from exc

    async def delete_object(self, *, bucket: str, object_key: str) -> None:
        def _delete() -> None:
            self._client.delete_object(Bucket=bucket, Key=object_key)

        try:
            await asyncio.to_thread(_delete)
        except Exception:  # noqa: BLE001 — best-effort orphan cleanup
            logger.warning("object storage delete failed key_present=yes")

    async def ensure_bucket(self, bucket: str) -> None:
        def _ensure() -> None:
            from botocore.exceptions import ClientError

            try:
                self._client.head_bucket(Bucket=bucket)
            except ClientError:
                self._client.create_bucket(Bucket=bucket)

        try:
            await asyncio.to_thread(_ensure)
        except Exception as exc:  # noqa: BLE001
            logger.warning("object storage ensure_bucket failed code=%s", type(exc).__name__)
            raise ObjectStorageError(
                "FACTORY_SOURCE_OBJECT_STORE_FAILED",
                "Failed to prepare Factory Object Storage bucket.",
            ) from exc


_memory_singleton: InMemoryObjectStorage | None = None


def get_object_storage(settings: Settings | None = None) -> ObjectStorageClient:
    """Resolve Object Storage client from settings.

    ``memory`` backend is the default for local/unit tests without credentials.
    """

    from app.core.config import get_settings

    cfg = settings or get_settings()
    backend = (cfg.object_storage_backend or "memory").strip().lower()
    if backend == "memory":
        global _memory_singleton
        if _memory_singleton is None:
            _memory_singleton = InMemoryObjectStorage()
        return _memory_singleton
    if backend in {"s3", "minio"}:
        access = cfg.object_storage_access_key or _read_secret_file(
            cfg.object_storage_access_key_file
        )
        secret = cfg.object_storage_secret_key or _read_secret_file(
            cfg.object_storage_secret_key_file
        )
        if not access or not secret:
            raise ObjectStorageError(
                "FACTORY_SOURCE_OBJECT_STORE_FAILED",
                "Object Storage credentials are not configured.",
            )
        return S3CompatibleObjectStorage(
            endpoint_url=cfg.object_storage_endpoint,
            access_key=access,
            secret_key=secret,
            region=cfg.object_storage_region,
        )
    raise ObjectStorageError(
        "FACTORY_SOURCE_OBJECT_STORE_FAILED",
        "Unsupported Object Storage backend.",
    )


def reset_memory_object_storage() -> InMemoryObjectStorage:
    """Test helper: replace the process singleton with a fresh store."""

    global _memory_singleton
    _memory_singleton = InMemoryObjectStorage()
    return _memory_singleton
