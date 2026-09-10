# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Platform-agnostic message processing service (v2 - multi-platform)."""
import asyncio
import base64
import logging
from datetime import datetime, UTC
from typing import Optional, TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.models.session import Session
from app.schemas.platform_event import PlatformEvent
from app.services.firestore_service import FirestoreService
from app.services.vertex_ai_service import VertexAIService
from app.services.identity_service import IdentityService
from app.services.platforms.base import PlatformConnector
from app.services.run_tracker import (
    CUT_OFF_STREAM_BROKE,
    CUT_OFF_VANISHED,
    active_run_marker,
    describe_cut_off,
    log_run_cut_off,
)
from app.services.session_healer import SessionHealer, get_session_healer
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
# Used when the agent has declared a capability beyond plain images, so the
# generic copy above would understate what it can actually read.
REJECTION_UNREADABLE_FILES_TEMPLATE = (
    "Sorry, it appears you sent me a file type that I can't read! "
    "I can accept typed words, {accepted}. "
    "I'm going to ignore those files and read the rest of your message."
)
REJECTION_MULTIPLE_IMAGES = (
    "Sorry, I can only handle one image at a time. "
    "Can you send me just the first one?"
)
REJECTION_MULTIPLE_FILES = (
    "Sorry, I can only handle one file at a time. "
    "Can you send me just the first one?"
)
ERR_DOWNLOAD = (
    "I couldn't download the image you sent. "
    "Could you try sending it again?"
)
ERR_TOO_LARGE_TEMPLATE = (
    "That image is too large for me to process (limit: {limit_mb} MB)."
)
ERR_FILE_TOO_LARGE_TEMPLATE = (
    "That file is too large for me to process (limit: {limit_mb} MB)."
)
ERR_UNSUPPORTED_TYPE = (
    "I can't read that image format. "
    "Please send a PNG, JPEG, GIF, WebP, or HEIC."
)
ERR_GCS_UPLOAD = (
    "I had trouble saving your image. "
    "Please try again in a minute."
)
ERR_GCS_UPLOAD_FILE = (
    "I had trouble saving your file. "
    "Please try again in a minute."
)
ERR_STREAM_BROKEN = (
    "I lost my train of thought halfway through. "
    "Could you ask that again?"
)
NOTE_NON_IMAGE_FILES_DROPPED = (
    "Note to Agent:  The user attempted to send you a file that was not "
    "an image, but we removed it since you can't handle files like that."
)
ERR_BROKEN_TOOL_TEMPLATE = (
    "Oh no, I appear to have a broken tool. "
    "I got stuck when I tried to {tool_name}. "
    "Could you tell the person that made me about this problem?"
)

# Friendly names for the rejection copy. Anything not listed falls back to
# the MIME subtype, uppercased — "application/zip" reads as "ZIP".
_MIME_LABELS = {
    "image/jpeg": "JPEG",
    "image/svg+xml": "SVG",
    "application/pdf": "PDF",
    "text/plain": "plain text",
    "text/csv": "CSV",
    "application/json": "JSON",
}


def normalize_mimetype(file_dict: dict) -> str:
    """
    Canonical MIME for a connector file dict.

    Connectors vary: Slack passes the platform's mimetype through, Telegram
    hardcodes image/jpeg for photos, Discord falls back to
    application/octet-stream. Lowercased and stripped of any ';charset='
    parameter so comparisons against a declared list behave.
    """
    raw = (file_dict.get("mimetype") or "").strip().lower()
    return raw.split(";", 1)[0].strip()


def accepted_file_types_for(agent) -> list[str]:
    """
    MIME types this agent can receive.

    An agent that declares nothing gets the default image allowlist — the
    behavior every agent had before the field existed. A non-empty
    declaration REPLACES that default, so an agent wanting images alongside
    documents must list the image types explicitly.
    """
    declared = getattr(agent, "accepted_file_types", None)
    if declared:
        return [t.strip().lower() for t in declared if t and t.strip()]
    return list(get_settings().allowed_image_mime_types)


