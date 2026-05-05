"""Telegram platform connector implementation."""
import logging
import secrets
from typing import Optional
from fastapi import Request

import aiohttp
from google.cloud import secretmanager

from app.services.platforms.base import PlatformConnector
from app.schemas.platform_event import PlatformEvent
from app.core.exceptions import FileDownloadError

logger = logging.getLogger(__name__)


class TelegramConnector(PlatformConnector):
    """
    Telegram platform connector implementation.

    Handles Telegram-specific operations including message sending,
    file downloads, and webhook verification using the Telegram Bot API.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        bot_token_secret: Optional[str] = None,
        bot_token_project_id: Optional[str] = None
    ):
        """
        Initialize Telegram connector.

        Args:
            bot_token: Direct Telegram bot token (from BotFather)
            webhook_secret: Secret token for webhook verification (X-Telegram-Bot-Api-Secret-Token)
            bot_token_secret: Secret Manager secret name for Telegram bot token
            bot_token_project_id: GCP project ID where the secret is stored

        Note:
            Either bot_token OR (bot_token_secret + bot_token_project_id) must be provided.
            The Secret Manager approach is preferred for better security.
        """
        self.webhook_secret = webhook_secret

        # Fetch token from Secret Manager if configured
        if bot_token_secret and bot_token_project_id:
            self.bot_token = self._fetch_token_from_secret_manager(
                bot_token_secret, bot_token_project_id
            )
        elif bot_token:
            # Direct token
            self.bot_token = bot_token
        else:
            raise ValueError(
                "Either bot_token OR (bot_token_secret + bot_token_project_id) must be provided"
            )

        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"

    def _fetch_token_from_secret_manager(self, secret_name: str, project_id: str) -> str:
        """
        Fetch Telegram bot token from Secret Manager.

        Args:
            secret_name: Secret Manager secret name
            project_id: GCP project ID where secret is stored

        Returns:
            Telegram bot token

        Raises:
            Exception: If secret cannot be accessed
        """
        try:
            client = secretmanager.SecretManagerServiceClient()
            secret_path = f"projects/{project_id}/secrets/{secret_name}/versions/latest"

            response = client.access_secret_version(request={"name": secret_path})
            token = response.payload.data.decode('UTF-8').strip()

            logger.debug(f"Fetched Telegram bot token from secret: {secret_name} in project: {project_id}")
            return token
        except Exception as e:
            logger.error(f"Failed to fetch Telegram bot token from secret {secret_name} in project {project_id}: {e}")
            raise

    async def send_message(self, recipient_id: str, text: str) -> dict:
        """
        Send message to Telegram chat.

        Args:
            recipient_id: Telegram chat ID (user's chat ID from incoming message)
            text: Message text to send

        Returns:
            Telegram API response dict

        Raises:
            Exception: If Telegram API call fails
        """
        try:
            logger.debug(f"Posting message to Telegram chat: {recipient_id}")

            async with aiohttp.ClientSession() as session:
                payload = {
                    "chat_id": recipient_id,
                    "text": text,
                    "parse_mode": "Markdown"  # Support basic markdown formatting
                }

                async with session.post(
                    f"{self.api_base}/sendMessage",
                    json=payload
                ) as response:
                    result = await response.json()

                    if response.status == 200 and result.get("ok"):
                        logger.info(
                            f"Successfully posted message to Telegram chat: {recipient_id}, "
                            f"message_id: {result.get('result', {}).get('message_id')}"
                        )
                        return result
                    else:
                        error_description = result.get("description", "unknown_error")
                        logger.error(
                            f"Telegram API error posting to chat {recipient_id}: "
                            f"Status {response.status}, Error: {error_description}"
                        )
                        raise Exception(
                            f"Telegram API error: {error_description}"
                        )

        except Exception as e:
            logger.error(
                f"Error posting to Telegram chat {recipient_id}: {e}"
            )
            raise

    async def download_file(self, download_ref: str) -> bytes:
        """
        Download a file from Telegram using its file_id.

        Telegram file download is a two-step process:
        1. Resolve file_id → file_path via getFile API
        2. GET the file from /file/bot<token>/<file_path>

        Args:
            download_ref: Telegram file_id from the message

        Returns:
            Raw file bytes

        Raises:
            FileDownloadError: If either step fails
        """
        file_id = download_ref
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base}/getFile",
                    params={"file_id": file_id}
                ) as response:
                    result = await response.json()

                    if not result.get("ok"):
                        error_description = result.get("description", "unknown_error")
                        raise FileDownloadError(
                            f"Telegram getFile failed: {error_description}"
                        )

                    file_path = result["result"]["file_path"]
                    logger.debug(f"Got Telegram file path: {file_path}")

                file_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"

                async with session.get(file_url) as download_response:
                    if download_response.status == 200:
                        file_bytes = await download_response.read()
                        logger.info(
                            f"Downloaded file from Telegram: {len(file_bytes)} bytes"
                        )
                        return file_bytes
                    error_text = await download_response.text()
                    logger.error(
                        f"Telegram file download failed: "
                        f"{download_response.status} - {error_text}"
                    )
                    raise FileDownloadError(
                        f"Telegram returned {download_response.status} when downloading file"
                    )

        except FileDownloadError:
            raise
        except Exception as e:
            logger.error(f"Telegram file download error (file_id={file_id}): {e}")
            raise FileDownloadError(f"Network error downloading Telegram file: {e}") from e

    async def get_user_info(self, user_id: str) -> dict:
        """
        Get Telegram user profile info.

        Uses the getChat API to retrieve user information.

        Args:
            user_id: Telegram user ID (numeric string)

        Returns:
            User info dict with keys:
                - display_name: User's display name (first_name + last_name)
                - first_name: User's first name
                - last_name: User's last name (if available)
                - username: User's username (if available)

        Raises:
            Exception: If Telegram API call fails
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base}/getChat",
                    params={"chat_id": user_id}
                ) as response:
                    result = await response.json()

                    if response.status == 200 and result.get("ok"):
                        chat = result["result"]
                        first_name = chat.get("first_name", "")
                        last_name = chat.get("last_name", "")
                        username = chat.get("username")

                        # Construct display name
                        display_name = f"{first_name} {last_name}".strip() if last_name else first_name
                        if not display_name:
                            display_name = username or user_id

                        return {
                            "display_name": display_name,
                            "first_name": first_name,
                            "last_name": last_name,
                            "username": username
                        }
                    else:
                        logger.error(f"Failed to get user info: {result}")
                        return {"display_name": user_id}

        except Exception as e:
            logger.error(f"Error getting Telegram user info for {user_id}: {e}")
            return {"display_name": user_id}

    async def open_conversation(self, user_id: str, space_id: str = None) -> str:
        """
        Get conversation ID for Telegram user.

        In Telegram, the chat_id IS the conversation identifier, so we can
        just return it directly. If space_id is provided from a previous message,
        use that; otherwise use user_id.

        Args:
            user_id: Telegram user ID
            space_id: Optional chat ID from previous message

        Returns:
            Chat ID for the conversation (typically same as user_id for DMs)
        """
        # For Telegram, the chat_id from incoming messages is what we use to reply
        # If we have space_id (from previous message), use that
        # Otherwise, assume user_id is the chat_id for DM
        conversation_id = space_id if space_id else user_id
        logger.debug(f"Using Telegram conversation ID: {conversation_id}")
        return conversation_id

    async def verify_request(self, request: Request) -> bool:
        """
        Verify Telegram webhook request using secret token.

        Telegram sends the secret token in the X-Telegram-Bot-Api-Secret-Token header.
        This is set when configuring the webhook via setWebhook.

        Args:
            request: FastAPI request object

        Returns:
            True if request is verified, False otherwise
        """
        if not self.webhook_secret:
            logger.warning("No webhook secret configured, skipping verification")
            return True

        # Get the secret token from the request header
        request_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

        # Use constant-time comparison to prevent timing attacks
        is_valid = secrets.compare_digest(self.webhook_secret, request_secret)

        if not is_valid:
            logger.warning("Telegram webhook verification failed: secret token mismatch")

        return is_valid

    def parse_event(self, data: dict) -> PlatformEvent:
        """
        Parse Telegram update into unified PlatformEvent.

        Telegram webhook sends updates with this structure:
        {
            "update_id": 123456789,
            "message": {
                "message_id": 123,
                "from": {
                    "id": 987654321,
                    "first_name": "John",
                    "last_name": "Doe",
                    "username": "johndoe"
                },
                "chat": {
                    "id": 987654321,
                    "first_name": "John",
                    "type": "private"
                },
                "date": 1234567890,
                "text": "Hello bot!",
                "photo": [...],  # Optional
                "document": {...}  # Optional
            }
        }

        Args:
            data: Telegram update dict

        Returns:
            Normalized PlatformEvent

        Raises:
            ValueError: If update format is invalid
        """
        message = data.get("message", {})

        if not message:
            raise ValueError("Invalid Telegram update: missing 'message' field")

        from_user = message.get("from", {})
        chat = message.get("chat", {})

        user_id = str(from_user.get("id", ""))
        chat_id = str(chat.get("id", ""))
        message_text = message.get("text", "")

        if not user_id or not chat_id:
            raise ValueError("Invalid Telegram message: missing user id or chat id")

        # Extract file attachments (photos, documents, etc.)
        # Each file dict carries canonical keys 'mimetype' and 'download_ref'
        # so MessageProcessorV2 can stay platform-agnostic.
        files = []

        if "photo" in message:
            # Telegram sends multiple sizes; the last entry is the largest.
            photos = message["photo"]
            if photos:
                largest_photo = photos[-1]
                files.append({
                    "mimetype": "image/jpeg",
                    "download_ref": largest_photo.get("file_id", ""),
                    "size": largest_photo.get("file_size"),
                    "file_type": "photo",
                })

        if "document" in message:
            doc = message["document"]
            files.append({
                "mimetype": doc.get("mime_type", "application/octet-stream"),
                "download_ref": doc.get("file_id", ""),
                "name": doc.get("file_name"),
                "size": doc.get("file_size"),
                "file_type": "document",
            })

        if "video" in message:
            video = message["video"]
            files.append({
                "mimetype": video.get("mime_type", "video/mp4"),
                "download_ref": video.get("file_id", ""),
                "size": video.get("file_size"),
                "file_type": "video",
            })

        if "voice" in message:
            voice = message["voice"]
            files.append({
                "mimetype": voice.get("mime_type", "audio/ogg"),
                "download_ref": voice.get("file_id", ""),
                "size": voice.get("file_size"),
                "file_type": "voice",
            })

        # Album signal: Telegram delivers each photo of an album as its own
        # webhook event sharing this id. Treated as "multiple images" by the
        # processor even though each event has len(files) == 1.
        media_group_id = message.get("media_group_id")

        return PlatformEvent(
            platform="telegram",
            user_id=user_id,
            user_email=None,  # Telegram doesn't provide email in updates
            message_text=message_text,
            space_id=chat_id,
            files=files,
            media_group_id=media_group_id,
            raw_event=data
        )
