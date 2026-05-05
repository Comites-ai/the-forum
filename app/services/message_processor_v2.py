"""Platform-agnostic message processing service (v2 - multi-platform)."""
import asyncio
import base64
import logging
from typing import Optional, TYPE_CHECKING

from app.config import get_settings
from app.schemas.platform_event import PlatformEvent
from app.services.firestore_service import FirestoreService
from app.services.vertex_ai_service import VertexAIService
from app.services.identity_service import IdentityService
from app.services.platforms.base import PlatformConnector
from app.core.exceptions import (
    ResourceExhaustedError,
    FileDownloadError,
    FileTooLargeError,
    UnsupportedImageTypeError,
    GcsUploadError,
    AgentStreamError,
)

if TYPE_CHECKING:
    from app.services.gcs_service import GCSService

logger = logging.getLogger(__name__)


REJECTION_NON_IMAGE_FILES = (
    "Sorry, it appears you sent me a file type that I can't read! "
    "I can only accept typed words and images. "
    "I'm going to ignore those files and read the rest of your message."
)
REJECTION_MULTIPLE_IMAGES = (
    "Sorry, I can only handle one image at a time. "
    "Can you send me just the first one?"
)
ERR_DOWNLOAD = (
    "I couldn't download the image you sent. "
    "Could you try sending it again?"
)
ERR_TOO_LARGE_TEMPLATE = (
    "That image is too large for me to process (limit: {limit_mb} MB)."
)
ERR_UNSUPPORTED_TYPE = (
    "I can't read that image format. "
    "Please send a PNG, JPEG, GIF, WebP, or HEIC."
)
ERR_GCS_UPLOAD = (
    "I had trouble saving your image. "
    "Please try again in a minute."
)
ERR_STREAM_BROKEN = (
    "I lost my train of thought halfway through. "
    "Could you ask that again?"
)


