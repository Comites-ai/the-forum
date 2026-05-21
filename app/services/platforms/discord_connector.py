# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Discord platform connector implementation.

Discord differs from Slack/Telegram/Google Chat in that we cannot receive
direct messages over an HTTP webhook — Discord only delivers DMs over its
Gateway (WebSocket) protocol. We therefore run a separate long-running
service (the "discord-worker", see docs/DISCORD_WORKER.md) that holds the
Gateway connection open and forwards normalized DM events to this service
over HTTPS. The route handler in app/api/v1/discord_events.py authenticates
those forwarded events via the worker's GCP service account (OIDC bearer
token), and hands the payload to this connector's parse_event.

This connector therefore deals with two distinct concerns:

1. Inbound events: parse the worker-normalized JSON payload into a
   PlatformEvent. We do NOT parse raw Discord Gateway events here — that
   lives in the worker.
2. Outbound API calls: send messages, download attachments, look up users
   via the Discord REST API using the bot token.

The verify_request implementation is a no-op because the route handler
performs OIDC verification before instantiating this connector.
"""
import logging
from typing import Optional

import aiohttp
from fastapi import Request
from google.cloud import secretmanager

from app.core.exceptions import FileDownloadError
from app.schemas.platform_event import PlatformEvent
from app.services.platforms.base import PlatformConnector

logger = logging.getLogger(__name__)


DISCORD_API_BASE = "https://discord.com/api/v10"


class DiscordConnector(PlatformConnector):
    """
    Discord platform connector.

    Inbound: consumes events forwarded from the discord-worker (which
    holds the actual Gateway WebSocket).

    Outbound: talks to the Discord REST API for sending messages,
    downloading attachments, and looking up users.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        bot_token_secret: Optional[str] = None,
        bot_token_project_id: Optional[str] = None,
    ):
        """
        Initialize Discord connector.

        Args:
            bot_token: Direct Discord bot token
            bot_token_secret: Secret Manager secret name for the bot token
            bot_token_project_id: GCP project ID where the secret lives

        Note:
            Either bot_token OR (bot_token_secret + bot_token_project_id)
            must be provided. Secret Manager is preferred for production.
        """
        if bot_token_secret and bot_token_project_id:
            self.bot_token = self._fetch_token_from_secret_manager(
                bot_token_secret, bot_token_project_id
            )
        elif bot_token:
            self.bot_token = bot_token
        else:
            raise ValueError(
                "Either bot_token OR (bot_token_secret + bot_token_project_id) must be provided"
            )

        self._auth_header = {"Authorization": f"Bot {self.bot_token}"}

    def _fetch_token_from_secret_manager(self, secret_name: str, project_id: str) -> str:
        """Fetch Discord bot token from Secret Manager."""
        try:
            client = secretmanager.SecretManagerServiceClient()
            secret_path = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
            response = client.access_secret_version(request={"name": secret_path})
            token = response.payload.data.decode("UTF-8").strip()
            logger.debug(
                f"Fetched Discord bot token from secret: {secret_name} in project: {project_id}"
            )
            return token
        except Exception as e:
            logger.error(
                f"Failed to fetch Discord bot token from secret {secret_name} in project {project_id}: {e}"
            )
            raise

    async def send_message(self, recipient_id: str, text: str) -> dict:
        """
        Send a message to a Discord channel.

        Args:
            recipient_id: Discord channel ID (for DMs, this is the DM
                channel ID obtained from open_conversation, not the user ID)
            text: Message text. Discord's hard limit is 2000 characters per
                message; the caller should pre-chunk anything longer.

        Returns:
            Discord API response dict
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{DISCORD_API_BASE}/channels/{recipient_id}/messages",
                    headers=self._auth_header,
                    json={"content": text},
                ) as response:
                    result = await response.json()
                    if response.status in (200, 201):
                        logger.info(
                            f"Successfully posted message to Discord channel: {recipient_id}, "
                            f"message_id: {result.get('id')}"
                        )
                        return result
                    error_msg = result.get("message", "unknown_error")
                    logger.error(
                        f"Discord API error posting to channel {recipient_id}: "
                        f"Status {response.status}, Error: {error_msg}, Body: {result}"
                    )
                    raise Exception(f"Discord API error: {error_msg}")
        except Exception as e:
            logger.error(f"Error posting to Discord channel {recipient_id}: {e}")
            raise

    async def download_file(self, download_ref: str) -> bytes:
        """
        Download a Discord attachment.

        Discord attachment URLs are pre-signed and time-limited; they
        require no auth header and are valid for ~24 hours from when the
        message was delivered. The download_ref is the attachment URL
        carried in the forwarded event payload.

        Args:
            download_ref: Discord attachment URL

        Returns:
            Raw file bytes
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(download_ref) as response:
                    if response.status == 200:
                        file_bytes = await response.read()
                        logger.info(f"Downloaded file from Discord: {len(file_bytes)} bytes")
                        return file_bytes
                    error_text = await response.text()
                    logger.error(
                        f"Discord attachment download failed: "
                        f"{response.status} - {error_text}"
                    )
                    raise FileDownloadError(
                        f"Discord returned {response.status} when downloading file"
                    )
        except FileDownloadError:
            raise
        except Exception as e:
            logger.error(f"Discord file download error (url={download_ref}): {e}")
            raise FileDownloadError(f"Network error downloading Discord file: {e}") from e

    async def get_user_info(self, user_id: str) -> dict:
        """
        Fetch Discord user profile.

        Uses GET /users/{user.id}. Discord does not expose email to bots;
        the returned dict therefore never includes one.

        Args:
            user_id: Discord user snowflake (numeric string)

        Returns:
            Dict with display_name, username, global_name (when available)
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{DISCORD_API_BASE}/users/{user_id}",
                    headers=self._auth_header,
                ) as response:
                    if response.status == 200:
                        user = await response.json()
                        username = user.get("username", "")
                        global_name = user.get("global_name")
                        display_name = global_name or username or user_id
                        return {
                            "display_name": display_name,
                            "username": username,
                            "global_name": global_name,
                        }
                    logger.error(
                        f"Failed to get Discord user info for {user_id}: "
                        f"status {response.status}"
                    )
                    return {"display_name": user_id}
        except Exception as e:
            logger.error(f"Error getting Discord user info for {user_id}: {e}")
            return {"display_name": user_id}

    async def open_conversation(self, user_id: str, space_id: Optional[str] = None) -> str:
        """
        Resolve a DM channel ID for a Discord user.

        If space_id is supplied (the DM channel ID we already saw on a
        previous inbound event), we trust it and avoid the API round-trip.
        Otherwise we POST to /users/@me/channels to create-or-fetch the DM
        channel.

        Args:
            user_id: Discord user snowflake
            space_id: Optional cached DM channel ID

        Returns:
            DM channel ID suitable for use with send_message
        """
        if space_id:
            return space_id

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{DISCORD_API_BASE}/users/@me/channels",
                    headers=self._auth_header,
                    json={"recipient_id": user_id},
                ) as response:
                    result = await response.json()
                    if response.status in (200, 201):
                        channel_id = result.get("id")
                        if not channel_id:
                            raise Exception(
                                f"Discord create-DM response missing 'id': {result}"
                            )
                        logger.debug(f"Opened DM channel {channel_id} for user {user_id}")
                        return channel_id
                    raise Exception(
                        f"Discord create-DM failed: status {response.status}, body {result}"
                    )
        except Exception as e:
            logger.error(f"Error opening Discord DM channel for user {user_id}: {e}")
            raise

    async def verify_request(self, request: Request) -> bool:
        """
        Inbound request verification.

        Discord events reach us via the discord-worker, not directly from
        Discord. The worker authenticates with a GCP-issued OIDC token,
        which is verified by the route handler before this connector is
        instantiated. There is therefore nothing for the connector itself
        to check.
        """
        return True

    def parse_event(self, data: dict) -> PlatformEvent:
        """
        Parse a worker-forwarded event into a PlatformEvent.

        Expected payload shape (produced by discord-worker/worker.py):

            {
              "event_type": "dm_message",
              "user_id": "987654321",
              "username": "johndoe",
              "global_name": "John Doe",
              "channel_id": "111222333",
              "message_id": "444555666",
              "text": "Hello bot!",
              "attachments": [
                {
                  "url": "https://cdn.discordapp.com/...",
                  "content_type": "image/png",
                  "filename": "photo.png",
                  "size": 12345
                }
              ]
            }

        Args:
            data: Worker-normalized event dict

        Returns:
            PlatformEvent ready for MessageProcessorV2
        """
        user_id = str(data.get("user_id", ""))
        channel_id = str(data.get("channel_id", ""))
        message_text = data.get("text") or ""

        if not user_id or not channel_id:
            raise ValueError("Invalid Discord event: missing user_id or channel_id")

        files: list[dict] = []
        for attachment in data.get("attachments") or []:
            url = attachment.get("url")
            if not url:
                continue
            files.append({
                "mimetype": attachment.get("content_type") or "application/octet-stream",
                "download_ref": url,
                "name": attachment.get("filename"),
                "size": attachment.get("size"),
                "file_type": "attachment",
            })

        return PlatformEvent(
            platform="discord",
            user_id=user_id,
            user_email=None,  # Discord does not expose email to bots
            message_text=message_text,
            space_id=channel_id,
            files=files,
            media_group_id=None,
            raw_event=data,
        )
