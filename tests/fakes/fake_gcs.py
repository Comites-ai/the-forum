# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""In-memory GCSService stand-in for tests."""
import uuid
from typing import Optional


class FakeGCSService:
    def __init__(self):
        self.uploads: list[dict] = []

    async def upload_file(
        self,
        file_bytes: bytes,
        mime_type: str,
        original_filename: Optional[str] = None,
    ) -> dict:
        gcs_uri = f"gs://test-bucket/fake-{uuid.uuid4().hex[:8]}"
        record = {
            "file_bytes": file_bytes,
            "mime_type": mime_type,
            "original_filename": original_filename,
            "gcs_uri": gcs_uri,
        }
        self.uploads.append(record)
        return {"gcs_uri": gcs_uri, "size_bytes": len(file_bytes)}