class MessageProcessorV2:
    """
    Platform-agnostic message processor.

    Handles messages from any platform (Slack, Google Chat, etc.) using
    unified user identities and platform connectors.
    """

    def __init__(
        self,
        firestore: FirestoreService,
        vertex_ai: VertexAIService,
        identity: IdentityService,
        gcs: Optional["GCSService"] = None,
    ):
        """
        Initialize message processor.

        Args:
            firestore: Firestore service instance
            vertex_ai: Vertex AI service instance
            identity: Identity service instance
            gcs: Optional GCS service instance for file uploads
        """
        self.firestore = firestore
        self.vertex_ai = vertex_ai
        self.identity = identity
        self.gcs = gcs

    async def process_platform_event(
        self,
        event: PlatformEvent,
        connector: PlatformConnector,
        agent_id: str
    ) -> None:
        """
        Process a platform event in the background.

        Flow:
        1. Resolve platform identity to unified user
        2. Apply file-handling rules (single-image policy + non-image rejection)
        3. Get/create Vertex AI session
        4. Send message to Vertex AI
        5. Post response back via platform connector

        File-handling rules:
        - Non-image attachments: send a "can't read that file type" message
          to the user, then continue processing the text + any image.
        - More than one image (or a Telegram album): send a "one image at a
          time" message and skip the agent call entirely.

        Args:
            event: Platform event (normalized from Slack, Google Chat, etc.)
            connector: Platform connector for sending responses
            agent_id: Agent ID handling this message

        Note:
            This function catches all exceptions to prevent background task
            failures from crashing the application.
        """
        user = None
        conversation_id = None
        try:
            user_info = await connector.get_user_info(event.user_id)
            display_name = user_info.get("display_name", event.user_id)
            email = user_info.get("email") or event.user_email

            user = await self.identity.resolve_user(
                platform=event.platform,
                platform_user_id=event.user_id,
                email=email,
                display_name=display_name
            )

            logger.info(
                f"Processing message from user {user.id} ({user.primary_name}) "
                f"on {event.platform}"
            )

            agent = await self.firestore.get_agent_by_id(agent_id)
            if not agent:
                logger.error(f"Agent {agent_id} not found")
                return

            # Open conversation early so we can send rejection messages
            # for the file-handling rules without doing it twice.
            conversation_id = await connector.open_conversation(
                event.user_id,
                space_id=event.space_id
            )

            # Apply the single-image / non-image rules. If this returns None
            # we've already messaged the user and should not call the agent.
            image_payload = await self._apply_file_rules(
                event=event,
                connector=connector,
                conversation_id=conversation_id,
            )
            if image_payload is False:
                # False sentinel = hard reject (multi-image): no agent call.
                return
            # image_payload is None (no image) or a dict (single image ready).

            message_text = f"[From: {user.primary_name}] {event.message_text}"

            if image_payload:
                if "gcs_uri" in image_payload:
                    image_ref = (
                        f"[IMAGE: {image_payload['gcs_uri']} | "
                        f"{image_payload['mime_type']}]"
                    )
                    message_text = f"{image_ref}\n\n{message_text}"
                    logger.info("Embedded 1 image reference in message")
                # base64 path: handled implicitly — the agent receives the
                # image via the same prompt structure already used today.

            session_id = await self._get_or_create_session(
                user_id=user.id,
                agent_id=agent_id,
                vertex_ai_agent_id=agent.vertex_ai_agent_id,
                platform=event.platform,
                user_name=user.primary_name
            )

            try:
                response = await self.vertex_ai.send_message(
                    agent_id=agent.vertex_ai_agent_id,
                    session_id=session_id,
                    message=message_text,
                )
            except AgentStreamError as e:
                logger.warning(
                    f"Agent stream broke mid-flight for user {user.id}: {e}"
                )
                await connector.send_message(
                    recipient_id=conversation_id,
                    text=ERR_STREAM_BROKEN,
                )
                return

            response_text = response.text.strip()
            if not response_text:
                image_count = 1 if image_payload else 0
                logger.warning(
                    f"Empty response from agent for user {user.id} "
                    f"(images: {image_count}, message_length: {len(message_text)})"
                )
                if image_count > 0:
                    response_text = (
                        "I wasn't able to process that request. "
                        "I may not be set up to handle images."
                    )
                else:
                    response_text = (
                        "I wasn't able to process that request. "
                        "Please try rephrasing or shortening your message."
                    )

            await connector.send_message(
                recipient_id=conversation_id,
                text=response_text
            )

            logger.info(
                f"Successfully processed message for user {user.id} on {event.platform}"
            )

        except ResourceExhaustedError as e:
            logger.warning(f"Rate limit hit for user {user.id if user else 'unknown'}: {e}")
            try:
                if connector and conversation_id is None:
                    conversation_id = await connector.open_conversation(
                        event.user_id,
                        space_id=event.space_id
                    )
                if connector and conversation_id:
                    await connector.send_message(
                        recipient_id=conversation_id,
                        text=str(e),
                    )
            except Exception as send_error:
                logger.error(f"Failed to send rate-limit message: {send_error}")

        except Exception as e:
            logger.exception(f"Unexpected error processing platform event: {e}")

    async def _apply_file_rules(
        self,
        event: PlatformEvent,
        connector: PlatformConnector,
        conversation_id: str,
    ):
        """
        Enforce the file-handling rules.

        Returns:
            - False if this is a hard reject (multi-image): caller must NOT
              call the agent. The user has already been messaged.
            - None if there are zero images to forward. Caller should call
              the agent with text only. (Non-image attachments may have been
              rejected already; that's fine, we still forward the text.)
            - dict with image payload (either {'gcs_uri','mime_type'} or
              {'data','mime_type'} for base64 fallback) if a single image is
              ready to forward. Caller embeds it in the prompt.
        """
        images = [
            f for f in event.files
            if f.get("mimetype", "").startswith("image/")
        ]
        non_images = [
            f for f in event.files
            if not f.get("mimetype", "").startswith("image/")
        ]
        is_multi_image = len(images) > 1 or event.media_group_id is not None

        # Rejection #1 always goes first if applicable.
        if non_images:
            logger.info(
                f"Rejecting {len(non_images)} non-image file(s) "
                f"(mimetypes: {[f.get('mimetype') for f in non_images]})"
            )
            await connector.send_message(
                recipient_id=conversation_id,
                text=REJECTION_NON_IMAGE_FILES,
            )

        # Rejection #2: hard stop, no agent call.
        if is_multi_image:
            logger.info(
                f"Rejecting multi-image submission "
                f"(image_count={len(images)}, "
                f"media_group_id={event.media_group_id})"
            )
            await connector.send_message(
                recipient_id=conversation_id,
                text=REJECTION_MULTIPLE_IMAGES,
            )
            return False

        if not images:
            return None

        return await self._intake_single_image(
            file_dict=images[0],
            connector=connector,
            conversation_id=conversation_id,
        )

    async def _intake_single_image(
        self,
        file_dict: dict,
        connector: PlatformConnector,
        conversation_id: str,
    ) -> Optional[dict]:
        """
        Validate, download, and stage a single image for the agent.

        On any failure, sends a specific user-facing message and returns None
        so the caller can continue without the image (or with a text-only
        fallback). On success, returns a dict ready to embed in the prompt.

        Validation order:
          1. MIME allowlist (cheap; reject before download).
          2. Size pre-check via metadata (only if connector provided 'size').
          3. Download (one retry on FileDownloadError).
          4. Size post-check on actual bytes (catches sources without metadata).
          5. GCS upload, or base64 fallback if GCS not configured.
        """
        settings = get_settings()
        mimetype = file_dict.get("mimetype", "")
        download_ref = file_dict.get("download_ref", "")
        size_hint = file_dict.get("size")
        filename = file_dict.get("name")
        max_bytes = settings.max_image_size_mb * 1024 * 1024

        # 1. MIME allowlist
        if mimetype not in settings.allowed_image_mime_types:
            logger.info(f"Rejecting unsupported image MIME type: {mimetype!r}")
            await connector.send_message(
                recipient_id=conversation_id,
                text=ERR_UNSUPPORTED_TYPE,
            )
            return None

        # 2. Pre-download size check (when size hint available)
        if isinstance(size_hint, int) and size_hint > max_bytes:
            logger.info(
                f"Rejecting oversized image at metadata stage: "
                f"{size_hint} bytes > {max_bytes} bytes"
            )
            await connector.send_message(
                recipient_id=conversation_id,
                text=ERR_TOO_LARGE_TEMPLATE.format(limit_mb=settings.max_image_size_mb),
            )
            return None

        # 3. Download with one retry on transient failure
        if not download_ref:
            logger.warning(
                f"Image has no download_ref; cannot fetch (mimetype={mimetype})"
            )
            await connector.send_message(
                recipient_id=conversation_id,
                text=ERR_DOWNLOAD,
            )
            return None

        image_bytes = await self._download_with_retry(
            connector=connector,
            download_ref=download_ref,
        )
        if image_bytes is None:
            await connector.send_message(
                recipient_id=conversation_id,
                text=ERR_DOWNLOAD,
            )
            return None

        # 4. Post-download size check
        if len(image_bytes) > max_bytes:
            logger.info(
                f"Rejecting oversized image post-download: "
                f"{len(image_bytes)} bytes > {max_bytes} bytes"
            )
            await connector.send_message(
                recipient_id=conversation_id,
                text=ERR_TOO_LARGE_TEMPLATE.format(limit_mb=settings.max_image_size_mb),
            )
            return None

        # 5. GCS upload, or base64 fallback when GCS not configured
        if self.gcs:
            try:
                gcs_result = await self.gcs.upload_file(
                    file_bytes=image_bytes,
                    mime_type=mimetype,
                    original_filename=filename,
                )
            except GcsUploadError as e:
                logger.error(f"GCS upload failed: {e}")
                await connector.send_message(
                    recipient_id=conversation_id,
                    text=ERR_GCS_UPLOAD,
                )
                return None

            logger.info(
                f"Uploaded image to GCS: {gcs_result['gcs_uri']} "
                f"({len(image_bytes)} bytes)"
            )
            return {
                "gcs_uri": gcs_result["gcs_uri"],
                "mime_type": mimetype,
            }

        logger.info(
            f"Encoded image as base64 (no GCS): {mimetype}, {len(image_bytes)} bytes"
        )
        return {
            "data": base64.b64encode(image_bytes).decode("utf-8"),
            "mime_type": mimetype,
        }

    async def _download_with_retry(
        self,
        connector: PlatformConnector,
        download_ref: str,
    ) -> Optional[bytes]:
        """
        Download with one retry on FileDownloadError.

        Returns None after exhaustion; otherwise the bytes.
        """
        try:
            return await connector.download_file(download_ref)
        except FileDownloadError as first_err:
            logger.warning(f"Image download failed once, retrying in 1s: {first_err}")
            await asyncio.sleep(1.0)
            try:
                return await connector.download_file(download_ref)
            except FileDownloadError as second_err:
                logger.error(f"Image download failed after retry: {second_err}")
                return None

    async def _get_or_create_session(
        self,
        user_id: str,
        agent_id: str,
        vertex_ai_agent_id: str,
        platform: str,
        user_name: str = None
    ) -> str:
        """
        Get existing session or create new one for unified user.

        Args:
            user_id: Unified user ID from users collection
            agent_id: Agent ID from agents collection
            vertex_ai_agent_id: Vertex AI agent resource name
            platform: Platform this message came from
            user_name: User's actual name to pass to the Reasoning Engine

        Returns:
            Vertex AI session ID

        Raises:
            Exception: If session operations fail
        """
        session = await self.firestore.get_session_by_user(
            user_id=user_id,
            agent_id=agent_id
        )

        if session:
            await self.firestore.update_session_platforms(session.id, platform)
            logger.info(
                f"Using existing session: {session.id} "
                f"(now includes platform: {platform})"
            )
            return session.vertex_ai_session_id

        vertex_session_id = await self.vertex_ai.create_session(
            vertex_ai_agent_id,
            user_name=user_name
        )

        await self.firestore.create_session_for_user(
            user_id=user_id,
            agent_id=agent_id,
            vertex_ai_session_id=vertex_session_id,
            platform=platform
        )

        logger.info(f"Created new session: {vertex_session_id} for user {user_id}")
        return vertex_session_id
