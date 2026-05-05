"""Google Chat platform connector implementation."""
import logging
import json
import urllib.parse
from typing import Optional
from fastapi import Request

from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import secretmanager
from googleapiclient.discovery import build
import aiohttp

from app.services.platforms.base import PlatformConnector
from app.schemas.platform_event import PlatformEvent
from app.config import get_settings
from app.core.exceptions import FileDownloadError

logger = logging.getLogger(__name__)


class GoogleChatConnector(PlatformConnector):
    """
    Google Chat platform connector implementation.

    Handles Google Chat-specific operations including message sending,
    user info retrieval, and webhook verification.
    """

    def __init__(self, service_account_secret_name: str, project_id: Optional[str] = None):
        """
        Initialize Google Chat connector.

        Args:
            service_account_secret_name: Secret Manager secret name (e.g., 'growth-coach-credentials')
            project_id: Optional GCP project ID where the secret is stored.
                       If not provided, uses the middleware's project ID (backward compatibility)
        """
        self.service_account_secret_name = service_account_secret_name
        self.project_id = project_id
        self.credentials = None
        self._init_credentials()

    def _init_credentials(self):
        """Initialize Google service account credentials from Secret Manager."""
        try:
            settings = get_settings()

            # Use provided project_id or fall back to middleware project (backward compatibility)
            project_id = self.project_id or settings.gcp_project_id

            # Load service account JSON from Secret Manager
            client = secretmanager.SecretManagerServiceClient()
            secret_path = f"projects/{project_id}/secrets/{self.service_account_secret_name}/versions/latest"

            response = client.access_secret_version(request={"name": secret_path})
            service_account_info = json.loads(response.payload.data.decode('UTF-8'))

            self.credentials = service_account.Credentials.from_service_account_info(
                service_account_info,
                scopes=['https://www.googleapis.com/auth/chat.bot']
            )
            logger.debug(
                f"Initialized Google Chat credentials from secret: {self.service_account_secret_name} "
                f"in project: {project_id}"
            )
        except Exception as e:
            logger.error(
                f"Failed to initialize Google Chat credentials from secret {self.service_account_secret_name} "
                f"in project {project_id}: {e}"
            )
            raise

    def _get_chat_service(self):
        """
        Get Google Chat API service.

        Returns:
            Google Chat API service instance
        """
        # Refresh credentials if needed
        if not self.credentials.valid:
            self.credentials.refresh(GoogleAuthRequest())

        return build('chat', 'v1', credentials=self.credentials)

    async def send_message(self, recipient_id: str, text: str) -> dict:
        """
        Send message to Google Chat space.

        Args:
            recipient_id: Google Chat space name (e.g., "spaces/AAAA...")
            text: Message text to send

        Returns:
            Google Chat API response dict

        Raises:
            Exception: If Google Chat API call fails
        """
        try:
            logger.debug(f"Posting message to Google Chat space: {recipient_id}")

            # Build message body
            message = {
                'text': text
            }

            # Get Chat service
            service = self._get_chat_service()

            # Send message
            response = service.spaces().messages().create(
                parent=recipient_id,
                body=message
            ).execute()

            logger.info(
                f"Successfully posted message to space: {recipient_id}, "
                f"message_name: {response.get('name')}"
            )

            return response

        except Exception as e:
            logger.error(
                f"Error posting to Google Chat space {recipient_id}: {e}"
            )
            raise

    async def download_file(self, download_ref: str) -> bytes:
        """
        Download an UPLOADED_CONTENT attachment from Google Chat.

        Args:
            download_ref: The resourceName from the attachment's
                attachmentDataRef (e.g. produced by parse_event for
                source=UPLOADED_CONTENT). Drive-shared files are not
                supported and parse_event marks them as non-image so they
                surface as the unsupported-file-type rejection.

        Returns:
            Raw file bytes

        Raises:
            FileDownloadError: If the download fails for any reason.
        """
        if not download_ref:
            raise FileDownloadError(
                "No Google Chat download reference (Drive-shared files are not supported)"
            )

        try:
            if not self.credentials.valid:
                self.credentials.refresh(GoogleAuthRequest())
            access_token = self.credentials.token

            encoded_ref = urllib.parse.quote(download_ref, safe='')
            url = f"https://chat.googleapis.com/v1/media/{encoded_ref}?alt=media"

            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {access_token}"}
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        file_bytes = await response.read()
                        logger.info(
                            f"Downloaded Google Chat attachment: {len(file_bytes)} bytes"
                        )
                        return file_bytes
                    error_text = await response.text()
                    logger.error(
                        f"Google Chat media download failed: "
                        f"{response.status} - {error_text}"
                    )
                    raise FileDownloadError(
                        f"Google Chat returned {response.status} when downloading attachment"
                    )
        except FileDownloadError:
            raise
        except Exception as e:
            logger.error(f"Google Chat file download error (ref={download_ref}): {e}")
            raise FileDownloadError(
                f"Network error downloading Google Chat attachment: {e}"
            ) from e

    async def get_user_info(self, user_id: str) -> dict:
        """
        Get Google Chat user profile info.

        Args:
            user_id: Google Chat user resource name (e.g., "users/12345...")

        Returns:
            User info dict with keys:
                - display_name: User's display name
                - email: User's email (if available)

        Raises:
            Exception: If Google Chat API call fails
        """
        try:
            service = self._get_chat_service()

            # Get user info
            user = service.users().get(name=user_id).execute()

            display_name = user.get('displayName', user_id)
            email = user.get('email')

            return {
                "display_name": display_name,
                "email": email
            }

        except Exception as e:
            logger.error(f"Error getting user info for {user_id}: {e}")
            # Return fallback
            return {"display_name": user_id, "email": None}

    async def open_conversation(self, user_id: str, space_id: str = None) -> str:
        """
        Open or get existing DM space with a user.

        For Google Chat, when responding to incoming messages, we already have
        the space_id from the event and can use it directly. For proactive
        messages (like scheduled jobs), we would need to create a space.

        Args:
            user_id: Google Chat user resource name
            space_id: Optional space ID if already known from incoming event

        Returns:
            Space name (resource ID) for the DM conversation

        Raises:
            Exception: If conversation cannot be opened
        """
        # If we already have a space_id (from incoming message), use it directly
        if space_id:
            logger.debug(f"Using existing space {space_id} for user {user_id}")
            return space_id

        # For proactive messages, we'd need to find or create the space
        # This requires additional scopes beyond chat.bot
        logger.warning(
            f"No space_id provided for user {user_id}. "
            "Proactive messaging not yet implemented for Google Chat."
        )
        raise NotImplementedError(
            "Proactive messaging (without space_id) not yet implemented for Google Chat. "
            "This would require additional OAuth scopes."
        )

    async def verify_request(self, request: Request) -> bool:
        """
        Verify Google Chat request authenticity.

        Google Chat uses bearer tokens in the Authorization header.
        We verify the request came from Google by checking the token.

        Args:
            request: FastAPI request object

        Returns:
            True if request is valid, False otherwise
        """
        try:
            # Google Chat sends a bearer token that we can verify
            # For now, we'll implement basic verification
            # TODO: Implement proper token verification with Google's public keys

            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                logger.warning("No Bearer token in Authorization header")
                return False

            # For MVP, we'll accept any bearer token
            # In production, verify the JWT token signature
            return True

        except Exception as e:
            logger.error(f"Error verifying Google Chat request: {e}")
            return False

    def parse_event(self, data: dict) -> PlatformEvent:
        """
        Parse Google Chat event into unified PlatformEvent.

        Args:
            data: Google Chat event data (webhook payload)

        Returns:
            Normalized PlatformEvent

        Raises:
            ValueError: If event format is invalid
        """
        # Google Chat webhook event structure:
        # {
        #   "chat": {
        #     "messagePayload": {
        #       "message": {
        #         "name": "spaces/.../messages/...",
        #         "sender": {
        #           "name": "users/12345...",
        #           "displayName": "User Name",
        #           "email": "user@example.com"
        #         },
        #         "text": "Message text",
        #         "space": {
        #           "name": "spaces/AAAA...",
        #           "type": "DM"
        #         },
        #         "attachments": [...]
        #       }
        #     }
        #   }
        # }

        # Extract message from webhook payload
        chat_data = data.get("chat", {})
        message_payload = chat_data.get("messagePayload", {})
        message = message_payload.get("message", {})

        if not message:
            raise ValueError(f"Invalid Google Chat event: missing message payload")

        sender = message.get("sender", {})
        space = message.get("space", {})

        user_id = sender.get("name")
        user_email = sender.get("email")
        space_id = space.get("name")
        message_text = message.get("text", "")
        attachments = message.get("attachments", [])

        if not user_id or not space_id:
            raise ValueError(f"Invalid Google Chat event: missing user or space")

        # Transform Google Chat attachments into canonical file dicts.
        # UPLOADED_CONTENT → real download_ref (resourceName); fetched via
        #   chat.googleapis.com/v1/media/{ref} in download_file().
        # DRIVE_FILE → marked with a non-image mimetype so the processor
        #   surfaces it as "unsupported file type". Real Drive support
        #   would require Drive API + extra scopes (out of scope).
        # Anything else → treated as unsupported.
        files = []
        for att in attachments:
            source = att.get("source", "")
            content_type = att.get("contentType", "")
            content_name = att.get("contentName")
            if source == "UPLOADED_CONTENT":
                ref_obj = att.get("attachmentDataRef", {}) or {}
                files.append({
                    "mimetype": content_type,
                    "download_ref": ref_obj.get("resourceName", ""),
                    "name": content_name,
                    "source": "uploaded",
                })
            elif source == "DRIVE_FILE":
                files.append({
                    "mimetype": "application/x-google-drive-file",
                    "download_ref": "",
                    "name": content_name,
                    "source": "drive",
                })
            else:
                files.append({
                    "mimetype": content_type or "application/octet-stream",
                    "download_ref": "",
                    "name": content_name,
                    "source": "unknown",
                })

        return PlatformEvent(
            platform="google_chat",
            user_id=user_id,
            user_email=user_email,
            message_text=message_text,
            space_id=space_id,
            files=files,
            raw_event=data
        )
