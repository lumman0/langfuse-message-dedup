"""Synchronous, confirmed writes using the public Langfuse media API.

Initial supported storage target: S3-compatible (tested with MinIO). Each put
registers the current trace association even when the server reuses a blob.
"""

import base64
import hashlib
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .codec import MediaContext, TOKEN


def _url(value: str) -> str:
    url = httpx.URL(value)
    if url.scheme not in ("http", "https") or not url.host or url.userinfo:
        raise ValueError("Expected an HTTP(S) URL without embedded credentials")
    return str(url)


class LangfuseMediaStore:
    def __init__(
        self,
        *,
        base_url: str,
        public_key: str,
        secret_key: str,
        timeout: float = 10,
        max_blob_bytes: int = 16 * 1024 * 1024,
        api_transport: httpx.BaseTransport | None = None,
        blob_transport: httpx.BaseTransport | None = None,
    ):
        if not public_key or not secret_key:
            raise ValueError("Project API keys are required")
        if type(max_blob_bytes) is not int or max_blob_bytes < 1:
            raise ValueError("max_blob_bytes must be a positive integer")
        self.base_url = _url(base_url).rstrip("/")
        self.max_blob_bytes = max_blob_bytes
        # Never share API authentication with the signed object-storage requests.
        self.api = httpx.Client(
            auth=(public_key, secret_key),
            timeout=timeout,
            follow_redirects=False,
            transport=api_transport,
        )
        self.blobs = httpx.Client(
            timeout=timeout, follow_redirects=False, transport=blob_transport
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        self.api.close()
        self.blobs.close()

    def put(self, data: bytes, context: MediaContext) -> str:
        if len(data) > self.max_blob_bytes:
            raise ValueError("Content exceeds max_blob_bytes")
        checksum = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
        body = {
            "traceId": context.trace_id,
            "field": context.field,
            "contentType": "application/json",
            "contentLength": len(data),
            "sha256Hash": checksum,
        }
        if context.observation_id is not None:
            body["observationId"] = context.observation_id
        response = self.api.post(f"{self.base_url}/api/public/media", json=body)
        response.raise_for_status()
        entry = response.json()
        token = f"@@@langfuseMedia:type=application/json|id={entry['mediaId']}|source=bytes@@@"
        if not TOKEN.fullmatch(token):
            raise ValueError("Server returned an invalid media ID")
        upload_url = entry["uploadUrl"]
        if upload_url is not None:
            uploaded = self.blobs.put(
                _url(upload_url),
                content=data,
                headers={
                    "Content-Type": "application/json",
                    "x-amz-checksum-sha256": checksum,
                },
            )
            uploaded.raise_for_status()
            # S3 PUT returns 200. Other backends have different protocol details
            # and are intentionally not claimed as supported by this prototype.
            if uploaded.status_code != 200:
                raise ValueError("Expected an S3-compatible PUT status of 200")
            patched = self.api.patch(
                f"{self.base_url}/api/public/media/{quote(entry['mediaId'], safe='')}",
                json={
                    "uploadedAt": datetime.now(timezone.utc).isoformat(),
                    "uploadHttpStatus": uploaded.status_code,
                },
            )
            patched.raise_for_status()
        return token

    def get(self, token: str) -> bytes:
        match = TOKEN.fullmatch(token)
        if match is None:
            raise ValueError("Invalid media token")
        media_id = match.group(1)
        response = self.api.get(
            f"{self.base_url}/api/public/media/{quote(media_id, safe='')}"
        )
        response.raise_for_status()
        entry = response.json()
        size = entry["contentLength"]
        if entry["mediaId"] != media_id or entry["contentType"] != "application/json":
            raise ValueError("Media metadata does not match the reference")
        if type(size) is not int or not 0 <= size <= self.max_blob_bytes:
            raise ValueError("Content exceeds max_blob_bytes or has invalid size")
        data = bytearray()
        with self.blobs.stream("GET", _url(entry["url"])) as downloaded:
            downloaded.raise_for_status()
            for chunk in downloaded.iter_bytes(chunk_size=65536):
                if len(data) + len(chunk) > self.max_blob_bytes:
                    raise ValueError("Download exceeds max_blob_bytes")
                data.extend(chunk)
        if len(data) != size:
            raise ValueError("Downloaded length does not match metadata")
        return bytes(data)
