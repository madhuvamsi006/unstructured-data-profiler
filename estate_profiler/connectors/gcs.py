"""Google Cloud Storage connector.

Listing returns metadata only; bytes are pulled per file with a ranged read so
a 2 GB video costs one HEAD and a 1 MB GET rather than a full download.
"""

from __future__ import annotations

from typing import Iterator

from .base import DEFAULT_EXCLUDE_GLOBS, FileRef, should_skip_name, split_ext, utc


class GCSConnector:
    source = "gcs"

    def __init__(self, bucket: str, prefix: str = "", project: str | None = None):
        from google.cloud import storage  # imported lazily so local-only runs need no GCP deps

        self.client = storage.Client(project=project) if project else storage.Client()
        self.bucket_name = bucket
        self.prefix = prefix.lstrip("/")
        self.container = f"gs://{bucket}" + (f"/{self.prefix}" if self.prefix else "")
        self._bucket = self.client.bucket(bucket)

    def walk(self, max_files: int) -> Iterator[FileRef]:
        count = 0
        for blob in self.client.list_blobs(self._bucket, prefix=self.prefix or None):
            if count >= max_files:
                return
            # GCS has no directories; zero-byte keys ending in "/" are folder markers.
            if blob.name.endswith("/"):
                continue
            name = blob.name.rsplit("/", 1)[-1]
            if should_skip_name(name, DEFAULT_EXCLUDE_GLOBS):
                continue
            rel = blob.name[len(self.prefix):].lstrip("/") if self.prefix else blob.name
            yield FileRef(
                uri=f"gs://{self.bucket_name}/{blob.name}",
                source=self.source,
                container=self.container,
                rel_path=rel,
                name=name,
                ext=split_ext(name),
                size_bytes=blob.size or 0,
                modified=utc(blob.updated),
                declared_content_type=blob.content_type,
                depth=rel.count("/"),
            )
            count += 1

    def read(self, ref: FileRef, n_bytes: int) -> bytes:
        blob_name = ref.uri.split(f"gs://{self.bucket_name}/", 1)[-1]
        try:
            blob = self._bucket.blob(blob_name)
            return blob.download_as_bytes(start=0, end=max(n_bytes - 1, 0), raw_download=True)
        except Exception:
            return b""