def describe_accepted_types(accepted: list[str]) -> str:
    """
    Render an accepted-type list for a user-facing message.

    Collapses the image types into the single word "images" — users think
    in "images", not in six MIME rows.
    """
    labels = []
    if any(t.startswith("image/") for t in accepted):
        labels.append("images")
    for mime in accepted:
        if mime.startswith("image/"):
            continue
        labels.append(_MIME_LABELS.get(mime, mime.split("/")[-1].upper()))

    if not labels:
        return "typed words only"
    if len(labels) == 1:
        return labels[0]
    return f"{', '.join(labels[:-1])}, and {labels[-1]}"
ERR_TOOL_RATE_LIMITED_TEMPLATE = (
    "One of my tools ({tool_name}) hit a rate limit. You can try that action again, but if it keeps happening, please advise the person that made me of this issue so they can investigate. "
)
ERR_TOOL_ERROR_TEMPLATE = (
    "One of my tools ({tool_name}) ran into an error. "
    "Could you try that again? If it keeps happening, "
    "please tell the person that made me."
)
ERR_TOOL_ACCESS_DENIED_TEMPLATE = (
    "One of my tools ({tool_name}) doesn't have the access it needs. "
    "Could you let the person who created me know? "
    "They may need to grant additional permissions."
)
ERR_TOOL_NO_RESPONSE_TEMPLATE = (
    "The first tool I called ({tool_name}) didn't respond at all. "
    "This often happens when it doesn't have permission to access an API. "
    "Could you let the person who created me know?"
)


