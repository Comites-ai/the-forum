"""Google Cloud Storage service for file uploads."""
import asyncio
import logging
import uuid
from datetime import datetime
from typing import Optional

from google.cloud import storage

from app.config import get_settings
from app.core.exceptions import GcsUploadError

logger = logging.getLogger(__name__)


class GCSService:
    """Handles Google Cloud Storage operations for file uploads."""

    def __init__(self):
        """Initialize GCS client."""
        settings = get_settings()
        self.client = storage.Client(project=settings.gcp_project_id)
        self.bucket_name = settings.gcs_bucket_name
        self.file_prefix = settings.gcs_file_prefix
        self.bucket = self.client.bucket(self.bucket_name)
        logger.info(f"GCS service initialized for bucket: {self.bucket_name}")

    async def upload_file(
        self,
        file_bytes: bytes,
        mime_type: str,
        original_filename: Optional[str] = None,
    ) -> dict:
        """
        Upload a file to GCS and return URL information.

        Args:
            file_bytes: Raw file content
            mime_type: MIME type of the file
            original_filename: Optional original filename for extension

        Returns:
            Dict with 'gcs_uri' and 'mime_type'

        Raises:
            GcsUploadError: If the upload fails for any reason.
        """
        timestamp = datetime.utcnow().strftime("%Y%m%d")
        unique_id = uuid.uuid4().hex[:12]
        extension = self._get_extension(mime_type, original_filename)
        blob_name = f"{self.file_prefix}/{timestamp}/{unique_id}{extension}"

        loop = asyncio.get_event_loop()

        def _upload():
            blob = self.bucket.blob(blob_name)
            blob.upload_from_string(file_bytes, content_type=mime_type)
            return blob

        try:
            await loop.run_in_executor(None, _upload)
        except Exception as e:
            logger.error(
                f"GCS upload failed for blob {blob_name} "
                f"({len(file_bytes)} bytes, {mime_type}): {e}"
            )
            raise GcsUploadError(f"Failed to upload to GCS: {e}") from e

        gcs_uri = f"gs://{self.bucket_name}/{blob_name}"
        logger.info(f"Uploaded file to GCS: {gcs_uri} ({len(file_bytes)} bytes)")

        return {
            "gcs_uri": gcs_uri,
            "mime_type": mime_type,
        }

    def _get_extension(self, mime_type: str, filename: Optional[str]) -> str:
        """Determine file extension from MIME type or filename."""
        if filename and "." in filename:
            return "." + filename.rsplit(".", 1)[-1].lower()

        mime_to_ext = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
            "application/pdf": ".pdf",
            "text/plain": ".txt",
            "application/json": ".json",
        }
        return mime_to_ext.get(mime_type, "")
