from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UploadedArtifact:
    name: str
    kind: str
    url: str
    content_type: str
    size_bytes: int


class R2Storage:
    """Cloudflare R2/S3-compatible artifact storage."""

    def __init__(
        self,
        *,
        bucket: str | None = None,
        endpoint_url: str | None = None,
        public_base_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        self.bucket = bucket or os.environ.get("VISUAL_AGENT_R2_BUCKET", "")
        self.endpoint_url = endpoint_url or os.environ.get("VISUAL_AGENT_R2_ENDPOINT", "")
        self.public_base_url = (public_base_url or os.environ.get("VISUAL_AGENT_R2_PUBLIC_BASE_URL", "")).rstrip("/")
        self.access_key_id = access_key_id or os.environ.get("VISUAL_AGENT_R2_ACCESS_KEY_ID", "")
        self.secret_access_key = secret_access_key or os.environ.get("VISUAL_AGENT_R2_SECRET_ACCESS_KEY", "")

    def configured(self) -> bool:
        return bool(self.bucket and self.endpoint_url and self.access_key_id and self.secret_access_key)

    def upload_file(self, path: str | Path, *, key: str) -> UploadedArtifact:
        if not self.configured():
            raise RuntimeError("R2 storage is not configured.")
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("Install boto3 to use R2 storage.") from exc
        file_path = Path(path)
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
        )
        client.upload_file(str(file_path), self.bucket, key, ExtraArgs={"ContentType": content_type})
        url = f"{self.public_base_url}/{key}" if self.public_base_url else f"s3://{self.bucket}/{key}"
        return UploadedArtifact(
            name=file_path.name,
            kind=file_path.suffix.lstrip(".") or "file",
            url=url,
            content_type=content_type,
            size_bytes=file_path.stat().st_size,
        )
