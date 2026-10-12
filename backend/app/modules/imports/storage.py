from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

from app.core.config import Settings
from app.core.errors import AppError


@dataclass(frozen=True)
class StoredObject:
    key: str
    version: str
    digest: str
    size: int


class StorageError(Exception):
    """Base for storage failures the caller must classify and handle."""


class StorageUnavailableError(StorageError):
    """Temporary: network error or 5xx from the storage backend. Retryable."""


class StorageObjectMissingError(StorageError):
    """Permanent: the object does not exist at the expected key."""


class StorageAccessDeniedError(StorageError):
    """Permanent: credentials rejected, or bucket/key not authorized."""


class StorageObjectCorruptError(StorageError):
    """Permanent: downloaded bytes do not match the recorded digest."""


class StorageBucketMissingError(StorageError):
    """Permanent: the configured bucket does not exist (deployment setup gap).

    Supabase reports this as HTTP 400 with statusCode "404" in the body, so it
    cannot be told apart from other rejections by the HTTP status alone.
    """


class SupabaseStorageClient:
    """Thin wrapper over the Supabase Storage REST API.

    Always authenticates with the service-role key. This client is
    backend/worker-only -- it must never be constructed from, or expose
    credentials to, client-facing code (CLAUDE.md Phase 3 task, "Upload
    Security": do not expose server secrets).
    """

    def __init__(
        self, settings: Settings | None = None, *, bucket: str | None = None
    ) -> None:
        self._settings = settings or Settings.current()
        # Defaults to the imports bucket; other features pass their own so one
        # client class serves every private bucket.
        self._bucket = bucket or self._settings.supabase_storage_bucket

    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        key = self._settings.supabase_service_role_key
        headers = {"Authorization": f"Bearer {key}", "apikey": key}
        if content_type is not None:
            headers["Content-Type"] = content_type
        return headers

    def _base_url(self) -> str:
        return f"{self._settings.supabase_url.rstrip('/')}/storage/v1"

    def upload_object(self, key: str, *, content_type: str, data: bytes) -> StoredObject:
        digest = hashlib.sha256(data).hexdigest()
        bucket = self._bucket
        try:
            response = httpx.post(
                f"{self._base_url()}/object/{bucket}/{key}",
                headers={**self._headers(content_type=content_type), "x-upsert": "false"},
                content=data,
                timeout=30.0,
            )
        except httpx.TransportError as exc:
            raise StorageUnavailableError(str(exc)) from exc
        self._raise_for_status(response, action="upload")

        payload: dict[str, object] = {}
        try:
            payload = response.json()
        except ValueError:
            pass
        version = str(payload.get("Id") or payload.get("Key") or digest)
        return StoredObject(key=key, version=version, digest=digest, size=len(data))

    def download_object(self, key: str) -> bytes:
        bucket = self._bucket
        try:
            response = httpx.get(
                f"{self._base_url()}/object/{bucket}/{key}",
                headers=self._headers(),
                timeout=60.0,
            )
        except httpx.TransportError as exc:
            raise StorageUnavailableError(str(exc)) from exc
        self._raise_for_status(response, action="download")
        return response.content

    def delete_object(self, key: str) -> None:
        """Remove an object. A missing object is not an error (delete is
        idempotent), so a retried cleanup converges."""
        try:
            response = httpx.delete(
                f"{self._base_url()}/object/{self._bucket}/{key}",
                headers=self._headers(),
                timeout=30.0,
            )
        except httpx.TransportError as exc:
            raise StorageUnavailableError(str(exc)) from exc
        if response.status_code == 404:
            return
        self._raise_for_status(response, action="delete")

    def create_signed_url(self, key: str, *, expires_in: int = 300) -> str:
        """Short-lived URL for previewing a private object in the browser."""
        try:
            response = httpx.post(
                f"{self._base_url()}/object/sign/{self._bucket}/{key}",
                headers=self._headers(content_type="application/json"),
                json={"expiresIn": expires_in},
                timeout=15.0,
            )
        except httpx.TransportError as exc:
            raise StorageUnavailableError(str(exc)) from exc
        self._raise_for_status(response, action="sign")
        signed = str(response.json().get("signedURL", ""))
        if not signed:
            raise StorageError("Storage did not return a signed URL")
        path = signed if signed.startswith("/") else f"/{signed}"
        return f"{self._base_url()}{path}"

    def object_exists(self, key: str) -> bool:
        bucket = self._bucket
        prefix, _, name = key.rpartition("/")
        try:
            response = httpx.post(
                f"{self._base_url()}/object/list/{bucket}",
                headers=self._headers(content_type="application/json"),
                json={"prefix": prefix, "search": name, "limit": 1},
                timeout=15.0,
            )
        except httpx.TransportError as exc:
            raise StorageUnavailableError(str(exc)) from exc
        self._raise_for_status(response, action="list")

        entries = response.json()
        return any(entry.get("name") == name for entry in entries)

    def _raise_for_status(self, response: httpx.Response, *, action: str) -> None:
        if response.status_code == 404:
            raise StorageObjectMissingError(f"Storage object not found ({action})")
        if response.status_code in (401, 403):
            raise StorageAccessDeniedError(f"Storage {action} denied: {response.status_code}")
        if response.status_code >= 500:
            raise StorageUnavailableError(f"Storage {action} failed: {response.status_code}")
        if response.status_code >= 400:
            # The body (e.g. "Bucket not found", "mime type not supported") is
            # the only way to see why storage refused; it goes to the server log
            # via the exception message and is never sent to the client.
            body = response.text[:300]
            if "bucket not found" in body.lower():
                raise StorageBucketMissingError(
                    f"Storage {action} rejected: bucket not found ({body})"
                )
            raise StorageError(
                f"Storage {action} rejected: {response.status_code} {body}"
            )


def map_storage_error(exc: StorageError) -> AppError:
    """Classify a storage failure into a stable, client-safe AppError.

    CLAUDE.md Phase 3 task, "Object Storage Failure": temporary vs missing vs
    access-denied vs corrupt map to distinct, safe outcomes -- never a raw
    exception message or credential detail reaches the response body.
    """
    if isinstance(exc, StorageObjectMissingError):
        return AppError(
            "import_object_missing",
            "The uploaded file could not be found",
            status_code=409,
        )
    if isinstance(exc, StorageAccessDeniedError):
        return AppError(
            "import_storage_denied", "Storage access was denied", status_code=500
        )
    if isinstance(exc, StorageObjectCorruptError):
        return AppError(
            "import_object_corrupt",
            "The uploaded file failed integrity verification",
            status_code=409,
        )
    if isinstance(exc, StorageUnavailableError):
        return AppError(
            "import_storage_unavailable",
            "Storage is temporarily unavailable",
            status_code=503,
        )
    return AppError(
        "import_storage_error", "The uploaded file could not be processed", status_code=500
    )
