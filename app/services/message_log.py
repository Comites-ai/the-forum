# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Write side of the conversation log (PLAT-43).

The message processor and the scheduled-job executor call ``record_message``
at the four points where a message crosses the Forum: user text in, agent
reply out, scheduled prompt in, scheduled reply out. The log is a side
effect of relaying, never a precondition for it: a failed write is logged
and swallowed so a Firestore hiccup can never cost a user their reply.
"""
import logging
from typing import Iterable, Optional

from app.models.message import Direction, Kind, LoggedMessage
from app.services.firestore_service import FirestoreService

logger = logging.getLogger(__name__)


def attachment_placeholders(files: Iterable[dict]) -> list[str]:
    """'[image: image/jpeg]' for each inbound file; the bytes never come along."""
    placeholders = []
    for f in files or []:
        mime = (f.get("mimetype") or "application/octet-stream").split(";")[0].strip().lower()
        label = "image" if mime.startswith("image/") else "file"
        placeholders.append(f"[{label}: {mime}]")
    return placeholders


async def record_message(
    firestore: FirestoreService,
    *,
    agent_id: str,
    user_id: str,
    platform: str,
    direction: Direction,
    author: str,
    text: str,
    kind: Kind,
    platform_message_ids: Optional[Iterable[str]] = None,
    attachments: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Append one message to the agent-user conversation. Never raises."""
    message = LoggedMessage(
        agent_id=agent_id,
        user_id=user_id,
        platform=platform,
        direction=direction,
        author=author,
        text=text,
        kind=kind,
        platform_message_ids=[str(i) for i in (platform_message_ids or []) if i],
        attachments=list(attachments or []),
    )
    try:
        return await firestore.append_message(message)
    except Exception as e:  # noqa: BLE001 - the relay must survive a lost log line
        logger.warning(
            f"Could not log {direction} {kind} message for agent {agent_id} / "
            f"user {user_id} on {platform}: {e}"
        )
        return None