def _localize(sent_at, tz_name: str) -> tuple[str, str]:
    """
    Render an aware UTC datetime as a human string in tz_name.

    Returns (formatted string like "3:42 PM on Monday, July 7, 2026",
    effective tz name). Falls back to UTC on an unknown zone name rather
    than failing the message.
    """
    try:
        local = sent_at.astimezone(ZoneInfo(tz_name))
    except (KeyError, ValueError):  # ZoneInfoNotFoundError subclasses KeyError
        logger.warning(f"Unknown timezone {tz_name!r}; falling back to UTC")
        tz_name = "UTC"
        local = sent_at.astimezone(ZoneInfo("UTC"))
    time_str = local.strftime("%I:%M %p").lstrip("0")
    return (
        f"{time_str} on {local.strftime('%A, %B')} {local.day}, {local.year}",
        tz_name,
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
        healer: Optional[SessionHealer] = None,
    ):
        """
        Initialize message processor.

        Args:
            firestore: Firestore service instance
            vertex_ai: Vertex AI service instance
            identity: Identity service instance
            gcs: Optional GCS service instance for file uploads
            healer: Repairs sessions left holding an unanswered tool call.
                Inert unless an operator sets HEAL_ORPHANED_TOOL_CALLS.
        """
        self.firestore = firestore
        self.vertex_ai = vertex_ai
        self.identity = identity
        self.gcs = gcs
        self.healer = healer or get_session_healer()

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

            # What this agent can actually read. Agents that declare nothing
            # get the image allowlist, i.e. exactly their previous behavior.
            accepted_types = accepted_file_types_for(agent)

            # Track whether any files were dropped so we can tell the agent
            # (otherwise it sees a request that the user phrased around a
            # file they expected it to look at).
            had_rejected_files = any(
                normalize_mimetype(f) not in accepted_types
                for f in event.files
            )

            # Apply the single-file / unreadable-type rules. If this returns
            # False we've already messaged the user and should not call the
            # agent.
            image_payload = await self._apply_file_rules(
                event=event,
                connector=connector,
                conversation_id=conversation_id,
                accepted_types=accepted_types,
            )
            if image_payload is False:
                # False sentinel = hard reject (multi-image / failed intake):
                # no agent call.
                return
            # image_payload is None (no image) or a dict (single image ready).

            time_context = await self._build_time_context(event, user, user_info)
            message_text = (
                f"[From: {user.primary_name}] [{time_context}] "
                f"{event.message_text}"
            )

            if image_payload:
                if "gcs_uri" in image_payload:
                    # Images keep the [IMAGE: …] token every deployed agent
                    # already matches on. Anything else gets [FILE: …], so
                    # widening the allowlist can never smuggle a PDF into an
                    # agent's photo-analysis path (#15).
                    mime = image_payload["mime_type"]
                    token = "IMAGE" if mime.startswith("image/") else "FILE"
                    image_ref = f"[{token}: {image_payload['gcs_uri']} | {mime}]"
                    message_text = f"{image_ref}\n\n{message_text}"
                    logger.info(f"Embedded 1 {token.lower()} reference in message")
                else:
                    # Base64 fallback (no GCS configured). There is nowhere to
                    # put the bytes: send_message() forwards only message /
                    # user_id / session_id, so the encoded image is dropped
                    # here. Configure GCS_BUCKET_NAME to forward images (#15).
                    logger.warning(
                        "Dropping inbound image: GCS is not configured, and "
                        "the agent input carries no base64 channel"
                    )

            if had_rejected_files:
                message_text = f"{NOTE_NON_IMAGE_FILES_DROPPED}\n\n{message_text}"
                logger.info("Prepended dropped-files note to agent prompt")

            session = await self._get_or_create_session(
                user_id=user.id,
                agent_id=agent_id,
                vertex_ai_agent_id=agent.vertex_ai_agent_id,
                platform=event.platform,
                user_name=user.primary_name
            )
            session_id = session.vertex_ai_session_id

            # A run marker still sitting here means the previous turn never
            # came back, which is where an unanswered tool call comes from.
            await self._recover_from_cut_off_run(
                session=session,
                vertex_ai_agent_id=agent.vertex_ai_agent_id,
                platform=event.platform,
            )
            await self.firestore.mark_run_started(session.id, active_run_marker())

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
                # Leave the marker in place, but say what happened: the
                # stream died mid-tool often enough that the next turn
                # should come looking.
                await self.firestore.mark_run_started(
                    session.id, active_run_marker(CUT_OFF_STREAM_BROKE)
                )
                await connector.send_message(
                    recipient_id=conversation_id,
                    text=ERR_STREAM_BROKEN,
                )
                return

            await self._close_out_run(session.id, response)

            response_text = response.text.strip()
            if not response_text:
                image_count = 1 if image_payload else 0
                # Include function_errors in the log for debugging
                logger.warning(
                    f"Empty response from agent for user {user.id} "
                    f"(images: {image_count}, "
                    f"message_length: {len(message_text)}, "
                    f"functions_called: {response.function_names or '[]'}, "
                    f"function_errors: {response.function_errors or '[]'})"
                )
                # Check for function calls that got no response at all.
                # This is the most severe failure - the tool never executed.
                if response.has_unanswered_function_calls:
                    last_tool = response.function_names[-1] if response.function_names else "a tool"
                    response_text = ERR_TOOL_NO_RESPONSE_TEMPLATE.format(
                        tool_name=last_tool
                    )
                # Check for specific error types in function_responses.
                # This gives users actionable information about what went wrong.
                elif response.function_errors:
                    # Find the most specific error to report
                    rate_limit_errors = [
                        e for e in response.function_errors
                        if e.get("error_type") == "rate_limit"
                    ]
                    access_denied_errors = [
                        e for e in response.function_errors
                        if e.get("error_type") == "access_denied"
                    ]
                    other_errors = [
                        e for e in response.function_errors
                        if e.get("error_type") == "error"
                    ]

                    if rate_limit_errors:
                        tool_name = rate_limit_errors[-1].get("tool_name", "a tool")
                        response_text = ERR_TOOL_RATE_LIMITED_TEMPLATE.format(
                            tool_name=tool_name
                        )
                    elif access_denied_errors:
                        tool_name = access_denied_errors[-1].get("tool_name", "a tool")
                        response_text = ERR_TOOL_ACCESS_DENIED_TEMPLATE.format(
                            tool_name=tool_name
                        )
                    elif other_errors:
                        tool_name = other_errors[-1].get("tool_name", "a tool")
                        response_text = ERR_TOOL_ERROR_TEMPLATE.format(
                            tool_name=tool_name
                        )
                # Fall back to the generic "broken tool" message if we have
                # function calls but couldn't detect a specific error type.
                elif response.function_names:
                    last_tool = response.function_names[-1]
                    response_text = ERR_BROKEN_TOOL_TEMPLATE.format(
                        tool_name=last_tool
                    )
                elif image_count > 0:
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

            # Structured fields here power the admin UI's per-platform
            # "last used" cells, which read these jsonPayload fields from
            # Cloud Logging. Keep the field names stable.
            logger.info(
                f"Successfully processed message for user {user.id} on {event.platform}",
                extra={
                    "json_fields": {
                        "event": "message_processed",
                        "agent_id": agent_id,
                        "platform": event.platform,
                        "user_id": user.id,
                    }
                },
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

    async def _build_time_context(self, event, user, user_info: dict) -> str:
        """
        Build the "when was this sent" sentence injected into the agent prompt.

        Slack reports the sender's profile timezone, so we use it directly
        (and seed the user's default_timezone from it if unset). The other
        platforms don't report one, so we localize to the user's default
        timezone, falling back to settings.default_user_timezone.
        """
        sent_at = event.sent_at or datetime.now(UTC)

        slack_tz = user_info.get("tz") if event.platform == "slack" else None
        if slack_tz:
            if not user.default_timezone and user.id:
                # Seed so Discord/Telegram/Google Chat localize sensibly
                # without manual setup. Never let this break the message.
                try:
                    await self.firestore.update_user(
                        user.id, {"default_timezone": slack_tz}
                    )
                    user.default_timezone = slack_tz
                    logger.info(
                        f"Seeded default_timezone={slack_tz} for user {user.id} "
                        f"from Slack profile"
                    )
                except Exception as e:
                    logger.warning(f"Could not seed default_timezone: {e}")
            local, tz_name = _localize(sent_at, slack_tz)
            return (
                f"The user sent this message at {local} "
                f"from the {tz_name} timezone."
            )

        tz = user.default_timezone or get_settings().default_user_timezone
        local, tz_name = _localize(sent_at, tz)
        return (
            f"This message was sent at {local} in the {tz_name} timezone, "
            f"which is the user's default time zone."
        )

    async def _apply_file_rules(
        self,
        event: PlatformEvent,
        connector: PlatformConnector,
        conversation_id: str,
        accepted_types: Optional[list[str]] = None,
    ):
        """
        Enforce the file-handling rules for this agent's capability.

        Which attachments count as readable is per-agent (#15): an agent
        that declares nothing gets the image allowlist, so this behaves
        exactly as it did before the capability field existed.

        Returns:
            - False if this is a hard reject: caller must NOT call the
              agent. The user has already been messaged. Triggered by
              multi-file submissions OR by a readable file being attached
              but failing intake (download / size / MIME / GCS).
            - None if there is nothing to forward and proceeding to the
              agent with text only is correct. This covers "no files" and
              "only unreadable files" — in the latter case the user has
              already received the rejection and the agent should still
              answer the text.
            - dict with the file payload (either {'gcs_uri','mime_type'} or
              {'data','mime_type'} for base64 fallback) if a single
              readable file is ready to forward.
        """
        if accepted_types is None:
            accepted_types = list(get_settings().allowed_image_mime_types)

        readable = [
            f for f in event.files
            if normalize_mimetype(f) in accepted_types
        ]
        unreadable = [
            f for f in event.files
            if normalize_mimetype(f) not in accepted_types
        ]
        is_multi = len(readable) > 1 or event.media_group_id is not None

        # Rejection #1 always goes first if applicable.
        if unreadable:
            logger.info(
                f"Rejecting {len(unreadable)} unreadable file(s) "
                f"(mimetypes: {[normalize_mimetype(f) for f in unreadable]}, "
                f"accepted: {accepted_types})"
            )
            await connector.send_message(
                recipient_id=conversation_id,
                text=self._rejection_copy(accepted_types),
            )

        # Rejection #2: hard stop, no agent call.
        if is_multi:
            logger.info(
                f"Rejecting multi-file submission "
                f"(file_count={len(readable)}, "
                f"media_group_id={event.media_group_id})"
            )
            all_images = all(
                normalize_mimetype(f).startswith("image/") for f in readable
            )
            await connector.send_message(
                recipient_id=conversation_id,
                text=(
                    REJECTION_MULTIPLE_IMAGES
                    if all_images
                    else REJECTION_MULTIPLE_FILES
                ),
            )
            return False

        if not readable:
            return None

        result = await self._intake_single_file(
            file_dict=readable[0],
            connector=connector,
            conversation_id=conversation_id,
            accepted_types=accepted_types,
        )
        if result is None:
            # A readable file was attached but couldn't be processed. The
            # user has already received a specific error message; calling
            # the agent text-only would produce a confused reply ("you
            # mentioned an image but I don't see it"), so we hard-reject.
            return False
        return result

    @staticmethod
    def _rejection_copy(accepted_types: list[str]) -> str:
        """
        User-facing copy for an unreadable attachment.

        Agents on the plain image default keep the exact wording they had
        before this was per-agent; anything wider gets copy that names what
        it can actually read, since "I can only accept typed words and
        images" would be a lie for an agent that reads PDFs.
        """
        default_types = get_settings().allowed_image_mime_types
        if sorted(accepted_types) == sorted(t.lower() for t in default_types):
            return REJECTION_NON_IMAGE_FILES
        return REJECTION_UNREADABLE_FILES_TEMPLATE.format(
            accepted=describe_accepted_types(accepted_types)
        )

    async def _intake_single_file(
        self,
        file_dict: dict,
        connector: PlatformConnector,
        conversation_id: str,
        accepted_types: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """
        Validate, download, and stage a single attachment for the agent.

        On any failure, sends a specific user-facing message and returns None
        so the caller can continue without the file (or with a text-only
        fallback). On success, returns a dict ready to embed in the prompt.

        Validation order:
          1. MIME allowlist (cheap; reject before download).
          2. Size pre-check via metadata (only if connector provided 'size').
          3. Download (one retry on FileDownloadError).
          4. Size post-check on actual bytes (catches sources without metadata).
          5. GCS upload, or base64 fallback if GCS not configured.
        """
        settings = get_settings()
        if accepted_types is None:
            accepted_types = list(settings.allowed_image_mime_types)

        mimetype = normalize_mimetype(file_dict)
        download_ref = file_dict.get("download_ref", "")
        size_hint = file_dict.get("size")
        filename = file_dict.get("name")

        # Images and documents are capped separately — a scanned multi-page
        # invoice is legitimately larger than a photo.
        is_image = mimetype.startswith("image/")
        limit_mb = (
            settings.max_image_size_mb if is_image else settings.max_document_size_mb
        )
        max_bytes = limit_mb * 1024 * 1024
        too_large_copy = (
            ERR_TOO_LARGE_TEMPLATE if is_image else ERR_FILE_TOO_LARGE_TEMPLATE
        )

        # 1. MIME allowlist
        if mimetype not in accepted_types:
            logger.info(f"Rejecting unsupported MIME type: {mimetype!r}")
            await connector.send_message(
                recipient_id=conversation_id,
                text=(
                    ERR_UNSUPPORTED_TYPE
                    if is_image
                    else self._rejection_copy(accepted_types)
                ),
            )
            return None

        # 2. Pre-download size check (when size hint available)
        if isinstance(size_hint, int) and size_hint > max_bytes:
            logger.info(
                f"Rejecting oversized file at metadata stage: "
                f"{size_hint} bytes > {max_bytes} bytes (mimetype={mimetype})"
            )
            await connector.send_message(
                recipient_id=conversation_id,
                text=too_large_copy.format(limit_mb=limit_mb),
            )
            return None

        # 3. Download with one retry on transient failure
        if not download_ref:
            logger.warning(
                f"File has no download_ref; cannot fetch (mimetype={mimetype})"
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
                f"Rejecting oversized file post-download: "
                f"{len(image_bytes)} bytes > {max_bytes} bytes (mimetype={mimetype})"
            )
            await connector.send_message(
                recipient_id=conversation_id,
                text=too_large_copy.format(limit_mb=limit_mb),
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
                    text=ERR_GCS_UPLOAD if is_image else ERR_GCS_UPLOAD_FILE,
                )
                return None

            logger.info(
                f"Uploaded file to GCS: {gcs_result['gcs_uri']} "
                f"({len(image_bytes)} bytes, {mimetype})"
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
    ) -> Session:
        """
        Get existing session or create new one for unified user.

        Args:
            user_id: Unified user ID from users collection
            agent_id: Agent ID from agents collection
            vertex_ai_agent_id: Vertex AI agent resource name
            platform: Platform this message came from
            user_name: User's actual name to pass to the Reasoning Engine

        Returns:
            The session mapping. Callers want the whole record, not just the
            Vertex session id: the run marker rides on it.

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
            return session

        vertex_session_id = await self.vertex_ai.create_session(
            vertex_ai_agent_id,
            user_name=user_name
        )

        session = await self.firestore.create_session_for_user(
            user_id=user_id,
            agent_id=agent_id,
            vertex_ai_session_id=vertex_session_id,
            platform=platform
        )

        logger.info(f"Created new session: {vertex_session_id} for user {user_id}")
        return session

    async def _recover_from_cut_off_run(
        self,
        session: Session,
        vertex_ai_agent_id: str,
        platform: Optional[str] = None,
    ) -> None:
        """
        Deal with a run that was started on this session and never returned.

        The log is the point of this even when healing is switched off: it is
        the only record of how often runs are cut off and on which agent. If
        healing *is* on, this is also the moment to repair a session left
        holding an unanswered tool call — a turn later than the damage, but
        before the turn that would have tripped over it.
        """
        cut_off = describe_cut_off(
            session.active_run,
            stale_after_seconds=get_settings().heal_grace_seconds,
        )
        if not cut_off:
            return

        log_run_cut_off(
            cut_off,
            agent_id=vertex_ai_agent_id,
            session_id=session.vertex_ai_session_id,
            platform=platform,
        )
        outcome = await self.healer.heal(
            agent_id=vertex_ai_agent_id,
            session_id=session.vertex_ai_session_id,
            reason=cut_off.reason,
            elapsed_seconds=cut_off.elapsed_seconds,
        )
        logger.info(
            f"Cut-off recovery for session {session.id}: {outcome.reason}"
            + (f" ({outcome.detail})" if outcome.detail else "")
        )

    async def _close_out_run(self, session_doc_id: str, response) -> None:
        """
        Clear the run marker — but only if the turn actually produced words.

        A turn that ends with a tool call and no text is the shape that
        leaves an orphan behind, so the marker stays and the next turn comes
        looking. The healer's own tail check decides whether there is really
        anything to repair, so a marker left on a merely-unproductive turn
        costs one read and nothing else.
        """
        if response.text and response.text.strip():
            await self.firestore.clear_active_run(session_doc_id)
            return

        marker = active_run_marker(CUT_OFF_VANISHED)
        if response.function_names:
            marker["last_tool"] = response.function_names[-1]
        await self.firestore.mark_run_started(session_doc_id, marker)
